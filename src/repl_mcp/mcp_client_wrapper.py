"""Synchronous wrapper around async MCP SDK for REPL integration."""

import asyncio
import logging
import os
import re
import subprocess
import sys
from typing import Any, Optional
from contextlib import AsyncExitStack

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

logger = logging.getLogger(__name__)


class MCPClientWrapper:
    """Synchronous wrapper around MCP client sessions (call/list_tools/help)."""

    def __init__(self):
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stacks: dict[str, AsyncExitStack] = {}
        self._errlogs: dict[str, Any] = {}  # Track errlog file handles
        # Loop the sessions were created on. Sessions (and their anyio
        # streams/tasks) are loop-affine: every await on them MUST run on
        # this loop. In v2 the callers are executor threads (the kernel
        # supervisor's RPC handlers), so _run_async routes coroutines back
        # to this loop with run_coroutine_threadsafe. Awaiting a session
        # from any other loop deadlocks (the historical mcp.help() hang).
        self._owner_loop: Optional[asyncio.AbstractEventLoop] = None
        # Connection failures by server name (reason string), for help()/servers
        self.failed: dict[str, str] = {}

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
        return ['servers', 'failed', 'call', 'list_tools', 'help']

    def call(self, server: str, tool: str, *, timeout: float = 60.0, **kwargs) -> Any:
        """
        Call a tool on a connected server.

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

    def _run_async(self, coro, timeout: Optional[float] = 30.0):
        """
        Run an async coroutine from sync code.

        Two supported contexts:
        - Sessions connected (owner loop running) and we're on a different
          thread: schedule onto the owner loop thread-safely. This is the
          normal v2 path (kernel RPC handlers run in executor threads).
        - No owner loop yet (nothing connected): run on a private loop —
          covers introspection like list_tools() before any connect.

        Calling from the owner loop's own thread is a programming error
        (it would deadlock) and raises immediately.
        """
        owner = self._owner_loop
        if owner is not None and owner.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is owner:
                raise RuntimeError(
                    "mcp bridge: sync call on the owner event loop thread "
                    "would deadlock — await the async API instead"
                )
            future = asyncio.run_coroutine_threadsafe(coro, owner)
            try:
                return future.result(timeout=timeout)
            except TimeoutError:
                future.cancel()
                raise TimeoutError(
                    f"MCP bridge call timed out after {timeout}s"
                ) from None

        # Nothing connected yet: private loop is safe (no loop-affine state)
        return asyncio.run(coro)

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
            output += "\nCall tools via mcp.call('server', 'tool', **args)"
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
