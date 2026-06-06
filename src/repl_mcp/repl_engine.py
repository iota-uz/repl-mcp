"""Stateful REPL execution engine."""

import sys
import ast
import time
import asyncio
import inspect
import traceback
from io import StringIO
from pathlib import Path
from typing import Optional, Any
from contextlib import redirect_stdout, redirect_stderr

from .models import ExecutionResult, ExceptionInfo, TruncationInfo, WarningInfo


class _BufferProxy:
    """Bytes facade over a StringIO capture (sys.stdout.buffer compatibility)."""

    def __init__(self, sio: StringIO):
        self._sio = sio

    def write(self, data: bytes) -> int:
        self._sio.write(data.decode("utf-8", "replace"))
        return len(data)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        pass


class _CaptureIO(StringIO):
    """StringIO with a .buffer attribute, like a real text stdout.

    Libraries that write binary via sys.stdout.buffer (progress bars, image
    pipes) otherwise die with AttributeError mid-cell.
    """

    @property
    def buffer(self) -> _BufferProxy:
        return _BufferProxy(self)

# Reserved names that should be excluded from namespace output
# and preserved during reset
RESERVED_NAMES = frozenset({
    "__builtins__", "__name__", "__doc__", "__package__",
    "mcp", "sh",
})

# Output size limits (characters)
MAX_STDOUT_SIZE = 50000       # 50KB of text
MAX_STDERR_SIZE = 10000       # 10KB of errors
MAX_RETURN_VALUE_SIZE = 20000  # 20KB for repr()
MAX_NAMESPACE_VAR_SIZE = 500   # 500 chars per variable (increased from 100)

# Warning thresholds
WARN_OUTPUT_SIZE = 25000       # Warn if output > 25KB
WARN_EXECUTION_TIME_MS = 5000  # Warn if execution > 5 seconds
WARN_NAMESPACE_SIZE = 50       # Warn if > 50 variables

# Truncation suffix
TRUNCATION_SUFFIX = "\n... [TRUNCATED]"


def _smart_truncate(
    content: str,
    max_size: int,
    suffix: str = TRUNCATION_SUFFIX,
    tail_fraction: float = 0.25,
) -> tuple[str, TruncationInfo]:
    """
    Truncate content keeping the HEAD and the TAIL, dropping the middle.

    Agents print progress first and summaries last — head-only truncation
    discards the most valuable bytes. The omitted-count marker sits where
    the middle was cut. Line boundaries are preserved when possible.

    Args:
        content: String to truncate
        max_size: Maximum size in characters
        suffix: Marker label (kept for API compat; the rendered marker
                includes the omitted character count)
        tail_fraction: Share of the budget reserved for the tail

    Returns:
        Tuple of (truncated_content, truncation_info)
    """
    original_size = len(content)

    if original_size <= max_size:
        return content, TruncationInfo(
            truncated=False,
            original_size=original_size,
            truncated_size=original_size,
            truncation_type="hard",
        )

    marker = f"\n... [TRUNCATED — {original_size - max_size}+ chars omitted] ...\n"
    effective_max = max_size - len(marker)
    tail_budget = int(effective_max * tail_fraction)
    head_budget = effective_max - tail_budget

    truncation_type = "smart"
    if "\n" in content:
        # Head: whole lines from the start
        head_lines = []
        current = 0
        for line in content.split("\n"):
            line_size = len(line) + 1
            if current + line_size > head_budget:
                break
            head_lines.append(line)
            current += line_size
        head = "\n".join(head_lines)
        if not head:
            head = content[:head_budget]
            truncation_type = "hard"

        # Tail: whole lines from the end
        tail_lines = []
        current = 0
        for line in reversed(content.split("\n")):
            line_size = len(line) + 1
            if current + line_size > tail_budget:
                break
            tail_lines.append(line)
            current += line_size
        tail = "\n".join(reversed(tail_lines))
        if not tail and tail_budget > 0:
            tail = content[-tail_budget:]
            truncation_type = "hard"
    else:
        head = content[:head_budget]
        tail = content[-tail_budget:] if tail_budget > 0 else ""
        truncation_type = "hard"

    truncated = head + marker + tail

    return truncated, TruncationInfo(
        truncated=True,
        original_size=original_size,
        truncated_size=len(truncated),
        truncation_type=truncation_type,
    )


def _format_repl_traceback(exc: BaseException) -> str:
    """
    Format a traceback starting at user code (the first <repl> frame).

    Drops the REPL engine's own exec/eval wrapper frames (which leak
    internal file paths like .../repl_mcp/repl_engine.py) so errors read
    as if they came directly from the user's snippet. Frames *below* the
    first <repl> frame (e.g. library code the snippet called into) are kept.
    Falls back to the full traceback if no <repl> frame is found.
    """
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != "<repl>":
        tb = tb.tb_next
    if tb is None:
        tb = exc.__traceback__
    return "".join(traceback.format_exception(type(exc), exc, tb))


def _detect_common_errors(
    exception: Exception,
    code: str,
    namespace: dict
) -> tuple[list[str], list[str]]:
    """
    Detect common error patterns and provide helpful hints.

    Args:
        exception: The exception that occurred
        code: The code that was executed
        namespace: Current namespace

    Returns:
        Tuple of (hints, similar_names)
    """
    import re
    from difflib import get_close_matches

    hints = []
    similar_names = []
    exc_type = type(exception).__name__
    exc_msg = str(exception)

    # NameError: suggest similar names
    if exc_type == "NameError":
        # Extract undefined name from message: "name 'foo' is not defined"
        match = re.search(r"name '(\w+)' is not defined", exc_msg)
        if match:
            undefined_name = match.group(1)

            # Find similar names in namespace using difflib
            available_names = list(namespace.keys())
            similar = get_close_matches(undefined_name, available_names, n=3, cutoff=0.6)
            similar_names.extend(similar)

            if similar:
                hints.append(f"Did you mean one of: {', '.join(similar)}?")

    # AttributeError: suggest available attributes
    elif exc_type == "AttributeError":
        # Extract object name if possible
        match = re.search(r"'(\w+)' object has no attribute '(\w+)'", exc_msg)
        if match:
            obj_type = match.group(1)
            attr_name = match.group(2)

            pass  # similar-name suggestions handled below

    # FileNotFoundError: suggest existence checks
    elif exc_type == "FileNotFoundError":
        if "open" in code or "Path" in code:
            hints.append("Check the path exists first (os.path.exists) — open() accepts absolute, relative, and ~-expanded paths")

    # ImportError: suggest installing into the server venv
    elif exc_type in ["ImportError", "ModuleNotFoundError"]:
        hints.append("Module not installed in the REPL server's venv — install it via sh('uv pip install <pkg>') or use the Bash tool")

    # Cross-call async resource trap: each call runs its own event loop
    elif exc_type == "RuntimeError" and (
        "Event loop is closed" in exc_msg
        or "attached to a different loop" in exc_msg
        or "different event loop" in exc_msg
    ):
        hints.append(
            "Each execute_python call runs its own event loop — async resources "
            "(httpx clients, tasks, locks) created in a previous call are bound to "
            "a closed loop. Recreate them in this call, or use sync APIs for "
            "objects that must persist across calls"
        )

    # multiprocessing inside the kernel: the kernel child is daemonic
    elif exc_type == "AssertionError" and "daemonic" in exc_msg:
        hints.append(
            "The REPL kernel is a daemonic process — multiprocessing workers can't "
            "be spawned here. Use concurrent.futures.ThreadPoolExecutor for "
            "parallelism, or sh() to run parallel CLI commands"
        )

    # ZeroDivisionError: generic hint
    elif exc_type == "ZeroDivisionError":
        hints.append("Check for zero values before division")

    # KeyError: suggest using .get()
    elif exc_type == "KeyError":
        hints.append("Use dict.get(key, default) to avoid KeyError")

    # IndexError: suggest bounds checking
    elif exc_type == "IndexError":
        hints.append("Check sequence length before indexing")

    # Verbose subprocess usage: point at the sh() helper
    if "subprocess" in code or "os.system" in code:
        hints.append("Tip: the pre-injected sh(cmd) helper runs shell commands and returns stdout as a str — e.g. json.loads(sh('gh pr list --json number'))")

    return hints, similar_names


def _generate_warnings(
    stdout: str,
    stderr: str,
    return_value: Optional[str],
    execution_time_ms: float,
    namespace_size: int,
    stdout_info: TruncationInfo,
    stderr_info: TruncationInfo,
    return_info: Optional[TruncationInfo]
) -> list[WarningInfo]:
    """
    Generate warnings for execution issues.

    Args:
        stdout: Captured stdout
        stderr: Captured stderr
        return_value: Return value repr
        execution_time_ms: Execution time
        namespace_size: Number of variables in namespace
        stdout_info: Stdout truncation info
        stderr_info: Stderr truncation info
        return_info: Return value truncation info

    Returns:
        List of warnings
    """
    warnings = []

    # Warn about truncated output
    if stdout_info.truncated:
        warnings.append(WarningInfo(
            category="output_truncated",
            message=f"stdout was truncated from {stdout_info.original_size} to {stdout_info.truncated_size} characters",
            suggestion="Consider processing data in smaller chunks or writing to a file",
            metadata={
                "original_size": stdout_info.original_size,
                "max_size": MAX_STDOUT_SIZE
            }
        ))

    if stderr_info.truncated:
        warnings.append(WarningInfo(
            category="output_truncated",
            message=f"stderr was truncated from {stderr_info.original_size} to {stderr_info.truncated_size} characters",
            suggestion="Error output is very large; consider capturing specific errors",
            metadata={
                "original_size": stderr_info.original_size,
                "max_size": MAX_STDERR_SIZE
            }
        ))

    if return_info and return_info.truncated:
        warnings.append(WarningInfo(
            category="return_value_truncated",
            message=f"return_value was truncated from {return_info.original_size} to {return_info.truncated_size} characters",
            suggestion="Return value is too large; consider summarizing or counting instead",
            metadata={
                "original_size": return_info.original_size,
                "max_size": MAX_RETURN_VALUE_SIZE
            }
        ))

    # Warn about large output (even if not truncated)
    if not stdout_info.truncated and len(stdout) > WARN_OUTPUT_SIZE:
        warnings.append(WarningInfo(
            category="large_output",
            message=f"stdout is large ({len(stdout)} characters)",
            suggestion="Consider limiting output or processing in batches",
            metadata={"size": len(stdout), "threshold": WARN_OUTPUT_SIZE}
        ))

    # Warn about slow execution
    if execution_time_ms > WARN_EXECUTION_TIME_MS:
        warnings.append(WarningInfo(
            category="slow_execution",
            message=f"Execution took {execution_time_ms:.0f}ms (>{WARN_EXECUTION_TIME_MS}ms threshold)",
            suggestion="Consider optimizing the code or breaking into smaller operations",
            metadata={
                "execution_time_ms": execution_time_ms,
                "threshold_ms": WARN_EXECUTION_TIME_MS
            }
        ))

    # Note: the large-namespace warning is handled in REPLEngine.execute()
    # (fires once per session, needs engine state)

    return warnings


class REPLEngine:
    """Stateful Python REPL with persistent namespace and output capture."""

    def __init__(
        self,
        mcp_wrapper: Optional[Any] = None,
        workspace_root: Optional[Path] = None,
    ):
        """
        Initialize REPL engine with persistent namespace.

        Args:
            mcp_wrapper: MCP client wrapper (or proxy) injected as `mcp`
            workspace_root: Default cwd for the sh() helper (default: cwd)
        """
        self.globals: dict[str, Any] = {"__builtins__": __builtins__}
        self.workspace_root = workspace_root or Path.cwd()

        # Large-namespace warning fires once per session (reset clears it)
        self._namespace_warning_emitted = False

        # Original helper objects, kept so reset_namespace() can restore them
        # even if a cell rebound the names (`sh = 'oops'`)
        self._injected: dict[str, Any] = {}
        # Helper names already warned about (avoid repeating every call)
        self._shadow_warned: set[str] = set()

        # Inject MCP wrapper
        if mcp_wrapper:
            self.globals["mcp"] = mcp_wrapper
            self._injected["mcp"] = mcp_wrapper

        # Inject shell helper bound to workspace root
        try:
            from .utilities.shell import make_sh
            bound_sh = make_sh(self.workspace_root)
            self.globals["sh"] = bound_sh
            self._injected["sh"] = bound_sh
        except Exception:
            pass  # Shell helper init failed, skip

    def execute(
        self,
        code: str,
        timeout: float = 120.0,
    ) -> ExecutionResult:
        """
        Execute Python code in persistent namespace with output capture.

        Args:
            code: Python code to execute
            timeout: Maximum execution time in seconds (default: 120s).
                    Enforced via cancellation for cells containing top-level
                    await; sync-code enforcement is the caller's job (the
                    kernel supervisor interrupts via SIGINT).
        Returns:
            ExecutionResult with captured output and execution status
        """
        start_time = time.perf_counter()
        stdout_capture = _CaptureIO()
        stderr_capture = _CaptureIO()
        return_value = None
        exception_info = None
        success = True

        try:
            stmt_code, expr_code, is_async = self._compile_cell(code)

            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                if is_async:
                    # Top-level await: run the cell as a coroutine on a fresh
                    # event loop. wait_for makes timeout REAL (cancellable)
                    # for async cells.
                    return_value = asyncio.run(
                        asyncio.wait_for(
                            self._run_cell_async(stmt_code, expr_code),
                            timeout=timeout,
                        )
                    )
                else:
                    if stmt_code is not None:
                        exec(stmt_code, self.globals, self.globals)
                    if expr_code is not None:
                        return_value = eval(expr_code, self.globals, self.globals)

                # Legacy convenience: a trailing expression that *returns* a
                # coroutine (e.g. `main()` without await) is awaited for the
                # caller on a fresh loop.
                if asyncio.iscoroutine(return_value):
                    return_value = asyncio.run(
                        asyncio.wait_for(return_value, timeout=timeout)
                    )

        except asyncio.TimeoutError:
            success = False
            exception_info = ExceptionInfo(
                type="TimeoutError",
                message=(
                    f"async execution cancelled after {timeout:.0f}s timeout. "
                    "Namespace changes made before the timeout are preserved."
                ),
                traceback=f"TimeoutError: async cell exceeded {timeout:.0f}s\n",
                hints=["Raise the timeout parameter for long-running async work"],
                similar_names=[],
            )
        except KeyboardInterrupt as e:
            success = False
            user_message = str(e)
            if user_message:
                # Raised by the user's own code (SIGINT interrupts carry no
                # message) — report it as their exception, not as a timeout.
                exception_info = ExceptionInfo(
                    type="KeyboardInterrupt",
                    message=user_message,
                    traceback=_format_repl_traceback(e),
                    hints=[],
                    similar_names=[],
                )
            else:
                exception_info = ExceptionInfo(
                    type="KeyboardInterrupt",
                    message=(
                        "execution interrupted. Namespace state up to the "
                        "interrupt is preserved."
                    ),
                    traceback="KeyboardInterrupt\n",
                    hints=[],
                    similar_names=[],
                )
        except asyncio.CancelledError:
            success = False
            exception_info = ExceptionInfo(
                type="CancelledError",
                message="awaited task or future was already cancelled",
                traceback="CancelledError\n",
                hints=[
                    "Background tasks don't survive across calls — each "
                    "execute_python call runs its own event loop, which cancels "
                    "pending tasks when the cell ends. Create and await tasks "
                    "within a single call"
                ],
                similar_names=[],
            )
        except SyntaxError as e:
            success = False

            # Extract context line from traceback
            context_line = self._extract_error_line(code, e.lineno if hasattr(e, 'lineno') else None)

            exception_info = ExceptionInfo(
                type=type(e).__name__,
                message=str(e),
                # SyntaxError is raised inside compile() — there is no <repl>
                # frame, so format the exception only (no engine frames).
                traceback="".join(traceback.format_exception_only(type(e), e)),
                context_line=context_line,
                hints=["Check for missing parentheses, brackets, or quotes"],
                similar_names=[]
            )
        except Exception as e:
            success = False

            # Enhanced error detection
            hints, similar_names = _detect_common_errors(e, code, self.globals)
            clean_tb = _format_repl_traceback(e)
            context_line = self._extract_error_line_from_traceback(clean_tb)

            exception_info = ExceptionInfo(
                type=type(e).__name__,
                message=str(e),
                traceback=clean_tb,
                context_line=context_line,
                hints=hints,
                similar_names=similar_names
            )

        end_time = time.perf_counter()
        execution_time_ms = (end_time - start_time) * 1000

        # Apply smart truncation to outputs
        stdout_str = stdout_capture.getvalue()
        stdout_truncated, stdout_info = _smart_truncate(stdout_str, MAX_STDOUT_SIZE)

        stderr_str = stderr_capture.getvalue()
        stderr_truncated, stderr_info = _smart_truncate(stderr_str, MAX_STDERR_SIZE)

        # repr() runs user __repr__ code — a raising __repr__ must not be
        # reported as a failure of the (successful) cell itself.
        return_value_str = None
        repr_warning = None
        if return_value is not None:
            try:
                return_value_str = repr(return_value)
            except Exception as repr_exc:
                return_value_str = (
                    f"<{type(return_value).__name__} object — repr() raised "
                    f"{type(repr_exc).__name__}: {repr_exc}>"
                )
                repr_warning = WarningInfo(
                    category="repr_failed",
                    message=(
                        f"The cell succeeded, but formatting its return value failed: "
                        f"repr() raised {type(repr_exc).__name__}: {repr_exc}"
                    ),
                    suggestion="The value is stored in the namespace; inspect it field by field",
                )
        return_value_truncated = None
        return_value_info = None
        if return_value_str:
            return_value_truncated, return_value_info = _smart_truncate(return_value_str, MAX_RETURN_VALUE_SIZE)

        # Get namespace with truncation info
        namespace_vars, namespace_vars_info = self._get_namespace_vars_with_truncation()

        # Generate warnings
        warnings = _generate_warnings(
            stdout_str,
            stderr_str,
            return_value_str,
            execution_time_ms,
            len(namespace_vars),
            stdout_info,
            stderr_info,
            return_value_info
        )

        if repr_warning is not None:
            warnings.append(repr_warning)

        # Non-UTF-8-encodable text (lone surrogates) is replaced at the model
        # boundary (see models.utf8_safe) — warn so the '?' marks are explained
        for label, text in (("stdout", stdout_str), ("stderr", stderr_str),
                            ("return_value", return_value_str or "")):
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                warnings.append(WarningInfo(
                    category="encoding_sanitized",
                    message=(
                        f"{label} contained characters that can't be encoded as "
                        "UTF-8 (e.g. lone surrogates) — they were replaced with '?'"
                    ),
                    suggestion="Decode binary data with errors='replace' instead of 'surrogateescape'",
                ))

        # Helper shadowing: a cell that rebinds sh/mcp silently breaks the
        # helpers for the rest of the session — say so once per shadow
        for name, original in self._injected.items():
            if self.globals.get(name) is not original:
                if name not in self._shadow_warned:
                    self._shadow_warned.add(name)
                    warnings.append(WarningInfo(
                        category="helper_shadowed",
                        message=(
                            f"'{name}' was overwritten and no longer refers to the "
                            f"built-in helper. Use reset=True to restore it"
                        ),
                    ))
            else:
                self._shadow_warned.discard(name)

        # Large-namespace warning: fire once per session, not on every call
        namespace_size = len(namespace_vars)
        if namespace_size > WARN_NAMESPACE_SIZE and not self._namespace_warning_emitted:
            self._namespace_warning_emitted = True
            warnings.append(WarningInfo(
                category="large_namespace",
                message=f"Namespace has {namespace_size} variables (>{WARN_NAMESPACE_SIZE} threshold)",
                suggestion="Consider using reset=True to clear namespace or use more focused variable names",
                metadata={
                    "namespace_size": namespace_size,
                    "threshold": WARN_NAMESPACE_SIZE
                }
            ))

        return ExecutionResult(
            success=success,
            stdout=stdout_truncated,
            stdout_info=stdout_info,
            stderr=stderr_truncated,
            stderr_info=stderr_info,
            return_value=return_value_truncated,
            return_value_info=return_value_info,
            exception=exception_info,
            execution_time_ms=execution_time_ms,
            namespace_vars=namespace_vars,
            namespace_vars_info=namespace_vars_info,
            warnings=warnings,
        )

    def _compile_cell(self, code: str):
        """
        Compile a cell into (stmt_code, expr_code, is_async).

        Splits a trailing expression off for return-value capture (REPL
        semantics) and compiles both parts with PyCF_ALLOW_TOP_LEVEL_AWAIT
        so `await` works at top level (IPython autoawait-style). Module-level
        code objects lack CO_NEWLOCALS, so eval(co, globals, globals) keeps
        assignments persistent in the namespace even on the async path.

        Returns:
            stmt_code: code object for leading statements (or None)
            expr_code: eval-mode code object for the trailing expression (or None)
            is_async: True if either part contains top-level await
        """
        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT

        # Parse once; SyntaxError propagates to the caller's handler.
        parsed = ast.parse(code, "<repl>", "exec")

        stmt_code = None
        expr_code = None

        if parsed.body and isinstance(parsed.body[-1], ast.Expr):
            statements = parsed.body[:-1]
            final_expr = parsed.body[-1]

            if statements:
                stmt_module = ast.Module(body=statements, type_ignores=[])
                ast.fix_missing_locations(stmt_module)
                stmt_code = compile(stmt_module, "<repl>", "exec", flags=flags)

            expr_code = compile(
                ast.Expression(body=final_expr.value), "<repl>", "eval", flags=flags
            )
        else:
            stmt_code = compile(parsed, "<repl>", "exec", flags=flags)

        is_async = any(
            co is not None and bool(co.co_flags & inspect.CO_COROUTINE)
            for co in (stmt_code, expr_code)
        )
        return stmt_code, expr_code, is_async

    async def _run_cell_async(self, stmt_code, expr_code) -> Any:
        """Run a compiled cell containing top-level await; returns the
        trailing-expression value (or None)."""
        if stmt_code is not None:
            result = eval(stmt_code, self.globals, self.globals)
            if stmt_code.co_flags & inspect.CO_COROUTINE:
                await result

        return_value = None
        if expr_code is not None:
            return_value = eval(expr_code, self.globals, self.globals)
            if expr_code.co_flags & inspect.CO_COROUTINE:
                return_value = await return_value
        return return_value

    def _extract_error_line(self, code: str, lineno: Optional[int]) -> Optional[str]:
        """Extract the specific line that caused an error."""
        if lineno is None:
            return None

        try:
            lines = code.split("\n")
            if 0 < lineno <= len(lines):
                return lines[lineno - 1].strip()
        except Exception:
            pass

        return None

    def _extract_error_line_from_traceback(self, tb: str) -> Optional[str]:
        """Extract error line from traceback string."""
        try:
            # Look for lines like: "    some_code_here"
            lines = tb.split("\n")
            for i, line in enumerate(lines):
                if line.strip().startswith("File \"<repl>\""):
                    # Next line should be the code
                    if i + 1 < len(lines):
                        return lines[i + 1].strip()
        except Exception:
            pass

        return None

    def _get_namespace_vars_dict(self) -> dict[str, str]:
        """Get namespace variables as dict, excluding reserved names."""
        namespace_vars = {}
        for key, value in self.globals.items():
            if key not in RESERVED_NAMES:
                try:
                    namespace_vars[key] = repr(value)[:100]  # Limit length
                except Exception:
                    namespace_vars[key] = "<unprintable>"
        return namespace_vars

    def _get_namespace_vars_with_truncation(self) -> tuple[dict[str, str], dict[str, TruncationInfo]]:
        """
        Get namespace variables with truncation metadata.

        Returns:
            Tuple of (namespace_vars, truncation_info)
        """
        namespace_vars = {}
        truncation_info = {}

        for key, value in self.globals.items():
            if key not in RESERVED_NAMES:
                try:
                    value_repr = repr(value)
                    truncated, trunc_info = _smart_truncate(value_repr, MAX_NAMESPACE_VAR_SIZE)
                    namespace_vars[key] = truncated
                    truncation_info[key] = trunc_info
                except Exception:
                    namespace_vars[key] = "<unprintable>"
                    truncation_info[key] = TruncationInfo(
                        truncated=False,
                        original_size=0,
                        truncated_size=13,  # len("<unprintable>")
                        truncation_type="hard",
                    )

        return namespace_vars, truncation_info

    def reset_namespace(self) -> None:
        """Reset namespace to initial state, restoring injected helpers.

        Restores the ORIGINAL sh/mcp objects (not whatever is currently bound
        to those names) so a cell that shadowed a helper (`sh = 'oops'`) is
        fully recoverable via reset=True.
        """
        self.globals.clear()
        self.globals["__builtins__"] = __builtins__
        self.globals.update(self._injected)

        # Fresh namespace may warn again when it grows large
        self._namespace_warning_emitted = False
        self._shadow_warned.clear()

    def get_namespace_vars(self) -> dict[str, str]:
        """Get current namespace variables (excluding reserved names)."""
        return self._get_namespace_vars_dict()
