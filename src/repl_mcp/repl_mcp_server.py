"""FastMCP server exposing stateful Python REPL."""

import json
import argparse
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from .repl_engine import REPLEngine
from .mcp_client_wrapper import MCPClientWrapper

# Initialize server
mcp = FastMCP("python-repl")

# Global instances
mcp_wrapper: Optional[MCPClientWrapper] = None
repl_engine: Optional[REPLEngine] = None


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
        print(f"Warning: Failed to load {config_path}: {e}")
        return {}


def initialize_server(autoconnect: bool = True, config_path: Path = Path(".mcp.json")):
    """
    Initialize global server state.

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
        if servers:
            print(f"Auto-connecting to {len(servers)} MCP servers from {config_path}...")
            results = mcp_wrapper.connect(servers)
            for name, success in results.items():
                status = "✓" if success else "✗"
                print(f"  {status} {name}")


@mcp.tool()
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


@mcp.tool()
def list_namespace_vars() -> dict[str, str]:
    """
    List all variables currently defined in the REPL namespace.

    Returns:
        Dict mapping variable names to their string representations (truncated to 100 chars)
    """
    return repl_engine.get_namespace_vars()


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


def main():
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(description="Stateful Python REPL MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="sse",
        help="Transport type (default: sse)",
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

    args = parser.parse_args()

    # Initialize server state
    initialize_server(
        autoconnect=not args.no_autoconnect,
        config_path=args.config,
    )

    # Run server
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"Starting REPL MCP server on {args.host}:{args.port}")
        print(f"SSE endpoint: http://{args.host}:{args.port}/sse")
        mcp.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
