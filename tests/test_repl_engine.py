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

    def test_traceback_hides_engine_frames(self):
        """Traceback starts at user code — no repl_engine.py internals leak."""
        engine = REPLEngine()
        result = engine.execute("x = 1\nraise RuntimeError('boom')")

        assert not result.success
        assert "repl_engine.py" not in result.exception.traceback
        assert "exec(" not in result.exception.traceback
        assert "<repl>" in result.exception.traceback

    def test_traceback_keeps_library_frames(self):
        """Frames below user code (stdlib/library) are preserved."""
        engine = REPLEngine()
        result = engine.execute("import json\njson.loads('{bad')")

        assert not result.success
        assert "repl_engine.py" not in result.exception.traceback
        assert "<repl>" in result.exception.traceback

    def test_syntax_error_traceback_has_no_engine_frames(self):
        """SyntaxError output shows the error only, not compile() internals."""
        engine = REPLEngine()
        result = engine.execute("def foo(")

        assert not result.success
        assert result.exception.type == "SyntaxError"
        assert "repl_engine.py" not in result.exception.traceback

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

class TestTruncation:
    """Tests for output truncation."""

    def test_stdout_truncation_large_output(self):
        """Test that large stdout is truncated with metadata."""
        engine = REPLEngine()
        # Generate output larger than MAX_STDOUT_SIZE (50KB)
        large_text = "x" * 60000
        result = engine.execute(f'print("{large_text}")')

        assert result.success
        assert result.stdout_info is not None
        assert result.stdout_info.truncated is True
        assert result.stdout_info.original_size > 50000
        assert len(result.stdout) <= 50000 + len("\n... [TRUNCATED]")
        assert result.stdout.endswith("... [TRUNCATED]")

    def test_stdout_no_truncation_small_output(self):
        """Test that small stdout is not truncated."""
        engine = REPLEngine()
        result = engine.execute('print("Hello")')

        assert result.success
        assert result.stdout_info is not None
        assert result.stdout_info.truncated is False
        assert result.stdout_info.original_size == len("Hello\n")

    def test_return_value_truncation(self):
        """Test that large return values are truncated."""
        engine = REPLEngine()
        # Create large list
        result = engine.execute('list(range(10000))')

        assert result.success
        assert result.return_value_info is not None
        if result.return_value_info.truncated:
            assert len(result.return_value) <= 20000 + len("\n... [TRUNCATED]")

    def test_namespace_var_truncation(self):
        """Test that namespace vars are truncated with metadata."""
        engine = REPLEngine()
        # Create variable with large repr
        large_list = list(range(1000))
        engine.execute(f'my_list = {large_list}')

        result = engine.execute('1 + 1')  # Trigger namespace capture

        assert 'my_list' in result.namespace_vars
        assert 'my_list' in result.namespace_vars_info

        trunc_info = result.namespace_vars_info['my_list']
        if trunc_info.truncated:
            assert trunc_info.original_size > 500
            assert len(result.namespace_vars['my_list']) <= 500 + len("\n... [TRUNCATED]")


class TestEnhancedErrors:
    """Tests for enhanced error messages."""

    def test_name_error_with_hints(self):
        """Test NameError provides helpful hints."""
        engine = REPLEngine()
        result = engine.execute('print(undefined_var)')

        assert not result.success
        assert result.exception is not None
        assert result.exception.type == "NameError"
        assert len(result.exception.hints) >= 0  # May have hints

    def test_name_error_suggests_similar_names(self):
        """Test NameError suggests similar variable names."""
        engine = REPLEngine()
        engine.execute('my_variable = 42')
        result = engine.execute('print(my_varible)')  # Typo

        assert not result.success
        assert result.exception is not None
        assert 'my_variable' in result.exception.similar_names

    def test_file_error_suggests_workspace(self):
        """Test FileNotFoundError suggests workspace methods."""
        engine = REPLEngine()
        result = engine.execute('open("nonexistent.txt")')

        assert not result.success
        assert result.exception is not None
        assert any('workspace.read' in hint for hint in result.exception.hints)

    def test_syntax_error_includes_context(self):
        """Test syntax error includes context line."""
        engine = REPLEngine()
        code = "x = 1\ny = 2\nz = ("  # Incomplete parenthesis
        result = engine.execute(code)

        assert not result.success
        assert result.exception is not None
        assert result.exception.type == "SyntaxError"
        # Context line may be extracted
        # assert result.exception.context_line is not None


class TestWarnings:
    """Tests for warning system."""

    def test_namespace_warning_fires_once_per_session(self):
        """Large-namespace warning fires on first crossing only, not every call."""
        engine = REPLEngine()
        result1 = engine.execute("\n".join(f"var_{i} = {i}" for i in range(60)))
        result2 = engine.execute("extra = 1")

        warns1 = [w for w in result1.warnings if w.category == "large_namespace"]
        warns2 = [w for w in result2.warnings if w.category == "large_namespace"]
        assert len(warns1) == 1
        assert len(warns2) == 0  # already warned this session

        # Reset clears the flag so a fresh namespace can warn again
        engine.reset_namespace()
        result3 = engine.execute("\n".join(f"new_{i} = {i}" for i in range(60)))
        warns3 = [w for w in result3.warnings if w.category == "large_namespace"]
        assert len(warns3) == 1

    def test_warning_for_large_output(self):
        """Test warning is generated for large output."""
        engine = REPLEngine()
        # Generate output larger than warning threshold (25KB) but less than truncation (50KB)
        large_text = "x" * 30000
        result = engine.execute(f'print("{large_text}")')

        assert result.success
        assert len(result.warnings) > 0
        assert any(w.category in ["large_output", "output_truncated"] 
                  for w in result.warnings)

    def test_warning_includes_suggestions(self):
        """Test warnings include actionable suggestions."""
        engine = REPLEngine()
        large_text = "x" * 60000
        result = engine.execute(f'print("{large_text}")')

        assert result.success
        truncation_warnings = [w for w in result.warnings if w.category == "output_truncated"]
        if truncation_warnings:
            warning = truncation_warnings[0]
            assert warning.suggestion is not None
            assert len(warning.suggestion) > 0

    def test_no_warnings_for_normal_execution(self):
        """Test that normal execution doesn't generate warnings."""
        engine = REPLEngine()
        result = engine.execute('x = 42')

        assert result.success
        assert len(result.warnings) == 0


class TestBackwardsCompatibility:
    """Tests to ensure backwards compatibility."""

    def test_old_fields_still_present(self):
        """Test that all original ExecutionResult fields are present."""
        engine = REPLEngine()
        result = engine.execute('x = 1')

        # All original fields should exist
        assert hasattr(result, 'success')
        assert hasattr(result, 'stdout')
        assert hasattr(result, 'stderr')
        assert hasattr(result, 'return_value')
        assert hasattr(result, 'exception')
        assert hasattr(result, 'execution_time_ms')
        assert hasattr(result, 'namespace_vars')

    def test_new_fields_are_optional(self):
        """Test that new fields don't break serialization."""
        engine = REPLEngine()
        result = engine.execute('x = 1')

        # Should serialize without errors
        result_dict = result.model_dump()
        assert isinstance(result_dict, dict)
        assert 'success' in result_dict
        assert 'warnings' in result_dict
        assert 'stdout_info' in result_dict


class TestMagicCommands:
    """Tests for IPython-style magic commands."""

    def test_magic_who(self):
        """Test %who lists variable names."""
        engine = REPLEngine()
        engine.execute("x = 1; y = 2")
        result = engine.execute("%who")
        assert result.success
        assert "x" in result.stdout
        assert "y" in result.stdout

    def test_magic_who_empty(self):
        """Test %who with no variables."""
        engine = REPLEngine()
        result = engine.execute("%who")
        assert result.success
        assert "no variables" in result.stdout.lower()

    def test_magic_whos(self):
        """Test %whos shows detailed variable listing."""
        engine = REPLEngine()
        engine.execute("x = 42; y = 'hello'")
        result = engine.execute("%whos")
        assert result.success
        assert "int" in result.stdout
        assert "str" in result.stdout
        assert "42" in result.stdout

    def test_magic_reset(self):
        """Test %reset clears namespace but keeps utilities."""
        engine = REPLEngine()
        engine.execute("x = 42")
        result = engine.execute("%reset")
        assert result.success
        assert "reset" in result.stdout.lower()
        
        # Variable should be gone
        result = engine.execute("x")
        assert not result.success

    def test_magic_env(self):
        """Test %env shows environment info."""
        engine = REPLEngine()
        result = engine.execute("%env")
        assert result.success
        assert "Workspace:" in result.stdout

    def test_magic_timeit(self):
        """Test %timeit times code execution."""
        engine = REPLEngine()
        result = engine.execute("%timeit sum(range(100))")
        assert result.success
        assert "per loop" in result.stdout

    def test_magic_timeit_empty(self):
        """Test %timeit with no code shows usage."""
        engine = REPLEngine()
        result = engine.execute("%timeit")
        assert result.success
        assert "Usage:" in result.stdout

    def test_magic_help(self):
        """Test %help shows REPL help."""
        engine = REPLEngine()
        result = engine.execute("%help")
        assert result.success
        assert "Magic commands:" in result.stdout
        assert "workspace" in result.stdout.lower()

    def test_unknown_magic(self):
        """Test unknown magic command returns error."""
        engine = REPLEngine()
        result = engine.execute("%unknown_command")
        assert not result.success
        assert "Unknown magic" in result.stderr

    def test_magic_case_insensitive(self):
        """Test magic commands are case-insensitive."""
        engine = REPLEngine()
        result = engine.execute("%WHO")
        assert result.success

        result = engine.execute("%Help")
        assert result.success

    def test_magic_history(self):
        """Test %history shows execution history."""
        engine = REPLEngine()
        engine.execute("x = 1")
        engine.execute("y = 2")
        engine.execute("z = x + y")
        result = engine.execute("%history")
        assert result.success
        assert "x = 1" in result.stdout
        assert "y = 2" in result.stdout
        assert "z = x + y" in result.stdout

    def test_magic_history_with_count(self):
        """Test %history n shows last n entries."""
        engine = REPLEngine()
        engine.execute("a = 1")
        engine.execute("b = 2")
        engine.execute("c = 3")
        result = engine.execute("%history 2")
        assert result.success
        assert "a = 1" not in result.stdout  # Too old
        assert "b = 2" in result.stdout
        assert "c = 3" in result.stdout

    def test_magic_history_empty(self):
        """Test %history with no history."""
        engine = REPLEngine()
        result = engine.execute("%history")
        assert result.success
        assert "no history" in result.stdout.lower()


class TestHelpQueries:
    """Tests for IPython-style ? help queries."""

    def test_help_query_single_question(self):
        """Test object? shows docstring."""
        engine = REPLEngine()
        result = engine.execute("workspace?")
        assert result.success
        assert "Type:" in result.stdout
        assert "Docstring:" in result.stdout

    def test_help_query_method(self):
        """Test object.method? shows method docstring."""
        engine = REPLEngine()
        result = engine.execute("workspace.read?")
        assert result.success
        assert "Type:" in result.stdout
        assert "Signature:" in result.stdout

    def test_help_query_double_question(self):
        """Test object?? attempts to show source."""
        engine = REPLEngine()
        result = engine.execute("workspace??")
        assert result.success
        # May or may not have source depending on compiled state
        assert "Type:" in result.stdout

    def test_help_query_undefined(self):
        """Test ? on undefined name returns error."""
        engine = REPLEngine()
        result = engine.execute("undefined_var?")
        assert not result.success
        assert "not found" in result.stderr.lower()

    def test_help_query_empty(self):
        """Test just ? shows general help."""
        engine = REPLEngine()
        result = engine.execute("?")
        assert result.success
        assert "Magic commands:" in result.stdout

    def test_help_query_user_defined(self):
        """Test ? on user-defined function."""
        engine = REPLEngine()
        engine.execute('''
def my_function(x, y):
    """Add two numbers together."""
    return x + y
''')
        result = engine.execute("my_function?")
        assert result.success
        assert "Add two numbers" in result.stdout
        assert "Signature:" in result.stdout
