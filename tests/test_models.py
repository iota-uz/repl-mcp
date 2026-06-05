"""Unit tests for data models."""

import pytest
from repl_mcp.models import (
    ExecutionResult, ExceptionInfo, ServerConfig, WarningInfo,
)


class TestModels:
    """Tests for Pydantic models."""

    def test_execution_result_success(self):
        """Test ExecutionResult for successful execution."""
        result = ExecutionResult(
            success=True,
            stdout="output",
            stderr="",
            return_value="42",
            execution_time_ms=10.5,
            namespace_vars={"x": "42"}
        )

        assert result.success
        assert result.stdout == "output"
        assert result.return_value == "42"
        assert result.exception is None

    def test_execution_result_with_exception(self):
        """Test ExecutionResult with exception."""
        exc_info = ExceptionInfo(
            type="ValueError",
            message="invalid value",
            traceback="Traceback..."
        )

        result = ExecutionResult(
            success=False,
            exception=exc_info,
            execution_time_ms=5.0,
        )

        assert not result.success
        assert result.exception.type == "ValueError"
        assert result.exception.message == "invalid value"

    def test_exception_info(self):
        """Test ExceptionInfo model."""
        exc = ExceptionInfo(
            type="TypeError",
            message="type mismatch",
            traceback="Full traceback here"
        )

        assert exc.type == "TypeError"
        assert exc.message == "type mismatch"
        assert exc.traceback == "Full traceback here"

    def test_server_config_stdio(self):
        """Test ServerConfig for stdio transport."""
        config = ServerConfig(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": "token123"}
        )

        assert config.command == "npx"
        assert len(config.args) == 2
        assert config.transport_type == "stdio"

    def test_server_config_sse(self):
        """Test ServerConfig for SSE transport."""
        config = ServerConfig(
            url="http://localhost:3001/sse"
        )

        assert config.url == "http://localhost:3001/sse"
        assert config.transport_type == "sse"

    def test_server_config_defaults(self):
        """Test ServerConfig defaults."""
        config = ServerConfig(command="test")

        assert config.args is None
        assert config.env is None
        assert config.url is None
        assert config.headers is None

    def test_server_config_http(self):
        """Test ServerConfig for streamable HTTP transport (Claude Code style)."""
        config = ServerConfig(
            type="http",
            url="https://api.example.com/mcp/",
            headers={"Authorization": "Bearer ${MY_TOKEN}"},
        )

        assert config.transport_type == "http"
        assert config.headers == {"Authorization": "Bearer ${MY_TOKEN}"}

    def test_server_config_explicit_sse(self):
        """Test explicit type=sse wins over the http heuristic."""
        config = ServerConfig(type="sse", url="https://api.example.com/sse")

        assert config.transport_type == "sse"

    def test_execution_result_defaults(self):
        """Test ExecutionResult default values."""
        result = ExecutionResult(
            success=True,
            execution_time_ms=1.0
        )

        assert result.stdout == ""
        assert result.stderr == ""
        assert result.return_value is None
        assert result.exception is None
        assert result.namespace_vars == {}

    def test_model_serialization(self):
        """Test models can be serialized to dict."""
        result = ExecutionResult(
            success=True,
            stdout="test",
            execution_time_ms=2.5,
            namespace_vars={"x": "1"}
        )

        data = result.model_dump()

        assert isinstance(data, dict)
        assert data["success"] is True
        assert data["stdout"] == "test"
        assert data["execution_time_ms"] == 2.5


class TestModelStrMethods:
    """Tests for human-readable __str__ methods on models."""

    def test_execution_result_str_success_with_output(self):
        """Test ExecutionResult str with stdout and return value."""
        result = ExecutionResult(
            success=True,
            stdout="Hello\n",
            return_value="42",
            execution_time_ms=1.0,
        )
        output = str(result)
        assert "Hello" in output
        assert "→ 42" in output

    def test_execution_result_str_success_no_output(self):
        """Test ExecutionResult str with no output."""
        result = ExecutionResult(
            success=True,
            execution_time_ms=1.0,
        )
        output = str(result)
        assert "executed successfully" in output.lower()

    def test_execution_result_str_error(self):
        """Test ExecutionResult str with error."""
        result = ExecutionResult(
            success=False,
            exception=ExceptionInfo(
                type="ValueError",
                message="bad value",
                traceback="Traceback...",
                hints=["Try using a positive number"],
            ),
            execution_time_ms=1.0,
        )
        output = str(result)
        assert "ValueError" in output
        assert "bad value" in output
        assert "Hint:" in output

    def test_execution_result_str_with_warning(self):
        """Test ExecutionResult str with warnings."""
        result = ExecutionResult(
            success=True,
            return_value="[large list...]",
            execution_time_ms=1.0,
            warnings=[WarningInfo(category="large_output", message="Output truncated")],
        )
        output = str(result)
        assert "⚠" in output
        assert "truncated" in output.lower()
