"""
Regression tests for the v2.0.x hardening fixes (explorative stress-test
findings): surrogate sanitization (server-wedge), exception size caps,
helper-shadow recovery, binary-safe sh(), timeout validation, error-display
dedup, repr guards, async/multiprocessing hints, stdout.buffer, head+tail
truncation.
"""

import asyncio
import json

import pytest
import pytest_asyncio

from repl_mcp.models import (
    ExecutionResult,
    ExceptionInfo,
    MAX_EXCEPTION_MESSAGE_SIZE,
    MAX_EXCEPTION_TRACEBACK_SIZE,
    utf8_safe,
)
from repl_mcp.repl_engine import REPLEngine
from repl_mcp.kernel.supervisor import KernelSupervisor
from repl_mcp.utilities.shell import sh, ShellResult

GUARD_S = 30


@pytest.fixture
def engine():
    return REPLEngine()


def assert_json_clean(result: ExecutionResult) -> None:
    """The invariant that prevents the stdio-writer wedge: every result must
    survive strict-UTF-8 JSON serialization (what pydantic_core does when the
    MCP response is written)."""
    result.model_dump_json().encode("utf-8")
    json.dumps(str(result)).encode("utf-8")


# -- Fix #1: surrogate sanitization (the server-wedge) ----------------------

class TestSurrogateSanitization:
    def test_utf8_safe_replaces_lone_surrogate(self):
        assert utf8_safe("ok \ud800 end") == "ok ? end"
        assert utf8_safe("clean") == "clean"

    def test_surrogate_in_stdout_is_sanitized(self, engine):
        r = engine.execute('print("x " + chr(0xD800) + " y")')
        assert r.success, r.exception
        assert "\ud800" not in r.stdout
        assert_json_clean(r)
        assert any(w.category == "encoding_sanitized" for w in r.warnings)

    def test_surrogate_in_return_value_repr(self, engine):
        r = engine.execute(
            "class S:\n"
            "    def __repr__(self): return 'sur ' + chr(0xD800) + ' rogate'\n"
            "S()"
        )
        assert r.success, r.exception
        assert "\ud800" not in (r.return_value or "")
        assert_json_clean(r)

    def test_surrogate_in_exception_message(self, engine):
        r = engine.execute('raise ValueError("bad " + chr(0xD800) + " char")')
        assert not r.success
        assert "\ud800" not in r.exception.message
        assert "\ud800" not in r.exception.traceback
        assert_json_clean(r)

    def test_escaped_surrogate_in_string_repr_is_untouched(self, engine):
        # repr() escapes the surrogate itself — nothing to sanitize, the
        # literal backslash-escape must survive
        r = engine.execute('"lone: \\ud800"')
        assert r.success
        assert "\\ud800" in r.return_value
        assert_json_clean(r)


# -- Fix #2: exception size caps ---------------------------------------------

class TestExceptionSizeCaps:
    def test_huge_exception_message_is_capped(self, engine):
        r = engine.execute("raise ValueError('E' * 1_000_000)")
        assert not r.success
        assert len(r.exception.message) <= MAX_EXCEPTION_MESSAGE_SIZE
        assert len(r.exception.traceback) <= MAX_EXCEPTION_TRACEBACK_SIZE
        assert "TRUNCATED" in r.exception.message
        # The rendered tool response must be bounded too
        assert len(str(r)) < 2 * (MAX_EXCEPTION_MESSAGE_SIZE + MAX_EXCEPTION_TRACEBACK_SIZE)

    def test_small_messages_untouched(self):
        info = ExceptionInfo(type="ValueError", message="short", traceback="tb")
        assert info.message == "short"


# -- Fix #3: helper shadowing -------------------------------------------------

class TestHelperShadowing:
    def test_shadow_warns_once(self, engine):
        r1 = engine.execute("sh = 'overwritten'")
        assert any(w.category == "helper_shadowed" for w in r1.warnings)
        r2 = engine.execute("1 + 1")
        assert not any(w.category == "helper_shadowed" for w in r2.warnings)

    def test_reset_restores_shadowed_helper(self, engine):
        original_sh = engine.globals["sh"]
        engine.execute("sh = 'overwritten'")
        assert engine.globals["sh"] == "overwritten"
        engine.reset_namespace()
        assert engine.globals["sh"] is original_sh
        r = engine.execute("sh('echo restored')")
        assert r.success, r.exception
        assert "restored" in r.return_value

    def test_rebinding_back_clears_warned_state(self, engine):
        engine.execute("_orig = sh; sh = 'x'")
        engine.execute("sh = _orig")
        r = engine.execute("sh = 'x again'")
        # Shadowing again after restoring should warn again
        assert any(w.category == "helper_shadowed" for w in r.warnings)


# -- Fix #6: binary-safe sh() -------------------------------------------------

class TestShBinarySafe:
    def test_binary_output_does_not_raise(self):
        r = sh("head -c 100 /bin/ls", check=False)
        assert isinstance(r, ShellResult)
        assert r.returncode == 0
        # Replacement character, never an exception, never a surrogate
        str(r).encode("utf-8")

    def test_invalid_utf8_byte_replaced(self):
        r = sh(r"printf 'a\377b'", check=False)
        assert str(r) == "a�b"
        assert r.ok

    def test_clean_utf8_untouched(self):
        assert str(sh("printf 'emoji 🐍'")) == "emoji 🐍"


# -- Fix #4: timeout validation ----------------------------------------------

@pytest.mark.asyncio
class TestTimeoutValidation:
    async def test_non_positive_timeout_is_clear_error(self):
        sup = KernelSupervisor(mcp_wrapper=None)  # validation runs pre-spawn
        for bad in (0, -5, -0.1):
            r = await sup.execute("1 + 1", timeout=bad)
            assert not r.success
            assert r.exception.type == "ValueError"
            assert "positive" in r.exception.message
            assert not any(w.category == "timeout_interrupt" for w in r.warnings)


# -- Fixes #7-#11: display dedup, repr guard, hints, stdout.buffer ------------

class TestErrorDisplay:
    def test_trivial_traceback_not_duplicated(self):
        # Shape produced by the kernel child's BaseException fallback
        # (e.g. sys.exit(3) in a cell)
        info = ExceptionInfo(type="SystemExit", message="3", traceback="SystemExit: 3\n")
        r = ExecutionResult(success=False, exception=info, execution_time_ms=0.0)
        assert str(r).count("SystemExit: 3") == 1

    def test_interrupt_message_not_duplicated(self):
        info = ExceptionInfo(
            type="KeyboardInterrupt",
            message="execution interrupted. Namespace state up to the interrupt is preserved.",
            traceback="KeyboardInterrupt\n",
        )
        r = ExecutionResult(success=False, exception=info, execution_time_ms=0.0)
        assert str(r).count("KeyboardInterrupt") == 1

    def test_real_traceback_still_shown(self, engine):
        r = engine.execute("def f():\n    return 1/0\nf()")
        out = str(r)
        assert "Traceback (most recent call last)" in out
        assert "ZeroDivisionError" in out

    def test_stdout_shown_on_error(self, engine):
        r = engine.execute("print('debug breadcrumb')\n1/0")
        out = str(r)
        assert "debug breadcrumb" in out
        assert "ZeroDivisionError" in out

    def test_warnings_shown_on_error(self):
        from repl_mcp.models import WarningInfo
        r = ExecutionResult(
            success=False,
            exception=ExceptionInfo(type="ValueError", message="x", traceback=""),
            execution_time_ms=0.0,
            warnings=[WarningInfo(category="c", message="warn me")],
        )
        assert "warn me" in str(r)


class TestReprGuard:
    def test_raising_repr_is_not_a_cell_failure(self, engine):
        r = engine.execute(
            "class EvilRepr:\n"
            "    def __repr__(self): raise ValueError('repr bomb')\n"
            "EvilRepr()"
        )
        assert r.success
        assert "repr() raised" in r.return_value
        assert any(w.category == "repr_failed" for w in r.warnings)


class TestUserRaisedKeyboardInterrupt:
    def test_user_message_preserved(self, engine):
        r = engine.execute("raise KeyboardInterrupt('my custom note')")
        assert not r.success
        assert r.exception.type == "KeyboardInterrupt"
        assert r.exception.message == "my custom note"


class TestAsyncAndMpHints:
    def test_event_loop_closed_hint(self, engine):
        r = engine.execute("raise RuntimeError('Event loop is closed')")
        assert not r.success
        assert any("event loop" in h.lower() for h in r.exception.hints)

    def test_cancelled_error_hint(self, engine):
        r = engine.execute("import asyncio\nraise asyncio.CancelledError()")
        assert not r.success
        assert r.exception.type == "CancelledError"
        assert any("event loop" in h.lower() for h in r.exception.hints)

    def test_daemonic_mp_hint(self, engine):
        r = engine.execute(
            "raise AssertionError('daemonic processes are not allowed to have children')"
        )
        assert not r.success
        assert any("ThreadPoolExecutor" in h for h in r.exception.hints)


class TestStdoutBuffer:
    def test_buffer_write_works(self, engine):
        r = engine.execute("import sys; sys.stdout.buffer.write(b'binary ok\\n'); 'done'")
        assert r.success, r.exception
        assert "binary ok" in r.stdout

    def test_buffer_invalid_bytes_replaced(self, engine):
        r = engine.execute("import sys; sys.stdout.buffer.write(b'\\xff\\xfe mark')")
        assert r.success, r.exception
        assert "mark" in r.stdout
        assert_json_clean(r)


# -- Fix #5: head+tail truncation ---------------------------------------------

class TestHeadTailTruncation:
    def test_tail_is_preserved(self, engine):
        r = engine.execute(
            "for i in range(30000):\n"
            "    print(f'line {i}')"
        )
        assert r.success
        assert r.stdout_info.truncated
        assert "line 0" in r.stdout            # head kept
        assert "line 29999" in r.stdout        # tail kept (the summary zone)
        assert "TRUNCATED" in r.stdout
        assert "chars omitted" in r.stdout
        assert len(r.stdout) <= 50_000 + 100

    def test_single_line_head_tail(self, engine):
        r = engine.execute("'A' * 10_000 + 'MID' + 'Z' * 30_000")
        assert r.success
        assert r.return_value.startswith("'A")
        assert r.return_value.rstrip("'").endswith("Z" * 10)
        assert "TRUNCATED" in r.return_value


# -- Through-kernel round trip: the original wedge scenario -------------------

@pytest_asyncio.fixture
async def kernel():
    sup = KernelSupervisor(mcp_wrapper=None)
    await asyncio.wait_for(sup.start(), timeout=GUARD_S)
    yield sup
    await asyncio.wait_for(sup.shutdown(), timeout=GUARD_S)


@pytest.mark.asyncio
class TestKernelSurrogateRoundTrip:
    async def test_surrogate_print_does_not_poison_the_response(self, kernel):
        r = await asyncio.wait_for(
            kernel.execute('print("wedge " + chr(0xD800) + " attempt")'),
            timeout=GUARD_S,
        )
        assert r.success
        assert_json_clean(r)

        # The server must still answer afterwards (pre-fix: permanent hang)
        r2 = await asyncio.wait_for(kernel.execute("'alive'"), timeout=GUARD_S)
        assert r2.success
        assert r2.return_value == "'alive'"
