"""Tests for server initialization."""

import inspect
from pathlib import Path
from repl_mcp import repl_mcp_server


def test_load_mcp_config_missing_file():
    """Test loading config when file doesn't exist."""
    config = repl_mcp_server.load_mcp_config(Path("/nonexistent/.mcp.json"))
    assert config == {}


def test_load_mcp_config_valid():
    """Test loading valid config file."""
    # Use the project's .mcp.json
    config_path = Path(__file__).parent.parent / ".mcp.json"
    if config_path.exists():
        config = repl_mcp_server.load_mcp_config(config_path)
        assert isinstance(config, dict)
        # Should have github server configured
        if "github" in config:
            assert "command" in config["github"]


def test_initialize_server_no_autoconnect():
    """Test server initializes without auto-connecting."""
    # This should not fail even if .mcp.json has invalid configs
    repl_mcp_server.initialize_server(autoconnect=False)

    assert repl_mcp_server.mcp_wrapper is not None
    assert repl_mcp_server.repl_engine is not None


def test_tools_registered():
    """Test that tools are defined in the server module."""
    # Tools are registered via @mcp_server.tool() decorators within create_server()
    # Just verify the module structure is correct
    import repl_mcp.repl_mcp_server as server

    # Check the server has the expected structure
    assert hasattr(server, 'create_server')
    assert hasattr(server, 'mcp_wrapper')
    assert hasattr(server, 'repl_engine')
    assert hasattr(server, 'initialize_server')
    assert hasattr(server, 'load_mcp_config')
    assert hasattr(server, 'filter_servers')
    assert hasattr(server, 'create_server_lifespan')


def test_execute_python_via_engine():
    """Test execute_python functionality via engine."""
    # Initialize server
    repl_mcp_server.initialize_server(autoconnect=False)

    # Access the engine directly
    engine = repl_mcp_server.repl_engine

    # Execute simple code
    result = engine.execute("x = 42")

    assert result.success is True
    assert "x" in result.namespace_vars


def test_list_namespace_vars_via_engine():
    """Test list_namespace_vars via engine."""
    repl_mcp_server.initialize_server(autoconnect=False)

    engine = repl_mcp_server.repl_engine

    # Set a variable
    engine.execute("test_var = 'hello'")

    # List vars
    vars_dict = engine.get_namespace_vars()

    assert isinstance(vars_dict, dict)
    assert "test_var" in vars_dict


def test_execute_python_no_return_annotation():
    """
    Regression test: execute_python must NOT have a return type annotation.

    FastMCP wraps annotated primitive returns (-> str, -> int) in
    {"result": "..."} JSON via structuredContent. We want plain text output,
    so the return annotation must be omitted.

    If this test fails, someone added a return type annotation - remove it!
    """
    server = repl_mcp_server.create_server()

    # Find the execute_python tool function
    # FastMCP stores tools internally, but we can check the source
    import repl_mcp.repl_mcp_server as module
    source = inspect.getsource(module.create_server)

    # Check that "def execute_python" doesn't have "-> str" or similar
    # Look for pattern: "def execute_python(...) -> " which would indicate annotation
    import re
    pattern = r"def execute_python\([^)]*\)\s*->"
    match = re.search(pattern, source)

    assert match is None, (
        "execute_python has a return type annotation! "
        "This causes FastMCP to wrap output in JSON. Remove the annotation."
    )
