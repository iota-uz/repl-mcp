//! Spawn ownership independent of Python, client cancellation and pipe backpressure.
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, HashSet},
    io::Write,
    os::fd::AsRawFd,
    path::PathBuf,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, Command},
};
use tokio_util::{
    codec::{FramedRead, LinesCodec},
    sync::CancellationToken,
};

#[derive(Serialize, Deserialize)]
pub struct CommandSpec {
    pub program: String,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub cwd: Option<PathBuf>,
    pub worker: bool,
}

struct LaunchGuard(u32, bool);
impl Drop for LaunchGuard {
    fn drop(&mut self) {
        if self.1 {
            unsafe {
                libc::kill(-(self.0 as i32), libc::SIGKILL);
            }
        }
    }
}

pub async fn spawn_owned(spec: CommandSpec) -> Result<Child, String> {
    let bytes = serde_json::to_vec(&spec).map_err(|_| "Cannot encode private process command")?;
    if bytes.len() > 1_048_576 {
        return Err("Private process command exceeds 1 MiB".into());
    }
    let mut child = crate::supervisor::spawn_guardian(0)?;
    let mut guard = LaunchGuard(child.id().ok_or("Guardian has no PID")?, true);
    let stdin = child.stdin.as_mut().ok_or("Guardian input missing")?;
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        stdin.write_all(&bytes).await?;
        stdin.write_all(b"\n").await?;
        stdin.flush().await
    })
    .await
    .map_err(|_| "Private process command write timed out")?
    .map_err(|_| "Guardian process command pipe closed")?;
    guard.1 = false;
    Ok(child)
}

pub fn run() -> anyhow::Result<()> {
    // The guardian alone ignores interrupts; its child restores the default.
    unsafe {
        libc::signal(libc::SIGINT, libc::SIG_IGN);
    }
    owner()
}

#[tokio::main(flavor = "current_thread")]
async fn owner() -> anyhow::Result<()> {
    let stop = CancellationToken::new();
    let watcher_stop = stop.clone();
    let (watcher_control, mut control) = std::os::unix::net::UnixStream::pair()?;
    // events=0 watches HUP even while unread data remains or forwarding blocks.
    let watcher = std::thread::spawn(move || {
        let mut fds = [
            libc::pollfd {
                fd: 0,
                events: 0,
                revents: 0,
            },
            libc::pollfd {
                fd: watcher_control.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        loop {
            if unsafe { libc::poll(fds.as_mut_ptr(), 2, -1) } < 0 {
                if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                watcher_stop.cancel();
                break;
            }
            if fds[0].revents != 0 {
                watcher_stop.cancel();
                break;
            }
            if fds[1].revents != 0 {
                break;
            }
        }
    });
    let mut input = BufReader::new(tokio::io::stdin()).take(1_048_577);
    let mut header = Vec::new();
    let count = tokio::select! { biased; _ = stop.cancelled() => 0, read = input.read_until(b'\n', &mut header) => read? };
    if count == 0 || count > 1_048_576 || header.last() != Some(&b'\n') {
        let _ = control.write_all(b"x");
        let _ = watcher.join();
        anyhow::bail!("Guardian command missing or oversized");
    }
    let spec: CommandSpec = serde_json::from_slice(&header)
        .map_err(|_| anyhow::anyhow!("Invalid private guardian command"))?;
    if stop.is_cancelled() {
        return Ok(());
    }
    let mut command = Command::new(spec.program);
    command
        .args(spec.args)
        .envs(spec.env)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(if spec.worker {
            std::process::Stdio::piped()
        } else {
            std::process::Stdio::null()
        })
        .kill_on_drop(true);
    if let Some(cwd) = spec.cwd {
        command.current_dir(cwd);
    }
    unsafe {
        command.pre_exec(|| {
            libc::signal(libc::SIGINT, libc::SIG_DFL);
            Ok(())
        });
    }
    let mut child = command
        .spawn()
        .map_err(|_| anyhow::anyhow!("Managed process could not start"))?;
    let mut child_input = child.stdin.take().unwrap();
    let child_output = child.stdout.take().unwrap();
    let parent_input = input.into_inner();
    let input_stop = stop.clone();
    let input_task = tokio::spawn(async move {
        let mut frames = FramedRead::new(parent_input, LinesCodec::new_with_max_length(1_048_576));
        while let Some(Ok(frame)) = frames.next().await {
            if child_input.write_all(frame.as_bytes()).await.is_err()
                || child_input.write_all(b"\n").await.is_err()
            {
                break;
            }
        }
        input_stop.cancel();
    });
    let jobs = std::sync::Arc::new(std::sync::Mutex::new(HashSet::<u32>::new()));
    let output_jobs = jobs.clone();
    let output_stop = stop.clone();
    let mut output_task = tokio::spawn(async move {
        let mut frames = FramedRead::new(child_output, LinesCodec::new_with_max_length(1_048_576));
        let mut output = tokio::io::stdout();
        while let Some(Ok(frame)) = frames.next().await {
            if spec.worker
                && let Ok(event) = serde_json::from_str::<Value>(&frame)
                && event["type"] == "job"
                && let Some(pid) = event["pid"]
                    .as_u64()
                    .and_then(|v| u32::try_from(v).ok())
                    .filter(|pid| *pid > 1)
            {
                let mut jobs = output_jobs.lock().unwrap();
                if event["action"] == "started" {
                    jobs.insert(pid);
                } else {
                    jobs.remove(&pid);
                }
            }
            if output.write_all(frame.as_bytes()).await.is_err()
                || output.write_all(b"\n").await.is_err()
                || output.flush().await.is_err()
            {
                break;
            }
        }
        output_stop.cancel();
    });
    let stderr_task = child.stderr.take().map(|mut stderr| {
        tokio::spawn(async move {
            let _ = tokio::io::copy(&mut stderr, &mut tokio::io::stderr()).await;
        })
    });
    tokio::select! { biased; _ = stop.cancelled() => { let _ = child.kill().await; }, _ = child.wait() => {} }
    for pid in jobs.lock().unwrap().drain() {
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
    }
    input_task.abort();
    if tokio::time::timeout(std::time::Duration::from_millis(200), &mut output_task)
        .await
        .is_err()
    {
        output_task.abort();
    }
    if let Some(mut task) = stderr_task
        && tokio::time::timeout(std::time::Duration::from_millis(200), &mut task)
            .await
            .is_err()
    {
        task.abort();
    }
    let _ = control.write_all(b"x");
    let _ = watcher.join();
    // Our own PGID stays allocated until the parent reaps this guardian. The
    // direct child was reaped above; this also terminates ordinary descendants.
    unsafe {
        libc::kill(-libc::getpid(), libc::SIGKILL);
    }
    Ok(())
}
