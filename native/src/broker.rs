//! Lazy native MCP client broker; envelopes survive intact and writes are never retried.
use crate::config::{Registry, ServerConfig};
use rmcp::{
    RoleClient, ServiceExt,
    model::{ClientRequest, ServerResult},
    service::{RequestHandle, RunningService, ServiceError},
    transport::StreamableHttpClientTransport,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet, VecDeque},
    path::PathBuf,
    sync::{Arc, Mutex as StdMutex},
    time::Duration,
};
use tokio::sync::{Mutex, Semaphore};

type Session = RunningService<RoleClient, ()>;
type Slot = Arc<Mutex<Option<Arc<Session>>>>;

pub struct Broker {
    registry: Mutex<Registry>,
    sessions: Mutex<BTreeMap<String, Slot>>,
    calls: Arc<Semaphore>,
    journal: Arc<StdMutex<VecDeque<Value>>>,
    journal_path: Arc<StdMutex<Option<PathBuf>>>,
    journal_io: Arc<JournalIo>,
    catalogue: Mutex<BTreeMap<String, (tokio::time::Instant, Value)>>,
    failures: Mutex<BTreeMap<String, tokio::time::Instant>>,
    refresh_at: Mutex<tokio::time::Instant>,
    validators: Mutex<BTreeMap<String, Arc<jsonschema::Validator>>>,
    config_error: Mutex<Option<String>>,
    validation_slots: Arc<Semaphore>,
}

struct CallRecord {
    journal: Arc<StdMutex<VecDeque<Value>>>,
    journal_path: Arc<StdMutex<Option<PathBuf>>>,
    journal_io: Arc<JournalIo>,
    id: String,
    finished: bool,
}
impl CallRecord {
    fn set_status(&mut self, status: &str) {
        if let Ok(mut journal) = self.journal.lock() {
            if let Some(v) = journal.iter_mut().find(|v| v["id"] == self.id) {
                v["status"] = json!(status);
                v["finished_ms"] = json!(now_ms());
            }
        }
        self.finished = true;
    }
    async fn finish(&mut self, status: &str) -> Result<(), String> {
        self.set_status(status);
        persist_journal(
            self.journal.clone(),
            self.journal_path.clone(),
            self.journal_io.clone(),
        )
        .await
    }
}
impl Drop for CallRecord {
    fn drop(&mut self) {
        if !self.finished {
            self.set_status("outcome_unknown");
            if self
                .journal_path
                .lock()
                .map(|path| path.is_none())
                .unwrap_or(true)
            {
                return;
            }
            let _ = schedule_journal(
                self.journal.clone(),
                self.journal_path.clone(),
                self.journal_io.clone(),
            );
        }
    }
}

// rmcp RequestHandle has no cancellation-on-drop. Retain ownership until completion.
struct PendingRequest(Option<RequestHandle<RoleClient>>);
impl Drop for PendingRequest {
    fn drop(&mut self) {
        if let Some(handle) = self.0.take() {
            tokio::spawn(async move {
                let _ = tokio::time::timeout(
                    Duration::from_secs(5),
                    handle.cancel(Some(
                        "REPL run cancelled; effects may already have occurred".into(),
                    )),
                )
                .await;
            });
        }
    }
}

impl Broker {
    pub fn from_config(config: Option<PathBuf>, scope: String) -> Result<Self, String> {
        if scope != "all"
            && scope != "none"
            && scope
                .split(',')
                .any(|s| !matches!(s, "project" | "user" | "local"))
        {
            return Err("MCP scopes must be project, user, local, all or none; export plugin servers into an explicit config file".into());
        }
        match Registry::load(config.clone(), &scope) {
            Ok(registry) => Ok(Self::new(registry)),
            Err(error) => {
                let mut broker = Self::new(Registry {
                    servers: BTreeMap::new(),
                    config,
                    scope,
                });
                broker.config_error = Mutex::new(Some(error));
                Ok(broker)
            }
        }
    }
    pub fn new(registry: Registry) -> Self {
        Self {
            registry: Mutex::new(registry),
            sessions: Mutex::new(BTreeMap::new()),
            calls: Arc::new(Semaphore::new(32)),
            journal: Arc::new(StdMutex::new(VecDeque::new())),
            journal_path: Arc::new(StdMutex::new(None)),
            journal_io: Arc::new(JournalIo::new()),
            catalogue: Mutex::new(BTreeMap::new()),
            failures: Mutex::new(BTreeMap::new()),
            refresh_at: Mutex::new(tokio::time::Instant::now()),
            validators: Mutex::new(BTreeMap::new()),
            config_error: Mutex::new(None),
            validation_slots: Arc::new(Semaphore::new(4)),
        }
    }
    pub fn set_journal(&self, path: PathBuf) -> Result<(), String> {
        use std::io::Read;
        #[cfg(unix)]
        let journal_lock = {
            use std::os::{fd::AsRawFd, unix::fs::OpenOptionsExt};
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(path.with_extension("lock"))
                .map_err(|_| "Cannot lock private broker journal")?;
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                return Err("Broker journal is already owned by another runtime; choose a separate --journal path".into());
            }
            file
        };
        let mut options = std::fs::OpenOptions::new();
        options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW);
        }
        let mut records: VecDeque<Value> = match options.open(&path) {
            Ok(file) => {
                let metadata = file
                    .metadata()
                    .map_err(|_| "Cannot inspect broker journal")?;
                if !metadata.is_file() || metadata.len() > 1024 * 1024 {
                    return Err("Broker journal must be a regular file at most1MiB".into());
                }
                #[cfg(unix)]
                {
                    use std::os::unix::fs::{MetadataExt, PermissionsExt};
                    if metadata.permissions().mode() & 0o077 != 0
                        || metadata.uid() != unsafe { libc::geteuid() }
                    {
                        return Err("Broker journal must be an owned private0600 file".into());
                    }
                }
                let mut bytes = Vec::new();
                file.take(1024 * 1024 + 1)
                    .read_to_end(&mut bytes)
                    .map_err(|_| "Cannot read broker journal")?;
                serde_json::from_slice(&bytes).map_err(|_| "Invalid broker journal JSON")?
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => VecDeque::new(),
            Err(_) => return Err("Cannot read broker journal; symlinks are forbidden".into()),
        };
        if records.len() > 256 {
            return Err("Broker journal exceeds256 records".into());
        }
        for record in &mut records {
            let object = record
                .as_object_mut()
                .ok_or("Broker journal record must be an object")?;
            if object.keys().any(|k| {
                !matches!(
                    k.as_str(),
                    "id" | "run_id" | "server" | "tool" | "started_ms" | "finished_ms" | "status"
                )
            }) {
                return Err("Broker journal may contain metadata only".into());
            }
            if object.values().any(|v| {
                v.as_str()
                    .is_some_and(|s| s.len() > 400 || s.chars().any(char::is_control))
            }) {
                return Err("Invalid broker journal metadata".into());
            }
            if object.get("status").and_then(Value::as_str) == Some("dispatched") {
                object.insert("status".into(), json!("outcome_unknown"));
            }
        }
        atomic_snapshot(&path, &records)?;
        *self
            .journal
            .lock()
            .map_err(|_| "Broker journal unavailable")? = records;
        *self
            .journal_path
            .lock()
            .map_err(|_| "Broker journal unavailable")? = Some(path);
        #[cfg(unix)]
        {
            *self
                .journal_io
                .file_lock
                .lock()
                .map_err(|_| "Broker journal unavailable")? = Some(journal_lock);
        }
        Ok(())
    }

    /// Explicit interactive login, kept outside REPL execution and ordinary MCP diagnostics.
    pub async fn oauth_login(&self, name: &str) -> Result<(), String> {
        if let Some(error) = self.config_error.lock().await.as_ref() {
            return Err(format!(
                "MCP configuration error: {error}; repair registry before OAuth login"
            ));
        }
        use rmcp::transport::auth::{AuthorizationManager, AuthorizationRequest, OAuthState};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let cfg = self
            .registry
            .lock()
            .await
            .servers
            .get(name)
            .cloned()
            .ok_or("Unknown OAuth server; configure it in the explicit broker registry")?;
        let oauth = cfg
            .oauth
            .as_ref()
            .filter(|o| o.grant_type == "authorization_code")
            .ok_or("Server requires oauth grantType authorization_code and credentialFile")?;
        let store = FileCredentialStore::new(
            oauth
                .credential_file
                .clone()
                .ok_or("OAuth credentialFile required")?,
        )?;
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", oauth.redirect_port))
            .await
            .map_err(|_| "Cannot bind OAuth callback listener")?;
        let redirect = format!(
            "http://127.0.0.1:{}/callback",
            listener
                .local_addr()
                .map_err(|_| "Cannot inspect OAuth listener")?
                .port()
        );
        let mut manager = tokio::time::timeout(
            Duration::from_secs(30),
            AuthorizationManager::new(cfg.url.as_deref().ok_or("OAuth requires HTTP URL")?),
        )
        .await
        .map_err(|_| "OAuth discovery timed out")?
        .map_err(|_| "Cannot initialize OAuth discovery")?;
        manager.set_credential_store(store);
        let mut state = OAuthState::Unauthorized(manager);
        let mut request = AuthorizationRequest::new(redirect.clone())
            .with_client_name("repl-mcp broker")
            .with_scopes(oauth.scopes.clone());
        if let Some(id) = &oauth.client_id {
            request = request.with_preregistered_client(id);
        }
        if let Some(secret) = &oauth.client_secret {
            request = request.with_client_secret(secret);
        }
        tokio::time::timeout(Duration::from_secs(30),state.start_authorization(request)).await.map_err(|_|"OAuth discovery timed out")?.map_err(|_|"OAuth authorization setup failed; check preregistered client or provider dynamic registration support")?;
        let url = state
            .get_authorization_url()
            .await
            .map_err(|_| "Cannot create OAuth authorization URL")?;
        eprintln!(
            "Open this authorization URL in your browser to grant this broker access:\n{url}"
        );
        let deadline = tokio::time::Instant::now() + Duration::from_secs(300);
        loop {
            let (mut stream, peer) = tokio::time::timeout_at(deadline, listener.accept())
                .await
                .map_err(|_| "OAuth callback timed out; run --oauth-login again")?
                .map_err(|_| "OAuth callback listener failed")?;
            if !peer.ip().is_loopback() {
                continue;
            }
            let mut bytes = Vec::new();
            let mut chunk = [0u8; 1024];
            loop {
                let n = tokio::time::timeout(Duration::from_secs(5), stream.read(&mut chunk))
                    .await
                    .map_err(|_| "OAuth callback read timed out")?
                    .map_err(|_| "OAuth callback read failed")?;
                if n == 0 {
                    break;
                }
                bytes.extend_from_slice(&chunk[..n]);
                if bytes.len() > 16384 {
                    return Err("OAuth callback exceeded 16 KiB limit".into());
                }
                if bytes.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            let first = std::str::from_utf8(&bytes)
                .map_err(|_| "Invalid OAuth callback request")?
                .lines()
                .next()
                .ok_or("Empty OAuth callback request")?;
            let mut parts = first.split_whitespace();
            if parts.next() != Some("GET") {
                continue;
            }
            let Some(target) = parts.next().filter(|t| t.starts_with("/callback?")) else {
                let _ = stream
                    .write_all(
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                    )
                    .await;
                continue;
            };
            let callback = format!("{}{}", redirect.trim_end_matches("/callback"), target);
            let result=tokio::time::timeout(Duration::from_secs(30),state.handle_callback_url(&callback)).await.map_err(|_|"OAuth token exchange timed out")?.map_err(|_|"OAuth callback rejected or token exchange failed; state, issuer and PKCE are checked by the SDK");
            let body = if result.is_ok() {
                "Broker authorized. You may close this tab."
            } else {
                "Authorization failed. Return to the terminal."
            };
            let response = format!(
                "HTTP/1.1 {}\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                if result.is_ok() {
                    "200 OK"
                } else {
                    "400 Bad Request"
                },
                body.len(),
                body
            );
            let _ = stream.write_all(response.as_bytes()).await;
            result?;
            eprintln!("Broker authorization saved to its independent private credential store.");
            return Ok(());
        }
    }

    pub async fn dispatch_with_run(
        &self,
        run_id: &str,
        op: &str,
        params: Value,
    ) -> Result<Value, String> {
        if op != "refresh" {
            self.refresh_if_due().await?;
        }
        if matches!(op, "servers" | "tools" | "call") {
            if let Some(error) = self.config_error.lock().await.as_ref() {
                return Err(format!(
                    "MCP configuration error: {error}; repair registry then mcp.refresh()"
                ));
            }
        }
        match op {
            "servers" => {
                let reg = self.registry.lock().await;
                Ok(json!(reg.servers.keys().collect::<Vec<_>>()))
            }
            "refresh" => {
                self.refresh().await?;
                Ok(self.health().await)
            }
            "journal" => Ok(json!(
                self.journal
                    .lock()
                    .map_err(|_| "Call journal unavailable")?
                    .iter()
                    .cloned()
                    .collect::<Vec<_>>()
            )),
            "tools" | "call" => {
                let name = params
                    .get("server")
                    .and_then(Value::as_str)
                    .ok_or("server must be a string")?;
                let cfg = self.registry.lock().await.servers.get(name).cloned().ok_or("Unknown or unauthorized MCP server; use mcp.servers() and explicit --config, then mcp.refresh()")?;
                let budget = params
                    .get("timeout")
                    .and_then(Value::as_f64)
                    .unwrap_or(if op == "tools" { 30.0 } else { 120.0 });
                if !budget.is_finite() || !(0.01..=3600.0).contains(&budget) {
                    return Err("MCP timeout must be between 0.01 and 3600 seconds".into());
                }
                let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(budget);
                let _permit = tokio::time::timeout_at(deadline, self.calls.acquire())
                    .await
                    .map_err(|_| "MCP request budget exceeded while queued")?
                    .map_err(|_| "MCP broker stopped")?;
                let (key, session) = self.session(&cfg, deadline).await?;
                let result = if op == "tools" {
                    self.tools(&session, &cfg, deadline).await
                } else {
                    let tool = params
                        .get("tool")
                        .and_then(Value::as_str)
                        .ok_or("tool must be a string")?;
                    if cfg
                        .allowed_tools
                        .as_ref()
                        .is_some_and(|allow| !allow.iter().any(|t| t == tool))
                    {
                        return Err("Tool denied by broker allowedTools policy".into());
                    }
                    let arguments = params
                        .get("arguments")
                        .cloned()
                        .unwrap_or_else(|| json!({}));
                    if !arguments.is_object() {
                        return Err("arguments must be a JSON object".into());
                    }
                    let tools = self.tools(&session, &cfg, deadline).await?;
                    let definition = tools
                        .as_array()
                        .and_then(|tools| tools.iter().find(|t| t["name"] == tool))
                        .ok_or(
                            "Unknown or unauthorized MCP tool; inspect mcp.list_tools(server)",
                        )?;
                    self.validate(definition.get("inputSchema"), &arguments, deadline)
                        .await?;
                    let id = format!("{}-{}", now_ms(), uuid::Uuid::new_v4());
                    {
                        let mut journal = self
                            .journal
                            .lock()
                            .map_err(|_| "Call journal unavailable")?;
                        if journal.len() >= 256 {
                            journal.pop_front();
                        }
                        journal.push_back(json!({"id":id,"run_id":run_id,"server":name,"tool":tool,"started_ms":now_ms(),"status":"dispatched"}));
                    }
                    let mut record = CallRecord {
                        journal: self.journal.clone(),
                        journal_path: self.journal_path.clone(),
                        journal_io: self.journal_io.clone(),
                        id,
                        finished: false,
                    };
                    persist_journal(
                        self.journal.clone(),
                        self.journal_path.clone(),
                        self.journal_io.clone(),
                    )
                    .await?;
                    let result = request(
                        &session,
                        json!({"method":"tools/call","params":{"name":tool,"arguments":arguments}}),
                        deadline,
                    )
                    .await;
                    if let Ok(envelope) = &result {
                        record.finish(
                            if envelope.get("isError").and_then(Value::as_bool) == Some(true) {
                                "tool_error"
                            } else {
                                "completed"
                            },
                        ).await.map_err(|_|"MCP call completed but durable journal update failed; do not retry blindly")?;
                    } else {
                        record.finish("outcome_unknown").await.map_err(|_|"MCP outcome is unknown and durable journal update failed; do not retry blindly")?;
                    }
                    result
                };
                if session.is_closed() || result.as_ref().is_err_and(|e| e.contains("transport")) {
                    self.invalidate(&key).await;
                }
                result
            }
            _ => Err("Unknown MCP bridge operation".into()),
        }
    }

    async fn session(
        &self,
        cfg: &ServerConfig,
        deadline: tokio::time::Instant,
    ) -> Result<(String, Arc<Session>), String> {
        // Canonical serialized configuration is only an in-memory key; never emitted.
        let key = transport_key(cfg)?;
        if self
            .failures
            .lock()
            .await
            .get(&key)
            .is_some_and(|at| at.elapsed() < Duration::from_secs(60))
        {
            return Err(
                "MCP connection recently failed; verify configuration then mcp.refresh() to retry"
                    .into(),
            );
        }
        let (slot, retired) = {
            let mut sessions = self.sessions.lock().await;
            let mut retired = None;
            if !sessions.contains_key(&key) && sessions.len() >= 32 {
                let idle = sessions.iter().find_map(|(key, slot)| {
                    slot.try_lock().ok().and_then(|guard| {
                        if guard
                            .as_ref()
                            .is_none_or(|s| s.is_closed() || Arc::strong_count(s) == 1)
                        {
                            Some(key.clone())
                        } else {
                            None
                        }
                    })
                });
                if let Some(idle) = idle {
                    if let Some(slot) = sessions.remove(&idle) {
                        if let Ok(mut guard) = slot.try_lock() {
                            retired = guard.take();
                        }
                    }
                } else {
                    return Err("MCP broker has32 active transports; finish or cancel an existing run before connecting another server".into());
                }
            }
            let slot = sessions
                .entry(key.clone())
                .or_insert_with(|| Arc::new(Mutex::new(None)))
                .clone();
            (slot, retired)
        };
        if let Some(retired) = retired {
            close_session(retired).await;
        }
        let connect_deadline = deadline.min(tokio::time::Instant::now() + Duration::from_secs(30));
        let mut guard = tokio::time::timeout_at(connect_deadline, slot.lock())
            .await
            .map_err(|_| "MCP connect budget exceeded")?;
        if let Some(session) = guard.as_ref().filter(|s| !s.is_closed()) {
            return Ok((key, session.clone()));
        }
        guard.take();
        let connected = tokio::time::timeout_at(connect_deadline, connect(cfg))
            .await
            .map_err(|_| "MCP connect timeout; verify server configuration".to_owned())
            .and_then(|v| v);
        let session = match connected {
            Ok(session) => session,
            Err(error) => {
                self.failures
                    .lock()
                    .await
                    .insert(key, tokio::time::Instant::now());
                return Err(error);
            }
        };
        let session = Arc::new(session);
        *guard = Some(session.clone());
        Ok((key, session))
    }

    async fn tools(
        &self,
        session: &Session,
        cfg: &ServerConfig,
        deadline: tokio::time::Instant,
    ) -> Result<Value, String> {
        let key = serde_json::to_string(cfg).map_err(|_| "Cannot identify MCP configuration")?;
        if let Some((at, value)) = self
            .catalogue
            .lock()
            .await
            .get(&key)
            .filter(|(at, _)| at.elapsed() < Duration::from_secs(15))
        {
            let _ = at;
            return Ok(value.clone());
        }
        let mut tools = Vec::new();
        let mut catalogue_bytes = 0usize;
        let mut names = HashSet::new();
        let mut cursor: Option<String> = None;
        let mut seen = HashSet::new();
        for _ in 0..128 {
            let params = cursor
                .as_ref()
                .map_or_else(|| json!({}), |c| json!({"cursor":c}));
            let result = request(
                session,
                json!({"method":"tools/list","params":params}),
                deadline,
            )
            .await?;
            let page = result
                .get("tools")
                .and_then(Value::as_array)
                .ok_or("Invalid MCP tools/list response")?;
            for tool in page {
                if cfg.allowed_tools.as_ref().is_none_or(|allow| {
                    tool.get("name")
                        .and_then(Value::as_str)
                        .is_some_and(|n| allow.iter().any(|t| t == n))
                }) {
                    catalogue_bytes += serde_json::to_vec(tool)
                        .map_err(|_| "Cannot inspect MCP tool definition")?
                        .len();
                    if catalogue_bytes > 1024 * 1024 {
                        return Err("MCP tool catalogue exceeds1MiB; reduce exposed tools or configure allowedTools".into());
                    }
                    if let Some(name) = tool.get("name").and_then(Value::as_str) {
                        if !names.insert(name.to_owned()) {
                            return Err("MCP tool catalogue contains duplicate names; catalogue is ambiguous".into());
                        }
                    }
                    tools.push(tool.clone());
                }
            }
            if tools.len() > 10000 {
                return Err("MCP tool catalogue exceeds limit of 10000 entries".into());
            }
            cursor = result
                .get("nextCursor")
                .and_then(Value::as_str)
                .map(str::to_owned);
            let Some(c) = &cursor else {
                let value = Value::Array(tools);
                let mut catalogue = self.catalogue.lock().await;
                if catalogue.len() >= 32 {
                    if let Some(oldest) = catalogue
                        .iter()
                        .min_by_key(|(_, v)| v.0)
                        .map(|(k, _)| k.clone())
                    {
                        catalogue.remove(&oldest);
                    }
                }
                catalogue.insert(key, (tokio::time::Instant::now(), value.clone()));
                return Ok(value);
            };
            if !seen.insert(c.clone()) {
                return Err("MCP tools/list repeated a cursor; catalogue is incomplete".into());
            }
        }
        Err("MCP tool catalogue exceeds 128 pages; catalogue is incomplete".into())
    }

    async fn invalidate(&self, key: &str) {
        self.catalogue.lock().await.clear();
        let slot = self.sessions.lock().await.remove(key);
        if let Some(slot) = slot {
            if let Some(session) = slot.lock().await.take() {
                close_session(session).await;
            }
        }
    }
    pub async fn refresh(&self) -> Result<(), String> {
        let loaded = self.registry.lock().await.reload();
        let next = match loaded {
            Ok(next) => next,
            Err(error) => {
                *self.config_error.lock().await = Some(error.clone());
                self.registry.lock().await.servers.clear();
                let keys: Vec<String> = self.sessions.lock().await.keys().cloned().collect();
                for key in keys {
                    self.invalidate(&key).await;
                }
                self.catalogue.lock().await.clear();
                *self.refresh_at.lock().await = tokio::time::Instant::now();
                return Err(error);
            }
        };
        let keep: HashSet<String> = next
            .servers
            .values()
            .filter_map(|s| transport_key(s).ok())
            .collect();
        let removed: Vec<String> = self
            .sessions
            .lock()
            .await
            .keys()
            .filter(|k| !keep.contains(*k))
            .cloned()
            .collect();
        for key in removed {
            self.invalidate(&key).await;
        }
        *self.registry.lock().await = next;
        *self.config_error.lock().await = None;
        self.catalogue.lock().await.clear();
        self.failures.lock().await.clear();
        self.validators.lock().await.clear();
        *self.refresh_at.lock().await = tokio::time::Instant::now();
        Ok(())
    }
    async fn refresh_if_due(&self) -> Result<(), String> {
        let due = self.refresh_at.lock().await.elapsed() >= Duration::from_secs(30);
        if due {
            self.refresh().await?;
        }
        Ok(())
    }
    async fn validate(
        &self,
        schema: Option<&Value>,
        arguments: &Value,
        deadline: tokio::time::Instant,
    ) -> Result<(), String> {
        let schema = schema.ok_or("MCP tool omitted inputSchema")?;
        bounded_json(schema, 65536, 32, 2048)?;
        bounded_json(arguments, 262144, 64, 16384)?;
        let key = serde_json::to_string(schema).map_err(|_| "Cannot inspect MCP schema")?;
        let cached = self.validators.lock().await.get(&key).cloned();
        let validator = if let Some(cached) = cached {
            cached
        } else {
            let schema = schema.clone();
            let compiled =
                blocking_validation(self.validation_slots.clone(), deadline, move || {
                    compile_schema(&schema)
                })
                .await??;
            let compiled = Arc::new(compiled);
            let mut validators = self.validators.lock().await;
            if validators.len() >= 1024 {
                validators.clear();
            }
            validators.insert(key, compiled.clone());
            compiled
        };
        let arguments = arguments.clone();
        let valid = blocking_validation(self.validation_slots.clone(), deadline, move || {
            validator.is_valid(&arguments)
        })
        .await?;
        if valid {
            Ok(())
        } else {
            Err("MCP arguments violate inputSchema; inspect mcp.help(server, tool). Values are omitted from diagnostics".into())
        }
    }
    pub async fn health(&self) -> Value {
        let servers = self.registry.lock().await.servers.len();
        let slots = self
            .sessions
            .lock()
            .await
            .values()
            .cloned()
            .collect::<Vec<_>>();
        let mut connected = 0;
        for slot in slots {
            if let Ok(guard) = slot.try_lock() {
                if guard.as_ref().is_some_and(|s| !s.is_closed()) {
                    connected += 1;
                }
            }
        }
        json!({"configured_servers":servers,"connected_transports":connected,"configuration_error":self.config_error.lock().await.clone(),"max_concurrent_requests":32,"authentication":"explicit headers, OAuth client credentials, independent PKCE browser grants", "journal":self.journal.lock().map(|j|j.len()).unwrap_or(0)})
    }
    pub async fn shutdown(&self) {
        self.calls.close();
        let slots = std::mem::take(&mut *self.sessions.lock().await);
        for slot in slots.into_values() {
            if let Some(session) = slot.lock().await.take() {
                close_session(session).await;
            }
        }
    }
}

async fn connect(cfg: &ServerConfig) -> Result<Session, String> {
    if cfg.transport.as_deref() == Some("stdio") {
        let mut env = cfg.env.clone();
        env.insert("REPL_MCP_NO_BRIDGE".into(), "1".into());
        let mut child = crate::guardian::spawn_owned(crate::guardian::CommandSpec {
            program: cfg.command.clone().ok_or("Missing MCP command")?,
            args: cfg.args.clone(),
            env,
            cwd: cfg.cwd.clone(),
            worker: false,
        })
        .await?;
        let pid = child.id().ok_or("Missing owned MCP process identifier")?;
        let stdout = child.stdout.take().ok_or("Missing MCP stdout")?;
        let stdin = child.stdin.take().ok_or("Missing MCP stdin")?;
        // Arbitrary backend stderr stays inside the guardian's null sink.
        drop(child.stderr.take());
        let transport = OwnedChildTransport {
            inner: rmcp::transport::async_rw::AsyncRwTransport::new(
                BoundedRead::new(stdout),
                stdin,
            ),
            child: Some(child),
            pid,
        };
        ().serve(transport).await.map_err(|_| {
            "MCP transport initialization failed; verify server and explicit credentials".into()
        })
    } else {
        let mut config =
            rmcp::transport::streamable_http_client::StreamableHttpClientTransportConfig::with_uri(
                cfg.url.clone().ok_or("Missing MCP URL")?,
            );
        config.reinit_on_expired_session = false;
        config.max_sse_event_size = 1024 * 1024;
        if cfg.oauth.is_some() {
            config.auth_header = Some(oauth_access_token(cfg).await?);
        }
        for (name, value) in &cfg.headers {
            config.custom_headers.insert(
                reqwest::header::HeaderName::from_bytes(name.as_bytes())
                    .map_err(|_| "Invalid header name")?,
                reqwest::header::HeaderValue::from_str(value)
                    .map_err(|_| "Invalid header value")?,
            );
        }
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| "Cannot create HTTP MCP transport")?;
        ().serve(StreamableHttpClientTransport::with_client(BoundedHttpClient(client),config)).await.map_err(|_| "HTTP MCP initialization failed; configure explicit credentials or run --oauth-login SERVER for an independent broker grant".into())
    }
}

async fn request(
    session: &Session,
    value: Value,
    deadline: tokio::time::Instant,
) -> Result<Value, String> {
    let req: ClientRequest = serde_json::from_value(value).map_err(|_| "Invalid broker request")?;
    let handle = tokio::time::timeout_at(
        deadline,
        session.send_cancellable_request(req, rmcp::service::PeerRequestOptions::no_options()),
    )
    .await
    .map_err(|_| "MCP deadline exceeded before dispatch; external outcome may be unknown")?
    .map_err(|_| "MCP transport unavailable; request was not retried")?;
    let mut pending = PendingRequest(Some(handle));
    let response = tokio::time::timeout_at(deadline, &mut pending.0.as_mut().unwrap().rx)
        .await
        .map_err(|_| "MCP deadline exceeded; external outcome is unknown, request was not retried")?
        .unwrap_or(Err(ServiceError::TransportClosed));
    // Feed the already received response through SDK completion so subscription/progress cleanup runs.
    let mut handle = pending.0.take().unwrap();
    let (tx, rx) = tokio::sync::oneshot::channel();
    handle.rx = rx;
    let _ = tx.send(response);
    let result:ServerResult=handle.await_response().await.map_err(|_| "MCP transport or protocol error; external outcome may be unknown, request was not retried")?;
    serde_json::to_value(result).map_err(|_| "MCP result cannot be represented as JSON".into())
}

fn compile_schema(schema: &Value) -> Result<jsonschema::Validator, String> {
    fn external_ref(v: &Value) -> bool {
        match v {
            Value::Object(map) => {
                ["$ref", "$dynamicRef", "$recursiveRef"].iter().any(|key| {
                    map.get(*key)
                        .and_then(Value::as_str)
                        .is_some_and(|r| !r.starts_with('#'))
                }) || map.values().any(external_ref)
            }
            Value::Array(values) => values.iter().any(external_ref),
            _ => false,
        }
    }
    if external_ref(schema) {
        return Err(
            "MCP inputSchema has external references; inline the schema before broker execution"
                .into(),
        );
    }
    jsonschema::options()
        .with_pattern_options(jsonschema::PatternOptions::fancy_regex().backtrack_limit(10_000))
        .build(schema)
        .map_err(|_| "MCP tool has invalid inputSchema".into())
}
async fn blocking_validation<T: Send + 'static>(
    slots: Arc<Semaphore>,
    deadline: tokio::time::Instant,
    work: impl FnOnce() -> T + Send + 'static,
) -> Result<T, String> {
    let permit = tokio::time::timeout_at(deadline, slots.acquire_owned())
        .await
        .map_err(|_| "MCP schema processors busy; request budget exceeded, no tool dispatched")?
        .map_err(|_| "MCP schema processor stopped")?;
    // Ownership stays inside the real blocking job, even if its async waiter is cancelled.
    tokio::time::timeout_at(
        deadline,
        tokio::task::spawn_blocking(move || {
            let _permit = permit;
            work()
        }),
    )
    .await
    .map_err(|_| "MCP schema processing exceeded request budget; no tool dispatched")?
    .map_err(|_| "MCP schema processor failed".into())
}
fn bounded_json(
    value: &Value,
    max_bytes: usize,
    max_depth: usize,
    max_nodes: usize,
) -> Result<(), String> {
    if serde_json::to_vec(value)
        .map_err(|_| "Invalid MCP JSON")?
        .len()
        > max_bytes
    {
        return Err("MCP schema or arguments exceed byte budget".into());
    }
    fn visit(
        value: &Value,
        depth: usize,
        max_depth: usize,
        nodes: &mut usize,
        max_nodes: usize,
    ) -> Result<(), String> {
        *nodes += 1;
        if depth > max_depth || *nodes > max_nodes {
            return Err("MCP schema or arguments exceed depth/node budget".into());
        }
        match value {
            Value::Object(map) => {
                for (key, val) in map {
                    if matches!(key.as_str(), "anyOf" | "oneOf" | "allOf")
                        && val.as_array().is_some_and(|v| v.len() > 16)
                    {
                        return Err("MCP schema exceeds composition budget".into());
                    }
                    if key == "pattern" && val.as_str().is_some_and(|v| v.len() > 256) {
                        return Err("MCP schema exceeds pattern budget".into());
                    }
                    visit(val, depth + 1, max_depth, nodes, max_nodes)?;
                }
            }
            Value::Array(values) => {
                for v in values {
                    visit(v, depth + 1, max_depth, nodes, max_nodes)?;
                }
            }
            _ => {}
        }
        Ok(())
    }
    visit(value, 0, max_depth, &mut 0, max_nodes)
}
fn now_ms() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}
fn transport_key(config: &ServerConfig) -> Result<String, String> {
    let mut transport = config.clone();
    transport.disabled = false;
    transport.broker_allowed = false;
    transport.allowed_tools = None;
    serde_json::to_string(&transport).map_err(|_| "Cannot identify MCP configuration".into())
}

#[derive(Default)]
struct JournalIoState {
    requested: u64,
    completed: u64,
    running: bool,
    error: Option<String>,
}
struct JournalIo {
    state: StdMutex<JournalIoState>,
    notify: tokio::sync::Notify,
    serial: StdMutex<()>,
    slots: Arc<Semaphore>,
    file_lock: StdMutex<Option<std::fs::File>>,
}
impl JournalIo {
    fn new() -> Self {
        Self {
            state: StdMutex::new(JournalIoState::default()),
            notify: tokio::sync::Notify::new(),
            serial: StdMutex::new(()),
            slots: Arc::new(Semaphore::new(1)),
            file_lock: StdMutex::new(None),
        }
    }
}
fn schedule_journal(
    journal: Arc<StdMutex<VecDeque<Value>>>,
    path: Arc<StdMutex<Option<PathBuf>>>,
    io: Arc<JournalIo>,
) -> Result<Option<u64>, String> {
    if path
        .lock()
        .map_err(|_| "Broker journal unavailable")?
        .is_none()
    {
        return Ok(None);
    }
    let (target, start) = {
        let mut state = io.state.lock().map_err(|_| "Broker journal unavailable")?;
        state.requested = state.requested.saturating_add(1);
        let start = !state.running;
        state.running = true;
        (state.requested, start)
    };
    if start {
        tokio::spawn(journal_writer(journal, path, io));
    }
    Ok(Some(target))
}
async fn journal_writer(
    journal: Arc<StdMutex<VecDeque<Value>>>,
    path: Arc<StdMutex<Option<PathBuf>>>,
    io: Arc<JournalIo>,
) {
    loop {
        let target = match io.state.lock() {
            Ok(state) => state.requested,
            Err(_) => return,
        };
        let permit = match io.slots.clone().acquire_owned().await {
            Ok(permit) => permit,
            Err(_) => return,
        };
        let job_io = io.clone();
        let job_journal = journal.clone();
        let job_path = path.clone();
        let result = tokio::task::spawn_blocking(move || {
            let _permit = permit;
            let _serialized = job_io
                .serial
                .lock()
                .map_err(|_| "Broker journal unavailable")?;
            // The small metadata/path locks never span file operations or fsync.
            let snapshot = job_journal
                .lock()
                .map_err(|_| "Broker journal unavailable")?
                .clone();
            let destination = job_path
                .lock()
                .map_err(|_| "Broker journal unavailable")?
                .clone();
            if let Some(destination) = destination {
                atomic_snapshot(&destination, &snapshot)?;
            }
            Ok::<(), String>(())
        })
        .await
        .unwrap_or_else(|_| Err("Broker journal persistence failed".into()));
        let again = if let Ok(mut state) = io.state.lock() {
            state.completed = target;
            state.error = result.err();
            let again = state.requested > target;
            if !again {
                state.running = false;
            }
            again
        } else {
            false
        };
        io.notify.notify_waiters();
        if !again {
            return;
        }
    }
}
async fn persist_journal(
    journal: Arc<StdMutex<VecDeque<Value>>>,
    path: Arc<StdMutex<Option<PathBuf>>>,
    io: Arc<JournalIo>,
) -> Result<(), String> {
    let Some(target) = schedule_journal(journal, path, io.clone())? else {
        return Ok(());
    };
    loop {
        let notified = io.notify.notified();
        tokio::pin!(notified);
        notified.as_mut().enable();
        {
            let state = io.state.lock().map_err(|_| "Broker journal unavailable")?;
            if state.completed >= target {
                return state.error.clone().map_or(Ok(()), Err);
            }
        }
        notified.await;
    }
}
fn atomic_snapshot(path: &std::path::Path, records: &VecDeque<Value>) -> Result<(), String> {
    use std::io::Write;
    let temporary = path.with_extension(format!("{}.new", uuid::Uuid::new_v4()));
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let result = (|| {
        let mut file = options
            .open(&temporary)
            .map_err(|_| "Cannot create private broker journal")?;
        serde_json::to_writer(&mut file, records).map_err(|_| "Cannot write broker journal")?;
        file.flush().map_err(|_| "Cannot flush broker journal")?;
        file.sync_all().map_err(|_| "Cannot sync broker journal")?;
        std::fs::rename(&temporary, path).map_err(|_| "Cannot replace broker journal")?;
        if let Some(parent) = path.parent() {
            std::fs::File::open(parent)
                .and_then(|dir| dir.sync_all())
                .map_err(|_| "Cannot sync broker journal directory")?;
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(temporary);
    }
    result
}
async fn close_session(session: Arc<Session>) {
    session.cancellation_token().cancel();
    if let Ok(mut service) = Arc::try_unwrap(session) {
        let _ = service.close_with_timeout(Duration::from_secs(5)).await;
    }
}

async fn oauth_access_token(cfg: &ServerConfig) -> Result<String, String> {
    use rmcp::transport::auth::{AuthorizationManager, ClientCredentialsConfig, OAuthState};
    let oauth = cfg.oauth.as_ref().ok_or("Missing OAuth configuration")?;
    let url = cfg.url.as_deref().ok_or("OAuth requires HTTP URL")?;
    let mut manager = AuthorizationManager::new(url)
        .await
        .map_err(|_| "Cannot initialize OAuth discovery")?;
    if oauth.grant_type == "client_credentials" {
        let mut state = OAuthState::Unauthorized(manager);
        state.authenticate_client_credentials(ClientCredentialsConfig::ClientSecret { client_id:oauth.client_id.clone().ok_or("Missing OAuth clientId")?,client_secret:oauth.client_secret.clone().ok_or("Missing OAuth clientSecret")?,scopes:oauth.scopes.clone(),resource:Some(url.to_owned()) }).await.map_err(|_|"OAuth client credentials exchange failed; verify independent broker grant and supported authentication method")?;
        let manager = state
            .into_authorization_manager()
            .ok_or("OAuth authorization manager unavailable")?;
        manager
            .get_access_token()
            .await
            .map_err(|_| "OAuth access token unavailable".into())
    } else {
        manager.set_credential_store(FileCredentialStore::new(
            oauth
                .credential_file
                .clone()
                .ok_or("OAuth credentialFile required")?,
        )?);
        if !manager.initialize_from_store().await.map_err(
            |_| "OAuth credential store or issuer validation failed; run --oauth-login SERVER",
        )? {
            return Err("Independent OAuth grant required: run repl-mcp --config REGISTRY --oauth-login SERVER".into());
        }
        if let (Some(id), Some(secret)) = (&oauth.client_id, &oauth.client_secret) {
            manager
                .configure_client(
                    rmcp::transport::auth::OAuthClientConfig::new(id, "http://127.0.0.1/callback")
                        .with_client_secret(secret)
                        .with_scopes(oauth.scopes.clone()),
                )
                .map_err(|_| "Cannot configure OAuth confidential client refresh")?;
        }
        manager
            .get_access_token()
            .await
            .map_err(|_| "OAuth refresh failed or grant expired; run --oauth-login SERVER".into())
    }
}

#[derive(Clone)]
struct FileCredentialStore {
    path: PathBuf,
}
impl FileCredentialStore {
    fn new(path: PathBuf) -> Result<Self, String> {
        let parent = path
            .parent()
            .ok_or("OAuth credentialFile needs a parent directory")?;
        if !parent.exists() {
            let mut builder = std::fs::DirBuilder::new();
            builder.recursive(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            builder
                .create(parent)
                .map_err(|_| "Cannot create private OAuth store directory")?;
        }
        let metadata = std::fs::symlink_metadata(parent)
            .map_err(|_| "Cannot inspect OAuth store directory")?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err("OAuth store directory must be a real private directory".into());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if metadata.permissions().mode() & 0o077 != 0 {
                return Err("OAuth store directory must have permissions 0700".into());
            }
        }
        Ok(Self { path })
    }
    fn check_file(&self) -> Result<(), rmcp::transport::auth::AuthError> {
        let metadata = std::fs::symlink_metadata(&self.path).map_err(|_| store_error())?;
        if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > 65536 {
            return Err(store_error());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if metadata.permissions().mode() & 0o077 != 0 {
                return Err(store_error());
            }
        }
        Ok(())
    }
}
fn store_error() -> rmcp::transport::auth::AuthError {
    rmcp::transport::auth::AuthError::CredentialStoreError("Private broker credential store unavailable or unsafe; require regular file0600 and directory0700".into())
}
#[async_trait::async_trait]
impl rmcp::transport::auth::CredentialStore for FileCredentialStore {
    async fn load(
        &self,
    ) -> Result<Option<rmcp::transport::auth::StoredCredentials>, rmcp::transport::auth::AuthError>
    {
        use std::io::Read;
        let mut options = std::fs::OpenOptions::new();
        options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW);
        }
        let file = match options.open(&self.path) {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(store_error()),
        };
        let metadata = file.metadata().map_err(|_| store_error())?;
        if !metadata.is_file() || metadata.len() > 65536 {
            return Err(store_error());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::{MetadataExt, PermissionsExt};
            if metadata.permissions().mode() & 0o077 != 0
                || metadata.uid() != unsafe { libc::geteuid() }
            {
                return Err(store_error());
            }
        }
        let mut bytes = Vec::new();
        file.take(65537)
            .read_to_end(&mut bytes)
            .map_err(|_| store_error())?;
        if bytes.len() > 65536 {
            return Err(store_error());
        }
        serde_json::from_slice(&bytes)
            .map(Some)
            .map_err(|_| store_error())
    }
    async fn save(
        &self,
        credentials: rmcp::transport::auth::StoredCredentials,
    ) -> Result<(), rmcp::transport::auth::AuthError> {
        use std::io::Write;
        if self.path.exists() {
            self.check_file()?;
        }
        let temporary = self
            .path
            .with_extension(format!("{}.new", uuid::Uuid::new_v4()));
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let result = (|| {
            let mut file = options.open(&temporary).map_err(|_| store_error())?;
            let bytes = serde_json::to_vec(&credentials).map_err(|_| store_error())?;
            if bytes.len() > 65536 {
                return Err(store_error());
            }
            file.write_all(&bytes).map_err(|_| store_error())?;
            file.sync_all().map_err(|_| store_error())?;
            std::fs::rename(&temporary, &self.path).map_err(|_| store_error())
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(temporary);
        }
        result
    }
    async fn clear(&self) -> Result<(), rmcp::transport::auth::AuthError> {
        if self.path.exists() {
            self.check_file()?;
            std::fs::remove_file(&self.path).map_err(|_| store_error())?;
        }
        Ok(())
    }
    async fn acquire_refresh_guard(
        &self,
    ) -> Result<
        Option<rmcp::transport::auth::CredentialRefreshGuard>,
        rmcp::transport::auth::AuthError,
    > {
        #[cfg(unix)]
        {
            use std::os::{fd::AsRawFd, unix::fs::OpenOptionsExt};
            let lockpath = self.path.with_extension("lock");
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(lockpath)
                .map_err(|_| store_error())?;
            let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
            loop {
                if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                    return Ok(Some(rmcp::transport::auth::CredentialRefreshGuard::new(
                        file,
                    )));
                }
                if tokio::time::Instant::now() >= deadline {
                    return Err(store_error());
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        }
        #[cfg(not(unix))]
        Ok(None)
    }
}

/// Bound bytes before the SDK's line parser allocates an entire peer frame.
struct BoundedRead<R> {
    inner: R,
    line_bytes: usize,
}
impl<R> BoundedRead<R> {
    fn new(inner: R) -> Self {
        Self {
            inner,
            line_bytes: 0,
        }
    }
}
impl<R: tokio::io::AsyncRead + Unpin> tokio::io::AsyncRead for BoundedRead<R> {
    fn poll_read(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        let this = self.get_mut();
        let start = buf.filled().len();
        match std::pin::Pin::new(&mut this.inner).poll_read(cx, buf) {
            std::task::Poll::Ready(Ok(())) => {
                for byte in &buf.filled()[start..] {
                    if *byte == b'\n' {
                        this.line_bytes = 0;
                    } else {
                        this.line_bytes += 1;
                        if this.line_bytes > 1024 * 1024 {
                            return std::task::Poll::Ready(Err(std::io::Error::other(
                                "MCP peer frame exceeds 1 MiB",
                            )));
                        }
                    }
                }
                std::task::Poll::Ready(Ok(()))
            }
            other => other,
        }
    }
}
type ChildIo = rmcp::transport::async_rw::AsyncRwTransport<
    RoleClient,
    BoundedRead<tokio::process::ChildStdout>,
    tokio::process::ChildStdin,
>;
struct OwnedChildTransport {
    inner: ChildIo,
    child: Option<tokio::process::Child>,
    pid: u32,
}
impl Drop for OwnedChildTransport {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            unsafe {
                libc::kill(-(self.pid as i32), libc::SIGKILL);
            }
            tokio::spawn(async move {
                let _ = child.wait().await;
            });
        }
    }
}
impl rmcp::transport::Transport<RoleClient> for OwnedChildTransport {
    type Error = std::io::Error;
    fn send(
        &mut self,
        item: rmcp::service::TxJsonRpcMessage<RoleClient>,
    ) -> impl std::future::Future<Output = Result<(), Self::Error>> + Send + 'static {
        self.inner.send(item)
    }
    async fn receive(&mut self) -> Option<rmcp::service::RxJsonRpcMessage<RoleClient>> {
        self.inner.receive().await
    }
    async fn close(&mut self) -> Result<(), Self::Error> {
        let result = self.inner.close().await;
        if let Some(mut child) = self.child.take() {
            if tokio::time::timeout(Duration::from_secs(3), child.wait())
                .await
                .is_err()
            {
                unsafe {
                    libc::kill(-(self.pid as i32), libc::SIGKILL);
                }
                let _ = child.wait().await;
            }
        }
        result
    }
}

#[derive(Clone)]
struct BoundedHttpClient(reqwest::Client);
impl rmcp::transport::streamable_http_client::StreamableHttpClient for BoundedHttpClient {
    type Error = reqwest::Error;
    async fn get_stream(
        &self,
        uri: Arc<str>,
        session: Option<Arc<str>>,
        last: Option<String>,
        auth: Option<String>,
        headers: std::collections::HashMap<
            reqwest::header::HeaderName,
            reqwest::header::HeaderValue,
        >,
    ) -> Result<
        futures_util::stream::BoxStream<'static, Result<sse_stream::Sse, sse_stream::Error>>,
        rmcp::transport::streamable_http_client::StreamableHttpError<Self::Error>,
    > {
        use rmcp::transport::streamable_http_client::StreamableHttpClient;
        StreamableHttpClient::get_stream_with_max_sse_event_size(
            &self.0,
            uri,
            session,
            last,
            auth,
            headers,
            1024 * 1024,
        )
        .await
    }
    async fn get_stream_with_max_sse_event_size(
        &self,
        uri: Arc<str>,
        session: Option<Arc<str>>,
        last: Option<String>,
        auth: Option<String>,
        headers: std::collections::HashMap<
            reqwest::header::HeaderName,
            reqwest::header::HeaderValue,
        >,
        max: usize,
    ) -> Result<
        futures_util::stream::BoxStream<'static, Result<sse_stream::Sse, sse_stream::Error>>,
        rmcp::transport::streamable_http_client::StreamableHttpError<Self::Error>,
    > {
        use rmcp::transport::streamable_http_client::StreamableHttpClient;
        StreamableHttpClient::get_stream_with_max_sse_event_size(
            &self.0,
            uri,
            session,
            last,
            auth,
            headers,
            max.min(1024 * 1024),
        )
        .await
    }
    async fn delete_session(
        &self,
        uri: Arc<str>,
        session: Arc<str>,
        auth: Option<String>,
        headers: std::collections::HashMap<
            reqwest::header::HeaderName,
            reqwest::header::HeaderValue,
        >,
    ) -> Result<(), rmcp::transport::streamable_http_client::StreamableHttpError<Self::Error>> {
        use rmcp::transport::streamable_http_client::StreamableHttpClient;
        StreamableHttpClient::delete_session(&self.0, uri, session, auth, headers).await
    }
    async fn post_message(
        &self,
        uri: Arc<str>,
        message: rmcp::model::ClientJsonRpcMessage,
        session: Option<Arc<str>>,
        auth: Option<String>,
        headers: std::collections::HashMap<
            reqwest::header::HeaderName,
            reqwest::header::HeaderValue,
        >,
    ) -> Result<
        rmcp::transport::streamable_http_client::StreamableHttpPostResponse,
        rmcp::transport::streamable_http_client::StreamableHttpError<Self::Error>,
    > {
        self.post_message_with_max_sse_event_size(uri, message, session, auth, headers, 1024 * 1024)
            .await
    }
    async fn post_message_with_max_sse_event_size(
        &self,
        uri: Arc<str>,
        message: rmcp::model::ClientJsonRpcMessage,
        session: Option<Arc<str>>,
        auth: Option<String>,
        headers: std::collections::HashMap<
            reqwest::header::HeaderName,
            reqwest::header::HeaderValue,
        >,
        max: usize,
    ) -> Result<
        rmcp::transport::streamable_http_client::StreamableHttpPostResponse,
        rmcp::transport::streamable_http_client::StreamableHttpError<Self::Error>,
    > {
        use futures_util::StreamExt;
        use rmcp::{
            model::ClientJsonRpcMessage,
            transport::streamable_http_client::{
                StreamableHttpError as E, StreamableHttpPostResponse as R,
            },
        };
        let mut request = self
            .0
            .post(uri.as_ref())
            .header("accept", "application/json, text/event-stream");
        if let Some(auth) = auth {
            request = request.bearer_auth(auth);
        }
        for (name, value) in headers {
            if matches!(name.as_str(), "accept" | "mcp-session-id" | "last-event-id") {
                return Err(E::ReservedHeaderConflict(name.to_string()));
            }
            request = request.header(name, value);
        }
        let attached = session.is_some();
        if let Some(session) = session {
            request = request.header("mcp-session-id", session.as_ref());
        }
        let response = request.json(&message).send().await.map_err(E::Client)?;
        let status = response.status();
        if status == reqwest::StatusCode::UNAUTHORIZED {
            return Err(E::AuthRequired(
                rmcp::transport::streamable_http_client::AuthRequiredError::new(
                    response
                        .headers()
                        .get("www-authenticate")
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or("")
                        .to_owned(),
                ),
            ));
        }
        if status == reqwest::StatusCode::NOT_FOUND && attached {
            return Err(E::SessionExpired);
        }
        if matches!(
            status,
            reqwest::StatusCode::ACCEPTED | reqwest::StatusCode::NO_CONTENT
        ) {
            return Ok(R::Accepted);
        }
        let session = response
            .headers()
            .get("mcp-session-id")
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned);
        let content = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_owned();
        if status.is_success() && content.starts_with("text/event-stream") {
            let max = max.min(1024 * 1024);
            let stream =
                response
                    .bytes_stream()
                    .scan((0usize, true, false, false), move |state, item| {
                        let output = if state.3 {
                            None
                        } else {
                            Some(match item {
                                Err(_) => {
                                    state.3 = true;
                                    Err(std::io::Error::other("MCP HTTP stream failed"))
                                }
                                Ok(chunk) => {
                                    let mut too_large = false;
                                    for &b in &chunk {
                                        if state.2 {
                                            state.2 = false;
                                            if b == b'\n' {
                                                continue;
                                            }
                                        }
                                        state.0 += 1;
                                        if matches!(b, b'\r' | b'\n') {
                                            if state.1 {
                                                state.0 = 0;
                                            }
                                            state.1 = true;
                                            state.2 = b == b'\r';
                                        } else {
                                            state.1 = false;
                                        }
                                        if state.0 > max {
                                            too_large = true;
                                            break;
                                        }
                                    }
                                    if too_large {
                                        state.3 = true;
                                        Err(std::io::Error::other("MCP HTTP SSE event exceeds1MiB"))
                                    } else {
                                        Ok(chunk)
                                    }
                                }
                            })
                        };
                        futures_util::future::ready(output)
                    });
            return Ok(R::Sse(
                sse_stream::SseStream::from_bytes_stream(stream).boxed(),
                session,
            ));
        }
        let body = bounded_body(response, max.min(1024 * 1024)).await?;
        if !status.is_success() {
            if let Ok(parsed) = serde_json::from_slice::<rmcp::model::ServerJsonRpcMessage>(&body) {
                if matches!(parsed, rmcp::model::ServerJsonRpcMessage::Error(_)) {
                    return Ok(R::Json(parsed, session));
                }
            }
            if let ClientJsonRpcMessage::Request(req) = &message {
                if !attached
                    && status.is_client_error()
                    && matches!(req.request, ClientRequest::DiscoverRequest(_))
                {
                    let parsed = serde_json::from_value(
                        json!({"jsonrpc":"2.0","id":req.id,"error":{"code":-32601,"message":"Server uses legacy MCP initialization"}}),
                    )?;
                    return Ok(R::Json(parsed, None));
                }
            }
            return Err(E::UnexpectedServerResponse(
                "MCP HTTP request failed; body omitted".into(),
            ));
        }
        if matches!(
            message,
            ClientJsonRpcMessage::Notification(_)
                | ClientJsonRpcMessage::Response(_)
                | ClientJsonRpcMessage::Error(_)
        ) && body.is_empty()
        {
            return Ok(R::Accepted);
        }
        if !content.starts_with("application/json") {
            return Err(E::UnexpectedContentType(None));
        }
        serde_json::from_slice(&body)
            .map(|v| R::Json(v, session))
            .map_err(|_| {
                E::UnexpectedServerResponse("Invalid MCP HTTP JSON response; body omitted".into())
            })
    }
}
async fn bounded_body(
    mut response: reqwest::Response,
    max: usize,
) -> Result<Vec<u8>, rmcp::transport::streamable_http_client::StreamableHttpError<reqwest::Error>> {
    use rmcp::transport::streamable_http_client::StreamableHttpError as E;
    if response
        .content_length()
        .is_some_and(|len| len > max as u64)
    {
        return Err(E::Io(std::io::Error::other(
            "MCP HTTP response exceeds1MiB",
        )));
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(E::Client)? {
        if body.len() + chunk.len() > max {
            return Err(E::Io(std::io::Error::other(
                "MCP HTTP response exceeds1MiB",
            )));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_before_effects() {
        let schema = json!({"type":"object","required":["x"],"properties":{"x":{"type":"integer"}},"additionalProperties":false});
        let validator = compile_schema(&schema).unwrap();
        assert!(validator.is_valid(&json!({"x":1})));
        assert!(!validator.is_valid(&json!({"x":"secret"})));
        assert!(compile_schema(&json!({"$ref":"https://private.invalid"})).is_err());
    }
    #[test]
    fn cancelled_record_is_unknown_without_payloads() {
        let journal = Arc::new(StdMutex::new(VecDeque::from([
            json!({"id":"a","status":"dispatched"}),
        ])));
        drop(CallRecord {
            journal: journal.clone(),
            journal_path: Arc::new(StdMutex::new(None)),
            journal_io: Arc::new(JournalIo::new()),
            id: "a".into(),
            finished: false,
        });
        assert_eq!(journal.lock().unwrap()[0]["status"], "outcome_unknown");
    }
    #[tokio::test]
    async fn timed_out_validation_retains_real_cpu_slot_until_job_finishes() {
        use std::sync::atomic::{AtomicBool, Ordering};
        let slots = Arc::new(Semaphore::new(1));
        let (release, wait) = std::sync::mpsc::channel();
        assert!(
            blocking_validation(
                slots.clone(),
                tokio::time::Instant::now() + Duration::from_millis(20),
                move || {
                    let _ = wait.recv();
                }
            )
            .await
            .is_err()
        );
        assert_eq!(slots.available_permits(), 0);
        let ran = Arc::new(AtomicBool::new(false));
        let record = ran.clone();
        assert!(
            blocking_validation(
                slots.clone(),
                tokio::time::Instant::now() + Duration::from_millis(20),
                move || record.store(true, Ordering::SeqCst)
            )
            .await
            .is_err()
        );
        assert!(!ran.load(Ordering::SeqCst));
        release.send(()).unwrap();
        assert_eq!(
            blocking_validation(
                slots.clone(),
                tokio::time::Instant::now() + Duration::from_secs(1),
                || 42
            )
            .await
            .unwrap(),
            42
        );
        assert_eq!(slots.available_permits(), 1);
    }
    #[tokio::test]
    async fn stalled_journal_io_keeps_health_and_metadata_available_and_coalesces_updates() {
        let broker = Broker::new(Registry {
            servers: BTreeMap::new(),
            config: None,
            scope: "none".into(),
        });
        broker
            .journal
            .lock()
            .unwrap()
            .push_back(json!({"id":"controlled","status":"dispatched"}));
        *broker.journal_path.lock().unwrap() =
            Some(std::env::current_dir().unwrap().join(format!(
                "missing-journal-fixture-{}/journal.json",
                uuid::Uuid::new_v4()
            )));
        let io = broker.journal_io.clone();
        let blocked_io = io.clone();
        let (started, ready) = std::sync::mpsc::channel();
        let (release, wait) = std::sync::mpsc::channel();
        let blocker = std::thread::spawn(move || {
            let _fake_io = blocked_io.serial.lock().unwrap();
            started.send(()).unwrap();
            let _ = wait.recv();
        });
        ready.recv_timeout(Duration::from_secs(1)).unwrap();
        let writer = tokio::spawn(persist_journal(
            broker.journal.clone(),
            broker.journal_path.clone(),
            io.clone(),
        ));
        tokio::time::timeout(Duration::from_secs(1), async {
            while io.slots.available_permits() != 0 {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap();
        broker.journal.try_lock().unwrap()[0]["status"] = json!("outcome_unknown");
        let health = tokio::time::timeout(Duration::from_millis(100), broker.health())
            .await
            .unwrap();
        assert_eq!(health["journal"], 1);
        for _ in 0..1000 {
            schedule_journal(
                broker.journal.clone(),
                broker.journal_path.clone(),
                io.clone(),
            )
            .unwrap();
        }
        assert_eq!(io.slots.available_permits(), 0);
        assert_eq!(io.state.lock().unwrap().requested, 1001);
        release.send(()).unwrap();
        blocker.join().unwrap();
        // The controlled destination has no parent directory, so no test artifacts are written.
        assert!(
            tokio::time::timeout(Duration::from_secs(1), writer)
                .await
                .unwrap()
                .unwrap()
                .is_err()
        );
        tokio::time::timeout(Duration::from_secs(1), async {
            while io.state.lock().unwrap().running {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(io.slots.available_permits(), 1);
    }
}
