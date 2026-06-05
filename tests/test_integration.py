"""Integration tests for REPL MCP server."""

import pytest
from repl_mcp import repl_mcp_server

def _init_globals():
    """Initialize module globals the way the server lifespan does."""
    from repl_mcp.mcp_client_wrapper import MCPClientWrapper
    from repl_mcp.repl_engine import REPLEngine
    repl_mcp_server.mcp_wrapper = MCPClientWrapper()
    repl_mcp_server.repl_engine = REPLEngine(mcp_wrapper=repl_mcp_server.mcp_wrapper)


class TestServerIntegration:
    """End-to-end integration tests."""

    def test_server_initialization(self):
        """Test server initializes correctly."""
        _init_globals()

        assert repl_mcp_server.mcp_wrapper is not None
        assert repl_mcp_server.repl_engine is not None

    def test_execute_python_basic(self):
        """Test basic code execution."""
        _init_globals()

        result = repl_mcp_server.repl_engine.execute("x = 42")
        assert result.success
        assert "x" in result.namespace_vars

    def test_execute_python_with_state(self):
        """Test state persistence across executions."""
        _init_globals()
        engine = repl_mcp_server.repl_engine

        result1 = engine.execute("x = 42")
        assert result1.success

        result2 = engine.execute("y = x + 8")
        assert result2.success
        assert result2.namespace_vars["y"] == "50"

    def test_execute_python_with_output(self):
        """Test output capture."""
        _init_globals()
        engine = repl_mcp_server.repl_engine

        result = engine.execute('print("Hello from REPL!")')
        assert result.success
        assert "Hello from REPL!" in result.stdout

    def test_execute_python_with_return(self):
        """Test return value capture."""
        _init_globals()
        engine = repl_mcp_server.repl_engine

        engine.execute("x = 42")
        engine.execute("y = 50")

        result = engine.execute("x + y")
        assert result.success
        assert result.return_value == "92"

    def test_error_handling(self):
        """Test error handling and recovery."""
        _init_globals()
        engine = repl_mcp_server.repl_engine

        # Set a variable
        engine.execute("x = 42")

        # Execute code that raises exception
        result_error = engine.execute("1 / 0")
        assert not result_error.success
        assert result_error.exception is not None
        assert result_error.exception.type == "ZeroDivisionError"

        # Verify namespace still works
        result_check = engine.execute("x")
        assert result_check.success
        assert result_check.return_value == "42"

    def test_imports_and_functions(self):
        """Test imports and function definitions."""
        _init_globals()
        engine = repl_mcp_server.repl_engine

        # Test import
        result1 = engine.execute("import json")
        assert result1.success

        # Test using import
        result2 = engine.execute('json.dumps({"key": "value"})')
        assert result2.success
        assert "key" in result2.return_value

        # Test function definition
        result3 = engine.execute("""
def greet(name):
    return f"Hello, {name}!"
""")
        assert result3.success

        # Test function call
        result4 = engine.execute('greet("World")')
        assert result4.success
        assert "Hello, World!" in result4.return_value

    def test_namespace_operations(self):
        """Test namespace operations."""
        _init_globals()
        engine = repl_mcp_server.repl_engine

        # Set some variables
        engine.execute("x = 42")
        engine.execute("y = 50")

        # List namespace vars
        vars_dict = engine.get_namespace_vars()
        assert "x" in vars_dict
        assert "y" in vars_dict

        # Reset namespace
        engine.reset_namespace()

        vars_after_reset = engine.get_namespace_vars()
        assert "x" not in vars_after_reset
        assert "y" not in vars_after_reset

    def test_mcp_client_wrapper(self):
        """Test MCP client wrapper structure."""
        _init_globals()
        wrapper = repl_mcp_server.mcp_wrapper

        # No servers connected: empty listing, clean help
        assert wrapper.list_tools() == {}
        assert wrapper.servers == []
        assert "No MCP servers" in wrapper.help()

    def test_multiline_code(self):
        """Test multiline code execution."""
        _init_globals()
        engine = repl_mcp_server.repl_engine

        code = """
# Create a list
numbers = [1, 2, 3, 4, 5]

# Calculate sum
total = sum(numbers)

# Return result
total * 2
"""

        result = engine.execute(code)
        assert result.success
        assert result.return_value == "30"
        assert "numbers" in result.namespace_vars
        assert "total" in result.namespace_vars


class TestConfigLoading:
    """Tests for configuration loading."""

    def test_load_mcp_config_missing(self, tmp_path):
        """Test loading config when file doesn't exist."""
        config_path = tmp_path / "nonexistent.json"
        config = repl_mcp_server.load_mcp_config(config_path)
        assert config == {}

    def test_load_mcp_config_invalid(self, tmp_path):
        """Test loading invalid config file."""
        config_path = tmp_path / "invalid.json"
        config_path.write_text("not valid json")

        config = repl_mcp_server.load_mcp_config(config_path)
        assert config == {}

    def test_load_mcp_config_valid(self, tmp_path):
        """Test loading valid config file."""
        config_path = tmp_path / "valid.json"
        config_path.write_text("""
{
  "mcpServers": {
    "test": {
      "command": "echo",
      "args": ["hello"]
    }
  }
}
""")

        config = repl_mcp_server.load_mcp_config(config_path)
        assert "test" in config
        assert config["test"]["command"] == "echo"
