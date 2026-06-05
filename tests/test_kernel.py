"""
Subprocess-kernel acceptance tests: state round-trips, REAL timeouts via
SIGINT (state survives), grace-kill + restart notice, crash isolation, reset.
"""

import asyncio

import pytest
import pytest_asyncio

from repl_mcp.kernel.supervisor import KernelSupervisor

GUARD_S = 30  # outer guard for any single kernel operation in tests


@pytest_asyncio.fixture
async def kernel():
    sup = KernelSupervisor(mcp_wrapper=None)
    await asyncio.wait_for(sup.start(), timeout=GUARD_S)
    yield sup
    await asyncio.wait_for(sup.shutdown(), timeout=GUARD_S)


async def run(sup, code, **kw):
    return await asyncio.wait_for(sup.execute(code, **kw), timeout=GUARD_S)


@pytest.mark.asyncio
class TestKernelExecution:
    async def test_round_trip_with_state(self, kernel):
        r1 = await run(kernel, "x = 42")
        assert r1.success
        r2 = await run(kernel, "x")
        assert r2.success
        assert r2.return_value == "42"

    async def test_stdout_crosses_ipc(self, kernel):
        r = await run(kernel, "print('hello from child')")
        assert r.success
        assert "hello from child" in r.stdout

    async def test_errors_cross_ipc_with_clean_traceback(self, kernel):
        r = await run(kernel, "1/0")
        assert not r.success
        assert r.exception.type == "ZeroDivisionError"
        assert "<repl>" in r.exception.traceback
        assert "repl_engine.py" not in r.exception.traceback

    async def test_top_level_await_in_child(self, kernel):
        r = await run(kernel, "import asyncio\nawait asyncio.sleep(0)\n'awaited'")
        assert r.success, r.exception
        assert r.return_value == "'awaited'"

    async def test_reset_clears_state(self, kernel):
        await run(kernel, "y = 7")
        r = await run(kernel, "'y' in dir()", reset=True)
        assert r.success
        assert r.return_value == "False"


@pytest.mark.asyncio
class TestKernelTimeout:
    async def test_sync_runaway_interrupted_state_survives(self, kernel):
        """THE headline fix: `while True: pass` is interruptible and the
        namespace survives the interrupt."""
        await run(kernel, "marker = 'alive'")

        r = await run(kernel, "while True: pass", timeout=1.0)
        assert not r.success
        assert r.exception.type == "KeyboardInterrupt"
        assert any(w.category == "timeout_interrupt" for w in r.warnings)

        # Same kernel process, state intact, no restart notice
        r2 = await run(kernel, "marker")
        assert r2.return_value == "'alive'"
        assert not any(w.category == "kernel_restarted" for w in r2.warnings)
        assert kernel.generation == 1

    async def test_interrupt_swallowed_triggers_restart(self, kernel):
        """A cell that ignores KeyboardInterrupt is killed after the grace
        period; restart is reported and the next call works on a fresh kernel."""
        kernel._grace_s = 1.0
        await run(kernel, "z = 1")

        code = (
            "import time\n"
            "while True:\n"
            "    try:\n"
            "        time.sleep(0.05)\n"
            "    except KeyboardInterrupt:\n"
            "        pass\n"
        )
        r = await run(kernel, code, timeout=1.0)
        assert not r.success
        assert r.exception.type == "KernelRestarted"
        assert "variables were cleared" in r.exception.message
        assert kernel.generation == 2

        r2 = await run(kernel, "'z' in dir()")
        assert r2.return_value == "False"


@pytest.mark.asyncio
class TestCrashIsolation:
    async def test_child_hard_exit_respawns(self, kernel):
        """os._exit in REPL code kills only the child; the supervisor
        respawns and reports the restart."""
        await run(kernel, "w = 5")

        r = await run(kernel, "import os; os._exit(13)")
        assert not r.success
        assert r.exception.type == "KernelRestarted"
        assert kernel.generation == 2

        r2 = await run(kernel, "'w' in dir()")
        assert r2.success
        assert r2.return_value == "False"

    async def test_segfault_respawns(self, kernel):
        """A C-level crash (abort) is isolated to the child."""
        r = await run(
            kernel,
            "import ctypes; ctypes.string_at(0)",  # NULL deref → SIGSEGV
        )
        assert not r.success
        assert r.exception.type == "KernelRestarted"

        r2 = await run(kernel, "1 + 1")
        assert r2.success
        assert r2.return_value == "2"

    async def test_restart_notice_on_first_call_after_respawn(self, kernel):
        """An explicit supervisor restart surfaces a variables-cleared warning
        on the next successful result."""
        await run(kernel, "v = 1")
        await asyncio.wait_for(kernel.restart(), timeout=GUARD_S)

        r = await run(kernel, "'fresh'")
        assert r.success
        assert any(w.category == "kernel_restarted" for w in r.warnings)
        # Notice fires once
        r2 = await run(kernel, "1")
        assert not any(w.category == "kernel_restarted" for w in r2.warnings)
