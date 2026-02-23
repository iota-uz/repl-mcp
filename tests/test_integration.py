"""Integration tests for REPL MCP server."""

import pytest
from repl_mcp.config import RuntimeConfig
from repl_mcp import repl_mcp_server


class TestServerIntegration:
    """End-to-end integration tests."""

    def test_server_initialization(self):
        """Test server initializes correctly."""
        repl_mcp_server.initialize_server(autoconnect=False)

        assert repl_mcp_server.mcp_wrapper is not None
        assert repl_mcp_server.repl_engine is not None

    def test_execute_python_basic(self):
        """Test basic code execution."""
        repl_mcp_server.initialize_server(autoconnect=False)

        result = repl_mcp_server.repl_engine.execute("x = 42")
        assert result.success
        assert "x" in result.namespace_vars

    def test_execute_python_with_state(self):
        """Test state persistence across executions."""
        repl_mcp_server.initialize_server(autoconnect=False)
        engine = repl_mcp_server.repl_engine

        result1 = engine.execute("x = 42")
        assert result1.success

        result2 = engine.execute("y = x + 8")
        assert result2.success
        assert result2.namespace_vars["y"] == "50"

    def test_execute_python_with_output(self):
        """Test output capture."""
        repl_mcp_server.initialize_server(autoconnect=False)
        engine = repl_mcp_server.repl_engine

        result = engine.execute('print("Hello from REPL!")')
        assert result.success
        assert "Hello from REPL!" in result.stdout

    def test_execute_python_with_return(self):
        """Test return value capture."""
        repl_mcp_server.initialize_server(autoconnect=False)
        engine = repl_mcp_server.repl_engine

        engine.execute("x = 42")
        engine.execute("y = 50")

        result = engine.execute("x + y")
        assert result.success
        assert result.return_value == "92"

    def test_error_handling(self):
        """Test error handling and recovery."""
        repl_mcp_server.initialize_server(autoconnect=False)
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
        repl_mcp_server.initialize_server(autoconnect=False)
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
        repl_mcp_server.initialize_server(autoconnect=False)
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
        repl_mcp_server.initialize_server(autoconnect=False)
        wrapper = repl_mcp_server.mcp_wrapper

        # Test discover_tools (should be empty since no servers connected)
        tools = wrapper.discover_tools()
        assert isinstance(tools, dict)

        # Test tools namespace exists
        assert hasattr(wrapper, "tools")

    def test_multiline_code(self):
        """Test multiline code execution."""
        repl_mcp_server.initialize_server(autoconnect=False)
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

    def test_initialize_server_daytona_mode(self):
        """Test Daytona-mode initialization creates session manager."""
        repl_mcp_server.initialize_server(
            autoconnect=False,
            runtime_config=RuntimeConfig(
                daytona_api_url="https://api.daytona.test",
                daytona_api_key="secret",
            ),
        )
        assert repl_mcp_server.session_manager is not None
        assert repl_mcp_server.repl_engine is None


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
