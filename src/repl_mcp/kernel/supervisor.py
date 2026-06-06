"""
Kernel supervisor: parent-side manager for the subprocess kernel.

Pure async — the MCP server's event loop is never blocked. Blocking pipe
reads run on small dedicated executor threads; everything else awaits.

Responsibilities:
  - spawn/respawn the child interpreter (crash isolation: a segfault or OOM
    in REPL code kills only the child; the MCP server survives)
  - REAL timeouts: SIGINT on timeout → KeyboardInterrupt in the running cell,
    namespace state preserved (Jupyter's interrupt model); hard kill + respawn
    only if the cell swallows the interrupt past a grace period
  - honest state semantics: `generation` bumps on every (re)spawn and the
    first result after a respawn carries a "variables cleared" warning
  - servicing child→parent MCP RPC (the in-child `mcp` proxy) against the
    parent-side MCPClientWrapper, with lazy autoconnect on first use
"""

import asyncio
import concurrent.futures
import logging
import multiprocessing
import os
import signal
from pathlib import Path
from typing import Any, Callable, Optional

from ..models import ExecutionResult, ExceptionInfo, WarningInfo
from .protocol import (
    EXECUTE, RESULT, RESET_OK, PONG, SHUTDOWN,
    MCP_CALL, MCP_LIST, MCP_HELP, MCP_STATE, MCP_REPLY,
    Frame, next_id,
)
from .child_main import child_serve

logger = logging.getLogger(__name__)

_MP = multiprocessing.get_context("spawn")


def _swallow_future_exception(fut) -> None:
    if not fut.cancelled():
        fut.exception()  # mark retrieved


class KernelSupervisor:
    """Owns the kernel child process and the IPC channels to it."""

    def __init__(
        self,
        mcp_wrapper: Optional[Any] = None,
        workspace_root: Optional[Path] = None,
        *,
        interrupt_grace_s: float = 3.0,
        connect_servers: Optional[Callable] = None,
    ):
        """
        Args:
            mcp_wrapper: parent-side MCPClientWrapper serving the child's
                `mcp` proxy (None disables the bridge)
            workspace_root: cwd for the child's sh() helper
            interrupt_grace_s: how long to wait after SIGINT before the
                hard kill + respawn
            connect_servers: optional async callable invoked once before the
                first MCP RPC is served (lazy autoconnect hook)
        """
        self._mcp = mcp_wrapper
        self._workspace_root = workspace_root or Path.cwd()
        self._grace_s = interrupt_grace_s
        self._connect_servers = connect_servers
        self._connect_once: Optional[asyncio.Task] = None

        self._proc: Optional[multiprocessing.process.BaseProcess] = None
        self._control = None  # parent end of control pipe
        self._rpc = None      # parent end of rpc pipe
        self._rpc_task: Optional[asyncio.Task] = None
        self._exec_lock = asyncio.Lock()
        # Dedicated 1-thread executors so a blocked control recv can't starve
        # rpc servicing (and vice versa)
        self._control_reader = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kernel-control"
        )
        self._rpc_reader = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kernel-rpc"
        )

        self.generation = 0
        self._notice_pending = False  # warn on first result after a respawn

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Spawn the kernel child and start servicing its MCP RPC channel."""
        await self._spawn()

    async def _spawn(self) -> None:
        parent_control, child_control = _MP.Pipe(duplex=True)
        parent_rpc, child_rpc = _MP.Pipe(duplex=True)

        proc = _MP.Process(
            target=child_serve,
            args=(child_control, child_rpc, str(self._workspace_root)),
            daemon=True,
            name="repl-kernel",
        )
        # Spawning blocks briefly (fresh interpreter): keep the loop free
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, proc.start)
        child_control.close()
        child_rpc.close()

        self._proc = proc
        self._control = parent_control
        self._rpc = parent_rpc
        self.generation += 1
        if self.generation > 1:
            self._notice_pending = True

        if self._rpc_task is None or self._rpc_task.done():
            self._rpc_task = asyncio.create_task(self._serve_rpc())
        logger.info("Kernel child started (pid=%s, generation=%d)",
                    proc.pid, self.generation)

    async def shutdown(self) -> None:
        """Stop the child and the RPC service."""
        if self._control is not None:
            try:
                self._control.send(Frame(SHUTDOWN, next_id("ctl")).to_wire())
            except (BrokenPipeError, OSError):
                pass
        await self._reap(timeout=2.0)
        if self._rpc_task is not None:
            self._rpc_task.cancel()
        self._control_reader.shutdown(wait=False)
        self._rpc_reader.shutdown(wait=False)

    async def restart(self) -> None:
        """Hard restart: kill the child and respawn (state is lost)."""
        await self._reap(timeout=0.5, kill=True)
        await self._spawn()

    async def _reap(self, timeout: float, kill: bool = False) -> None:
        proc = self._proc
        if proc is None:
            return
        loop = asyncio.get_running_loop()
        if kill and proc.is_alive():
            proc.kill()
        await loop.run_in_executor(None, proc.join, timeout)
        if proc.is_alive():
            proc.kill()
            await loop.run_in_executor(None, proc.join, 1.0)
        for conn in (self._control, self._rpc):
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        self._proc = None
        self._control = None
        self._rpc = None

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    # -- execution ---------------------------------------------------------

    async def execute(
        self,
        code: str,
        *,
        timeout: float = 120.0,
        reset: bool = False,
    ) -> ExecutionResult:
        """
        Run one cell in the kernel child.

        Timeout semantics: the parent waits `timeout` + margin, then SIGINTs
        the child (KeyboardInterrupt in the cell; state survives). If the
        child doesn't return within the grace period, it is killed and
        respawned (state lost, reported via a kernel_restarted warning).
        """
        if not (isinstance(timeout, (int, float)) and timeout > 0):
            return ExecutionResult(
                success=False,
                exception=ExceptionInfo(
                    type="ValueError",
                    message=f"timeout must be a positive number of seconds (got {timeout!r})",
                    traceback="",
                    hints=["Call again with a positive timeout, e.g. timeout=120"],
                ),
                execution_time_ms=0.0,
            )

        async with self._exec_lock:
            if not self._alive():
                await self._respawn_after_crash()

            fid = next_id("exec")
            try:
                self._control.send(Frame(EXECUTE, fid, {
                    "code": code, "timeout": timeout, "reset": bool(reset),
                }).to_wire())
            except (BrokenPipeError, OSError):
                await self._respawn_after_crash()
                return self._kernel_died_result("kernel was not reachable; restarted")

            interrupted = False
            # One pending recv reused across both waits: a timed-out wait_for
            # must NOT abandon the executor read (the RESULT frame would be
            # consumed by a dead future) — shield keeps it awaitable.
            pending = asyncio.ensure_future(self._recv_control())
            # If we abandon the read (restart paths), the eventual EOFError
            # must not surface as an "exception never retrieved" warning.
            pending.add_done_callback(_swallow_future_exception)
            try:
                # +1s margin: async cells self-cancel at `timeout` inside the
                # engine; this outer wait should only fire for sync runaways.
                wire = await asyncio.wait_for(
                    asyncio.shield(pending), timeout=timeout + 1.0
                )
            except asyncio.TimeoutError:
                interrupted = True
                self._interrupt()
                try:
                    wire = await asyncio.wait_for(
                        asyncio.shield(pending), timeout=self._grace_s
                    )
                except asyncio.TimeoutError:
                    # Cell swallowed the interrupt: kill + respawn
                    await self.restart()
                    return self._kernel_died_result(
                        f"execution exceeded timeout={timeout:.0f}s and "
                        f"ignored the interrupt for {self._grace_s:.0f}s — "
                        "kernel was killed and restarted"
                    )
            except (EOFError, OSError):
                # Child crashed mid-cell (segfault/OOM/os._exit)
                await self._respawn_after_crash()
                return self._kernel_died_result(
                    "kernel crashed during execution (restarted)"
                )

            frame = Frame.from_wire(wire)
            result = ExecutionResult(**frame.payload)

            # Only annotate results that actually show the interrupt — a cell
            # that finished in the SIGINT race (or caught it and succeeded)
            # must not carry a bogus "was interrupted" warning.
            if interrupted and not result.success:
                result.warnings.append(WarningInfo(
                    category="timeout_interrupt",
                    message=(
                        f"Execution exceeded timeout={timeout:.0f}s and was "
                        "interrupted; namespace state up to the interrupt is "
                        "preserved"
                    ),
                ))
            self._attach_restart_notice(result)
            return result

    def _interrupt(self) -> None:
        if self._proc is not None and self._proc.pid and self._alive():
            try:
                os.kill(self._proc.pid, signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass

    async def _recv_control(self):
        loop = asyncio.get_running_loop()
        conn = self._control

        def _recv():
            return conn.recv()

        return await loop.run_in_executor(self._control_reader, _recv)

    async def _respawn_after_crash(self) -> None:
        exit_code = self._proc.exitcode if self._proc is not None else None
        logger.warning("Kernel child died (exitcode=%s) — respawning", exit_code)
        await self._reap(timeout=0.5, kill=True)
        await self._spawn()

    def _kernel_died_result(self, message: str) -> ExecutionResult:
        result = ExecutionResult(
            success=False,
            exception=ExceptionInfo(
                type="KernelRestarted",
                message=f"{message}. All variables were cleared.",
                traceback=f"KernelRestarted: {message}\n",
                hints=["Re-run any setup code (imports, variables) before continuing"],
            ),
            execution_time_ms=0.0,
        )
        # The death itself reports the state loss; don't double-notice next call
        self._notice_pending = False
        return result

    def _attach_restart_notice(self, result: ExecutionResult) -> None:
        if self._notice_pending:
            self._notice_pending = False
            result.warnings.insert(0, WarningInfo(
                category="kernel_restarted",
                message=(
                    "Kernel was restarted since the previous call — all "
                    "variables were cleared. Re-run setup code if needed."
                ),
            ))

    # -- MCP RPC service ----------------------------------------------------

    async def _serve_rpc(self) -> None:
        """Forward the child's mcp.* requests to the parent-side wrapper."""
        loop = asyncio.get_running_loop()
        while True:
            conn = self._rpc
            if conn is None:
                await asyncio.sleep(0.1)
                continue
            try:
                wire = await loop.run_in_executor(self._rpc_reader, conn.recv)
            except (EOFError, OSError):
                # Child died or pipe replaced on respawn — wait for new pipe
                await asyncio.sleep(0.1)
                continue
            except asyncio.CancelledError:
                return
            try:
                frame = Frame.from_wire(wire)
            except ValueError:
                continue
            # Service without blocking the read loop
            asyncio.create_task(self._handle_rpc(conn, frame))

    async def _handle_rpc(self, conn, frame: Frame) -> None:
        loop = asyncio.get_running_loop()
        try:
            if self._mcp is None:
                raise RuntimeError("mcp bridge is not available in this server")

            await self._ensure_connected()

            if frame.kind == MCP_CALL:
                p = frame.payload
                value = await loop.run_in_executor(None, lambda: self._mcp.call(
                    p["server"], p["tool"], timeout=float(p.get("timeout", 60.0)),
                    **(p.get("kwargs") or {}),
                ))
            elif frame.kind == MCP_LIST:
                server = frame.payload.get("server")
                value = await loop.run_in_executor(
                    None, lambda: self._mcp.list_tools(server)
                )
            elif frame.kind == MCP_HELP:
                p = frame.payload
                value = await loop.run_in_executor(
                    None, lambda: self._mcp.help(p.get("server"), p.get("tool"))
                )
            elif frame.kind == MCP_STATE:
                value = {
                    "servers": list(self._mcp.servers),
                    "failed": dict(self._mcp.failed),
                }
            else:
                raise RuntimeError(f"unsupported rpc kind: {frame.kind}")

            reply = {"ok": True, "value": value}
        except Exception as e:
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        try:
            conn.send(Frame(MCP_REPLY, frame.id, reply).to_wire())
        except (BrokenPipeError, OSError):
            pass  # child died while we serviced the call

    async def _ensure_connected(self) -> None:
        """Lazy autoconnect: run the connect hook once, on first MCP use."""
        if self._connect_servers is None:
            return
        if self._connect_once is None:
            self._connect_once = asyncio.create_task(self._connect_servers())
        try:
            await asyncio.shield(self._connect_once)
        except Exception as e:
            logger.warning("Lazy MCP autoconnect failed: %s", e)
