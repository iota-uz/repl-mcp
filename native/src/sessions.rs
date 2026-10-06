//! Session ownership: interpreter, project, Python state and broker never cross projects.
use crate::{
    artifacts::Artifacts,
    broker::Broker,
    guardian::{self, CommandSpec},
    supervisor::{Execution, Supervisor},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    sync::Semaphore,
};
use tokio_util::sync::CancellationToken;

pub struct SessionManager {
    pub server_id: String,
    pub artifacts: Arc<Artifacts>,
    default: Arc<Session>,
    sessions: Mutex<HashMap<String, Arc<Session>>>,
    brokers: Mutex<HashMap<PathBuf, Arc<Broker>>>,
    config: Option<PathBuf>,
    scope: String,
    python: PathBuf,
    memory: u64,
    opening: Arc<Semaphore>,
}
pub struct Session {
    pub supervisor: Arc<Supervisor>,
    pub project: PathBuf,
    pub name: String,
    pub environment: Value,
    pub lifetime: CancellationToken,
}
impl Session {
    pub fn metadata(&self) -> Value {
        let mut value = self.supervisor.health();
        value["name"] = json!(self.name);
        value["environment"] = self.environment.clone();
        value["environment"]["runtime_status"] = json!(if value["closed"] == true {
            "closed"
        } else if value["python"].is_null() {
            "lazy"
        } else if value["python"]["running"] == true {
            "running"
        } else {
            "stopped"
        });
        if let Some(executable) = value["python"]["python_executable"]
            .as_str()
            .map(str::to_owned)
        {
            value["environment"]["actual_python_executable"] = json!(executable);
        }
        if let Some(version) = value["python"]["python_version"]
            .as_str()
            .map(str::to_owned)
        {
            value["environment"]["version"] = json!(version);
            value["environment"]["validated"] = json!(true);
        }
        value
    }
}
impl SessionManager {
    pub fn new(
        python: PathBuf,
        broker: Arc<Broker>,
        memory: u64,
        config: Option<PathBuf>,
        scope: String,
    ) -> Result<Self, String> {
        let project = std::env::current_dir()
            .map_err(|_| "Cannot resolve server project")?
            .canonicalize()
            .map_err(|_| "Cannot resolve server project")?;
        let config = config.map(|path| {
            if path.is_absolute() {
                path
            } else {
                project.join(path)
            }
        });
        let python = if !python.is_absolute() && python.components().count() > 1 {
            project.join(python)
        } else {
            python
        };
        let server_id = uuid::Uuid::new_v4().to_string();
        let artifacts = Arc::new(Artifacts::new()?);
        let supervisor = Arc::new(Supervisor::with_context(
            python.clone(),
            broker.clone(),
            memory,
            project.clone(),
            server_id.clone(),
        ));
        artifacts.register_owner(supervisor.session_id())?;
        supervisor.set_artifacts(artifacts.clone());
        let default = Arc::new(Session {
            supervisor,
            project,
            name: "default".into(),
            lifetime: CancellationToken::new(),
            environment: json!({"source":"server_default","python":python,"validated":false,"runtime_status":"lazy"}),
        });
        let mut sessions = HashMap::new();
        sessions.insert(default.supervisor.session_id().to_string(), default.clone());
        let brokers = Mutex::new(HashMap::from([(default.project.clone(), broker)]));
        Ok(Self {
            server_id,
            artifacts,
            default,
            sessions: Mutex::new(sessions),
            brokers,
            config,
            scope,
            python,
            memory,
            opening: Arc::new(Semaphore::new(4)),
        })
    }
    pub fn default(&self) -> Arc<Session> {
        self.default.clone()
    }
    pub fn get(&self, id: Option<&str>) -> Result<Arc<Session>, String> {
        let key = id.unwrap_or(self.default.supervisor.session_id());
        self.sessions
            .lock()
            .unwrap()
            .get(key)
            .cloned()
            .ok_or_else(|| "Session not found or closed; open a new session".into())
    }
    pub async fn open(
        &self,
        name: &str,
        project: &str,
        python: Option<&str>,
    ) -> Result<Value, String> {
        if name.is_empty() || name.len() > 128 {
            return Err("Session name must contain 1..128 bytes".into());
        }
        let permit = self.opening.clone().try_acquire_owned().map_err(
            |_| "Session opening capacity exhausted; wait for active interpreter probes",
        )?;
        let project = PathBuf::from(project);
        let explicit = python.map(PathBuf::from);
        let default_python = self.python.clone();
        let config = self.config.clone();
        let scope = self.scope.clone();
        let root_broker = self.default.supervisor.broker().clone();
        let prepared = tokio::task::spawn_blocking(move || {
            // The slot belongs to the physical job, even if the MCP request
            // times out or is cancelled while the filesystem is stalled.
            let project = project
                .canonicalize()
                .map_err(|_| "Project directory does not exist")?;
            if !project.is_dir() {
                return Err("Project must be a directory".to_string());
            }
            let venv_exists = if explicit.is_some() {
                false
            } else {
                match std::fs::symlink_metadata(project.join(".venv")) {
                    Ok(_) => true,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
                    Err(_) => return Err("Cannot inspect the project .venv environment".into()),
                }
            };
            let (selected, source) = match explicit {
                Some(explicit) => (resolve_python(&explicit, &project)?, "explicit"),
                None if venv_exists => (
                    resolve_python(&project.join(".venv/bin/python"), &project)?,
                    "project_venv",
                ),
                None => (resolve_python(&default_python, &project)?, "server_default"),
            };
            let mut broker = Broker::from_project(config, scope, project.clone())?;
            broker.inherit_journal(&root_broker);
            Ok((project, selected, source, Arc::new(broker), permit))
        });
        let (project, selected, source, staged, _permit) =
            tokio::time::timeout(Duration::from_secs(10), prepared)
                .await
                .map_err(|_| "Session filesystem preparation timed out".to_string())?
                .map_err(|_| "Session filesystem preparation failed".to_string())??;
        let version = probe_python(&selected, &project).await?;
        let mut sessions = self.sessions.lock().unwrap();
        if sessions.len() >= 32 {
            return Err("Session limit reached (32); close unused sessions".into());
        }
        let broker = {
            let mut brokers = self.brokers.lock().unwrap();
            if let Some(broker) = brokers.get(&project) {
                broker.clone()
            } else {
                brokers.insert(project.clone(), staged.clone());
                staged
            }
        };
        let supervisor = Arc::new(Supervisor::with_context(
            selected.clone(),
            broker,
            self.memory,
            project.clone(),
            self.server_id.clone(),
        ));
        self.artifacts.register_owner(supervisor.session_id())?;
        supervisor.set_artifacts(self.artifacts.clone());
        let session = Arc::new(Session {
            supervisor,
            project,
            name: name.into(),
            lifetime: CancellationToken::new(),
            environment: json!({"source":source,"python":selected,"version":version["version"],"actual_python_executable":version["executable"],"version_info":version,"validated":true,"runtime_status":"lazy"}),
        });
        sessions.insert(session.supervisor.session_id().into(), session.clone());
        Ok(session.metadata())
    }
    pub fn list(&self) -> Value {
        let mut sessions: Vec<_> = self
            .sessions
            .lock()
            .unwrap()
            .values()
            .map(|s| s.metadata())
            .collect();
        sessions.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        json!({"server_id":self.server_id,"sessions":sessions,"limit":32})
    }
    pub async fn inspect(
        &self,
        id: Option<&str>,
        namespace: bool,
        ct: CancellationToken,
    ) -> Result<Value, String> {
        let session = self.get(id)?;
        let mut result = session.metadata();
        if namespace {
            let execution = Execution {
                code: String::new(),
                reset: false,
                timeout: 5.0,
                expected_generation: None,
                extra: json!({"inventory":true}),
                fresh: false,
            };
            let inventory = session.supervisor.execute_request(execution, ct).await;
            if inventory["success"] != true {
                return Err(inventory["error"]
                    .as_str()
                    .unwrap_or("Namespace inventory failed")
                    .into());
            }
            result = session.metadata();
            result["namespace"] = inventory["value"].clone();
        }
        Ok(result)
    }
    pub async fn close(&self, id: &str) -> Result<Value, String> {
        if id == self.default.supervisor.session_id() {
            return Err("The legacy default session cannot be closed; use execute_python reset=true to clear it".into());
        }
        let (session, broker) = {
            let mut sessions = self.sessions.lock().unwrap();
            let session = sessions
                .remove(id)
                .ok_or("Session not found or already closed")?;
            session.lifetime.cancel();
            let broker = if sessions.values().any(|s| s.project == session.project) {
                None
            } else {
                self.brokers.lock().unwrap().remove(&session.project)
            };
            (session, broker)
        };
        session.supervisor.close().await;
        self.artifacts.remove_owner(id).await?;
        if let Some(broker) = broker {
            broker.shutdown().await;
        }
        let mut result = json!({"closed":true});
        session
            .supervisor
            .identify(&mut result, session.supervisor.generation());
        Ok(result)
    }
    pub async fn shutdown(&self) {
        let sessions: Vec<_> = self
            .sessions
            .lock()
            .unwrap()
            .drain()
            .map(|(_, s)| s)
            .collect();
        for session in &sessions {
            session.lifetime.cancel();
        }
        futures_util::future::join_all(sessions.iter().map(|s| s.supervisor.close())).await;
        let brokers: Vec<_> = self
            .brokers
            .lock()
            .unwrap()
            .drain()
            .map(|(_, b)| b)
            .collect();
        futures_util::future::join_all(brokers.iter().map(|b| b.shutdown())).await;
    }
}
fn resolve_python(path: &Path, project: &Path) -> Result<PathBuf, String> {
    let candidate = if path.is_absolute() {
        path.to_owned()
    } else if path.components().count() > 1 {
        project.join(path)
    } else {
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
            .map(|p| p.join(path))
            .find(|p| p.is_file())
            .ok_or("Requested Python executable was not found on PATH")?
    };
    let metadata =
        std::fs::metadata(&candidate).map_err(|_| "Requested Python executable does not exist")?;
    if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return Err("Requested Python executable is not executable".into());
    }
    // Preserve venv symlink location: canonicalizing bin/python loses environment identity.
    Ok(candidate)
}
struct ProbeGuard(u32, bool);
impl Drop for ProbeGuard {
    fn drop(&mut self) {
        if self.1 {
            unsafe {
                libc::kill(-(self.0 as i32), libc::SIGKILL);
            }
        }
    }
}
async fn probe_python(python: &Path, project: &Path) -> Result<Value, String> {
    let mut child=guardian::spawn_owned(CommandSpec { program:python.to_string_lossy().into(),args:vec!["-I".into(),"-c".into(),"import json,sys; print(json.dumps({'version':sys.version.split()[0], 'executable':sys.executable, 'major':sys.version_info.major,'minor':sys.version_info.minor}))".into()],env:BTreeMap::new(),cwd:Some(project.into()),worker:false }).await?;
    let mut guard = ProbeGuard(child.id().ok_or("Interpreter probe process missing")?, true);
    let stdout = child
        .stdout
        .take()
        .ok_or("Interpreter probe output missing")?;
    // Child::wait closes stdin stored in Child. This pipe instead belongs to
    // the probe scope until natural completion or cancellation cleanup.
    let _ownership = child
        .stdin
        .take()
        .ok_or("Interpreter probe input missing")?;
    let read = async {
        let mut reader = BufReader::new(stdout);
        let mut bytes = Vec::new();
        // Stop oversized output before allocating an unbounded line.
        loop {
            let available = reader
                .fill_buf()
                .await
                .map_err(|_| "Interpreter probe output failed")?;
            if available.is_empty() {
                break;
            }
            let take = available
                .iter()
                .position(|b| *b == b'\n')
                .map_or(available.len(), |n| n + 1);
            if bytes.len() + take > 4096 {
                return Err("Interpreter probe output exceeded limit".to_string());
            }
            bytes.extend_from_slice(&available[..take]);
            reader.consume(take);
            if bytes.ends_with(b"\n") {
                break;
            }
        }
        let value: Value = serde_json::from_slice(&bytes)
            .map_err(|_| "Requested executable is not a supported Python interpreter")?;
        if value["major"] != 3 || value["minor"].as_u64().unwrap_or(0) < 10 {
            return Err("Python 3.10 or newer is required".into());
        }
        child
            .wait()
            .await
            .map_err(|_| "Interpreter probe cleanup failed")?;
        guard.1 = false;
        Ok(value)
    };
    let result = tokio::time::timeout(Duration::from_secs(10), read)
        .await
        .map_err(|_| "Interpreter probe timed out".to_string())
        .and_then(|r| r);
    drop(guard);
    if result.is_err() {
        let _ = child.wait().await;
    }
    result
}
