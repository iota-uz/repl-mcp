"""Tests for MCP client wrapper introspection features."""

import pytest
from repl_mcp.mcp_client_wrapper import (
    MCPClientWrapper,
    _expand_env_value,
    _failure_reason,
)


class TestMCPClientWrapperIntrospection:
    """Tests for MCPClientWrapper dir()."""

    def test_dir_includes_expected_methods(self):
        """Test that dir(mcp) includes expected public methods."""
        wrapper = MCPClientWrapper()

        result = dir(wrapper)
        expected = ['servers', 'failed', 'call', 'list_tools', 'help']
        for item in expected:
            assert item in result

    def test_call_not_connected_raises(self):
        """Test that mcp.call() on an unknown server raises a clear error."""
        wrapper = MCPClientWrapper()

        with pytest.raises(ValueError, match="not configured"):
            wrapper.call('github', 'create_issue', title='Bug')

    def test_call_unknown_server_lists_available(self):
        """The error must tell the agent what it *can* call."""
        wrapper = MCPClientWrapper()
        wrapper.register_raw({"telegram-mcp": {"command": "true"}})

        with pytest.raises(ValueError, match="telegram-mcp"):
            wrapper.call('github', 'create_issue', title='Bug')

    def test_call_delegates_to_invoke_tool(self):
        """Test that mcp.call() delegates to _invoke_tool with args intact."""
        wrapper = MCPClientWrapper()
        captured = {}

        def fake_invoke(server, tool, timeout=60.0, **kwargs):
            captured.update(server=server, tool=tool, timeout=timeout, kwargs=kwargs)
            return "ok"

        wrapper._invoke_tool = fake_invoke
        result = wrapper.call('github', 'create_issue', timeout=30.0, title='Bug')

        assert result == "ok"
        assert captured == {
            'server': 'github',
            'tool': 'create_issue',
            'timeout': 30.0,
            'kwargs': {'title': 'Bug'},
        }

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
        assert "call" in result.return_value
        assert "servers" in result.return_value
        assert "failed" in result.return_value


class TestEnvExpansion:
    """Tests for ${VAR} expansion in config values (headers, env)."""

    def test_full_value_reference(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret")
        assert _expand_env_value("${MY_TOKEN}") == "secret"

    def test_embedded_reference(self, monkeypatch):
        """Bearer ${TOKEN} — the birbozor 401 regression."""
        monkeypatch.setenv("MY_TOKEN", "secret")
        assert _expand_env_value("Bearer ${MY_TOKEN}") == "Bearer secret"

    def test_multiple_references(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _expand_env_value("${A}-${B}") == "1-2"

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert _expand_env_value("${NOPE:-fallback}") == "fallback"

    def test_env_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("YEP", "real")
        assert _expand_env_value("${YEP:-fallback}") == "real"

    def test_unset_without_default_is_empty(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert _expand_env_value("Bearer ${NOPE}") == "Bearer "

    def test_plain_value_untouched(self):
        assert _expand_env_value("application/json") == "application/json"


class TestFailureReason:
    """Tests for ExceptionGroup unwrapping in connect-failure messages."""

    def test_plain_exception(self):
        reason = _failure_reason(ValueError("bad config"))
        assert reason == "ValueError: bad config"

    def test_nested_exception_group_unwraps_to_leaf(self):
        """anyio wraps the real error (e.g. HTTP 401) in nested groups."""
        try:
            from exceptiongroup import ExceptionGroup as EG  # py3.10 backport
        except ImportError:
            EG = ExceptionGroup
        leaf = ConnectionError("401 Unauthorized for url https://x/mcp/")
        group = EG("unhandled errors in a TaskGroup", [EG("inner", [leaf])])

        reason = _failure_reason(group)
        assert "401 Unauthorized" in reason
        assert "TaskGroup" not in reason

    def test_multiple_leaves_capped(self):
        try:
            from exceptiongroup import ExceptionGroup as EG
        except ImportError:
            EG = ExceptionGroup
        leaves = [ValueError(f"e{i}") for i in range(5)]
        group = EG("group", leaves)

        reason = _failure_reason(group)
        assert "e0" in reason and "e2" in reason
        assert "and 2 more" in reason
