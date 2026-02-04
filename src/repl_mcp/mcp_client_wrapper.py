"""Synchronous wrapper around async MCP SDK for REPL integration."""

import asyncio
import logging
import os
import subprocess
from typing import Any, Optional
from contextlib import AsyncExitStack

import nest_asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from .models import ServerConfig

# Allow nested event loops for sync wrapper
nest_asyncio.apply()

logger = logging.getLogger(__name__)


class ToolNamespace:
    """Dynamic namespace for accessing tools on a specific server."""

    def __init__(self, client_wrapper: "MCPClientWrapper", server_name: str):
        self._client = client_wrapper
        self._server = server_name

    def __getattr__(self, tool_name: str):
        """Return callable for the specified tool."""

        def tool_caller(**kwargs):
            # Extract timeout if provided, otherwise use default
            timeout = kwargs.pop('_timeout', 60.0)
            return self._client._invoke_tool(self._server, tool_name, timeout=timeout, **kwargs)

        return tool_caller


class ToolsContainer:
    """Container for dynamic server namespaces."""

    def __init__(self, client_wrapper: "MCPClientWrapper"):
        self._client = client_wrapper

    def __getattr__(self, server_name: str) -> ToolNamespace:
        """Return namespace for the specified server."""
        return ToolNamespace(self._client, server_name)


class MCPClientWrapper:
    """Synchronous wrapper for MCP client with dynamic tool access and Python-native introspection."""

    def __init__(self):
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stacks: dict[str, AsyncExitStack] = {}
        self._errlogs: dict[str, Any] = {}  # Track errlog file handles
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.tools = ToolsContainer(self)

    @property
    def servers(self) -> list[str]:
        """
        List connected server names (Python property, not tool call).

        Returns:
            List of connected server names

        Example:
            >>> mcp.servers
            ['github', 'playwright']
        """
        return list(self._sessions.keys())

    def __dir__(self):
        """Support dir(mcp) introspection."""
        return ['tools', 'servers', 'list_tools', 'help', 'discover_tools']

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop."""
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        return self._loop

    def _run_async(self, coro):
        """Run async coroutine in sync context."""
        loop = self._get_loop()
        # With nest_asyncio, we can always use run_until_complete
        # even when the loop is already running
        try:
            return loop.run_until_complete(coro)
        except RuntimeError:
            # Fallback to creating new loop
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()

    async def _connect_server(self, name: str, config: ServerConfig) -> bool:
        """Connect to a single MCP server."""
        try:
            exit_stack = AsyncExitStack()
            self._exit_stacks[name] = exit_stack

            if config.transport_type == "stdio":
                # Prepare environment
                env = os.environ.copy()
                if config.env:
                    # Expand environment variables in values
                    for key, value in config.env.items():
                        if value.startswith("${") and value.endswith("}"):
                            env_var = value[2:-1]
                            env[key] = os.environ.get(env_var, "")
                        else:
                            env[key] = value

                # Create stdio transport
                server_params = StdioServerParameters(
                    command=config.command,
                    args=config.args or [],
                    env=env,
                )

                # Redirect child server stderr to suppress banner messages
                # This prevents output like "GitHub MCP Server running on stdio" from appearing
                errlog = open(os.devnull, 'w')
                self._errlogs[name] = errlog

                stdio_transport = await exit_stack.enter_async_context(
                    stdio_client(server_params, errlog=errlog)
                )
                stdio, write = stdio_transport
                session = await exit_stack.enter_async_context(
                    ClientSession(stdio, write)
                )

            else:  # SSE transport
                sse_transport = await exit_stack.enter_async_context(
                    sse_client(config.url)
                )
                sse, write = sse_transport
                session = await exit_stack.enter_async_context(
                    ClientSession(sse, write)
                )

            # Initialize session
            await session.initialize()

            self._sessions[name] = session
            return True

        except asyncio.CancelledError:
            # Ensure subprocesses/transports are cleaned up if a connect is cancelled.
            try:
                if name in self._exit_stacks:
                    await self._exit_stacks[name].aclose()
                    del self._exit_stacks[name]
            finally:
                if name in self._errlogs:
                    try:
                        self._errlogs[name].close()
                    except Exception:
                        pass
                    del self._errlogs[name]
            raise
        except Exception as e:
            logger.warning("Failed to connect to %s: %s", name, e)
            if name in self._exit_stacks:
                await self._exit_stacks[name].aclose()
                del self._exit_stacks[name]
            # Clean up errlog if connection failed
            if name in self._errlogs:
                try:
                    self._errlogs[name].close()
                except Exception:
                    pass
                del self._errlogs[name]
            return False

    async def connect_async(
        self,
        servers: dict[str, dict],
        *,
        timeout_s: float = 15.0,
        sequential: bool = False,
    ) -> dict[str, bool]:
        """
        Async connect to multiple MCP servers with timeouts.

        Args:
            servers: Dict mapping server names to config dicts
            timeout_s: Default per-server connect timeout
            sequential: If True, connect one-by-one (useful to reduce load)

        Returns:
            Dict mapping server names to connection success status (True/False)
        """

        async def connect_one(name: str, config_dict: dict) -> tuple[str, bool]:
            config = ServerConfig(**config_dict)
            per_server_timeout = config.timeout_s if config.timeout_s is not None else timeout_s
            try:
                ok = await asyncio.wait_for(self._connect_server(name, config), timeout=per_server_timeout)
                return name, ok
            except asyncio.TimeoutError:
                logger.warning("Timed out connecting to %s after %.1fs", name, per_server_timeout)
                # Best-effort cleanup (covers partial initialization).
                if name in self._exit_stacks:
                    await self._exit_stacks[name].aclose()
                    del self._exit_stacks[name]
                if name in self._errlogs:
                    try:
                        self._errlogs[name].close()
                    except Exception:
                        pass
                    del self._errlogs[name]
                return name, False
            except Exception as e:
                logger.warning("Failed to connect to %s: %s", name, e)
                return name, False

        results: dict[str, bool] = {}

        if sequential:
            for name, config_dict in servers.items():
                n, ok = await connect_one(name, config_dict)
                results[n] = ok
            return results

        tasks = [connect_one(name, config_dict) for name, config_dict in servers.items()]
        for coro in asyncio.as_completed(tasks):
            name, ok = await coro
            results[name] = ok
        return results

    def connect(self, servers: dict[str, dict]) -> dict[str, bool]:
        """
        Connect to multiple MCP servers.

        Args:
            servers: Dict mapping server names to config dicts

        Returns:
            Dict mapping server names to connection success status
        """
        return self._run_async(self.connect_async(servers))

    def list_tools(self, server: Optional[str] = None) -> dict[str, list[str]]:
        """
        List available tools from connected servers.

        Args:
            server: Specific server name, or None for all servers

        Returns:
            Dict mapping server names to tool lists

        Examples:
            >>> mcp.list_tools()
            {'github': ['create_issue', 'list_issues', ...], 'playwright': [...]}

            >>> mcp.list_tools('github')
            {'github': ['create_issue', 'list_issues', ...]}
        """

        async def get_tools():
            tools_by_server = {}
            servers_to_query = [server] if server else list(self._sessions.keys())

            for name in servers_to_query:
                if name not in self._sessions:
                    logger.warning("Server %s not connected", name)
                    continue

                try:
                    result = await self._sessions[name].list_tools()
                    tools_by_server[name] = [tool.name for tool in result.tools]
                except Exception as e:
                    logger.warning("Failed to list tools from %s: %s", name, e)
                    tools_by_server[name] = []
            return tools_by_server

        return self._run_async(get_tools())

    def discover_tools(self) -> dict[str, list[str]]:
        """
        List all available tools from connected servers.

        Deprecated: Use list_tools() instead for Python-native introspection.

        Returns:
            Dict mapping server names to lists of tool names
        """
        return self.list_tools()

    def help(self, server: Optional[str] = None, tool: Optional[str] = None) -> str:
        """
        Show help for MCP servers and tools.

        Args:
            server: Server name, or None to show all servers
            tool: Tool name (requires server), or None to show all tools

        Returns:
            Help text string

        Usage:
            >>> print(mcp.help())              # Show all servers
            >>> print(mcp.help('github'))       # Show github tools
            >>> print(mcp.help('github', 'create_issue'))  # Show specific tool
        """
        if server is None:
            # Show all servers
            servers = list(self._sessions.keys())
            if not servers:
                return "No MCP servers connected"

            output = "Connected MCP servers:\n"
            tool_list = self.list_tools()
            for srv in servers:
                tools = tool_list.get(srv, [])
                output += f"  {srv} ({len(tools)} tools)\n"
            output += "\nUse mcp.help('server') to see tools for a specific server"
            return output

        elif tool is None:
            # Show all tools for a server
            if server not in self._sessions:
                return f"Server '{server}' not connected"

            tool_list = self.list_tools(server)
            tools = tool_list.get(server, [])

            if not tools:
                return f"No tools available from server '{server}'"

            output = f"Tools from '{server}':\n"
            for t in tools:
                output += f"  - {t}\n"
            output += f"\nUse mcp.help('{server}', 'tool_name') for details on a specific tool"
            output += f"\nOr: help(mcp.tools.{server}.tool_name)"
            return output

        else:
            # Show specific tool help
            if server not in self._sessions:
                return f"Server '{server}' not connected"

            # Get tool schema
            async def get_tool_schema():
                try:
                    result = await self._sessions[server].list_tools()
                    for t in result.tools:
                        if t.name == tool:
                            desc = t.description or "No description available"
                            schema = t.inputSchema if hasattr(t, 'inputSchema') else {}
                            return f"{server}.{tool}:\n  {desc}\n\n  Schema: {schema}"
                    return f"Tool '{tool}' not found in server '{server}'"
                except Exception as e:
                    return f"Error getting tool info: {e}"

            return self._run_async(get_tool_schema())

    def _invoke_tool(self, server: str, tool: str, timeout: float = 60.0, **kwargs) -> Any:
        """
        Invoke a tool on a specific server.

        Args:
            server: Server name
            tool: Tool name
            timeout: Timeout in seconds for the tool call (default: 60s)
            **kwargs: Tool arguments

        Returns:
            Tool result (parsed from content)

        Raises:
            asyncio.TimeoutError: If the tool call exceeds the timeout
        """
        if server not in self._sessions:
            raise ValueError(f"Server '{server}' not connected")

        async def call_tool():
            session = self._sessions[server]
            result = await asyncio.wait_for(
                session.call_tool(tool, arguments=kwargs),
                timeout=timeout
            )

            # Parse result from content
            if result.content:
                # MCP returns list of content blocks
                if isinstance(result.content, list) and len(result.content) > 0:
                    first_content = result.content[0]
                    if hasattr(first_content, "text"):
                        return first_content.text
                    return str(first_content)
                return str(result.content)
            return None

        return self._run_async(call_tool())

    def disconnect(self, server: Optional[str] = None) -> None:
        """
        Disconnect from server(s).

        Args:
            server: Specific server to disconnect, or None to disconnect all
        """

        async def disconnect_all():
            servers_to_close = [server] if server else list(self._sessions.keys())
            for name in servers_to_close:
                if name in self._exit_stacks:
                    await self._exit_stacks[name].aclose()
                    del self._exit_stacks[name]
                if name in self._sessions:
                    del self._sessions[name]
                # Close errlog file handle
                if name in self._errlogs:
                    try:
                        self._errlogs[name].close()
                    except Exception:
                        pass
                    del self._errlogs[name]

        self._run_async(disconnect_all())

    def __del__(self):
        """Cleanup on deletion."""
        if self._sessions:
            try:
                self.disconnect()
            except Exception:
                pass
