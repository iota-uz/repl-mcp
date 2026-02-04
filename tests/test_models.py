"""Unit tests for data models."""

from datetime import datetime
import pytest
from repl_mcp.models import (
    ExecutionResult, ExceptionInfo, ServerConfig, WarningInfo,
    CommitInfo, FileDiff, BlameLine, GitStatus, BranchInfo,
    FunctionDef, ClassDef, CallSite,
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

    def test_commit_info_str(self):
        """Test CommitInfo str format."""
        commit = CommitInfo(
            hash="abc123def456",
            short_hash="abc123d",
            message="Fix important bug in authentication",
            author_name="Jane Doe",
            author_email="jane@example.com",
            authored_date=datetime(2024, 1, 15, 10, 30),
        )
        output = str(commit)
        assert "abc123d" in output
        assert "Fix important bug" in output
        assert "Jane Doe" in output
        assert "2024-01-15" in output

    def test_file_diff_str(self):
        """Test FileDiff str format."""
        diff = FileDiff(
            path="src/main.py",
            change_type="modified",
            additions=10,
            deletions=3,
        )
        output = str(diff)
        assert "M" in output
        assert "src/main.py" in output
        assert "+10/-3" in output

    def test_file_diff_str_renamed(self):
        """Test FileDiff str for renamed file."""
        diff = FileDiff(
            path="new_name.py",
            old_path="old_name.py",
            change_type="renamed",
        )
        output = str(diff)
        assert "R" in output
        assert "old_name.py → new_name.py" in output

    def test_blame_line_str(self):
        """Test BlameLine str format."""
        blame = BlameLine(
            line_num=42,
            content="    return x + y",
            commit_hash="abc123def456",
            short_hash="abc123d",
            author="John Doe",
            author_email="john@example.com",
            date=datetime(2024, 1, 10),
        )
        output = str(blame)
        assert "42" in output
        assert "abc123d" in output
        assert "John Doe" in output
        assert "return x + y" in output

    def test_git_status_str(self):
        """Test GitStatus str format."""
        status = GitStatus(
            branch="main",
            tracking_branch="origin/main",
            ahead=2,
            behind=1,
            staged=["file1.py"],
            unstaged=["file2.py", "file3.py"],
            is_dirty=True,
        )
        output = str(status)
        assert "main" in output
        assert "ahead 2" in output
        assert "behind 1" in output
        assert "Staged:" in output
        assert "Modified:" in output

    def test_git_status_str_clean(self):
        """Test GitStatus str for clean working tree."""
        status = GitStatus(
            branch="feature",
            is_dirty=False,
        )
        output = str(status)
        assert "feature" in output
        assert "clean" in output.lower()

    def test_branch_info_str(self):
        """Test BranchInfo str format."""
        branch = BranchInfo(
            name="feature/new-thing",
            is_current=True,
            commit_hash="abc123def456",
            commit_message="Add new feature",
        )
        output = str(branch)
        assert "* " in output  # current branch marker
        assert "feature/new-thing" in output
        assert "abc123d" in output

    def test_function_def_str(self):
        """Test FunctionDef str format."""
        func = FunctionDef(
            file="src/utils.py",
            line=42,
            name="calculate_total",
            params=["items", "discount"],
            return_annotation="float",
        )
        output = str(func)
        assert "src/utils.py:42" in output
        assert "def calculate_total" in output
        assert "items, discount" in output
        assert "-> float" in output

    def test_function_def_str_async(self):
        """Test FunctionDef str for async function."""
        func = FunctionDef(
            file="src/api.py",
            line=10,
            name="fetch_data",
            params=["url"],
            is_async=True,
        )
        output = str(func)
        assert "async def" in output

    def test_class_def_str(self):
        """Test ClassDef str format."""
        cls = ClassDef(
            file="src/models.py",
            line=15,
            name="UserModel",
            bases=["BaseModel"],
            methods=["validate", "save", "delete"],
        )
        output = str(cls)
        assert "src/models.py:15" in output
        assert "class UserModel(BaseModel)" in output
        assert "[3 methods]" in output

    def test_call_site_str(self):
        """Test CallSite str format."""
        call = CallSite(
            file="src/main.py",
            line=100,
            column=4,
            function_name="process",
            full_call="data.process",
        )
        output = str(call)
        assert "src/main.py:100" in output
        assert "data.process()" in output
