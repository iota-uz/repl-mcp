"""Stateful REPL execution engine."""

import sys
import ast
import time
import asyncio
import traceback
from io import StringIO
from pathlib import Path
from typing import Optional, Any
from contextlib import redirect_stdout, redirect_stderr

from .models import ExecutionResult, ExceptionInfo, TruncationInfo, WarningInfo

# Reserved names that should be excluded from namespace output
# and preserved during reset
RESERVED_NAMES = frozenset({
    "__builtins__", "__name__", "__doc__", "__package__",
    "mcp", "workspace", "git", "ast_utils", "code", "sh",
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
    suffix: str = TRUNCATION_SUFFIX
) -> tuple[str, TruncationInfo]:
    """
    Intelligently truncate content while preserving structure.

    For multi-line content, tries to preserve complete lines.
    For single-line content, truncates at max_size.

    Args:
        content: String to truncate
        max_size: Maximum size in characters
        suffix: Suffix to append when truncated

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

    # Adjust max_size to account for suffix
    effective_max = max_size - len(suffix)

    # Try smart truncation (preserve line boundaries)
    truncation_type = "smart"
    if "\n" in content:
        lines = content.split("\n")
        kept_lines = []
        current_size = 0

        for line in lines:
            line_size = len(line) + 1  # +1 for newline
            if current_size + line_size <= effective_max:
                kept_lines.append(line)
                current_size += line_size
            else:
                break

        if kept_lines:
            truncated = "\n".join(kept_lines) + suffix
        else:
            # First line alone is too long, fall back to hard truncation
            truncated = content[:effective_max] + suffix
            truncation_type = "hard"
    else:
        # Single line, hard truncate
        truncated = content[:effective_max] + suffix
        truncation_type = "hard"

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

            # Check for common mistakes with injected utilities
            if obj_type == "Workspace" and attr_name in ["open", "file"]:
                hints.append("Use workspace.read(path) to read files")
            elif obj_type == "GitUtils" and attr_name == "commits":
                hints.append("Use git.log(n=10) to get commits")

    # FileNotFoundError: suggest existence checks
    elif exc_type == "FileNotFoundError":
        if "open" in code or "Path" in code or "workspace" in code:
            hints.append("Check the path exists first: workspace.exists(path) — workspace.read() and open() both accept absolute, relative, and ~ paths")

    # ImportError: suggest installing into the server venv
    elif exc_type in ["ImportError", "ModuleNotFoundError"]:
        hints.append("Module not installed in the REPL server's venv — install it via sh('uv pip install <pkg>') or use the Bash tool")

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
        enable_workspace: bool = True,
        enable_git: bool = True,
        enable_ast: bool = True,
        enable_code: bool = True,
        max_history: int = 100,
    ):
        """
        Initialize REPL engine with persistent namespace and utilities.

        Args:
            mcp_wrapper: MCP client wrapper to inject into namespace
            workspace_root: Root directory for workspace operations (default: cwd)
            enable_workspace: Enable workspace file utilities
            enable_git: Enable git utilities (if in a git repo)
            enable_ast: Enable AST analysis utilities (Python only)
            enable_code: Enable multi-language code analysis utilities (tree-sitter)
            max_history: Maximum number of history entries to keep (default: 100)
        """
        self.globals: dict[str, Any] = {"__builtins__": __builtins__}
        self.workspace_root = workspace_root or Path.cwd()

        # History tracking
        self._history: list[str] = []
        self._max_history = max_history

        # Large-namespace warning fires once per session (reset clears it)
        self._namespace_warning_emitted = False

        # Inject MCP wrapper
        if mcp_wrapper:
            self.globals["mcp"] = mcp_wrapper

        # Initialize workspace utilities
        self._workspace = None
        if enable_workspace:
            try:
                from .utilities.workspace import Workspace
                self._workspace = Workspace(self.workspace_root)
                self.globals["workspace"] = self._workspace
            except Exception:
                pass  # Workspace init failed, skip

        # Inject shell helper bound to workspace root
        try:
            from .utilities.shell import make_sh
            self.globals["sh"] = make_sh(self.workspace_root)
        except Exception:
            pass  # Shell helper init failed, skip

        # Initialize git utilities (Phase 2 - will be implemented later)
        self._git = None
        if enable_git:
            try:
                from .utilities.git_utils import GitUtils
                self._git = GitUtils(self.workspace_root)
                self.globals["git"] = self._git
            except ImportError:
                pass  # GitUtils not yet implemented
            except Exception:
                pass  # Not a git repo or other error

        # Initialize AST utilities (Python-specific, uses built-in ast module)
        self._ast_utils = None
        if enable_ast and self._workspace:
            try:
                from .utilities.ast_utils import ASTUtils
                self._ast_utils = ASTUtils(self._workspace)
                self.globals["ast_utils"] = self._ast_utils
            except ImportError:
                pass  # ASTUtils not available
            except Exception:
                pass  # AST init failed

        # Initialize multi-language code utilities (uses tree-sitter)
        self._code_utils = None
        if enable_code and self._workspace:
            try:
                from .utilities.code_utils import CodeUtils
                self._code_utils = CodeUtils(self._workspace)
                self.globals["code"] = self._code_utils
            except ImportError:
                pass  # tree-sitter not available
            except Exception:
                pass  # CodeUtils init failed

    def execute(
        self,
        code: str,
        timeout: float = 120.0,
        inject: Optional[dict] = None,
    ) -> ExecutionResult:
        """
        Execute Python code in persistent namespace with output capture.

        Args:
            code: Python code to execute
            timeout: Maximum execution time in seconds (default: 120s)
                    Note: This timeout is enforced for MCP tool calls via the mcp object.
                    Direct Python code execution cannot be easily interrupted due to
                    Python's execution model.
            inject: Optional dict of variables to inject into namespace before execution.
                   These variables persist in the namespace after execution.

        Returns:
            ExecutionResult with captured output and execution status
        """
        # Detect magic commands (start with %)
        code_stripped = code.strip()
        if code_stripped.startswith('%'):
            return self._execute_magic(code_stripped, timeout)

        # Detect IPython-style help queries (object? or object??)
        if code_stripped.endswith('??'):
            return self._execute_help_query(code_stripped[:-2].strip(), show_source=True)
        elif code_stripped.endswith('?') and not code_stripped.endswith('??'):
            return self._execute_help_query(code_stripped[:-1].strip(), show_source=False)

        # Track history (excluding magics and help queries which are handled above)
        self._add_to_history(code_stripped)

        start_time = time.perf_counter()
        stdout_capture = StringIO()
        stderr_capture = StringIO()
        return_value = None
        exception_info = None
        success = True

        # Inject variables into namespace
        if inject:
            for key, value in inject.items():
                if key in RESERVED_NAMES:
                    # Don't allow overwriting reserved names
                    continue
                self.globals[key] = value

        try:
            # Compile code to check for syntax errors early
            compiled_code = compile(code, "<repl>", "exec")

            # Parse AST to detect trailing expression for return value
            parsed = ast.parse(code, "<repl>", "exec")
            if parsed.body and isinstance(parsed.body[-1], ast.Expr):
                # Split into statements and final expression
                statements = parsed.body[:-1]
                final_expr = parsed.body[-1]

                # Execute statements
                if statements:
                    stmt_module = ast.Module(body=statements, type_ignores=[])
                    ast.fix_missing_locations(stmt_module)
                    stmt_code = compile(stmt_module, "<repl>", "exec")

                    with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                        exec(stmt_code, self.globals, self.globals)

                # Evaluate final expression for return value
                expr_code = compile(ast.Expression(body=final_expr.value), "<repl>", "eval")
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    return_value = eval(expr_code, self.globals, self.globals)

                    # If result is awaitable (Task or coroutine), await it
                    if asyncio.iscoroutine(return_value) or asyncio.isfuture(return_value):
                        try:
                            loop = asyncio.get_running_loop()
                            # We're in an async context, run the coroutine
                            return_value = loop.run_until_complete(return_value)
                        except RuntimeError:
                            # No running loop, create one
                            return_value = asyncio.run(return_value)
            else:
                # No trailing expression, just execute
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    exec(compiled_code, self.globals, self.globals)

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

        return_value_str = repr(return_value) if return_value is not None else None
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
        """Reset namespace to initial state, preserving injected utilities."""
        # Save utilities
        saved = {}
        for name in RESERVED_NAMES:
            if name in self.globals and name != "__builtins__":
                saved[name] = self.globals[name]

        # Clear and restore
        self.globals.clear()
        self.globals["__builtins__"] = __builtins__
        self.globals.update(saved)

        # Fresh namespace may warn again when it grows large
        self._namespace_warning_emitted = False

    def get_namespace_vars(self) -> dict[str, str]:
        """Get current namespace variables (excluding reserved names)."""
        return self._get_namespace_vars_dict()

    def _add_to_history(self, code: str) -> None:
        """Add code to execution history."""
        # Don't add empty or whitespace-only entries
        if not code.strip():
            return

        # Don't add duplicates of the last entry
        if self._history and self._history[-1] == code:
            return

        self._history.append(code)

        # Trim if over max
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def get_history(self, n: Optional[int] = None) -> list[str]:
        """
        Get execution history.

        Args:
            n: Number of recent entries to return (default: all)

        Returns:
            List of executed code strings
        """
        if n is None:
            return list(self._history)
        return list(self._history[-n:])

    # =========================================================================
    # IPython-style Help Queries (object? and object??)
    # =========================================================================

    def _execute_help_query(self, obj_name: str, show_source: bool = False) -> ExecutionResult:
        """
        Execute IPython-style help query (object? or object??).

        Args:
            obj_name: Name of object to get help for (e.g., "workspace", "workspace.read")
            show_source: If True (??), show source code if available

        Returns:
            ExecutionResult with help text
        """
        import inspect
        start_time = time.perf_counter()

        if not obj_name:
            # Just "?" with no object - show general help
            execution_time_ms = (time.perf_counter() - start_time) * 1000
            return ExecutionResult(
                success=True,
                stdout=self._magic_help(),
                execution_time_ms=execution_time_ms,
            )

        try:
            # Resolve the object name (handles dotted names like workspace.read)
            obj = self._resolve_name(obj_name)

            from io import StringIO
            out = StringIO()

            # Get type info
            obj_type = type(obj).__name__
            out.write(f"Type: {obj_type}\n")

            # Get signature for callables
            if callable(obj):
                try:
                    sig = inspect.signature(obj)
                    out.write(f"Signature: {obj_name}{sig}\n")
                except (ValueError, TypeError):
                    pass

            # Get docstring
            docstring = inspect.getdoc(obj)
            if docstring:
                out.write(f"\nDocstring:\n{docstring}\n")
            else:
                out.write("\nNo docstring available.\n")

            # Show source if requested (??)
            if show_source:
                try:
                    source = inspect.getsource(obj)
                    out.write(f"\nSource:\n{source}\n")
                except (TypeError, OSError):
                    out.write("\nSource code not available.\n")

            execution_time_ms = (time.perf_counter() - start_time) * 1000
            return ExecutionResult(
                success=True,
                stdout=out.getvalue(),
                execution_time_ms=execution_time_ms,
            )

        except NameError as e:
            execution_time_ms = (time.perf_counter() - start_time) * 1000
            return ExecutionResult(
                success=False,
                stderr=f"Object not found: {obj_name}",
                exception=ExceptionInfo(
                    type="NameError",
                    message=str(e),
                    traceback="",
                ),
                execution_time_ms=execution_time_ms,
            )
        except Exception as e:
            execution_time_ms = (time.perf_counter() - start_time) * 1000
            return ExecutionResult(
                success=False,
                exception=ExceptionInfo(
                    type=type(e).__name__,
                    message=str(e),
                    traceback="".join(traceback.format_exception_only(type(e), e)),
                ),
                execution_time_ms=execution_time_ms,
            )

    def _resolve_name(self, name: str) -> Any:
        """
        Resolve a dotted name to an object in the namespace.

        Args:
            name: Dotted name like "workspace" or "workspace.read"

        Returns:
            The resolved object

        Raises:
            NameError: If name cannot be resolved
        """
        parts = name.split('.')
        obj = self.globals.get(parts[0])

        if obj is None:
            raise NameError(f"name '{parts[0]}' is not defined")

        for part in parts[1:]:
            try:
                obj = getattr(obj, part)
            except AttributeError:
                raise NameError(f"'{'.'.join(parts[:parts.index(part)])}' has no attribute '{part}'")

        return obj

    # =========================================================================
    # Magic Commands (IPython-style)
    # =========================================================================

    def _execute_magic(self, magic_line: str, timeout: float) -> ExecutionResult:
        """
        Execute IPython-style magic command.

        Supported magics:
            %who          List variable names
            %whos         Detailed variable listing
            %reset        Reset namespace (keep utilities)
            %env          Show environment info
            %timeit <code>  Time code execution
            %mcp          Show MCP server info
            %help         Show REPL help
        """
        start_time = time.perf_counter()

        # Parse magic command
        parts = magic_line[1:].split(maxsplit=1)
        magic_name = parts[0].lower()
        magic_args = parts[1] if len(parts) > 1 else ""

        # Dispatch to magic handlers
        try:
            if magic_name == "who":
                result = self._magic_who()
            elif magic_name == "whos":
                result = self._magic_whos()
            elif magic_name == "reset":
                result = self._magic_reset()
            elif magic_name == "env":
                result = self._magic_env()
            elif magic_name == "timeit":
                result = self._magic_timeit(magic_args, timeout)
            elif magic_name == "mcp":
                result = self._magic_mcp()
            elif magic_name == "history":
                result = self._magic_history(magic_args)
            elif magic_name == "help":
                result = self._magic_help()
            else:
                execution_time_ms = (time.perf_counter() - start_time) * 1000
                return ExecutionResult(
                    success=False,
                    stderr=f"Unknown magic command: %{magic_name}\nUse %help to see available magics",
                    execution_time_ms=execution_time_ms,
                )

            # Add execution time to result
            execution_time_ms = (time.perf_counter() - start_time) * 1000
            return ExecutionResult(
                success=True,
                stdout=result,
                execution_time_ms=execution_time_ms,
            )

        except Exception as e:
            execution_time_ms = (time.perf_counter() - start_time) * 1000
            return ExecutionResult(
                success=False,
                exception=ExceptionInfo(
                    type=type(e).__name__,
                    message=str(e),
                    traceback="".join(traceback.format_exception_only(type(e), e)),
                ),
                execution_time_ms=execution_time_ms,
            )

    def _magic_who(self) -> str:
        """List variable names (like IPython %who)."""
        var_names = [k for k in self.globals.keys() if k not in RESERVED_NAMES]
        if var_names:
            return "  ".join(sorted(var_names))
        return "(no variables)"

    def _magic_whos(self) -> str:
        """Detailed variable listing (like IPython %whos)."""
        from io import StringIO
        out = StringIO()
        out.write("Variable      Type           Value\n")
        out.write("-" * 60 + "\n")

        for name in sorted(self.globals.keys()):
            if name in RESERVED_NAMES:
                continue
            value = self.globals[name]
            type_name = type(value).__name__
            try:
                value_repr = repr(value)[:40]
            except Exception:
                value_repr = "<unprintable>"
            out.write(f"{name:12}  {type_name:12}  {value_repr}\n")

        result = out.getvalue()
        if result.count("\n") == 2:  # Only header, no variables
            return "(no variables)"
        return result

    def _magic_reset(self) -> str:
        """Reset namespace, preserving utilities."""
        self.reset_namespace()
        return "Namespace reset (utilities preserved)"

    def _magic_env(self) -> str:
        """Show environment information."""
        from io import StringIO
        out = StringIO()

        out.write(f"Workspace: {self.workspace_root}\n")

        if self._git:
            try:
                status = self._git.status()
                out.write(f"Git repo: {status.branch}\n")
            except Exception:
                out.write("Git repo: (not a git repo)\n")

        out.write(f"\nUtilities loaded:\n")
        for util in ['workspace', 'git', 'ast_utils', 'code', 'mcp']:
            if util in self.globals:
                if util == 'mcp':
                    mcp = self.globals['mcp']
                    if hasattr(mcp, 'servers'):
                        server_count = len(mcp.servers)
                        out.write(f"  - mcp ({server_count} servers connected)\n")
                    else:
                        out.write(f"  - mcp\n")
                else:
                    out.write(f"  - {util}\n")

        return out.getvalue()

    def _magic_timeit(self, code: str, timeout: float) -> str:
        """Time code execution (simplified version of IPython %timeit)."""
        if not code.strip():
            return "Usage: %timeit <code>\nExample: %timeit sum(range(1000))"

        # Run 3 times, take best
        times = []
        for _ in range(3):
            start = time.perf_counter()
            exec(code, self.globals, self.globals)
            end = time.perf_counter()
            times.append((end - start) * 1000)  # Convert to ms

        best_time = min(times)
        if best_time < 1:
            return f"3 loops, best of 3: {best_time * 1000:.1f} µs per loop"
        elif best_time < 1000:
            return f"3 loops, best of 3: {best_time:.3f} ms per loop"
        else:
            return f"3 loops, best of 3: {best_time / 1000:.2f} s per loop"

    def _magic_mcp(self) -> str:
        """Show MCP server and tool information."""
        mcp = self.globals.get('mcp')
        if not mcp or not hasattr(mcp, 'servers'):
            return "No MCP servers connected\n\nConfigure servers in .mcp.json for auto-connect"

        servers = mcp.servers
        if not servers:
            return "No MCP servers connected\n\nConfigure servers in .mcp.json for auto-connect"

        from io import StringIO
        out = StringIO()
        out.write("Connected MCP servers:\n")

        tool_list = mcp.list_tools()
        for server in servers:
            tools = tool_list.get(server, [])
            out.write(f"\n  {server} ({len(tools)} tools)\n")
            for tool in tools[:5]:  # Show first 5 tools
                out.write(f"    - {tool}\n")
            if len(tools) > 5:
                out.write(f"    ... and {len(tools) - 5} more\n")

        out.write(f"\nUse mcp.help('server') to see all tools for a server")

        return out.getvalue()

    def _magic_history(self, args: str) -> str:
        """Show execution history (like IPython %history)."""
        # Parse optional count argument
        n = None
        if args.strip():
            try:
                n = int(args.strip())
            except ValueError:
                return f"Usage: %history [n]\nExample: %history 10"

        history = self.get_history(n)

        if not history:
            return "(no history)"

        from io import StringIO
        out = StringIO()

        # Show numbered history entries
        start_num = len(self._history) - len(history) + 1
        for i, code in enumerate(history, start=start_num):
            # Handle multi-line code
            lines = code.split('\n')
            if len(lines) == 1:
                out.write(f"{i:4}: {code}\n")
            else:
                out.write(f"{i:4}: {lines[0]}\n")
                for line in lines[1:]:
                    out.write(f"      {line}\n")

        return out.getvalue()

    def _magic_help(self) -> str:
        """Show REPL help."""
        return """
Python REPL with integrated utilities

Utilities:
  sh         - Shell commands: data = json.loads(sh("gh pr list --json number"))
               Returns stdout str with .returncode/.stderr/.ok; check=False to not raise
  workspace  - File access: workspace.read(path), workspace.glob("**/*.py")
               Absolute and ~ paths supported
  git        - Git ops: git.log(), git.diff(), git.blame(path)
  ast_utils  - Python AST: ast_utils.find_functions(path)
  code       - Multi-lang: code.find_functions(path) (100+ languages)
  mcp        - MCP tools: mcp.tools.<server>.<method>()

Magic commands:
  %who          List variable names
  %whos         Detailed variable listing (type, value)
  %history [n]  Show execution history
  %reset        Reset namespace (keep utilities)
  %env          Show environment info
  %timeit code  Time code execution
  %mcp          Show MCP servers and tools
  %help         Show this help

Quick help (IPython-style):
  object?       Show docstring (e.g., workspace?, git.log?)
  object??      Show source code if available

Standard Python:
  help(obj)     Full documentation
  dir(obj)      List attributes
  type(obj)     Show type

Tips:
  - open(), absolute paths, and ~ all work (full filesystem access)
  - sh() composes shell + Python in one call (replaces `cmd | python3 -c`)
  - Variables persist between calls (use %reset to clear)
  - Use %history to recall previous code
"""
