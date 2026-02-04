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

from .models import ExecutionResult, ExceptionInfo

# Reserved names that should be excluded from namespace output
# and preserved during reset
RESERVED_NAMES = frozenset({
    "__builtins__", "__name__", "__doc__", "__package__",
    "mcp", "workspace", "git", "ast_utils", "code",
})


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
        """
        self.globals: dict[str, Any] = {"__builtins__": __builtins__}
        self.workspace_root = workspace_root or Path.cwd()

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
            exception_info = ExceptionInfo(
                type=type(e).__name__,
                message=str(e),
                traceback=traceback.format_exc(),
            )
        except Exception as e:
            success = False
            exception_info = ExceptionInfo(
                type=type(e).__name__,
                message=str(e),
                traceback=traceback.format_exc(),
            )

        end_time = time.perf_counter()
        execution_time_ms = (end_time - start_time) * 1000

        # Get namespace variables (exclude reserved names)
        namespace_vars = self._get_namespace_vars_dict()

        return ExecutionResult(
            success=success,
            stdout=stdout_capture.getvalue(),
            stderr=stderr_capture.getvalue(),
            return_value=repr(return_value) if return_value is not None else None,
            exception=exception_info,
            execution_time_ms=execution_time_ms,
            namespace_vars=namespace_vars,
        )

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

    def get_namespace_vars(self) -> dict[str, str]:
        """Get current namespace variables (excluding reserved names)."""
        return self._get_namespace_vars_dict()
