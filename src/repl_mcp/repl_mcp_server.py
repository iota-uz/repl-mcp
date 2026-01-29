"""FastMCP server exposing stateful Python REPL."""

import sys
import json
import argparse
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastmcp import FastMCP

from .repl_engine import REPLEngine
from .mcp_client_wrapper import MCPClientWrapper

# Global instances
mcp_wrapper: Optional[MCPClientWrapper] = None
repl_engine: Optional[REPLEngine] = None


def setup_logging(transport: str, debug: bool = False) -> logging.Logger:
    """
    Configure logging to avoid stdout pollution in SSE mode.

    Args:
        transport: Transport type ('stdio' or 'sse')
        debug: Enable debug logging to stdout even in SSE mode

    Returns:
        Logger instance
    """
    if transport == "sse" and not debug:
        # Log to stderr to keep stdout clean for SSE
        handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.StreamHandler(sys.stdout)

    handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
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

        # --- STARTUP ---
        logger.info("Initializing REPL MCP server...")

        # Initialize MCP wrapper
        mcp_wrapper = MCPClientWrapper()

        # Initialize REPL engine
        repl_engine = REPLEngine(mcp_wrapper=mcp_wrapper)

        # Auto-connect to configured servers
        if autoconnect_enabled:
            servers = load_mcp_config(config_path)

            # Filter out self to prevent recursive connection
            servers = filter_servers(servers, exclude=["python-repl"])

            if servers:
                logger.info(f"Auto-connecting to {len(servers)} MCP servers...")
                results = mcp_wrapper.connect(servers)
                # If connect returns a Task (because we're in async context), await it
                if hasattr(results, '__await__'):
                    results = await results
                for name, success in results.items():
                    status = "✓" if success else "✗"
                    logger.info(f"  {status} {name}")

        # Yield control - server runs here
        yield {}

        # --- SHUTDOWN ---
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
    @mcp_server.tool()
    def execute_python(code: str, reset: bool = False) -> dict:
        """
        Execute Python code in persistent REPL environment.

        The REPL maintains state across executions. Variables, imports, and function
        definitions persist between calls. A pre-injected 'mcp' object provides access
        to connected MCP servers and their tools.

        Args:
            code: Python code to execute
            reset: If True, reset namespace before execution (preserves mcp object)

        Returns:
            Dict containing:
            - success: Whether execution succeeded
            - stdout: Captured standard output
            - stderr: Captured standard error
            - return_value: String representation of return value (if any)
            - exception: Exception details (if execution failed)
            - execution_time_ms: Execution time in milliseconds
            - namespace_vars: Current namespace variables

        Examples:
            # Basic execution with state persistence
            execute_python(code="x = 42")
            execute_python(code="print(x)")  # Output: 42

            # Call MCP tools
            execute_python(code='''
result = mcp.tools.github.create_issue(
    owner="myuser",
    repo="myrepo",
    title="Test",
    body="Created from REPL"
)
print(result)
            ''')

            # Reset namespace
            execute_python(code="y = 100", reset=True)
        """
        if reset:
            repl_engine.reset_namespace()

        result = repl_engine.execute(code)
        return result.model_dump()

    @mcp_server.tool()
    def list_namespace_vars() -> dict[str, str]:
        """
        List all variables currently defined in the REPL namespace.

        Returns:
            Dict mapping variable names to their string representations (truncated to 100 chars)
        """
        return repl_engine.get_namespace_vars()

    @mcp_server.tool()
    def connect_mcp_servers(servers: dict) -> dict[str, bool]:
        """
        Connect to external MCP servers at runtime.

        This allows dynamically adding new MCP servers without restarting the REPL server.
        Once connected, tools from these servers are available via the 'mcp' object in
        the REPL namespace.

        Args:
            servers: Dict mapping server names to server configurations.
                    Each config should have:
                    - command: Command to run (for stdio transport)
                    - args: Command arguments (optional)
                    - url: URL for HTTP/SSE transport (alternative to command)
                    - env: Environment variables (optional)

        Returns:
            Dict mapping server names to connection success status (True/False)

        Examples:
            # Connect to GitHub MCP server
            connect_mcp_servers(servers={
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {
                        "GITHUB_TOKEN": "${GH_TOKEN}"
                    }
                }
            })

            # Connect via HTTP/SSE
            connect_mcp_servers(servers={
                "playwright": {
                    "url": "http://localhost:3001/sse"
                }
            })
        """
        return mcp_wrapper.connect(servers)

    @mcp_server.tool()
    def list_connected_servers() -> dict[str, list[str]]:
        """
        List all currently connected MCP servers and their available tools.

        Returns:
            Dict mapping server names to lists of tool names

        Example:
            {
                "github": ["create_issue", "list_issues", "search_code", ...],
                "playwright": ["navigate", "screenshot", "fill_form", ...]
            }
        """
        return mcp_wrapper.discover_tools()

    @mcp_server.tool()
    def disconnect_mcp_server(server: Optional[str] = None) -> dict:
        """
        Disconnect from MCP server(s).

        Args:
            server: Server name to disconnect, or None to disconnect all servers

        Returns:
            Dict with status message
        """
        mcp_wrapper.disconnect(server)
        if server:
            return {"status": f"Disconnected from {server}"}
        return {"status": "Disconnected from all servers"}

    return mcp_server


def initialize_server(autoconnect: bool = True, config_path: Path = Path(".mcp.json")):
    """
    Initialize global server state.

    NOTE: This function is primarily for testing. In production, initialization
    happens automatically via the server's lifespan hook.

    Args:
        autoconnect: Whether to auto-connect to servers from .mcp.json
        config_path: Path to .mcp.json configuration file
    """
    global mcp_wrapper, repl_engine

    # Initialize MCP wrapper
    mcp_wrapper = MCPClientWrapper()

    # Initialize REPL engine with mcp wrapper
    repl_engine = REPLEngine(mcp_wrapper=mcp_wrapper)

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
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for SSE transport (default: 8000)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host for SSE transport (default: 0.0.0.0)",
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
        help="Enable debug logging to stdout (even in SSE mode)",
    )

    args = parser.parse_args()

    # Setup logging before server starts
    logger = setup_logging(args.transport, args.debug)

    # Check port availability for SSE mode
    if args.transport == "sse":
        if not check_port_available(args.port):
            logger.error(f"Port {args.port} is already in use")
            logger.error("Try: uv run repl-mcp --transport sse --port <different-port>")
            sys.exit(1)

    # Create server with lifespan (initialization happens automatically)
    mcp_server = create_server(
        config_path=args.config,
        autoconnect=not args.no_autoconnect,
        logger=logger
    )

    # Run server
    if args.transport == "stdio":
        mcp_server.run(transport="stdio")
    else:
        mcp_server.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
