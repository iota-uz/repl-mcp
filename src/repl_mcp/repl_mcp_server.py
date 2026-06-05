"""FastMCP server exposing stateful Python REPL."""

import asyncio
import sys
import json
import argparse
import logging

import anyio.to_thread
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import AsyncIterator, Optional

from fastmcp import FastMCP

from .repl_engine import REPLEngine
from .mcp_client_wrapper import MCPClientWrapper

# Global instances
mcp_wrapper: Optional[MCPClientWrapper] = None
repl_engine: Optional[REPLEngine] = None


def setup_logging(debug: bool = False) -> logging.Logger:
    """
    Configure logging to stderr (stdout carries stdio JSON-RPC).
    """
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


def create_server_lifespan(config_path: Path, autoconnect_enabled: bool, logger: logging.Logger):
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
        global mcp_wrapper, repl_engine
        autoconnect_task = None

        # --- STARTUP ---
        logger.info("Initializing REPL MCP server...")

        # Initialize MCP wrapper
        mcp_wrapper = MCPClientWrapper()

        # Initialize REPL engine with workspace root
        repl_engine = REPLEngine(
            mcp_wrapper=mcp_wrapper,
            workspace_root=Path.cwd(),
        )

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

        # Yield control - server runs here
        yield {}

        # --- SHUTDOWN ---
        if autoconnect_task and not autoconnect_task.done():
            autoconnect_task.cancel()
            with suppress(BaseException):
                await autoconnect_task
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
    logger: logging.Logger = None
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
    lifespan = create_server_lifespan(config_path, autoconnect, logger)

    # Create server with lifespan
    mcp_server = FastMCP("python-repl", lifespan=lifespan)

    # Register tools
    #
    # NOTE: No return type annotation on execute_python intentionally!
    # Adding `-> str` causes FastMCP to wrap output in {"result": "..."} JSON
    # via structuredContent. We want plain text output for better readability.
    #
    # NOTE: async + to_thread offload intentionally! FastMCP runs sync tools
    # inline on the event loop thread, so long REPL executions froze the whole
    # server (autoconnect starved, protocol handling blocked). Offloading also
    # means mcp bridge calls always come from a worker thread and take the
    # loop-affine run_coroutine_threadsafe path in MCPClientWrapper.
    @mcp_server.tool()
    async def execute_python(
        code: str,
        reset: bool = False,
        timeout: float = 120.0,
    ):
        """
        Persistent Python REPL — use this instead of `python3 -c`, heredocs,
        or `cmd | python3` via Bash.

        State (variables, imports, functions) survives across calls: a warm
        call takes ~0.1s vs ~3s for each fresh `python3` Bash spawn. Full
        filesystem access — open(), absolute paths, and ~ all work. Top-level
        `await` is supported (e.g. `await client.get(url)` with httpx).

        Helpers:
          sh   - Shell commands: json.loads(sh("gh pr view 1 --json title"))
                 Returns stdout str with .returncode/.stderr/.ok
          mcp  - Bridge to this project's .mcp.json MCP servers:
                 .call(server, tool, **args), .servers, .failed, .help()
                 Connects lazily on first use. Host-level connectors
                 (claude.ai Notion/GitHub, user-scope servers) are NOT
                 reachable here — call their tools directly.

        Missing package? Install into the running REPL env: sh('uv pip install <pkg>')

        Args:
            code: Python code to execute
            reset: Clear namespace (keeps sh/mcp helpers)
            timeout: Max execution seconds (default 120). Enforced for real:
                     runaway code is interrupted (KeyboardInterrupt) with
                     namespace state preserved.

        Returns:
            Plain text output (stdout, return value, or error message)
        """
        if reset:
            repl_engine.reset_namespace()

        result = await anyio.to_thread.run_sync(
            lambda: repl_engine.execute(code, timeout=timeout)
        )
        return str(result)

    return mcp_server


def main():
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(description="Stateful Python REPL MCP Server (stdio)")
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Transport type (stdio only)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(".mcp.json"),
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

    # Setup logging before server starts
    logger = setup_logging(args.debug)

    # Create server with lifespan (initialization happens automatically)
    mcp_server = create_server(
        config_path=args.config,
        autoconnect=not args.no_autoconnect,
        logger=logger
    )

    # Run server
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
