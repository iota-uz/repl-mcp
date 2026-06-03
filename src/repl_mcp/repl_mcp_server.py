"""FastMCP server exposing stateful Python REPL."""

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import RuntimeConfig, ServerConfig as StartupConfig
from .daytona_adapter import DaytonaAdapterError, DaytonaSandboxAdapter
from .mcp_client_wrapper import MCPClientWrapper
from .repl_engine import REPLEngine
from .session_manager import ChatSessionManager

# Global instances
mcp_wrapper: Optional[MCPClientWrapper] = None
repl_engine: Optional[REPLEngine] = None
sandbox_adapter: Optional[DaytonaSandboxAdapter] = None
session_manager: Optional[ChatSessionManager] = None


def setup_logging(transport: str, debug: bool = False) -> logging.Logger:
    """
    Configure logging to avoid stdout pollution.

    Args:
        transport: Transport type ('stdio' or 'sse')
        debug: Enable debug logging (still logs to stderr)

    Returns:
        Logger instance
    """
    # Stdout must be kept clean for the stdio transport (it carries JSON-RPC).
    # Logging to stderr is also safe for SSE mode.
    handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, handlers=[handler], force=True)
    return logging.getLogger(__name__)


def load_mcp_config(config_path: Path = Path(".mcp.json")) -> dict:
    """
    Load MCP server configuration from .mcp.json file.

    Args:
        config_path: Path to .mcp.json file

    Returns:
        Dict of server configurations, or empty dict if file doesn't exist
    """
    if not config_path.exists():
        return {}

    try:
        config = json.loads(config_path.read_text())
        servers = config.get("mcpServers", {})
        return servers
    except Exception as e:
        logging.warning(f"Failed to load {config_path}: {e}")
        return {}


def filter_servers(servers: dict, exclude: list[str] = None) -> dict:
    """
    Filter out servers by name.

    Args:
        servers: Dict of server configurations
        exclude: List of server names to exclude

    Returns:
        Filtered dict of servers
    """
    if exclude is None:
        exclude = []

    return {
        name: config
        for name, config in servers.items()
        if name not in exclude
    }


def _resolve_chat_session_id(ctx: Context, header_name: str) -> str:
    """
    Resolve chat session identity from MCP/HTTP context.

    Precedence:
      1) MCP session_id
      2) request meta (chat_session_id/session_id/conversation_id)
      3) configured HTTP header (default x-chat-session-id) and common aliases
      4) client_id
      5) auth token hash fallback
      6) default-chat
    """
    if getattr(ctx, "session_id", None):
        return str(ctx.session_id)

    request_context = getattr(ctx, "request_context", None)
    meta = getattr(request_context, "meta", None)
    if isinstance(meta, dict):
        for key in ("chat_session_id", "session_id", "conversation_id"):
            value = meta.get(key)
            if value:
                return str(value)

    headers = get_http_headers(include_all=True)
    normalized_headers = {k.lower(): v for k, v in headers.items()}
    candidates = [header_name.lower(), "x-chat-session-id", "x-session-id", "x-session"]
    for name in candidates:
        value = normalized_headers.get(name)
        if value:
            return str(value)

    if getattr(ctx, "client_id", None):
        return str(ctx.client_id)

    auth_header = normalized_headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
            return f"auth-{digest}"

    return "default-chat"


def _decode_upload_payload(
    *,
    content: str,
    is_base64: bool,
    encoding: str,
    max_upload_bytes: int,
) -> tuple[Optional[bytes], Optional[str]]:
    """Decode upload content and return (payload, error_message)."""
    try:
        if is_base64:
            payload = base64.b64decode(content, validate=True)
        else:
            payload = content.encode(encoding)
    except (binascii.Error, ValueError) as exc:
        return None, f"invalid content encoding ({exc})"
    except UnicodeEncodeError as exc:
        return None, f"unable to encode content ({exc})"

    if len(payload) > max_upload_bytes:
        return (
            None,
            "file exceeds size limit "
            f"({len(payload)} bytes > {max_upload_bytes} bytes)",
        )

    return payload, None


async def _persist_upload_payload(
    *,
    path: str,
    payload: bytes,
    session_id: str,
    runtime_config: RuntimeConfig,
) -> tuple[bool, str]:
    """Persist upload payload to Daytona/local workspace."""
    if runtime_config.daytona_enabled:
        if not session_manager:
            return False, "session manager is not initialized"
        try:
            await asyncio.to_thread(session_manager.upload_bytes, session_id, path, payload)
            return True, f"Uploaded {len(payload)} bytes to {path}"
        except DaytonaAdapterError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, str(exc)

    if not repl_engine or not getattr(repl_engine, "_workspace", None):
        return False, "workspace is not available"

    try:
        repl_engine._workspace.write(path, payload)
    except Exception as exc:
        return False, str(exc)
    return True, f"Uploaded {len(payload)} bytes to {path}"


def create_server_lifespan(
    config_path: Path,
    autoconnect_enabled: bool,
    logger: logging.Logger,
    runtime_config: RuntimeConfig,
):
    """
    Factory to create a lifespan function with captured config.

    Args:
        config_path: Path to .mcp.json config file
        autoconnect_enabled: Whether to auto-connect to servers
        logger: Logger instance for output

    Returns:
        Async context manager for server lifespan
    """

    @asynccontextmanager
    async def server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
        """Lifespan context manager for the REPL MCP server."""
        global mcp_wrapper, repl_engine, sandbox_adapter, session_manager
        autoconnect_task = None
        session_sweeper_task = None

        # --- STARTUP ---
        logger.info("Initializing REPL MCP server...")

        # Initialize MCP wrapper
        mcp_wrapper = MCPClientWrapper()

        if runtime_config.daytona_enabled:
            logger.info("Daytona mode enabled: using chat-scoped sandbox sessions")
            sandbox_adapter = DaytonaSandboxAdapter(runtime_config)
            session_manager = ChatSessionManager(
                sandbox_adapter,
                idle_timeout_minutes=runtime_config.session_idle_timeout_minutes,
                idle_action=runtime_config.session_idle_action,
            )
            repl_engine = None
        else:
            # Initialize REPL engine with workspace root
            repl_engine = REPLEngine(
                mcp_wrapper=mcp_wrapper,
                workspace_root=Path.cwd(),
            )
            sandbox_adapter = None
            session_manager = None

        # Auto-connect to configured servers
        if autoconnect_enabled:
            servers = load_mcp_config(config_path)

            # Filter out self to prevent recursive connection
            servers = filter_servers(servers, exclude=["python-repl"])

            if servers:
                logger.info(f"Auto-connecting to {len(servers)} MCP servers (background)...")

                async def do_connect():
                    # Use sequential connects by default to reduce startup load.
                    results = await mcp_wrapper.connect_async(servers, timeout_s=20.0, sequential=True)
                    for name, success in results.items():
                        status = "✓" if success else "✗"
                        logger.info(f"  {status} {name}")

                autoconnect_task = asyncio.create_task(do_connect())

        if runtime_config.daytona_enabled and session_manager:
            async def sweep_idle_sessions() -> None:
                while True:
                    await asyncio.sleep(max(5, runtime_config.session_sweep_interval_seconds))
                    try:
                        stale = await asyncio.to_thread(session_manager.apply_idle_policy)
                        if stale:
                            logger.info(
                                "Applied idle policy '%s' to %d session(s)",
                                runtime_config.session_idle_action,
                                len(stale),
                            )
                    except Exception as exc:
                        logger.warning("Session idle sweeper error: %s", exc)

            session_sweeper_task = asyncio.create_task(sweep_idle_sessions())

        # Yield control - server runs here
        yield {}

        # --- SHUTDOWN ---
        if autoconnect_task and not autoconnect_task.done():
            autoconnect_task.cancel()
            with suppress(BaseException):
                await autoconnect_task
        if session_sweeper_task and not session_sweeper_task.done():
            session_sweeper_task.cancel()
            with suppress(BaseException):
                await session_sweeper_task
        try:
            logger.info("Shutting down REPL MCP server...")
        except ValueError:
            # File may be closed during stdio shutdown
            pass
        if mcp_wrapper:
            mcp_wrapper.disconnect()

    return server_lifespan


def create_server(
    config_path: Path = Path(".mcp.json"),
    autoconnect: bool = True,
    logger: logging.Logger = None,
    runtime_config: Optional[RuntimeConfig] = None,
) -> FastMCP:
    """
    Create and configure the FastMCP server.

    Args:
        config_path: Path to .mcp.json config file
        autoconnect: Whether to auto-connect to servers
        logger: Logger instance for output

    Returns:
        Configured FastMCP server instance
    """
    # Create lifespan
    if logger is None:
        logger = logging.getLogger(__name__)
    if runtime_config is None:
        runtime_config = RuntimeConfig.from_env()
    lifespan = create_server_lifespan(config_path, autoconnect, logger, runtime_config)

    # Create server with lifespan
    mcp_server = FastMCP("python-repl", lifespan=lifespan)

    # Register tools
    #
    # NOTE: No return type annotation on execute_python intentionally!
    # Adding `-> str` causes FastMCP to wrap output in {"result": "..."} JSON
    # via structuredContent. We want plain text output for better readability.
    @mcp_server.tool()
    async def execute_python(
        code: str,
        reset: bool = False,
        timeout: float = runtime_config.exec_timeout_seconds,
        inject: Optional[dict] = None,
        ctx: Context = CurrentContext(),
    ):
        """
        Persistent Python REPL — use this instead of `python3 -c`, heredocs,
        or `cmd | python3` via Bash.

        State (variables, imports, functions) survives across calls: a warm
        call takes ~0.1s vs ~3s for each fresh `python3` Bash spawn. Full
        filesystem access — open(), absolute paths, and ~ all work.

        Pre-injected utilities:
          sh         - Shell commands: json.loads(sh("gh pr view 1 --json title"))
                       Returns stdout str with .returncode/.stderr/.ok
          workspace  - File ops: .read(), .write(), .glob() (absolute paths OK)
          git        - Git ops: .log(), .diff(), .blame(), .status()
          ast_utils  - Python AST: .find_functions(), .find_classes(), .find_calls()
          code       - Multi-lang (100+ languages): .find_functions(), .find_classes()
          mcp        - MCP tools: .tools.<server>.<method>(), .servers, .help()

        Quick reference:
          %help       - Full documentation and examples
          object?     - Show docstring (e.g., sh?, workspace?, git.log?)
          %who        - List variables
          %history    - Show execution history

        Args:
            code: Python code to execute
            reset: Clear namespace (keeps utilities)
            timeout: Max execution seconds (default 120)
            inject: Variables to inject (e.g., {"data": [1,2,3]})

        Returns:
            Plain text output (stdout, return value, or error message)
        """
        if runtime_config.daytona_enabled:
            if not session_manager:
                return "Error: session manager is not initialized"
            try:
                chat_session_id = _resolve_chat_session_id(ctx, runtime_config.session_header_name)
                result = await asyncio.to_thread(
                    session_manager.execute_python,
                    chat_session_id,
                    code,
                    reset=reset,
                    timeout=timeout,
                    inject=inject,
                )
                return str(result)
            except DaytonaAdapterError as exc:
                return f"Daytona error: {exc}"
            except Exception as exc:
                return f"Execution error: {exc}"

        if not repl_engine:
            return "Error: REPL engine is not initialized"

        if reset:
            repl_engine.reset_namespace()

        result = repl_engine.execute(code, timeout=timeout, inject=inject)
        return str(result)

    if runtime_config.feature_upload_enabled:
        @mcp_server.custom_route("/api/sessions/{session_id}/uploads/bulk", methods=["POST"])
        async def bulk_upload_endpoint(request: Request):
            """
            Control-plane bulk upload endpoint (not exposed as an MCP tool).
            """
            session_id = str(request.path_params.get("session_id") or "").strip()
            if not session_id:
                return JSONResponse({"error": "session_id is required"}, status_code=400)

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "invalid JSON payload"}, status_code=400)

            files = body.get("files")
            default_encoding = body.get("default_encoding", "utf-8")
            if not isinstance(files, list) or not files:
                return JSONResponse({"error": "files must be a non-empty array"}, status_code=400)
            if not isinstance(default_encoding, str) or not default_encoding:
                return JSONResponse({"error": "default_encoding must be a non-empty string"}, status_code=400)

            results: list[dict[str, Any]] = []
            success_count = 0

            for index, item in enumerate(files):
                result: dict[str, Any] = {"index": index}
                if not isinstance(item, dict):
                    result["status"] = "error"
                    result["error"] = "file entry must be an object"
                    results.append(result)
                    continue

                path = item.get("path")
                content = item.get("content")
                is_base64 = bool(item.get("is_base64", False))
                encoding = item.get("encoding", default_encoding)

                result["path"] = path

                if not isinstance(path, str) or not path:
                    result["status"] = "error"
                    result["error"] = "path must be a non-empty string"
                    results.append(result)
                    continue
                if not isinstance(content, str):
                    result["status"] = "error"
                    result["error"] = "content must be a string"
                    results.append(result)
                    continue
                if not isinstance(encoding, str) or not encoding:
                    result["status"] = "error"
                    result["error"] = "encoding must be a non-empty string"
                    results.append(result)
                    continue

                payload, error = _decode_upload_payload(
                    content=content,
                    is_base64=is_base64,
                    encoding=encoding,
                    max_upload_bytes=runtime_config.max_upload_bytes,
                )
                if payload is None:
                    result["status"] = "error"
                    result["error"] = error
                    results.append(result)
                    continue

                success, message = await _persist_upload_payload(
                    path=path,
                    payload=payload,
                    session_id=session_id,
                    runtime_config=runtime_config,
                )
                if success:
                    success_count += 1
                    result["status"] = "ok"
                    result["bytes"] = len(payload)
                    result["message"] = message
                else:
                    result["status"] = "error"
                    result["error"] = message
                results.append(result)

            status_code = 200 if success_count == len(files) else 207
            return JSONResponse(
                {
                    "session_id": session_id,
                    "uploaded": success_count,
                    "total": len(files),
                    "results": results,
                },
                status_code=status_code,
            )

    if runtime_config.feature_bash_enabled:
        @mcp_server.tool()
        async def run_bash(
            command: str,
            cwd: Optional[str] = None,
            timeout: float = runtime_config.bash_timeout_seconds,
            ctx: Context = CurrentContext(),
        ):
            """
            Execute bash command in the active chat sandbox.

            This tool is feature-flagged by FEATURE_BASH_ENABLED.
            """
            if not runtime_config.daytona_enabled:
                return (
                    "run_bash is only available when DAYTONA_API_URL and "
                    "DAYTONA_API_KEY are configured"
                )
            if not session_manager:
                return "Error: session manager is not initialized"

            try:
                chat_session_id = _resolve_chat_session_id(ctx, runtime_config.session_header_name)
                result = await asyncio.to_thread(
                    session_manager.run_bash,
                    chat_session_id,
                    command,
                    cwd=cwd,
                    timeout=timeout,
                )
            except DaytonaAdapterError as exc:
                return f"Bash error: {exc}"
            except Exception as exc:
                return f"Bash error: {exc}"

            parts: list[str] = []
            if result.stdout:
                parts.append(result.stdout.rstrip())
            if result.stderr:
                parts.append(result.stderr.rstrip())
            parts.append(f"[exit_code={result.exit_code}]")
            return "\n".join(part for part in parts if part)

    return mcp_server


def initialize_server(
    autoconnect: bool = True,
    config_path: Path = Path(".mcp.json"),
    runtime_config: Optional[RuntimeConfig] = None,
):
    """
    Initialize global server state.

    NOTE: This function is primarily for testing. In production, initialization
    happens automatically via the server's lifespan hook.

    Args:
        autoconnect: Whether to auto-connect to servers from .mcp.json
        config_path: Path to .mcp.json configuration file
    """
    global mcp_wrapper, repl_engine, sandbox_adapter, session_manager
    if runtime_config is None:
        runtime_config = RuntimeConfig.from_env()

    # Initialize MCP wrapper
    mcp_wrapper = MCPClientWrapper()

    if runtime_config.daytona_enabled:
        sandbox_adapter = DaytonaSandboxAdapter(runtime_config)
        session_manager = ChatSessionManager(
            sandbox_adapter,
            idle_timeout_minutes=runtime_config.session_idle_timeout_minutes,
            idle_action=runtime_config.session_idle_action,
        )
        repl_engine = None
    else:
        # Initialize REPL engine with mcp wrapper and workspace root
        repl_engine = REPLEngine(
            mcp_wrapper=mcp_wrapper,
            workspace_root=Path.cwd(),
        )
        sandbox_adapter = None
        session_manager = None

    # Auto-connect to configured servers
    if autoconnect:
        servers = load_mcp_config(config_path)

        # Filter out self
        servers = filter_servers(servers, exclude=["python-repl"])

        if servers:
            logger = logging.getLogger(__name__)
            logger.info(f"Auto-connecting to {len(servers)} MCP servers from {config_path}...")
            results = mcp_wrapper.connect(servers)
            for name, success in results.items():
                status = "✓" if success else "✗"
                logger.info(f"  {status} {name}")


def check_port_available(port: int) -> bool:
    """
    Check if port is available before starting server.

    Args:
        port: Port number to check

    Returns:
        True if port is available, False otherwise
    """
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('0.0.0.0', port))
        sock.close()
        return True
    except OSError:
        return False


def main():
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(description="Stateful Python REPL MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=None,
        help="Transport type (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for SSE transport (default: 8000)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Host for SSE transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to .mcp.json config file (default: .mcp.json)",
    )
    parser.add_argument(
        "--no-autoconnect",
        action="store_true",
        help="Disable auto-connecting to servers from .mcp.json",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (logs to stderr)",
    )

    args = parser.parse_args()
    startup_config = StartupConfig.from_sources(args)
    runtime_config = RuntimeConfig.from_env()

    # Setup logging before server starts
    logger = setup_logging(startup_config.transport, startup_config.debug)

    # Check port availability for SSE mode
    if startup_config.transport == "sse":
        if not check_port_available(startup_config.port):
            logger.error(f"Port {startup_config.port} is already in use")
            logger.error("Try: uv run repl-mcp --transport sse --port <different-port>")
            sys.exit(1)

    # Create server with lifespan (initialization happens automatically)
    mcp_server = create_server(
        config_path=startup_config.config_path,
        autoconnect=startup_config.autoconnect,
        logger=logger,
        runtime_config=runtime_config,
    )

    # Run server
    if startup_config.transport == "stdio":
        mcp_server.run(transport="stdio")
    else:
        mcp_server.run(
            transport="sse",
            host=startup_config.host,
            port=startup_config.port,
        )


if __name__ == "__main__":
    main()
