mod artifacts;
mod broker;
mod config;
mod guardian;
mod server;
mod sessions;
mod supervisor;

use clap::Parser;
use futures_util::StreamExt;
use rmcp::ServiceExt;
use std::{path::PathBuf, sync::Arc};
use tokio_util::codec::{FramedRead, FramedWrite};

#[derive(Parser)]
#[command(version, about)]
struct Options {
    #[arg(long, hide = true)]
    internal_guardian: Option<u32>,
    #[arg(long, default_value = "stdio", value_parser = ["stdio"])]
    transport: String,
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long, default_value = "project")]
    mcp_scope: String,
    #[arg(long)]
    no_autoconnect: bool,
    #[arg(long, env = "REPL_MCP_PYTHON")]
    python: Option<PathBuf>,
    /// Worker RSS ceiling in MiB; zero disables. This is a resource budget, not isolation.
    #[arg(long, default_value_t = 2048)]
    max_memory_mib: u64,
    /// Authorize one explicitly configured OAuth server with a loopback PKCE flow.
    #[arg(long, value_name = "SERVER")]
    oauth_login: Option<String>,
    /// Persist redacted external-effect metadata in a private journal.
    #[arg(long)]
    journal: Option<PathBuf>,
}

fn python_executable(explicit: Option<PathBuf>) -> PathBuf {
    if let Some(path) = explicit {
        return path;
    }
    if let Ok(executable) = std::env::current_exe()
        && let Some(directory) = executable.parent()
    {
        for name in ["python", "python3"] {
            let candidate = directory.join(name);
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    PathBuf::from("python3")
}

fn main() -> anyhow::Result<()> {
    let options = Options::parse();
    if let Some(pid) = options.internal_guardian {
        return supervisor::guardian(pid);
    }
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()?;
    let result = runtime.block_on(run(options));
    // stdin and optional filesystem jobs can be blocked in the OS. They must
    // not keep a terminated stdio server alive indefinitely.
    runtime.shutdown_timeout(std::time::Duration::from_secs(1));
    result
}

async fn run(options: Options) -> anyhow::Result<()> {
    // Kept for older launchers; all connections are now lazy regardless.
    let scope = options.mcp_scope;
    let config = options.config.clone();
    let broker = Arc::new(
        broker::Broker::from_config(options.config, scope.clone()).map_err(anyhow::Error::msg)?,
    );
    if let Some(path) = options.journal {
        broker.set_journal(path).map_err(anyhow::Error::msg)?;
    }
    if let Some(server) = options.oauth_login {
        broker
            .oauth_login(&server)
            .await
            .map_err(anyhow::Error::msg)?;
        return Ok(());
    }
    let sessions = Arc::new(
        sessions::SessionManager::new(
            python_executable(options.python),
            broker.clone(),
            options.max_memory_mib,
            config,
            scope,
        )
        .map_err(anyhow::Error::msg)?,
    );
    let server = server::ReplServer::new(sessions.clone());
    let input = FramedRead::new(
        tokio::io::stdin(),
        rmcp::transport::async_rw::JsonRpcMessageCodec::<
            rmcp::service::RxJsonRpcMessage<rmcp::RoleServer>,
        >::new_with_max_length(1_048_576),
    )
    .take_while(|message| std::future::ready(message.is_ok()))
    .map(Result::unwrap);
    let output = FramedWrite::new(
        tokio::io::stdout(),
        rmcp::transport::async_rw::JsonRpcMessageCodec::<
            rmcp::service::TxJsonRpcMessage<rmcp::RoleServer>,
        >::new_with_max_length(1_048_576),
    );
    let service = server.serve((output, input)).await.map_err(|_| {
        anyhow::anyhow!("MCP initialization failed; check client protocol and framing")
    })?;
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        result = service.waiting() => { if result.is_err() { eprintln!("MCP transport ended with an error"); } }
        _ = tokio::signal::ctrl_c() => {}
        _ = terminate.recv() => {}
    }
    sessions.shutdown().await;
    broker.shutdown().await;
    Ok(())
}
