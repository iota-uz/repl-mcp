"""
Kernel child process: owns the REPL namespace and engine.

Plain synchronous process — no event loop of its own at the top level (async
cells run on a per-cell loop inside the engine via asyncio.run). The parent
supervisor talks to it over two pipe Connections:

  control: EXECUTE/RESET/PING/SHUTDOWN in, RESULT/RESET_OK/PONG out
  rpc:     the in-child `mcp` proxy sends MCP_* requests and blocks for
           MCP_REPLY while a cell is mid-execution (parent services these
           on an independent task, so no deadlock)

Interrupt model (Jupyter-style): parent sends SIGINT on timeout. The handler
raises KeyboardInterrupt only while a cell is executing — the engine catches
it and returns a clean "interrupted" result with namespace state preserved.
While idle, SIGINT is ignored (it must not kill the control loop).
"""

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .protocol import (
    EXECUTE, RESULT, RESET, RESET_OK, PING, PONG, SHUTDOWN,
    MCP_CALL, MCP_LIST, MCP_HELP, MCP_STATE, MCP_REPLY,
    Frame, ensure_json, next_id,
)

# Set while a cell is executing; gates the SIGINT → KeyboardInterrupt raise.
_EXECUTING = threading.Event()


def _sigint_handler(signum, frame):
    if _EXECUTING.is_set():
        raise KeyboardInterrupt
    # Idle: swallow — the control loop must survive stray interrupts.


class ServerList(list):
    """
    Available MCP servers.

    A plain list for every practical purpose (membership, iteration, indexing)
    — only the repr differs, because the REPL echoes return values and a bare
    `['github', 'telegram-mcp']` reads as "these are running". They are
    *discovered*; naming one in mcp.call() is what starts it.
    """

    def __init__(self, names, connected=()):
        super().__init__(names)
        self.connected = list(connected)

    def __repr__(self) -> str:
        base = super().__repr__()
        if not self:
            return base
        # Angle-bracket form on purpose: a trailing "# comment" would produce
        # nonsense once this is nested in a tuple/dict repr.
        live = "all" if all(n in self.connected for n in self) else repr(self.connected)
        return f"<available: {base} | live: {live}>"


class ChildMcpProxy:
    """
    The `mcp` object inside the kernel child.

    Every operation is an RPC to the parent (where the real MCP sessions
    live). Calls block the executing cell until the parent replies; a SIGINT
    during the wait surfaces as KeyboardInterrupt (interrupt-safe poll loop)
    and the stale reply is drained before the next request.
    """

    def __init__(self, rpc_conn):
        self._rpc = rpc_conn
        self._lock = threading.Lock()

    # -- public surface -------------------------------------------------

    def call(self, server: str, tool: str, *, timeout: float = 60.0, **kwargs) -> Any:
        """
        Call a tool on a connected MCP server.

        Example:
            >>> mcp.call('github', 'create_issue', owner='me', repo='proj', title='Bug')
        """
        ensure_json(kwargs, what="mcp.call() arguments")
        return self._request(
            MCP_CALL,
            {"server": server, "tool": tool, "kwargs": kwargs, "timeout": timeout},
            # Parent-side margin: on-demand connect (clamped to 30s) + the call
            # itself. Must exceed the parent's own budget or the child gives up
            # first and reports a bogus "no reply from parent".
            wait_s=timeout + 45.0,
        )

    @property
    def servers(self) -> list:
        """
        Available server names (discovered across all config scopes).

        Discovered, not necessarily connected — a server starts on its first
        mcp.call(). See mcp.help() for per-server status.
        """
        state = self._request(MCP_STATE, {}, wait_s=70.0)
        return ServerList(state["servers"], state.get("connected") or [])

    @property
    def failed(self) -> dict:
        """Connection failures by server name (reason strings)."""
        return self._request(MCP_STATE, {}, wait_s=70.0)["failed"]

    def list_tools(self, server: Optional[str] = None) -> dict:
        """Dict mapping server names to lists of tool names."""
        return self._request(MCP_LIST, {"server": server}, wait_s=70.0)

    def help(self, server: Optional[str] = None, tool: Optional[str] = None) -> str:
        """Help text for servers/tools. Connects lazily on first use."""
        return self._request(MCP_HELP, {"server": server, "tool": tool}, wait_s=70.0)

    def __dir__(self):
        return ["servers", "failed", "call", "list_tools", "help"]

    def __repr__(self):
        # Naming the servers here is the cheapest way for an agent that just
        # types `mcp` to learn what it can reach. Best-effort: a bridge that
        # can't answer must still have a usable repr.
        try:
            state = self._request(MCP_STATE, {}, wait_s=5.0)
            names = ", ".join(state.get("servers") or []) or "none discovered"
        except Exception:
            names = "see mcp.servers"
        return (
            f"<mcp bridge → {names} | mcp.call(server, tool, **args) starts one "
            "on demand; print(mcp.help()) for scope + status>"
        )

    # -- plumbing --------------------------------------------------------

    def _request(self, kind: str, payload: dict, *, wait_s: float) -> Any:
        with self._lock:
            # Drain stale replies left over from interrupted calls
            while self._rpc.poll(0):
                try:
                    self._rpc.recv()
                except EOFError:
                    raise RuntimeError("mcp bridge: parent connection closed")

            fid = next_id("rpc")
            self._rpc.send(Frame(kind, fid, payload).to_wire())

            deadline = time.monotonic() + wait_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"mcp bridge: no reply from parent after {wait_s:.0f}s"
                    )
                # Short poll slices keep SIGINT delivery prompt (interrupt-safe)
                if self._rpc.poll(min(0.2, remaining)):
                    reply = Frame.from_wire(self._rpc.recv())
                    if reply.kind != MCP_REPLY or reply.id != fid:
                        continue  # stale frame from an interrupted call — drop
                    if reply.payload.get("ok"):
                        return reply.payload.get("value")
                    raise RuntimeError(reply.payload.get("error", "mcp bridge error"))


def child_serve(control_conn, rpc_conn, workspace_root: str) -> None:
    """Child entrypoint: serve EXECUTE requests until SHUTDOWN or parent EOF."""
    # Protect the parent's stdio JSON-RPC channel: anything the cell (or a C
    # library) writes to fd 1 must NOT reach the MCP transport. Route the
    # child's real stdout to stderr; Python-level prints are captured by the
    # engine first anyway.
    try:
        os.dup2(2, 1)
        sys.stdout = sys.stderr
    except OSError:
        pass

    signal.signal(signal.SIGINT, _sigint_handler)

    # Import here (not module top) so the engine import cost is paid in the
    # child, after spawn.
    from ..repl_engine import REPLEngine
    from ..models import ExecutionResult, ExceptionInfo

    engine = REPLEngine(
        mcp_wrapper=ChildMcpProxy(rpc_conn),
        workspace_root=Path(workspace_root),
    )

    while True:
        try:
            frame = Frame.from_wire(control_conn.recv())
        except (EOFError, OSError):
            return  # parent gone
        except KeyboardInterrupt:
            continue  # stray interrupt while idle

        if frame.kind == SHUTDOWN:
            return

        if frame.kind == PING:
            control_conn.send(Frame(PONG, frame.id).to_wire())
            continue

        if frame.kind == RESET:
            engine.reset_namespace()
            control_conn.send(Frame(RESET_OK, frame.id).to_wire())
            continue

        if frame.kind == EXECUTE:
            code = frame.payload.get("code", "")
            timeout = float(frame.payload.get("timeout", 120.0))
            if frame.payload.get("reset"):
                engine.reset_namespace()

            _EXECUTING.set()
            try:
                result = engine.execute(code, timeout=timeout)
            except KeyboardInterrupt:
                # SIGINT landed outside the engine's own handler window
                result = ExecutionResult(
                    success=False,
                    exception=ExceptionInfo(
                        type="KeyboardInterrupt",
                        message=(
                            "execution interrupted. Namespace state up to "
                            "the interrupt is preserved."
                        ),
                        traceback="KeyboardInterrupt\n",
                    ),
                    execution_time_ms=0.0,
                )
            except BaseException as e:  # engine must never kill the child
                result = ExecutionResult(
                    success=False,
                    exception=ExceptionInfo(
                        type=type(e).__name__,
                        message=str(e),
                        traceback=f"{type(e).__name__}: {e}\n",
                    ),
                    execution_time_ms=0.0,
                )
            finally:
                _EXECUTING.clear()

            try:
                control_conn.send(
                    Frame(RESULT, frame.id, result.model_dump(mode="json")).to_wire()
                )
            except (BrokenPipeError, OSError):
                return  # parent gone mid-result
