"""Synchronous wrapper around async MCP SDK for REPL integration."""

import asyncio
import logging
import os
import re
import subprocess
import sys
from typing import Any, Optional
from contextlib import AsyncExitStack

import nest_asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from .models import ServerConfig


_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand_env_value(value: str) -> str:
    """
    Expand ${VAR} / ${VAR:-default} references anywhere in the string
    (Claude Code .mcp.json semantics, e.g. "Bearer ${MY_TOKEN}").

    Unset vars without a default expand to "" with a warning.
    """

    def repl(m: re.Match) -> str:
        var, default = m.group(1), m.group(2)
        if var in os.environ:
            return os.environ[var]
        if default is not None:
            return default
        logger.warning("Env var %s referenced in config but not set", var)
        return ""

    return _ENV_REF.sub(repl, value)


def _failure_reason(exc: BaseException) -> str:
    """
    Human-readable reason for a connect failure.

    anyio task groups wrap the real error (e.g. an HTTP 401) in nested
    ExceptionGroups whose str() is just "unhandled errors in a TaskGroup" —
    unwrap to the leaf exceptions instead.
    """
    leaves: list[BaseException] = []

    def walk(e: BaseException) -> None:
        subs = getattr(e, "exceptions", None)  # ExceptionGroup (incl. py3.10 backport)
        if subs:
            for sub in subs:
                walk(sub)
        else:
            leaves.append(e)

    walk(exc)
    reasons = [f"{type(e).__name__}: {e}" for e in leaves[:3]]
    if len(leaves) > 3:
        reasons.append(f"... and {len(leaves) - 3} more")
    return "; ".join(reasons) if reasons else f"{type(exc).__name__}: {exc}"

# Allow nested event loops for sync wrapper.
# Python 3.14 + anyio can fail when nest_asyncio patches the loop used by
# FastMCP server startup, so skip auto-patching there.
if sys.version_info < (3, 14):
    nest_asyncio.apply()

logger = logging.getLogger(__name__)


class ToolNamespace:
    """
    Dynamic namespace for accessing tools on a specific server.

    Provides attribute-style access to MCP tools:
        mcp.tools.github.create_issue(owner="...", repo="...", title="...")

    Supports introspection:
        dir(mcp.tools.github)  # List available tools
        mcp.tools.github       # Shows server info and tool count
    """

    def __init__(self, client_wrapper: "MCPClientWrapper", server_name: str):
        self._client = client_wrapper
        self._server = server_name
        # Cache tool list for introspection (lazy-loaded)
        self._tools_cache: list[str] | None = None

    def _get_tools(self) -> list[str]:
        """Get list of tools, with caching."""
        if self._tools_cache is None:
            try:
                tools_dict = self._client.list_tools(self._server)
                self._tools_cache = tools_dict.get(self._server, [])
            except Exception:
                self._tools_cache = []
        return self._tools_cache

    def __dir__(self) -> list[str]:
        """Support dir(mcp.tools.server) to list available tools."""
        return self._get_tools()

    def __repr__(self) -> str:
        """Helpful repr showing server name and tool count."""
        tools = self._get_tools()
        if tools:
            return f"<ToolNamespace '{self._server}' with {len(tools)} tools: {', '.join(tools[:3])}{'...' if len(tools) > 3 else ''}>"
        return f"<ToolNamespace '{self._server}' (not connected or no tools)>"

    def __getattr__(self, tool_name: str):
        """Return callable for the specified tool."""
        # Skip private attributes to avoid recursion
        if tool_name.startswith('_'):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{tool_name}'")

        # Get tool description for docstring
        tool_doc = self._get_tool_description(tool_name)

        def tool_caller(**kwargs):
            # Extract timeout if provided, otherwise use default
            timeout = kwargs.pop('_timeout', 60.0)
            return self._client._invoke_tool(self._server, tool_name, timeout=timeout, **kwargs)

        # Attach metadata to the callable
        tool_caller.__name__ = tool_name
        tool_caller.__qualname__ = f"mcp.tools.{self._server}.{tool_name}"
        tool_caller.__doc__ = tool_doc
        return tool_caller

    def _get_tool_description(self, tool_name: str) -> str:
        """Get description for a specific tool."""
        try:
            help_text = self._client.help(self._server, tool_name)
            return help_text
        except Exception:
            return f"MCP tool: {self._server}.{tool_name}\n\nUse mcp.help('{self._server}', '{tool_name}') for details."


class ToolsContainer:
    """
    Container providing attribute-style access to MCP server namespaces.

    Supports introspection:
        dir(mcp.tools)     # List connected servers
        mcp.tools          # Shows connected server info
        mcp.tools.github   # Access tools on 'github' server
    """

    def __init__(self, client_wrapper: "MCPClientWrapper"):
        self._client = client_wrapper

    def __dir__(self) -> list[str]:
        """Support dir(mcp.tools) to list connected servers."""
        return self._client.servers

    def __repr__(self) -> str:
        """Helpful repr showing connected servers."""
        servers = self._client.servers
        if servers:
            return f"<ToolsContainer with {len(servers)} servers: {', '.join(servers)}>"
        return "<ToolsContainer (no servers connected)>"

    def __getattr__(self, server_name: str) -> ToolNamespace:
        """Return namespace for the specified server."""
        # Skip private attributes
        if server_name.startswith('_'):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{server_name}'")
        return ToolNamespace(self._client, server_name)


class MCPClientWrapper:
    """Synchronous wrapper for MCP client with dynamic tool access and Python-native introspection."""

    def __init__(self):
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stacks: dict[str, AsyncExitStack] = {}
        self._errlogs: dict[str, Any] = {}  # Track errlog file handles
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Loop the sessions were created on. Sessions (and their anyio
        # streams/tasks) are loop-affine: every await on them MUST run on
        # this loop. REPL code executes on a worker thread (FastMCP runs
        # sync tools via anyio.to_thread), so _run_async routes coroutines
        # back to this loop with run_coroutine_threadsafe. Without this,
        # any mcp.* call from the REPL deadlocks forever.
        self._owner_loop: Optional[asyncio.AbstractEventLoop] = None
        # Connection failures by server name (reason string), for help()/servers
        self.failed: dict[str, str] = {}
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
        return ['tools', 'servers', 'failed', 'call', 'list_tools', 'help', 'discover_tools']

    def call(self, server: str, tool: str, *, timeout: float = 60.0, **kwargs) -> Any:
        """
        Call a tool on a connected server (alias for mcp.tools.<server>.<tool>(...)).

        Args:
            server: Server name (see mcp.servers)
            tool: Tool name (see mcp.list_tools(server))
            timeout: Timeout in seconds for the tool call (default: 60s)
            **kwargs: Tool arguments

        Returns:
            Tool result (parsed from content)

        Example:
            >>> mcp.call('github', 'create_issue', owner='me', repo='proj', title='Bug')
        """
        return self._invoke_tool(server, tool, timeout=timeout, **kwargs)

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop."""
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        return self._loop

    def _run_async(self, coro, timeout: Optional[float] = 30.0):
        """
        Run async coroutine in sync context.

        Sessions are bound to the event loop they were connected on
        (self._owner_loop). When called from a different thread — the normal
        case: execute_python offloads REPL execution to a worker thread
        (anyio.to_thread) while the sessions live on the server's main loop —
        the coroutine is scheduled onto the owner loop thread-safely. Awaiting
        a session from any other loop deadlocks (anyio streams never wake
        cross-loop), which is exactly the historical mcp.help() hang.

        Args:
            coro: Coroutine to run
            timeout: Max seconds to wait for the result when routing to the
                     owner loop (None = wait forever; avoid)
        """
        owner = self._owner_loop
        if owner is not None and owner.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not owner:
                future = asyncio.run_coroutine_threadsafe(coro, owner)
                try:
                    return future.result(timeout=timeout)
                except TimeoutError:
                    future.cancel()
                    raise TimeoutError(
                        f"MCP bridge call timed out after {timeout}s"
                    ) from None
            # Same loop and it's running: fall through to nest_asyncio path.

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
                        env[key] = _expand_env_value(value)

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

            else:  # HTTP transports
                # Headers (e.g. Authorization: Bearer ${TOKEN}) with env expansion
                headers = None
                if config.headers:
                    headers = {k: _expand_env_value(v) for k, v in config.headers.items()}

                if config.transport_type == "http":  # streamable HTTP
                    read, write, _ = await exit_stack.enter_async_context(
                        streamablehttp_client(config.url, headers=headers)
                    )
                else:  # SSE transport
                    read, write = await exit_stack.enter_async_context(
                        sse_client(config.url, headers=headers)
                    )
                session = await exit_stack.enter_async_context(
                    ClientSession(read, write)
                )

            # Initialize session
            await session.initialize()

            self._sessions[name] = session
            self.failed.pop(name, None)
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
            reason = _failure_reason(e)
            logger.warning("Failed to connect to %s: %s", name, reason)
            logger.debug("Connect failure for %s", name, exc_info=e)
            self.failed[name] = reason
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
        # Sessions become affine to the loop running this connect — record it
        # so _run_async can route later sync calls (from REPL worker threads)
        # back here.
        self._owner_loop = asyncio.get_running_loop()

        async def connect_one(name: str, config_dict: dict) -> tuple[str, bool]:
            config = ServerConfig(**config_dict)
            per_server_timeout = config.timeout_s if config.timeout_s is not None else timeout_s
            try:
                ok = await asyncio.wait_for(self._connect_server(name, config), timeout=per_server_timeout)
                if not ok and name not in self.failed:
                    self.failed[name] = "connection failed (see server stderr log)"
                return name, ok
            except asyncio.TimeoutError:
                logger.warning("Timed out connecting to %s after %.1fs", name, per_server_timeout)
                self.failed[name] = f"timed out after {per_server_timeout:.0f}s"
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
                reason = _failure_reason(e)
                logger.warning("Failed to connect to %s: %s", name, reason)
                logger.debug("Connect failure for %s", name, exc_info=e)
                self.failed[name] = reason
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
                    # Per-server timeout: one unresponsive server must not
                    # stall introspection of the rest.
                    result = await asyncio.wait_for(
                        self._sessions[name].list_tools(), timeout=10.0
                    )
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
        scope_note = (
            "Servers come from this project's .mcp.json (autoconnect). "
            "Host-level connectors (claude.ai Notion/GitHub, user-scope `claude mcp add` servers, plugins) "
            "are NOT visible here — call those tools directly instead."
        )

        if server is None:
            # Show all servers
            servers = list(self._sessions.keys())
            if not servers and not self.failed:
                return f"No MCP servers connected.\n\n{scope_note}"

            output = "Connected MCP servers:\n" if servers else ""
            tool_list = self.list_tools() if servers else {}
            for srv in servers:
                tools = tool_list.get(srv, [])
                output += f"  {srv} ({len(tools)} tools)\n"
            if self.failed:
                output += "Failed to connect:\n"
                for srv, reason in self.failed.items():
                    output += f"  {srv}: {reason}\n"
            output += "\nUse mcp.help('server') to see tools for a specific server"
            output += "\nCall tools via mcp.call('server', 'tool', **args) or mcp.tools.<server>.<tool>(**args)"
            output += f"\n\n{scope_note}"
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

        # Outer margin over the inner wait_for so the inner timeout fires first
        return self._run_async(call_tool(), timeout=timeout + 10.0)

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
                    try:
                        await self._exit_stacks[name].aclose()
                    except BaseException as e:
                        # Best-effort: anyio cancel scopes can't always be
                        # closed from a different task than they were entered
                        # in (e.g. shutdown from another task/thread). The
                        # child transport dies with the process anyway.
                        logger.debug("Error closing %s: %s", name, e)
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
