"""Tests for MCP client wrapper introspection features."""

import pytest
from repl_mcp.mcp_client_wrapper import MCPClientWrapper, ToolsContainer, ToolNamespace


class TestToolNamespaceIntrospection:
    """Tests for ToolNamespace dir(), repr(), and docstrings."""

    def test_dir_returns_empty_when_not_connected(self):
        """Test that dir() returns empty list when server not connected."""
        wrapper = MCPClientWrapper()
        ns = ToolNamespace(wrapper, "fake_server")

        # Should return empty list (server doesn't exist)
        result = dir(ns)
        assert isinstance(result, list)
        assert result == []

    def test_repr_when_not_connected(self):
        """Test repr shows appropriate message when not connected."""
        wrapper = MCPClientWrapper()
        ns = ToolNamespace(wrapper, "fake_server")

        result = repr(ns)
        assert "fake_server" in result
        assert "not connected" in result or "no tools" in result

    def test_tool_callable_has_name(self):
        """Test that tool callables have proper __name__."""
        wrapper = MCPClientWrapper()
        ns = ToolNamespace(wrapper, "github")

        func = ns.create_issue
        assert func.__name__ == "create_issue"

    def test_tool_callable_has_qualname(self):
        """Test that tool callables have proper __qualname__."""
        wrapper = MCPClientWrapper()
        ns = ToolNamespace(wrapper, "github")

        func = ns.create_issue
        assert func.__qualname__ == "mcp.tools.github.create_issue"

    def test_tool_callable_has_docstring(self):
        """Test that tool callables have a docstring."""
        wrapper = MCPClientWrapper()
        ns = ToolNamespace(wrapper, "github")

        func = ns.create_issue
        assert func.__doc__ is not None
        assert len(func.__doc__) > 0
        # Should mention the tool name
        assert "create_issue" in func.__doc__ or "github" in func.__doc__

    def test_private_attr_raises_attribute_error(self):
        """Test that accessing private attributes raises AttributeError."""
        wrapper = MCPClientWrapper()
        ns = ToolNamespace(wrapper, "github")

        with pytest.raises(AttributeError):
            _ = ns._private_thing


class TestToolsContainerIntrospection:
    """Tests for ToolsContainer dir() and repr()."""

    def test_dir_returns_empty_when_no_servers(self):
        """Test that dir() returns empty list when no servers connected."""
        wrapper = MCPClientWrapper()
        container = wrapper.tools

        result = dir(container)
        assert isinstance(result, list)
        assert result == []

    def test_repr_when_no_servers(self):
        """Test repr shows appropriate message when no servers."""
        wrapper = MCPClientWrapper()
        container = wrapper.tools

        result = repr(container)
        assert "no servers" in result.lower()

    def test_getattr_returns_tool_namespace(self):
        """Test that attribute access returns ToolNamespace."""
        wrapper = MCPClientWrapper()
        container = wrapper.tools

        ns = container.some_server
        assert isinstance(ns, ToolNamespace)
        assert ns._server == "some_server"

    def test_private_attr_raises_attribute_error(self):
        """Test that accessing private attributes raises AttributeError."""
        wrapper = MCPClientWrapper()
        container = wrapper.tools

        with pytest.raises(AttributeError):
            _ = container._private_thing


class TestMCPClientWrapperIntrospection:
    """Tests for MCPClientWrapper dir()."""

    def test_dir_includes_expected_methods(self):
        """Test that dir(mcp) includes expected public methods."""
        wrapper = MCPClientWrapper()

        result = dir(wrapper)
        expected = ['tools', 'servers', 'list_tools', 'help', 'discover_tools']
        for item in expected:
            assert item in result

    def test_servers_property_empty_initially(self):
        """Test that servers property is empty when nothing connected."""
        wrapper = MCPClientWrapper()

        assert wrapper.servers == []

    def test_list_tools_empty_initially(self):
        """Test that list_tools returns empty when nothing connected."""
        wrapper = MCPClientWrapper()

        result = wrapper.list_tools()
        assert result == {}

    def test_help_no_servers(self):
        """Test help() message when no servers connected."""
        wrapper = MCPClientWrapper()

        result = wrapper.help()
        assert "No MCP servers" in result


class TestIntrospectionIntegration:
    """Integration tests for introspection from REPL context."""

    def test_dir_mcp_in_repl(self):
        """Test that dir(mcp) works in REPL context."""
        from repl_mcp.repl_engine import REPLEngine

        wrapper = MCPClientWrapper()
        engine = REPLEngine(mcp_wrapper=wrapper)

        result = engine.execute("dir(mcp)")
        assert result.success
        assert "tools" in result.return_value
        assert "servers" in result.return_value

    def test_dir_mcp_tools_in_repl(self):
        """Test that dir(mcp.tools) works in REPL context."""
        from repl_mcp.repl_engine import REPLEngine

        wrapper = MCPClientWrapper()
        engine = REPLEngine(mcp_wrapper=wrapper)

        result = engine.execute("dir(mcp.tools)")
        assert result.success
        # Should return empty list (no servers)
        assert "[]" in result.return_value

    def test_repr_mcp_tools_in_repl(self):
        """Test that repr(mcp.tools) is informative."""
        from repl_mcp.repl_engine import REPLEngine

        wrapper = MCPClientWrapper()
        engine = REPLEngine(mcp_wrapper=wrapper)

        result = engine.execute("mcp.tools")
        assert result.success
        assert "ToolsContainer" in result.return_value
        assert "no servers" in result.return_value.lower()

    def test_repr_tool_namespace_in_repl(self):
        """Test that repr(mcp.tools.server) is informative."""
        from repl_mcp.repl_engine import REPLEngine

        wrapper = MCPClientWrapper()
        engine = REPLEngine(mcp_wrapper=wrapper)

        result = engine.execute("mcp.tools.github")
        assert result.success
        assert "ToolNamespace" in result.return_value
        assert "github" in result.return_value

    def test_help_on_tool_callable_in_repl(self):
        """Test that help() works on tool callables."""
        from repl_mcp.repl_engine import REPLEngine

        wrapper = MCPClientWrapper()
        engine = REPLEngine(mcp_wrapper=wrapper)

        # Get a tool callable and check its docstring
        result = engine.execute("mcp.tools.github.create_issue.__doc__")
        assert result.success
        assert result.return_value is not None
        # Should have some documentation
        assert "create_issue" in result.return_value or "github" in result.return_value

    def test_help_query_on_tool_callable(self):
        """Test that object? works on tool callables."""
        from repl_mcp.repl_engine import REPLEngine

        wrapper = MCPClientWrapper()
        engine = REPLEngine(mcp_wrapper=wrapper)

        result = engine.execute("mcp.tools.github.create_issue?")
        assert result.success
        # Should show type and docstring info
        assert "Type:" in result.stdout or "Docstring:" in result.stdout
