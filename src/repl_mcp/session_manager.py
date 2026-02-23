"""Chat session manager for sandbox lifecycle and routing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Optional

from .config import IdleAction
from .daytona_adapter import BashResult, DaytonaSandboxAdapter
from .models import ExecutionResult


@dataclass
class ChatSession:
    """Tracked chat session metadata."""

    session_id: str
    created_at: datetime
    last_activity_at: datetime
    status: str = "started"


class ChatSessionManager:
    """Manage chat-to-sandbox mapping and idle lifecycle transitions."""

    def __init__(
        self,
        adapter: DaytonaSandboxAdapter,
        *,
        idle_timeout_minutes: int = 30,
        idle_action: IdleAction = "stop",
    ):
        self._adapter = adapter
        self._idle_timeout = max(1, idle_timeout_minutes)
        self._idle_action: IdleAction = idle_action
        self._lock = RLock()
        self._sessions: dict[str, ChatSession] = {}

    def get_or_create_session(self, session_id: str) -> ChatSession:
        """Create a session and sandbox if missing, then mark as active."""
        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                self._adapter.ensure_session(session_id)
                session = ChatSession(
                    session_id=session_id,
                    created_at=now,
                    last_activity_at=now,
                    status="started",
                )
                self._sessions[session_id] = session
                return session

            if session.status in {"stopped", "archived"}:
                self._adapter.start_session(session_id)
                session.status = "started"

            session.last_activity_at = now
            return session

    def touch(self, session_id: str) -> None:
        """Update last activity timestamp for an existing session."""
        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_activity_at = now

    def execute_python(
        self,
        session_id: str,
        code: str,
        *,
        reset: bool = False,
        timeout: float = 120.0,
        inject: Optional[dict] = None,
    ) -> ExecutionResult:
        """Execute Python code in the target chat session sandbox."""
        self.get_or_create_session(session_id)
        result = self._adapter.execute_python(
            session_id,
            code,
            reset=reset,
            timeout=timeout,
            inject=inject,
        )
        self.touch(session_id)
        return result

    def upload_bytes(self, session_id: str, path: str, data: bytes, timeout_seconds: int = 1800) -> None:
        """Upload bytes into a chat session sandbox."""
        self.get_or_create_session(session_id)
        self._adapter.upload_bytes(session_id, path, data, timeout_seconds=timeout_seconds)
        self.touch(session_id)

    def run_bash(
        self,
        session_id: str,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> BashResult:
        """Run a shell command in a chat session sandbox."""
        self.get_or_create_session(session_id)
        result = self._adapter.run_bash(session_id, command, cwd=cwd, timeout=timeout)
        self.touch(session_id)
        return result

    def close_session(self, session_id: str, action: IdleAction = "delete") -> None:
        """Close a specific session with the requested action."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return

            if action == "delete":
                self._adapter.delete_session(session_id)
                del self._sessions[session_id]
                return

            if action == "archive":
                self._adapter.archive_session(session_id)
                session.status = "archived"
            else:
                self._adapter.stop_session(session_id)
                session.status = "stopped"

    def apply_idle_policy(self, now: Optional[datetime] = None) -> list[str]:
        """Apply configured idle action to inactive sessions."""
        now = now or datetime.now(timezone.utc)
        threshold = now - timedelta(minutes=self._idle_timeout)

        stale_session_ids: list[str] = []
        with self._lock:
            for session in self._sessions.values():
                if session.last_activity_at <= threshold:
                    stale_session_ids.append(session.session_id)

        for session_id in stale_session_ids:
            self.close_session(session_id, action=self._idle_action)

        return stale_session_ids

    def list_sessions(self) -> list[ChatSession]:
        """Return tracked sessions for diagnostics/testing."""
        with self._lock:
            return list(self._sessions.values())
