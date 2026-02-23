"""Tests for chat session manager behavior."""

from datetime import datetime, timedelta, timezone

from repl_mcp.daytona_adapter import BashResult
from repl_mcp.models import ExecutionResult
from repl_mcp.session_manager import ChatSessionManager


class FakeAdapter:
    def __init__(self):
        self.ensure_calls: list[str] = []
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.archive_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.upload_calls: list[tuple[str, str, bytes, int]] = []
        self.bash_calls: list[tuple[str, str]] = []

    def ensure_session(self, session_id: str):
        self.ensure_calls.append(session_id)
        return object()

    def start_session(self, session_id: str) -> None:
        self.start_calls.append(session_id)

    def stop_session(self, session_id: str) -> None:
        self.stop_calls.append(session_id)

    def archive_session(self, session_id: str) -> None:
        self.archive_calls.append(session_id)

    def delete_session(self, session_id: str) -> None:
        self.delete_calls.append(session_id)

    def execute_python(self, session_id: str, code: str, **_kwargs) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            stdout="",
            return_value=repr(f"{session_id}:{code}"),
            execution_time_ms=1.0,
        )

    def upload_bytes(self, session_id: str, path: str, data: bytes, timeout_seconds: int = 1800) -> None:
        self.upload_calls.append((session_id, path, data, timeout_seconds))

    def run_bash(self, session_id: str, command: str, **_kwargs) -> BashResult:
        self.bash_calls.append((session_id, command))
        return BashResult(exit_code=0, stdout="ok", stderr="")


def test_get_or_create_session_and_delegate_calls():
    adapter = FakeAdapter()
    manager = ChatSessionManager(adapter, idle_timeout_minutes=30, idle_action="stop")

    manager.get_or_create_session("chat-1")
    result = manager.execute_python("chat-1", "1 + 1")
    manager.upload_bytes("chat-1", "file.txt", b"hello")
    bash = manager.run_bash("chat-1", "echo hi")

    assert adapter.ensure_calls == ["chat-1"]
    assert result.success is True
    assert "chat-1" in (result.return_value or "")
    assert adapter.upload_calls[0][1] == "file.txt"
    assert bash.exit_code == 0


def test_idle_policy_stop_and_restore():
    adapter = FakeAdapter()
    manager = ChatSessionManager(adapter, idle_timeout_minutes=30, idle_action="stop")
    session = manager.get_or_create_session("chat-1")
    session.last_activity_at = datetime.now(timezone.utc) - timedelta(minutes=60)

    stale = manager.apply_idle_policy(now=datetime.now(timezone.utc))
    assert stale == ["chat-1"]
    assert adapter.stop_calls == ["chat-1"]

    # Next message should revive/start the session.
    manager.get_or_create_session("chat-1")
    assert adapter.start_calls == ["chat-1"]


def test_idle_policy_archive():
    adapter = FakeAdapter()
    manager = ChatSessionManager(adapter, idle_timeout_minutes=5, idle_action="archive")
    session = manager.get_or_create_session("chat-arch")
    session.last_activity_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    stale = manager.apply_idle_policy(now=datetime.now(timezone.utc))
    assert stale == ["chat-arch"]
    assert adapter.archive_calls == ["chat-arch"]


def test_idle_policy_delete_removes_session():
    adapter = FakeAdapter()
    manager = ChatSessionManager(adapter, idle_timeout_minutes=5, idle_action="delete")
    session = manager.get_or_create_session("chat-del")
    session.last_activity_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    stale = manager.apply_idle_policy(now=datetime.now(timezone.utc))
    assert stale == ["chat-del"]
    assert adapter.delete_calls == ["chat-del"]
    assert manager.list_sessions() == []
