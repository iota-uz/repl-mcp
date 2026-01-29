"""Unit tests for REPL engine."""

import pytest
from repl_mcp.repl_engine import REPLEngine
from repl_mcp.models import ExecutionResult


class TestREPLEngine:
    """Tests for REPLEngine class."""

    def test_simple_execution(self):
        """Test simple code execution."""
        engine = REPLEngine()
        result = engine.execute("x = 42")

        assert isinstance(result, ExecutionResult)
        assert result.success
        assert "x" in result.namespace_vars
        assert result.namespace_vars["x"] == "42"

    def test_state_persistence(self):
        """Test that state persists across executions."""
        engine = REPLEngine()

        result1 = engine.execute("x = 10")
        assert result1.success

        result2 = engine.execute("y = x + 5")
        assert result2.success
        assert result2.namespace_vars["y"] == "15"

    def test_stdout_capture(self):
        """Test stdout capture."""
        engine = REPLEngine()
        result = engine.execute('print("Hello")')

        assert result.success
        assert result.stdout.strip() == "Hello"

    def test_stderr_capture(self):
        """Test stderr capture."""
        engine = REPLEngine()
        result = engine.execute('import sys; print("Error", file=sys.stderr)')

        assert result.success
        assert "Error" in result.stderr

    def test_return_value(self):
        """Test return value capture."""
        engine = REPLEngine()
        result = engine.execute("2 + 2")

        assert result.success
        assert result.return_value == "4"

    def test_multiline_return(self):
        """Test multiline code with return value."""
        engine = REPLEngine()
        code = """
x = 10
y = 20
x + y
"""
        result = engine.execute(code)

        assert result.success
        assert result.return_value == "30"

    def test_syntax_error(self):
        """Test syntax error handling."""
        engine = REPLEngine()
        result = engine.execute("def foo(")

        assert not result.success
        assert result.exception is not None
        assert result.exception.type == "SyntaxError"

    def test_runtime_error(self):
        """Test runtime error handling."""
        engine = REPLEngine()
        result = engine.execute("1 / 0")

        assert not result.success
        assert result.exception is not None
        assert result.exception.type == "ZeroDivisionError"
        assert "traceback" in result.exception.traceback.lower()

    def test_namespace_preserved_after_error(self):
        """Test namespace is preserved after error."""
        engine = REPLEngine()

        engine.execute("x = 42")
        result_error = engine.execute("y = 1 / 0")
        assert not result_error.success

        result_check = engine.execute("x")
        assert result_check.success
        assert result_check.return_value == "42"

    def test_reset_namespace(self):
        """Test namespace reset."""
        engine = REPLEngine()

        engine.execute("x = 1")
        engine.execute("y = 2")

        vars_before = engine.get_namespace_vars()
        assert "x" in vars_before
        assert "y" in vars_before

        engine.reset_namespace()

        vars_after = engine.get_namespace_vars()
        assert "x" not in vars_after
        assert "y" not in vars_after

    def test_mcp_preserved_on_reset(self):
        """Test mcp object preserved on reset."""

        class MockMCP:
            pass

        mcp = MockMCP()
        engine = REPLEngine(mcp_wrapper=mcp)

        engine.execute("x = 42")
        engine.reset_namespace()

        assert engine.globals.get("mcp") is mcp

    def test_imports(self):
        """Test that imports work."""
        engine = REPLEngine()

        result1 = engine.execute("import json")
        assert result1.success

        result2 = engine.execute('json.dumps({"a": 1})')
        assert result2.success
        assert '"a"' in result2.return_value

    def test_function_definition(self):
        """Test function definition and call."""
        engine = REPLEngine()

        result1 = engine.execute("""
def add(a, b):
    return a + b
""")
        assert result1.success

        result2 = engine.execute("add(10, 20)")
        assert result2.success
        assert result2.return_value == "30"

    def test_class_definition(self):
        """Test class definition and usage."""
        engine = REPLEngine()

        result1 = engine.execute("""
class Counter:
    def __init__(self):
        self.count = 0

    def increment(self):
        self.count += 1
        return self.count
""")
        assert result1.success

        result2 = engine.execute("c = Counter()")
        assert result2.success

        result3 = engine.execute("c.increment()")
        assert result3.success
        assert result3.return_value == "1"

    def test_execution_time(self):
        """Test execution time measurement."""
        engine = REPLEngine()
        result = engine.execute("sum(range(1000))")

        assert result.success
        assert result.execution_time_ms > 0
        assert result.execution_time_ms < 1000  # Should be fast

    def test_namespace_vars_representation(self):
        """Test namespace vars are properly represented."""
        engine = REPLEngine()

        engine.execute("x = [1, 2, 3]")
        engine.execute("y = {'a': 1}")

        vars_dict = engine.get_namespace_vars()
        assert "x" in vars_dict
        assert "y" in vars_dict
        assert "[1, 2, 3]" in vars_dict["x"]
        assert "'a': 1" in vars_dict["y"]
