"""Synchronous wrapper around async MCP SDK for REPL integration."""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from .models import ServerConfig
from .mcp_config import (
    DiscoveredServer,
    DiscoveryResult,
    expand_config,
    expand_value,
    format_registry,
    spawn_env,
)

# Hard ceiling for an on-demand connect. A server's own `timeout_s` may be
# larger, but the kernel child only waits `call timeout + 45s` for the whole
# connect+call round trip — a 90s connect would blow that budget and leave the
# child reporting a bogus "no reply from parent".
MAX_ON_DEMAND_CONNECT_S = 30.0


def _expand_env_value(value: str) -> str:
    """
    Expand ${VAR} / ${VAR:-default} references anywhere in the string
    (Claude Code .mcp.json semantics, e.g. "Bearer ${MY_TOKEN}").

    Unset vars without a default expand to "" with a warning.
    """
    unresolved: list[str] = []
    expanded = expand_value(value, unresolved=unresolved)
    for var in unresolved:
        logger.warning("Env var %s referenced in config but not set", var)
    return expanded


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
    """
    Synchronous wrapper around MCP client sessions (call/list_tools/help).

    Holds a *registry* of every discovered server (see `mcp_config`) and
    connects them ONE AT A TIME, on demand: naming a server in `mcp.call()` is
    what spawns it. With ~10 servers configured across scopes, connecting them
    all up front would cost ~10 child processes and tens of seconds that most
    sessions never need.
    """

    def __init__(self, *, connect_timeout_s: float = 20.0, negative_ttl_s: float = 60.0):
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

        # Discovered-but-not-connected servers
        self._registry: Optional[DiscoveryResult] = None
        self._extra: dict[str, DiscoveredServer] = {}  # registered at runtime
        # In-flight connects, shared between concurrent waiters
        self._connect_tasks: dict[str, asyncio.Task] = {}
        # Failed connects are remembered for a while: without this, a loop like
        # `for x in items: mcp.call('flaky', ...)` pays the full connect timeout
        # on every iteration.
        self._negative: dict[str, float] = {}
        self._connect_timeout_s = connect_timeout_s
        self._negative_ttl_s = negative_ttl_s

    # -- registry ---------------------------------------------------------

    def set_registry(self, result: DiscoveryResult) -> None:
        """Install the discovered-server registry (connects nothing)."""
        self._registry = result

    def register_raw(
        self,
        servers: dict[str, dict],
        *,
        scope: str = "project",
        origin: Optional[Path] = None,
    ) -> None:
        """Register server configs directly (tests, `--config`, embedders)."""
        for name, config in servers.items():
            self._extra[name] = DiscoveredServer(
                name=name,
                config=dict(config),
                scope=scope,  # type: ignore[arg-type]
                origin=origin or Path("<runtime>"),
            )

    def _known(self) -> dict[str, DiscoveredServer]:
        known: dict[str, DiscoveredServer] = {}
        if self._registry is not None:
            known.update(self._registry.servers)
        known.update(self._extra)
        return known

    def _registry_view(self) -> DiscoveryResult:
        return DiscoveryResult(
            servers=self._known(),
            excluded=dict(self._registry.excluded) if self._registry else {},
            sources=self._registry.sources if self._registry else (),
        )

    def resolve(self, name: str) -> Optional[DiscoveredServer]:
        """Look up a discovered server by bare or qualified name."""
        return self._known().get(name)

    @property
    def available(self) -> list[str]:
        """Every discovered server name, connected or not."""
        return sorted(self._known())

    @property
    def servers(self) -> list[str]:
        """
        Available server names (Python property, not tool call).

        These are *discovered*, not necessarily connected — a server starts on
        its first `mcp.call()`. Connection status is shown by `mcp.help()`.

        Example:
            >>> mcp.servers
            ['github', 'railway', 'telegram-mcp']
        """
        return self.available

    @property
    def connected(self) -> list[str]:
        """Server names with a live session."""
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

    def _pin_loop(self) -> None:
        """
        Bind sessions to the loop creating them — once.

        Sessions and their anyio streams are loop-affine. Blindly reassigning
        `_owner_loop` on a later connect would orphan the earlier sessions and
        re-introduce the historical `mcp.help()` hang, so a second *running*
        loop is a hard error. Rebinding is allowed only once the previous loop
        is gone (fresh loop per test, server restart).
        """
        loop = asyncio.get_running_loop()
        owner = self._owner_loop
        if owner is None or owner is loop or not owner.is_running():
            self._owner_loop = loop
            return
        raise RuntimeError(
            "mcp bridge: sessions are pinned to another running event loop — "
            "connect from the loop that owns the bridge"
        )

    async def _connect_server(self, name: str, config: ServerConfig) -> bool:
        """
        Connect to a single MCP server.

        `config` must already be `${VAR}`-expanded (see `_connect_entry`).
        """
        try:
            exit_stack = AsyncExitStack()
            self._exit_stacks[name] = exit_stack

            if config.transport_type == "stdio":
                # Inherited env + the entry's own vars + the recursion guard
                env = spawn_env({"env": config.env or {}})

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
                headers = dict(config.headers) if config.headers else None

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

    async def _cleanup_partial(self, name: str) -> None:
        """Tear down a half-built transport (timeout / cancelled connect)."""
        if name in self._exit_stacks:
            try:
                await self._exit_stacks[name].aclose()
            except BaseException as e:
                logger.debug("Error closing partial connect for %s: %s", name, e)
            self._exit_stacks.pop(name, None)
        if name in self._errlogs:
            try:
                self._errlogs[name].close()
            except Exception:
                pass
            del self._errlogs[name]

    async def _connect_entry(
        self, name: str, entry: DiscoveredServer, timeout_s: Optional[float]
    ) -> bool:
        """Expand, validate and connect one registry entry. Never raises."""
        overrides = (
            {"CLAUDE_PLUGIN_ROOT": str(entry.plugin_root)} if entry.plugin_root else None
        )
        expanded, unresolved = expand_config(entry.config, overrides=overrides)
        if unresolved:
            # An empty command would exec nothing and surface as a confusing
            # FileNotFoundError — report the real cause instead.
            self.failed[name] = "unset env var(s): " + ", ".join(unresolved)
            self._negative[name] = time.monotonic() + self._negative_ttl_s
            return False

        try:
            config = ServerConfig(**expanded)
        except Exception as e:
            self.failed[name] = f"invalid config: {type(e).__name__}: {e}"
            self._negative[name] = time.monotonic() + self._negative_ttl_s
            return False

        if timeout_s is not None:
            effective = timeout_s
        else:
            effective = config.timeout_s if config.timeout_s is not None else self._connect_timeout_s
            effective = min(effective, MAX_ON_DEMAND_CONNECT_S)

        try:
            ok = await asyncio.wait_for(
                self._connect_server(name, config), timeout=effective
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out connecting to %s after %.1fs", name, effective)
            self.failed[name] = f"timed out after {effective:.0f}s"
            await self._cleanup_partial(name)
            ok = False
        except asyncio.CancelledError:
            await self._cleanup_partial(name)
            raise
        except Exception as e:
            reason = _failure_reason(e)
            logger.warning("Failed to connect to %s: %s", name, reason)
            logger.debug("Connect failure for %s", name, exc_info=e)
            self.failed[name] = reason
            ok = False

        if ok:
            self._negative.pop(name, None)
        else:
            self.failed.setdefault(name, "connection failed (see server stderr log)")
            self._negative[name] = time.monotonic() + self._negative_ttl_s
        return ok

    async def ensure_connected_async(
        self,
        name: str,
        *,
        force: bool = False,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """
        Connect one server if it isn't already. Idempotent and concurrency-safe.

        Must be awaited on the owner event loop (the supervisor's RPC handler
        does exactly that) — sessions are loop-affine.

        Args:
            name: bare or qualified server name from the registry
            force: retry even if a recent connect failed
            timeout_s: explicit connect timeout (otherwise the entry's own
                `timeout_s`, clamped to MAX_ON_DEMAND_CONNECT_S)

        Returns:
            True when a live session exists; False with `self.failed[name]` set.
        """
        self._pin_loop()

        if name in self._sessions:
            return True

        if not force:
            until = self._negative.get(name)
            if until is not None and until > time.monotonic():
                return False
        else:
            self._negative.pop(name, None)

        task = self._connect_tasks.get(name)
        if task is None or task.done():
            entry = self.resolve(name)
            if entry is None:
                self.failed[name] = "not configured"
                return False
            task = asyncio.ensure_future(self._connect_entry(name, entry, timeout_s))
            self._connect_tasks[name] = task

        # shield: one waiter giving up (cell interrupted) must not cancel the
        # connect for the others sharing it
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._connect_tasks.pop(name, None)

    async def connect_async(
        self,
        servers: dict[str, dict],
        *,
        timeout_s: float = 15.0,
        sequential: bool = False,
    ) -> dict[str, bool]:
        """
        Register and eagerly connect a set of servers.

        Kept for tests, embedders and the explicit "connect everything" path
        (`mcp.list_tools()` with no argument). Normal operation goes through
        `ensure_connected_async` instead.

        Args:
            servers: Dict mapping server names to config dicts
            timeout_s: Per-server connect timeout
            sequential: If True, connect one-by-one (useful to reduce load)

        Returns:
            Dict mapping server names to connection success status (True/False)
        """
        self._pin_loop()
        self.register_raw(servers)

        results: dict[str, bool] = {}

        if sequential:
            for name in servers:
                results[name] = await self.ensure_connected_async(
                    name, force=True, timeout_s=timeout_s
                )
            return results

        async def one(name: str) -> tuple[str, bool]:
            return name, await self.ensure_connected_async(
                name, force=True, timeout_s=timeout_s
            )

        for coro in asyncio.as_completed([one(name) for name in servers]):
            name, ok = await coro
            results[name] = ok
        return results

    async def connect_all_async(self, *, timeout_s: Optional[float] = None) -> dict[str, bool]:
        """Connect every discovered server in parallel (the expensive path)."""
        names = self.available

        async def one(name: str) -> tuple[str, bool]:
            return name, await self.ensure_connected_async(name, timeout_s=timeout_s)

        results: dict[str, bool] = {}
        for coro in asyncio.as_completed([one(name) for name in names]):
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

    def _unreachable(self, server: str) -> str:
        """Actionable message for a server that has no live session."""
        if server in self.failed:
            return f"Server '{server}' failed to connect: {self.failed[server]}"
        if self.resolve(server) is not None:
            return f"Server '{server}' is not connected yet"
        available = self.available
        listing = ", ".join(available) if available else "(none discovered)"
        return (
            f"Server '{server}' is not configured. Available: {listing}. "
            "Run print(mcp.help()) to see scopes."
        )

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
        if server is None:
            # Registry overview — connects nothing, so an agent can discover
            # that e.g. telegram-mcp exists before paying to start it.
            counts = {
                name: len(tools) for name, tools in self.list_tools().items()
            } if self._sessions else {}
            return format_registry(
                self._registry_view(),
                connected=self.connected,
                failed=self.failed,
                tool_counts=counts,
            )

        elif tool is None:
            # Show all tools for a server
            if server not in self._sessions:
                return self._unreachable(server)

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
                return self._unreachable(server)

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
            raise ValueError(self._unreachable(server))

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

    def _nothing_to_disconnect(self, server: Optional[str]) -> bool:
        if server is not None:
            return server not in self._sessions and server not in self._connect_tasks
        return not self._sessions and not self._connect_tasks

    async def disconnect_async(self, server: Optional[str] = None) -> None:
        """
        Close sessions from the owner loop.

        The sync `disconnect()` cannot be used from inside a running loop (the
        server's own shutdown path) — it would try `asyncio.run()` there.
        """
        if self._nothing_to_disconnect(server):
            return
        await self._disconnect_impl(server)

    def disconnect(self, server: Optional[str] = None) -> None:
        """
        Disconnect from server(s).

        Args:
            server: Specific server to disconnect, or None to disconnect all
        """
        # Nothing open: stay a no-op rather than spinning up a loop. A session
        # that never touched the bridge must still shut down cleanly.
        if self._nothing_to_disconnect(server):
            return
        self._run_async(self._disconnect_impl(server))

    async def _disconnect_impl(self, server: Optional[str] = None) -> None:
        async def disconnect_all():
            # Cancel in-flight connects first: closing an exit stack while
            # enter_async_context is still running races the transport setup.
            pending = (
                [self._connect_tasks[server]] if server and server in self._connect_tasks
                else list(self._connect_tasks.values()) if not server
                else []
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if server:
                self._connect_tasks.pop(server, None)
            else:
                self._connect_tasks.clear()

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

        await disconnect_all()

    def __del__(self):
        """Cleanup on deletion."""
        if self._sessions:
            try:
                self.disconnect()
            except Exception:
                pass
