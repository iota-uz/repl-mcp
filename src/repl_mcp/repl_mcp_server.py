"""FastMCP server exposing stateful Python REPL."""

import sys
import json
import asyncio
import argparse
import logging

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastmcp import FastMCP

from .repl_engine import REPLEngine  # noqa: F401  (re-export for tests/embedders)
from .mcp_client_wrapper import MCPClientWrapper
from .kernel.supervisor import KernelSupervisor
from .mcp_config import ALL_SCOPES, discover_servers

# Global instances
mcp_wrapper: Optional[MCPClientWrapper] = None
kernel: Optional[KernelSupervisor] = None
repl_engine = None  # set by tests that drive the engine in-process


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


def parse_scopes(value: str) -> tuple[str, ...]:
    """
    Parse the --mcp-scope flag into a scope tuple.

    'all' -> every scope, 'none'/'' -> no bridge, otherwise a comma-separated
    subset of local,project,user,plugin (unknown names are ignored).
    """
    text = (value or "").strip().lower()
    if text in ("", "none"):
        return ()
    if text == "all":
        return ALL_SCOPES
    wanted = [part.strip() for part in text.split(",")]
    return tuple(s for s in ALL_SCOPES if s in wanted)


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


def create_server_lifespan(
    config_path: Path,
    autoconnect_enabled: bool,
    logger: logging.Logger,
    scopes: tuple[str, ...] = ALL_SCOPES,
):
    """
    Factory to create a lifespan function with captured config.

    Args:
        config_path: Path to the project's .mcp.json (overrides project scope)
        autoconnect_enabled: Whether the mcp bridge is available at all
        logger: Logger instance for output
        scopes: config scopes to discover (see mcp_config.ALL_SCOPES)

    Returns:
        Async context manager for server lifespan
    """

    @asynccontextmanager
    async def server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
        """Lifespan context manager for the REPL MCP server."""
        global mcp_wrapper, kernel

        # --- STARTUP ---
        logger.info("Initializing REPL MCP server...")

        mcp_wrapper = MCPClientWrapper()

        # Discovery is cheap (a few file reads) and spawns nothing, so it runs
        # eagerly — `mcp.servers` is populated from the first cell. Actual
        # connections happen per server, on the first mcp.call() naming it.
        refresh_registry = None
        if autoconnect_enabled and scopes:
            def _discover():
                return discover_servers(
                    cwd=Path.cwd(), project_config_path=config_path, scopes=scopes
                )

            async def refresh_registry():
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, _discover)
                mcp_wrapper.set_registry(result)
                return result

            try:
                registry = await refresh_registry()
                logger.info(
                    "MCP bridge: %d servers available (%s) — connect on first use",
                    len(registry.servers),
                    ", ".join(registry.names) or "none",
                )
            except Exception as e:
                logger.warning("MCP config discovery failed: %s", e)

        kernel = KernelSupervisor(
            mcp_wrapper=mcp_wrapper,
            workspace_root=Path.cwd(),
            refresh_registry=refresh_registry,
        )
        await kernel.start()

        # Yield control - server runs here
        yield {}

        # --- SHUTDOWN ---
        try:
            logger.info("Shutting down REPL MCP server...")
        except ValueError:
            # File may be closed during stdio shutdown
            pass
        if kernel:
            await kernel.shutdown()
        if mcp_wrapper:
            # Async form: we are on the server's event loop here
            await mcp_wrapper.disconnect_async()

    return server_lifespan


def create_server(
    config_path: Path = Path(".mcp.json"),
    autoconnect: bool = True,
    logger: logging.Logger = None,
    scopes: tuple[str, ...] = ALL_SCOPES,
) -> FastMCP:
    """
    Create and configure the FastMCP server.

    Args:
        config_path: Path to the project's .mcp.json config file
        autoconnect: Whether the mcp bridge is enabled
        logger: Logger instance for output
        scopes: config scopes to discover (see mcp_config.ALL_SCOPES)

    Returns:
        Configured FastMCP server instance
    """
    # Create lifespan
    if logger is None:
        logger = logging.getLogger(__name__)
    lifespan = create_server_lifespan(config_path, autoconnect, logger, scopes)

    # Create server with lifespan
    mcp_server = FastMCP("python-repl", lifespan=lifespan)

    # Register tools
    #
    # NOTE: No return type annotation on execute_python intentionally!
    # Adding `-> str` causes FastMCP to wrap output in {"result": "..."} JSON
    # via structuredContent. We want plain text output for better readability.
    #
    # NOTE: pure-async handler — execution happens in the kernel CHILD
    # process; this just awaits the IPC result, so the server's event loop
    # is never blocked by REPL code.
    @mcp_server.tool()
    async def execute_python(
        code: str,
        reset: bool = False,
        timeout: float = 120.0,
    ):
        """
        Persistent Python REPL — use instead of `python3 -c`, heredocs or
        `cmd | python3` via Bash. Also the way to BATCH MCP WORK: the injected
        `mcp` bridge reaches your project, global (user-scope) and plugin MCP
        servers, so one loop replaces N separate tool calls —
        `for f in files: mcp.call('telegram-mcp', 'download_media', **f)`.

        State (variables, imports, functions) survives across calls: a warm
        call takes ~0.1s vs ~3s for each fresh `python3` Bash spawn. Full
        filesystem access — open(), absolute paths, and ~ all work. Top-level
        `await` is supported (e.g. `await client.get(url)` with httpx).

        Helpers:
          sh   - Shell commands: json.loads(sh("gh pr view 1 --json title"))
                 Returns stdout str with .returncode/.stderr/.ok
          mcp  - Bridge to your MCP servers across every Claude Code config
                 scope — project (./.mcp.json), user (global), and plugins:
                 .call(server, tool, **args), .servers, .failed, .help()
                 Servers connect ON DEMAND: naming one in .call() starts it
                 (~1-3s), later calls are warm. `.servers` lists what is
                 available; print(mcp.help()) shows scope + status.
                 Not reachable: claude.ai host connectors (Notion/Gmail/
                 Drive/chrome) — call their tools directly.

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
        result = await kernel.execute(code, timeout=timeout, reset=reset)
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
        "--mcp-scope",
        default="all",
        help=(
            "Config scopes the mcp bridge discovers: 'all' (default), 'none', "
            "or a comma-separated subset of local,project,user,plugin"
        ),
    )
    parser.add_argument(
        "--no-autoconnect",
        action="store_true",
        help="Disable the mcp bridge entirely (alias for --mcp-scope none)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (logs to stderr)",
    )

    args = parser.parse_args()

    # Setup logging before server starts
    logger = setup_logging(args.debug)

    scopes = parse_scopes(args.mcp_scope)

    # Create server with lifespan (initialization happens automatically)
    mcp_server = create_server(
        config_path=args.config,
        autoconnect=not args.no_autoconnect,
        logger=logger,
        scopes=scopes,
    )

    # Run server
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
