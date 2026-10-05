//! Lazy worker ownership, bounded wire transport and linked cell/RPC cancellation.
use crate::broker::Broker;
use futures_util::StreamExt;
use serde_json::{Value, json};
use std::{
    collections::{HashSet, VecDeque},
    path::PathBuf,
    sync::{
        Arc, Mutex as StdMutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::{Child, ChildStdin, Command},
    sync::{Mutex, Notify, mpsc, oneshot},
    task::JoinHandle,
};
use tokio_util::{
    codec::{FramedRead, LinesCodec},
    sync::CancellationToken,
};

const FRAME_LIMIT: usize = 1_048_576;
const CODE_LIMIT: usize = 262_144;
const HISTORY_LIMIT: usize = 64;
const WORKER_SOURCE: &str = include_str!("../../src/repl_mcp/native_worker.py");

struct Worker {
    child: Child,
    stdin: FrameWriter,
    writer: JoinHandle<()>,
    events: mpsc::Receiver<Value>,
    reader: JoinHandle<()>,
    stderr: JoinHandle<()>,
    raw_stderr: Arc<StdMutex<Vec<u8>>>,
    stderr_sync: mpsc::Sender<oneshot::Sender<()>>,
    rpc: Arc<StdMutex<Vec<tokio::task::AbortHandle>>>,
    run: Arc<StdMutex<Option<(String, CancellationToken)>>>,
    jobs: Arc<StdMutex<HashSet<u32>>>,
    alive: bool,
    live: Arc<AtomicBool>,
    pid: u32,
    rss_pid: u32,
}

fn signal_group(pid: u32, signal: i32) {
    // Every worker is a new session leader; negative PID addresses its entire job.
    unsafe {
        libc::kill(-(pid as i32), signal);
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        self.live.store(false, Ordering::Release);
        if let Some((_, ct)) = self.run.lock().unwrap().take() {
            ct.cancel();
        }
        // Stop the guardian before our direct child is reaped/PID becomes reusable.
        if self.alive {
            signal_group(self.pid, libc::SIGKILL);
        }
        for pid in self.jobs.lock().unwrap().drain() {
            signal_group(pid, libc::SIGKILL);
        }
        self.reader.abort();
        self.stdin.stop.cancel();
        self.writer.abort();
        self.stderr.abort();
        for task in self.rpc.lock().unwrap().drain(..) {
            task.abort();
        }
    }
}

impl Worker {
    async fn terminate(&mut self) {
        self.stdin.stop.cancel();
        if tokio::time::timeout(Duration::from_secs(1), &mut self.writer)
            .await
            .is_err()
        {
            self.writer.abort();
        }
        for pid in self.jobs.lock().unwrap().drain() {
            signal_group(pid, libc::SIGKILL);
        }
        if tokio::time::timeout(Duration::from_secs(3), self.child.wait())
            .await
            .is_err()
        {
            signal_group(self.pid, libc::SIGKILL);
            let _ = self.child.wait().await;
        }
        self.alive = false;
    }
}

struct ExecutionGuard<'a> {
    pid: u32,
    armed: bool,
    owner: &'a Supervisor,
    id: String,
    started: Instant,
}
impl Drop for ExecutionGuard<'_> {
    fn drop(&mut self) {
        if self.armed {
            signal_group(self.pid, libc::SIGKILL);
            let mut result = failure(
                &self.id,
                "Request abandoned; worker killed; external effects may already have occurred",
                "cleared",
                self.started,
            );
            if let Some(active) = self.owner.active.lock().unwrap().as_ref() {
                let output = active.output.lock().unwrap();
                let raw = active.raw_stderr.lock().unwrap();
                result["stdout"] = json!(output[0]);
                result["stderr"] = json!(format!("{}{}", output[1], String::from_utf8_lossy(&raw)));
                bound_execution_output(&mut result);
                active.cancel.cancel();
            }
            self.owner.remember(result);
        }
        *self.owner.active.lock().unwrap() = None;
    }
}

#[derive(Clone)]
struct Active {
    id: String,
    cancel: CancellationToken,
    request_cancel: CancellationToken,
    started: Instant,
    output: Arc<StdMutex<[String; 2]>>,
    raw_stderr: Arc<StdMutex<Vec<u8>>>,
    status: &'static str,
}

struct Reservation<'a> {
    owner: &'a Supervisor,
    id: String,
    started: Instant,
}
impl Drop for Reservation<'_> {
    fn drop(&mut self) {
        let abandoned = self.owner.active.lock().unwrap().take().is_some();
        if abandoned {
            self.owner.remember(failure(
                &self.id,
                "Execution abandoned before completion",
                "cleared",
                self.started,
            ));
        }
        self.owner.busy.store(false, Ordering::Release);
        self.owner.available.notify_waiters();
    }
}

pub struct Supervisor {
    python: PathBuf,
    broker: Arc<Broker>,
    worker: Mutex<Option<Worker>>,
    active: StdMutex<Option<Active>>,
    records: StdMutex<VecDeque<Value>>,
    runtime: StdMutex<Option<Value>>,
    live: StdMutex<Option<Arc<AtomicBool>>>,
    max_memory_mib: u64,
    session_id: String,
    generation: AtomicU64,
    busy: AtomicBool,
    background: StdMutex<Option<JoinHandle<()>>>,
    available: Notify,
}

#[derive(Clone)]
struct FrameWriter {
    sender: mpsc::Sender<OutboundFrame>,
    stop: CancellationToken,
}

type OutboundFrame = (Vec<u8>, oneshot::Sender<Result<(), String>>);

fn frame_writer(mut pipe: ChildStdin, pid: u32) -> (FrameWriter, JoinHandle<()>) {
    let (sender, mut messages) = mpsc::channel::<OutboundFrame>(4);
    let stop = CancellationToken::new();
    let stopped = stop.clone();
    let task = tokio::spawn(async move {
        loop {
            let message = tokio::select! { biased; _ = stopped.cancelled() => break, message = messages.recv() => message };
            let Some((bytes, ack)) = message else {
                break;
            };
            // An accepted frame completes independently of caller cancellation.
            let written = tokio::select! { biased; _ = stopped.cancelled() => break, written = tokio::time::timeout(Duration::from_secs(5), async { pipe.write_all(&bytes).await?; pipe.flush().await }) => written };
            let result = written
                .map_err(|_| "Worker frame write timed out".to_string())
                .and_then(|result| result.map_err(|_| "Worker command pipe closed".to_string()));
            let failed = result.is_err();
            let _ = ack.send(result);
            if failed {
                signal_group(pid, libc::SIGKILL);
                break;
            }
        }
    });
    (FrameWriter { sender, stop }, task)
}

async fn send(stdin: &FrameWriter, message: &Value) -> Result<(), String> {
    let mut bytes = serde_json::to_vec(message).map_err(|_| "Cannot serialize worker command")?;
    if bytes.len() > FRAME_LIMIT {
        return Err("Worker command exceeds 1 MiB".into());
    }
    bytes.push(b'\n');
    let (ack, result) = oneshot::channel();
    tokio::time::timeout(Duration::from_secs(5), async {
        stdin
            .sender
            .send((bytes, ack))
            .await
            .map_err(|_| "Worker writer closed".to_string())?;
        result
            .await
            .map_err(|_| "Worker writer ended".to_string())?
    })
    .await
    .map_err(|_| "Worker frame queue/write deadline exceeded".to_string())?
}

impl Supervisor {
    pub fn new(python: PathBuf, broker: Arc<Broker>, max_memory_mib: u64) -> Self {
        Self {
            python,
            broker,
            worker: Mutex::new(None),
            active: StdMutex::new(None),
            records: StdMutex::new(VecDeque::new()),
            runtime: StdMutex::new(None),
            live: StdMutex::new(None),
            max_memory_mib,
            session_id: uuid::Uuid::new_v4().to_string(),
            generation: AtomicU64::new(0),
            busy: AtomicBool::new(false),
            background: StdMutex::new(None),
            available: Notify::new(),
        }
    }

    async fn spawn(&self) -> Result<Worker, String> {
        let mut child = crate::guardian::spawn_owned(crate::guardian::CommandSpec {
            program: self.python.to_string_lossy().into_owned(),
            args: vec!["-u".into(), "-c".into(), WORKER_SOURCE.into()],
            env: [("REPL_MCP_NO_BRIDGE".into(), "1".into())].into(),
            cwd: None,
            worker: true,
        })
        .await?;
        let pid = child.id().ok_or("Worker has no PID")?;
        // EOF processing must remain independent of Python's GIL/native calls.
        let (stdin, writer_task) =
            frame_writer(child.stdin.take().ok_or("Worker stdin missing")?, pid);
        let stdout = child.stdout.take().ok_or("Worker stdout missing")?;
        let mut stderr_pipe = child.stderr.take().ok_or("Worker stderr missing")?;
        // Raw fd output belongs in bounded execution results, never server logs.
        let raw_stderr: Arc<StdMutex<Vec<u8>>> = Arc::default();
        let raw_capture = raw_stderr.clone();
        let (stderr_sync, mut stderr_barrier) = mpsc::channel::<oneshot::Sender<()>>(4);
        let stderr = tokio::spawn(async move {
            let mut bytes = [0u8; 8192];
            loop {
                tokio::select! {
                    biased;
                    read = stderr_pipe.read(&mut bytes) => match read {
                        Ok(n) if n > 0 => {
                            let mut capture = raw_capture.lock().unwrap();
                            let length = n.min(65536usize.saturating_sub(capture.len()));
                            capture.extend_from_slice(&bytes[..length]);
                        }
                        _ => break,
                    },
                    barrier = stderr_barrier.recv() => match barrier {
                        Some(ack) => { let _ = ack.send(()); },
                        None => break,
                    }
                }
            }
        });
        let (tx, events) = mpsc::channel(32);
        let live = Arc::new(AtomicBool::new(true));
        let reader_live = live.clone();
        let broker = self.broker.clone();
        let writer = stdin.clone();
        let rpc: Arc<StdMutex<Vec<tokio::task::AbortHandle>>> = Arc::default();
        let run: Arc<StdMutex<Option<(String, CancellationToken)>>> = Arc::default();
        let run_info = run.clone();
        let jobs: Arc<StdMutex<HashSet<u32>>> = Arc::default();
        let job_info = jobs.clone();
        let tasks = rpc.clone();
        let reader = tokio::spawn(async move {
            let mut frames = FramedRead::new(stdout, LinesCodec::new_with_max_length(FRAME_LIMIT));
            while let Some(Ok(frame)) = frames.next().await {
                let event = match serde_json::from_str::<Value>(&frame) {
                    Ok(event) => event,
                    Err(_) => {
                        // libtest banner precedes the subprocess entry; the
                        // production transport never tolerates non-JSON output.
                        #[cfg(test)]
                        if frame.is_empty() || frame == "running 1 test" {
                            continue;
                        }
                        break;
                    }
                };
                if event["type"] == "job" {
                    if let Some(pid) = event["pid"]
                        .as_u64()
                        .and_then(|pid| u32::try_from(pid).ok())
                        .filter(|pid| *pid > 1)
                    {
                        let mut jobs = job_info.lock().unwrap();
                        if event["action"] == "started" {
                            jobs.insert(pid);
                        } else {
                            jobs.remove(&pid);
                        }
                    }
                } else if event["type"] == "rpc" {
                    let authorized = run_info
                        .lock()
                        .unwrap()
                        .as_ref()
                        .is_some_and(|(id, ct)| event["run_id"] == *id && !ct.is_cancelled());
                    let full = {
                        let mut pending = tasks.lock().unwrap();
                        pending.retain(|task| !task.is_finished());
                        pending.len() >= 64
                    };
                    if full || !authorized {
                        let _ = send(&writer, &json!({"type":"rpc_result","id":event["id"],"error":"Too many outstanding MCP calls","result":null})).await;
                        continue;
                    }
                    let broker = broker.clone();
                    let writer = writer.clone();
                    let handle = tokio::spawn(async move {
                        let result = broker
                            .dispatch_with_run(
                                event["run_id"].as_str().unwrap_or(""),
                                event["op"].as_str().unwrap_or(""),
                                event["params"].clone(),
                            )
                            .await;
                        let (result, error) = match result {
                            Ok(v) => (v, Value::Null),
                            Err(e) => (Value::Null, Value::String(e)),
                        };
                        let sent = send(&writer, &json!({"type":"rpc_result","id":event["id"],"result":result,"error":error})).await;
                        if sent.as_ref().is_err_and(|error| error.contains("exceeds")) {
                            let _ = send(&writer, &json!({"type":"rpc_result","id":event["id"],"result":null,"error":"MCP result exceeds IPC limit; call may have completed. Inspect mcp.journal(); do not blindly retry external writes."})).await;
                        }
                    });
                    tasks.lock().unwrap().push(handle.abort_handle());
                } else if tx.send(event).await.is_err() {
                    break;
                }
            }
            reader_live.store(false, Ordering::Release);
        });
        let mut worker = Worker {
            child,
            stdin,
            writer: writer_task,
            events,
            reader,
            stderr,
            raw_stderr,
            stderr_sync,
            rpc,
            run,
            jobs,
            alive: true,
            live: live.clone(),
            pid,
            rss_pid: 0,
        };
        let ready = tokio::time::timeout(Duration::from_secs(10), worker.events.recv())
            .await
            .map_err(|_| "Python worker startup timed out")?
            .ok_or("Python worker exited during startup")?;
        if ready["type"] != "ready" {
            return Err("Invalid Python worker handshake".into());
        }
        worker.rss_pid = ready["pid"]
            .as_u64()
            .and_then(|v| u32::try_from(v).ok())
            .ok_or("Worker handshake omitted real PID")?;
        *self.runtime.lock().unwrap() = Some(ready);
        *self.live.lock().unwrap() = Some(live);
        self.generation.fetch_add(1, Ordering::Relaxed);
        Ok(worker)
    }

    fn remember(&self, result: Value) {
        let mut records = self.records.lock().unwrap();
        records.retain(|record| record["run_id"] != result["run_id"]);
        records.push_back(result);
        while records.len() > HISTORY_LIMIT {
            records.pop_front();
        }
    }

    fn reserve(
        &self,
        id: &str,
        cancel: CancellationToken,
        request_cancel: CancellationToken,
    ) -> bool {
        if self
            .busy
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return false;
        }
        *self.active.lock().unwrap() = Some(Active {
            id: id.into(),
            cancel,
            request_cancel,
            started: Instant::now(),
            output: Arc::new(StdMutex::new([String::new(), String::new()])),
            raw_stderr: Arc::default(),
            status: "starting",
        });
        true
    }

    pub async fn execute(
        &self,
        code: String,
        reset: bool,
        timeout: f64,
        request_cancel: CancellationToken,
    ) -> Value {
        let id = uuid::Uuid::new_v4().to_string();
        let cancel = CancellationToken::new();
        let cleanup_deadline = tokio::time::Instant::now() + Duration::from_millis(3500);
        loop {
            let available = self.available.notified();
            tokio::pin!(available);
            available.as_mut().enable();
            if self.reserve(&id, cancel.clone(), request_cancel.clone()) {
                break;
            }
            let cancelling = self.active.lock().unwrap().as_ref().is_some_and(|active| {
                active.cancel.is_cancelled() || active.request_cancel.is_cancelled()
            });
            if cancelling
                && tokio::time::timeout_at(cleanup_deadline, available)
                    .await
                    .is_ok()
            {
                continue;
            }
            return failure(
                &id,
                "A cell is already running; inspect python_run or cancel it",
                "preserved",
                Instant::now(),
            );
        }
        let _reservation = Reservation {
            owner: self,
            id: id.clone(),
            started: Instant::now(),
        };
        let result = self
            .execute_inner(id, code, reset, timeout, request_cancel, cancel)
            .await;
        *self.active.lock().unwrap() = None;
        self.remember(result.clone());
        result
    }

    pub fn start(self: &Arc<Self>, code: String, reset: bool, timeout: f64) -> Value {
        let id = uuid::Uuid::new_v4().to_string();
        if code.len() > CODE_LIMIT || !timeout.is_finite() || !(0.01..=3600.0).contains(&timeout) {
            return json!({"error":"Code limit is 256 KiB; timeout must be 0.01..3600 seconds"});
        }
        let cancel = CancellationToken::new();
        if !self.reserve(&id, cancel.clone(), CancellationToken::new()) {
            return json!({"error":"A cell is already running; inspect python_run or cancel it"});
        }
        let owner = self.clone();
        let run_id = id.clone();
        let task = tokio::spawn(async move {
            let _reservation = Reservation {
                owner: &owner,
                id: run_id.clone(),
                started: Instant::now(),
            };
            let result = owner
                .execute_inner(
                    run_id,
                    code,
                    reset,
                    timeout,
                    CancellationToken::new(),
                    cancel,
                )
                .await;
            *owner.active.lock().unwrap() = None;
            owner.remember(result);
        });
        *self.background.lock().unwrap() = Some(task);
        json!({"run_id":id,"session_id":self.session_id,"status":"starting","poll_tool":"python_run","cancel_tool":"python_cancel"})
    }

    async fn execute_inner(
        &self,
        id: String,
        code: String,
        reset: bool,
        timeout: f64,
        request_cancel: CancellationToken,
        cancel: CancellationToken,
    ) -> Value {
        let started = Instant::now();
        if cancel.is_cancelled() || request_cancel.is_cancelled() {
            return failure(
                &id,
                "Execution cancelled before dispatch",
                "preserved",
                started,
            );
        }
        if code.len() > CODE_LIMIT || !timeout.is_finite() || !(0.01..=3600.0).contains(&timeout) {
            return failure(
                &id,
                "Code limit is 256 KiB; timeout must be 0.01..3600 seconds",
                "preserved",
                started,
            );
        }
        let Ok(mut slot) = self.worker.try_lock() else {
            return failure(
                &id,
                "A cell is already running; inspect python_run or cancel it",
                "preserved",
                started,
            );
        };
        if reset {
            Self::stop(&mut slot).await;
        }
        if let Some(worker) = slot.as_mut()
            && !worker.live.load(Ordering::Acquire)
        {
            Self::stop(&mut slot).await;
        }
        let cleared = slot.is_none();
        if cleared {
            match self.spawn().await {
                Ok(worker) => *slot = Some(worker),
                Err(error) => return failure(&id, &error, "cleared", started),
            }
        }
        // Own the child locally throughout execution. If the handler future is
        // dropped, Worker::drop kills/aborts the job and Tokio reaps its Child.
        let mut worker = slot.take().unwrap();
        worker.raw_stderr.lock().unwrap().clear();
        let output = Arc::new(StdMutex::new([String::new(), String::new()]));
        *self.active.lock().unwrap() = Some(Active {
            id: id.clone(),
            cancel: cancel.clone(),
            request_cancel: request_cancel.clone(),
            started,
            output: output.clone(),
            raw_stderr: worker.raw_stderr.clone(),
            status: "running",
        });
        *worker.run.lock().unwrap() = Some((id.clone(), cancel.clone()));
        let mut guard = ExecutionGuard {
            pid: worker.pid,
            armed: true,
            owner: self,
            id: id.clone(),
            started,
        };
        if cancel.is_cancelled() || request_cancel.is_cancelled() {
            guard.armed = false;
            *slot = Some(worker);
            return failure(
                &id,
                "Execution cancelled before dispatch",
                "preserved",
                started,
            );
        }
        let command = json!({"type":"execute","id":id,"code":code,"reset":false,"timeout":timeout});
        let sent = send(&worker.stdin, &command).await;
        let deadline = tokio::time::sleep(Duration::from_secs_f64(timeout));
        tokio::pin!(deadline);
        let mut interruption = None;
        let mut memory_poll = tokio::time::interval(Duration::from_millis(250));
        let mut result = if let Err(error) = sent {
            failure(&id, &error, "cleared", started)
        } else {
            loop {
                tokio::select! {
                    _ = request_cancel.cancelled() => { interruption = Some("Execution cancelled"); break Value::Null; }
                    _ = cancel.cancelled() => { interruption = Some("Execution cancelled"); break Value::Null; }
                    _ = &mut deadline => { interruption = Some("Execution timed out"); break Value::Null; }
                    _ = memory_poll.tick(), if self.max_memory_mib > 0 => {
                        if worker_rss(worker.rss_pid).is_some_and(|rss| rss > self.max_memory_mib.saturating_mul(1024 * 1024)) {
                            break failure(&id, "Python worker exceeded RSS budget; variables cleared", "cleared", started);
                        }
                    }
                    event = worker.events.recv() => {
                        match event {
                            Some(event) if event["type"] == "result" && event["id"] == id => break event["result"].clone(),
                            Some(event) => collect_output(&event, &mut output.lock().unwrap()),
                            None => break failure(&id, "Python worker crashed; variables cleared", "cleared", started),
                        }
                    }
                }
            }
        };
        if let Some(reason) = interruption {
            cancel.cancel();
            for task in worker.rpc.lock().unwrap().drain(..) {
                task.abort();
            }
            for pid in worker.jobs.lock().unwrap().drain() {
                signal_group(pid, libc::SIGKILL);
            }
            signal_group(worker.pid, libc::SIGINT);
            let grace = tokio::time::sleep(Duration::from_secs(2));
            tokio::pin!(grace);
            result = loop {
                tokio::select! {
                    _ = &mut grace => break failure(&id, &format!("{reason}; worker did not stop; variables cleared"), "cleared", started),
                    event = worker.events.recv() => match event {
                        Some(event) if event["type"] == "result" && event["id"] == id => {
                            let mut result = event["result"].clone();
                            result["success"] = json!(false); result["error"] = json!(reason); break result;
                        }
                        Some(event) => collect_output(&event, &mut output.lock().unwrap()),
                        None => break failure(&id, &format!("{reason}; worker exited; variables cleared"), "cleared", started),
                    }
                }
            };
        }
        for task in worker.rpc.lock().unwrap().drain(..) {
            task.abort();
        }
        *worker.run.lock().unwrap() = None;
        let (ack, receipt) = oneshot::channel();
        let _ = tokio::time::timeout(Duration::from_secs(1), async {
            if worker.stderr_sync.send(ack).await.is_ok() {
                let _ = receipt.await;
            }
        })
        .await;
        let raw_stderr = String::from_utf8_lossy(&worker.raw_stderr.lock().unwrap()).into_owned();
        if result["stderr"].as_str().unwrap_or("").is_empty() {
            result["stderr"] = json!(output.lock().unwrap()[1]);
        }
        if !raw_stderr.is_empty() {
            let previous = result["stderr"].as_str().unwrap_or("");
            result["stderr"] = json!(format!("{previous}{raw_stderr}"));
            if worker.raw_stderr.lock().unwrap().len() >= 65536 {
                result["truncated"]["stderr"] = json!(true);
            }
        }
        guard.armed = false;
        if result["state"] == "cleared" {
            worker.terminate().await;
        } else {
            *slot = Some(worker);
        }
        result["state_reset"] = json!(cleared);
        result["run_id"] = json!(id);
        result["elapsed_ms"] = json!(started.elapsed().as_secs_f64() * 1000.0);
        {
            let output = output.lock().unwrap();
            if result["stdout"].as_str().unwrap_or("").is_empty() {
                result["stdout"] = json!(output[0]);
            }
            if result["stderr"].as_str().unwrap_or("").is_empty() {
                result["stderr"] = json!(output[1]);
            }
        }
        bound_execution_output(&mut result);
        *self.active.lock().unwrap() = None;
        self.remember(result.clone());
        result
    }

    async fn stop(slot: &mut Option<Worker>) {
        if let Some(mut worker) = slot.take() {
            worker.terminate().await;
        }
    }

    pub async fn shutdown(&self) {
        if let Some(active) = self.active.lock().unwrap().as_ref() {
            active.cancel.cancel();
        }
        let background = self.background.lock().unwrap().take();
        if let Some(mut task) = background
            && tokio::time::timeout(Duration::from_secs(5), &mut task)
                .await
                .is_err()
        {
            task.abort();
            let _ = task.await;
        }
        let mut slot = self.worker.lock().await;
        Self::stop(&mut slot).await;
    }

    pub fn cancel(&self, id: Option<&str>) -> Value {
        let active = self.active.lock().unwrap();
        if let Some(active) = active
            .as_ref()
            .filter(|active| id.is_none_or(|id| id == active.id))
        {
            active.cancel.cancel();
            json!({"cancel_requested":true,"run_id":active.id,"external_effects":"May already have occurred; never retried automatically"})
        } else {
            json!({"cancel_requested":false,"error":"No matching active run"})
        }
    }

    pub fn run(&self, id: Option<&str>) -> Value {
        let active = self.active.lock().unwrap().clone();
        if let Some(active) = active.as_ref().filter(|a| id.is_none_or(|id| id == a.id)) {
            let output = active.output.lock().unwrap();
            let raw = active.raw_stderr.lock().unwrap();
            return json!({"run_id":active.id,"status":active.status,"elapsed_ms":active.started.elapsed().as_secs_f64()*1000.0,"stdout":output[0],"stderr":output[1],"native_stderr":String::from_utf8_lossy(&raw),"output_limit_bytes_per_stream":65536});
        }
        let records = self.records.lock().unwrap();
        records
            .iter()
            .rev()
            .find(|r| id.is_none_or(|id| r["run_id"] == id))
            .cloned()
            .unwrap_or(json!({"error":"Run not found; history retains last 64 runs"}))
    }

    pub fn health(&self) -> Value {
        let mut runtime = self.runtime.lock().unwrap().clone();
        if let Some(runtime) = runtime.as_mut() {
            runtime["running"] = json!(
                self.live
                    .lock()
                    .unwrap()
                    .as_ref()
                    .is_some_and(|live| live.load(Ordering::Acquire))
            );
        }
        json!({"session_id":self.session_id,"generation":self.generation.load(Ordering::Relaxed),"version":env!("CARGO_PKG_VERSION"),"runtime":"rust-supervisor/cpython-worker","execution_mode":"trusted-local; full filesystem access; not a security sandbox","python_executable":self.python.to_string_lossy(),"python":runtime,"active_run":self.active.lock().unwrap().as_ref().map(|a| a.id.clone()),"limits":{"code_bytes":CODE_LIMIT,"frame_bytes":FRAME_LIMIT,"timeout_seconds":3600,"history_runs":HISTORY_LIMIT,"rpc_concurrency":64,"worker_rss_mib":self.max_memory_mib},"memory_limit":"Worker RSS sampled every 250ms during execution; descendants excluded; zero disables"})
    }

    pub fn session_id(&self) -> &str {
        &self.session_id
    }
    pub fn generation(&self) -> u64 {
        self.generation.load(Ordering::Relaxed)
    }
}

fn collect_output(event: &Value, output: &mut [String; 2]) {
    if event["type"] != "output" {
        return;
    }
    let index = usize::from(event["stream"] == "stderr");
    if let Some(text) = event["text"].as_str() {
        let remaining = 65536usize.saturating_sub(output[index].len());
        let mut boundary = text.len().min(remaining);
        while !text.is_char_boundary(boundary) {
            boundary -= 1;
        }
        output[index].push_str(&text[..boundary]);
    }
}

fn failure(id: &str, error: &str, state: &str, started: Instant) -> Value {
    json!({"run_id":id,"success":false,"stdout":"","stderr":"","return_value":null,"error":error,"elapsed_ms":started.elapsed().as_secs_f64()*1000.0,"truncated":{"stdout":false,"stderr":false,"return":false},"state":state})
}

fn bound_execution_output(result: &mut Value) {
    for field in ["stdout", "stderr", "return_value", "error"] {
        let budget = if field == "stdout" || field == "stderr" {
            50_000
        } else {
            20_000
        };
        if let Some(text) = result[field].as_str().filter(|text| text.len() > budget) {
            let mut boundary = budget;
            while !text.is_char_boundary(boundary) {
                boundary -= 1;
            }
            result[field] = json!(&text[..boundary]);
            let flag = if field == "return_value" {
                "return"
            } else {
                field
            };
            result["truncated"][flag] = json!(true);
        }
    }
}

#[cfg(target_os = "macos")]
fn worker_rss(pid: u32) -> Option<u64> {
    #[repr(C)]
    #[derive(Default)]
    struct TaskInfo {
        virtual_size: u64,
        resident_size: u64,
        times: [u64; 4],
        counters: [i32; 12],
    }
    unsafe extern "C" {
        fn proc_pidinfo(
            pid: i32,
            flavor: i32,
            arg: u64,
            buffer: *mut libc::c_void,
            size: i32,
        ) -> i32;
    }
    let mut info = TaskInfo::default();
    let size = std::mem::size_of::<TaskInfo>();
    let count = unsafe {
        proc_pidinfo(
            pid as i32,
            4,
            0,
            (&mut info as *mut TaskInfo).cast(),
            size as i32,
        )
    };
    (count as usize == size).then_some(info.resident_size)
}

#[cfg(target_os = "linux")]
fn worker_rss(pid: u32) -> Option<u64> {
    let statm = std::fs::read_to_string(format!("/proc/{pid}/statm")).ok()?;
    let resident = statm.split_whitespace().nth(1)?.parse::<u64>().ok()?;
    let page_size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
    (page_size > 0).then(|| resident.saturating_mul(page_size as u64))
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("repl-mcp native lifecycle currently supports Linux and macOS only");

/// Independent parent-death watcher; does not execute Python or require its GIL.
pub fn spawn_guardian(pid: u32) -> Result<Child, String> {
    let mut command =
        Command::new(std::env::current_exe().map_err(|_| "Cannot locate native guardian")?);
    #[cfg(not(test))]
    command.args(["--internal-guardian", &pid.to_string()]);
    #[cfg(test)]
    command
        .args([
            "--exact",
            "supervisor::tests::guardian_child_entry",
            "--ignored",
            "--nocapture",
            "--format",
            "terse",
        ])
        .env("REPL_TEST_GUARDIAN_PID", pid.to_string());
    command
        .stdin(std::process::Stdio::piped())
        .stdout(if pid == 0 {
            std::process::Stdio::piped()
        } else {
            std::process::Stdio::null()
        })
        .stderr(if pid == 0 {
            std::process::Stdio::piped()
        } else {
            std::process::Stdio::null()
        })
        .kill_on_drop(true);
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    command
        .spawn()
        .map_err(|_| "Cannot start native process guardian".into())
}

/// Independent parent-death watcher; does not execute Python or require its GIL.
pub fn guardian(worker_pid: u32) -> anyhow::Result<()> {
    if worker_pid != 0 {
        anyhow::bail!("Unsupported internal guardian role");
    }
    crate::guardian::run()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "subprocess entrypoint used only by guardian ownership tests"]
    fn guardian_child_entry() {
        let pid = std::env::var("REPL_TEST_GUARDIAN_PID")
            .unwrap()
            .parse()
            .unwrap();
        guardian(pid).unwrap();
    }

    fn supervisor() -> Arc<Supervisor> {
        Arc::new(Supervisor::new(
            PathBuf::from("python3"),
            Arc::new(Broker::from_config(None, "none".into()).unwrap()),
            2048,
        ))
    }

    async fn executing_worker(server: &Supervisor, id: Option<&str>, output: &str) -> i32 {
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let progress = server.run(id);
                let pid = server
                    .runtime
                    .lock()
                    .unwrap()
                    .as_ref()
                    .and_then(|runtime| runtime["pid"].as_u64());
                if progress["stdout"]
                    .as_str()
                    .is_some_and(|text| text.contains(output))
                    && let Some(pid) = pid
                {
                    return i32::try_from(pid).expect("Worker PID must fit pid_t");
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .unwrap_or_else(|_| {
            panic!(
                "Cell never emitted {output:?} with a ready worker within10s: progress={}, health={}",
                server.run(id),
                server.health()
            )
        })
    }

    async fn worker_reaped(pid: i32) {
        tokio::time::timeout(Duration::from_secs(10), async {
            while unsafe { libc::kill(pid, 0) } == 0 {
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("Worker {pid} should be dead and reaped within10s"));
    }

    #[test]
    fn native_rss_monitor_reports_real_memory() {
        assert!(worker_rss(std::process::id()).unwrap() > 0);
    }

    #[tokio::test]
    async fn idle_health_does_not_spawn_python() {
        let server = supervisor();
        assert!(server.health()["python"].is_null());
        assert!(server.worker.lock().await.is_none());
        server.shutdown().await;
    }

    #[tokio::test]
    async fn accepted_large_frame_survives_caller_cancellation_without_corrupting_next_line() {
        use tokio::io::{AsyncBufReadExt, BufReader};
        let mut child = Command::new("/bin/cat")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let (writer, mut actor) = frame_writer(child.stdin.take().unwrap(), child.id().unwrap());
        let sender = writer.clone();
        let expected = "🦀".repeat(100_000);
        let payload = json!({"value":expected});
        let caller = tokio::spawn(async move { send(&sender, &payload).await });
        // cat's full stdout backpressures stdin until the parent starts draining.
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert!(!caller.is_finished());
        caller.abort();
        let _ = caller.await;
        let mut reader = BufReader::new(child.stdout.take().unwrap());
        let mut line = String::new();
        tokio::time::timeout(Duration::from_secs(3), reader.read_line(&mut line))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&line).unwrap()["value"],
            expected
        );
        send(&writer, &json!({"next":42})).await.unwrap();
        line.clear();
        reader.read_line(&mut line).await.unwrap();
        assert_eq!(serde_json::from_str::<Value>(&line).unwrap()["next"], 42);
        writer.stop.cancel();
        tokio::time::timeout(Duration::from_secs(1), &mut actor)
            .await
            .unwrap()
            .unwrap();
        child.wait().await.unwrap();
    }

    #[tokio::test]
    async fn reset_restarts_environment_and_persistent_state() {
        let server = supervisor();
        let result = server
            .execute(
                "import os; os.environ['REPL_TEST_RESET']='changed'; value=19".into(),
                false,
                5.0,
                CancellationToken::new(),
            )
            .await;
        assert_eq!(result["success"], true);
        let result = server
            .execute(
                "('value' in globals(), os.environ.get('REPL_TEST_RESET'))"
                    .replace("os.environ", "__import__('os').environ"),
                true,
                5.0,
                CancellationToken::new(),
            )
            .await;
        assert_eq!(result["success"], true);
        assert_eq!(result["value"], json!([false, null]));
        server.shutdown().await;
    }

    #[tokio::test]
    async fn abandoning_handler_kills_worker_and_reaps_before_next_cell() {
        let server = supervisor();
        let executor = server.clone();
        let task = tokio::spawn(async move {
            executor
                .execute(
                    "import time; print('started', flush=True); time.sleep(20); late_effect=True"
                        .into(),
                    false,
                    30.0,
                    CancellationToken::new(),
                )
                .await
        });
        let pid = executing_worker(&server, None, "started\n").await;
        task.abort();
        let _ = task.await;
        worker_reaped(pid).await;
        assert_ne!(
            unsafe { libc::kill(pid, 0) },
            0,
            "Worker should be dead and reaped without another cell"
        );
        assert!(server.health()["active_run"].is_null());
        let result = server
            .execute(
                "'late_effect' in globals()".into(),
                false,
                5.0,
                CancellationToken::new(),
            )
            .await;
        assert_eq!(result["value"], false);
        server.shutdown().await;
    }

    #[tokio::test]
    async fn low_rss_budget_stops_overallocation() {
        let server = Supervisor::new(
            PathBuf::from("python3"),
            Arc::new(Broker::from_config(None, "none".into()).unwrap()),
            48,
        );
        let result = server
            .execute(
                "import time; big=bytearray(128*1024*1024); time.sleep(2)".into(),
                false,
                5.0,
                CancellationToken::new(),
            )
            .await;
        assert_eq!(result["success"], false);
        assert!(result["error"].as_str().unwrap().contains("RSS budget"));
        assert!(server.worker.lock().await.is_none());
        server.shutdown().await;
    }

    #[tokio::test]
    async fn background_start_polls_live_output_cancels_and_owns_shutdown() {
        let server = supervisor();
        let started = server.start(
            "import time; print('live', flush=True); time.sleep(20); late_effect=True".into(),
            false,
            30.0,
        );
        let id = started["run_id"].as_str().unwrap();
        assert!(server.start("1".into(), false, 5.0).get("error").is_some());
        executing_worker(&server, Some(id), "live\n").await;
        assert_eq!(server.cancel(Some(id))["cancel_requested"], true);
        tokio::time::timeout(Duration::from_secs(10), async {
            while server.run(Some(id)).get("success").is_none() {
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .unwrap_or_else(|_| {
            panic!(
                "Cancelled run never completed within10s: {}",
                server.run(Some(id))
            )
        });
        assert_eq!(server.run(Some(id))["success"], false);
        let result = server
            .execute(
                "'late_effect' in globals()".into(),
                false,
                5.0,
                CancellationToken::new(),
            )
            .await;
        assert_eq!(result["value"], false);
        let next = server.start(
            "import time; print('shutdown-ready', flush=True); time.sleep(20)".into(),
            false,
            30.0,
        );
        assert!(next["run_id"].is_string());
        let pid = executing_worker(&server, next["run_id"].as_str(), "shutdown-ready\n").await;
        tokio::time::timeout(Duration::from_secs(10), server.shutdown())
            .await
            .expect("Owned background task shutdown must complete within10s");
        worker_reaped(pid).await;
        assert!(!server.busy.load(Ordering::Acquire));
        assert!(server.worker.lock().await.is_none());
    }
}
