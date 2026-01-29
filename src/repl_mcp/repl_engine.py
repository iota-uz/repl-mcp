"""Stateful REPL execution engine."""

import sys
import ast
import time
import traceback
from io import StringIO
from typing import Optional, Any
from contextlib import redirect_stdout, redirect_stderr

from .models import ExecutionResult, ExceptionInfo


class REPLEngine:
    """Stateful Python REPL with persistent namespace and output capture."""

    def __init__(self, mcp_wrapper: Optional[Any] = None):
        """
        Initialize REPL engine with persistent namespace.

        Args:
            mcp_wrapper: MCP client wrapper to inject into namespace
        """
        self.globals: dict[str, Any] = {"__builtins__": __builtins__}
        if mcp_wrapper:
            self.globals["mcp"] = mcp_wrapper

    def execute(self, code: str) -> ExecutionResult:
        """
        Execute Python code in persistent namespace with output capture.

        Args:
            code: Python code to execute

        Returns:
            ExecutionResult with captured output and execution status
        """
        start_time = time.perf_counter()
        stdout_capture = StringIO()
        stderr_capture = StringIO()
        return_value = None
        exception_info = None
        success = True

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

        # Get namespace variables (exclude builtins and mcp)
        namespace_vars = {}
        for key, value in self.globals.items():
            if key not in ("__builtins__", "mcp", "__name__", "__doc__", "__package__"):
                try:
                    namespace_vars[key] = repr(value)[:100]  # Limit length
                except Exception:
                    namespace_vars[key] = "<unprintable>"

        return ExecutionResult(
            success=success,
            stdout=stdout_capture.getvalue(),
            stderr=stderr_capture.getvalue(),
            return_value=repr(return_value) if return_value is not None else None,
            exception=exception_info,
            execution_time_ms=execution_time_ms,
            namespace_vars=namespace_vars,
        )

    def reset_namespace(self) -> None:
        """Reset namespace to initial state, preserving mcp object."""
        mcp_obj = self.globals.get("mcp")
        self.globals.clear()
        self.globals["__builtins__"] = __builtins__
        if mcp_obj:
            self.globals["mcp"] = mcp_obj

    def get_namespace_vars(self) -> dict[str, str]:
        """Get current namespace variables (excluding builtins and mcp)."""
        namespace_vars = {}
        for key, value in self.globals.items():
            if key not in ("__builtins__", "mcp", "__name__", "__doc__", "__package__"):
                try:
                    namespace_vars[key] = repr(value)[:100]
                except Exception:
                    namespace_vars[key] = "<unprintable>"
        return namespace_vars
