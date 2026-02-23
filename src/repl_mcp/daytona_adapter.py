"""Daytona sandbox adapter for chat-scoped execution."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from threading import RLock
from typing import Any, Optional

from .config import RuntimeConfig
from .models import ExceptionInfo, ExecutionResult


class DaytonaAdapterError(RuntimeError):
    """Raised for Daytona adapter errors."""


@dataclass
class SandboxHandle:
    """In-memory handle for a chat-scoped Daytona sandbox."""

    session_id: str
    sandbox: Any
    status: str = "started"
    python_history: list[str] = field(default_factory=list)


@dataclass
class BashResult:
    """Normalized bash execution result."""

    exit_code: int
    stdout: str
    stderr: str = ""


class DaytonaSandboxAdapter:
    """Adapter that maps chat session IDs to Daytona sandboxes."""

    def __init__(self, config: RuntimeConfig):
        self._config = config
        self._lock = RLock()
        self._daytona: Any = None
        self._handles: dict[str, SandboxHandle] = {}

    def ensure_session(self, session_id: str) -> SandboxHandle:
        """Ensure a sandbox exists for the given chat session."""
        with self._lock:
            handle = self._handles.get(session_id)
            if handle is not None:
                return handle

            sandbox = self._create_sandbox(session_id)
            handle = SandboxHandle(session_id=session_id, sandbox=sandbox)
            self._handles[session_id] = handle
            return handle

    def execute_python(
        self,
        session_id: str,
        code: str,
        *,
        reset: bool = False,
        timeout: float = 120.0,
        inject: Optional[dict] = None,
    ) -> ExecutionResult:
        """
        Execute Python code in a chat sandbox.

        Implementation uses deterministic history replay to preserve chat REPL state
        across calls without requiring a persistent Python process.
        """
        handle = self.ensure_session(session_id)
        if reset:
            handle.python_history.clear()

        inject_payload: dict[str, Any] = {}
        if inject:
            try:
                json.dumps(inject)
                inject_payload = inject
            except (TypeError, ValueError):
                return ExecutionResult(
                    success=False,
                    exception=ExceptionInfo(
                        type="ValueError",
                        message="inject must be JSON-serializable in Daytona mode",
                        traceback="",
                    ),
                    execution_time_ms=0.0,
                )

        command = self._build_python_command(
            history=handle.python_history,
            code=code,
            inject=inject_payload,
        )

        start = time.perf_counter()
        response = self._exec_command(handle, command=command, timeout=timeout)
        elapsed_ms = (time.perf_counter() - start) * 1000

        payload = self._parse_runner_payload(response.stdout)
        if payload is None:
            return ExecutionResult(
                success=False,
                stdout=response.stdout,
                stderr=response.stderr,
                exception=ExceptionInfo(
                    type="RuntimeError",
                    message="Failed to parse Daytona Python execution payload",
                    traceback="",
                ),
                execution_time_ms=elapsed_ms,
            )

        exception = None
        if payload.get("exception"):
            exception = ExceptionInfo(
                type=payload["exception"].get("type", "RuntimeError"),
                message=payload["exception"].get("message", "Daytona execution failed"),
                traceback=payload["exception"].get("traceback", ""),
            )

        result = ExecutionResult(
            success=bool(payload.get("success", False)),
            stdout=payload.get("stdout", ""),
            stderr=payload.get("stderr", ""),
            return_value=payload.get("return_value"),
            exception=exception,
            execution_time_ms=elapsed_ms,
        )

        if result.success and code.strip():
            handle.python_history.append(code)

        return result

    def upload_bytes(self, session_id: str, path: str, data: bytes, timeout_seconds: int = 1800) -> None:
        """Upload bytes into a sandbox filesystem path."""
        handle = self.ensure_session(session_id)
        safe_path = self._normalize_relative_path(path)
        try:
            handle.sandbox.fs.upload_file(data, safe_path, timeout=timeout_seconds)
        except TypeError:
            # Some SDK versions may not expose timeout on this overload.
            handle.sandbox.fs.upload_file(data, safe_path)

    def run_bash(
        self,
        session_id: str,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> BashResult:
        """Execute a bash command in the session sandbox."""
        handle = self.ensure_session(session_id)
        result = self._exec_command(handle, command=command, timeout=timeout, cwd=cwd)
        return BashResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    def start_session(self, session_id: str) -> None:
        """Start a stopped/archived sandbox for a session."""
        with self._lock:
            handle = self._handles.get(session_id)
            if not handle:
                return

            daytona = self._get_daytona_client()
            sandbox = handle.sandbox
            if hasattr(daytona, "start"):
                daytona.start(sandbox, timeout=60)
            elif hasattr(sandbox, "start"):
                sandbox.start()
            handle.status = "started"

    def stop_session(self, session_id: str) -> None:
        """Stop a sandbox for a session."""
        with self._lock:
            handle = self._handles.get(session_id)
            if not handle:
                return

            daytona = self._get_daytona_client()
            sandbox = handle.sandbox
            if hasattr(daytona, "stop"):
                daytona.stop(sandbox, timeout=60)
            elif hasattr(sandbox, "stop"):
                sandbox.stop()
            handle.status = "stopped"

    def archive_session(self, session_id: str) -> None:
        """Archive a sandbox for a session."""
        with self._lock:
            handle = self._handles.get(session_id)
            if not handle:
                return

            sandbox = handle.sandbox
            # SDK support differs across versions; use best-effort fallback.
            if hasattr(sandbox, "archive"):
                sandbox.archive()
                handle.status = "archived"
                return

            # Fallback to stop if explicit archive API is not available.
            self.stop_session(session_id)
            handle.status = "archived"

    def delete_session(self, session_id: str) -> None:
        """Delete a sandbox and forget local session state."""
        with self._lock:
            handle = self._handles.pop(session_id, None)
            if not handle:
                return

            daytona = self._get_daytona_client()
            sandbox = handle.sandbox
            if hasattr(daytona, "delete"):
                daytona.delete(sandbox, timeout=60)
            elif hasattr(sandbox, "delete"):
                sandbox.delete()

    def get_status(self, session_id: str) -> Optional[str]:
        """Get a cached session status."""
        with self._lock:
            handle = self._handles.get(session_id)
            if not handle:
                return None
            return handle.status

    @staticmethod
    def _normalize_relative_path(path: str) -> str:
        """Normalize path and block traversal/absolute targets."""
        norm = PurePosixPath(path)
        if not path or norm.is_absolute():
            raise DaytonaAdapterError("upload path must be a non-empty relative path")
        if ".." in norm.parts:
            raise DaytonaAdapterError("upload path cannot contain '..'")
        return str(norm)

    def _create_sandbox(self, session_id: str) -> Any:
        """Create a Daytona sandbox using snapshot/image/default strategy."""
        daytona = self._get_daytona_client()
        label_value = session_id[:64]

        params = None
        if self._config.daytona_snapshot:
            params = self._build_snapshot_params(session_id, label_value)
        elif self._config.daytona_image:
            params = self._build_image_params(session_id, label_value)

        if params is None:
            return daytona.create(timeout=60)
        return daytona.create(params, timeout=60)

    def _build_snapshot_params(self, session_id: str, label_value: str) -> Any:
        """Build snapshot creation params if SDK symbols are available."""
        try:
            from daytona import CreateSandboxFromSnapshotParams
        except Exception:
            return None

        return CreateSandboxFromSnapshotParams(
            snapshot=self._config.daytona_snapshot,
            name=f"chat-{session_id[:24]}",
            labels={"repl_mcp_chat_session": label_value},
            auto_stop_interval=max(0, self._config.session_idle_timeout_minutes),
        )

    def _build_image_params(self, session_id: str, label_value: str) -> Any:
        """Build image creation params if SDK symbols are available."""
        try:
            from daytona import CreateSandboxFromImageParams
        except Exception:
            return None

        return CreateSandboxFromImageParams(
            image=self._config.daytona_image,
            language="python",
            name=f"chat-{session_id[:24]}",
            labels={"repl_mcp_chat_session": label_value},
            auto_stop_interval=max(0, self._config.session_idle_timeout_minutes),
        )

    def _get_daytona_client(self) -> Any:
        """Initialize and cache Daytona client lazily."""
        if self._daytona is not None:
            return self._daytona

        try:
            from daytona import Daytona, DaytonaConfig
        except Exception as exc:
            raise DaytonaAdapterError(
                "Daytona SDK is not available. Install the Daytona Python SDK."
            ) from exc

        config_kwargs: dict[str, Any] = {}
        if self._config.daytona_api_url:
            config_kwargs["api_url"] = self._config.daytona_api_url
        if self._config.daytona_api_key:
            config_kwargs["api_key"] = self._config.daytona_api_key
        if self._config.daytona_target:
            config_kwargs["target"] = self._config.daytona_target

        if config_kwargs:
            self._daytona = Daytona(DaytonaConfig(**config_kwargs))
        else:
            # Falls back to DAYTONA_* env vars managed by the SDK.
            self._daytona = Daytona()
        return self._daytona

    @staticmethod
    def _build_python_command(history: list[str], code: str, inject: dict[str, Any]) -> str:
        """Build a deterministic Python runner command."""
        history_b64 = base64.b64encode(json.dumps(history).encode("utf-8")).decode("ascii")
        code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
        inject_b64 = base64.b64encode(json.dumps(inject).encode("utf-8")).decode("ascii")

        runner = f"""
import ast
import base64
import json
import traceback
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

history = json.loads(base64.b64decode("{history_b64}").decode("utf-8"))
code = base64.b64decode("{code_b64}").decode("utf-8")
inject = json.loads(base64.b64decode("{inject_b64}").decode("utf-8"))

globals_ns = {{"__builtins__": __builtins__}}
for key, value in inject.items():
    globals_ns[key] = value

for snippet in history:
    try:
        exec(compile(snippet, "<repl-history>", "exec"), globals_ns, globals_ns)
    except Exception:
        # History replay is best-effort; previously successful snippets should replay.
        pass

stdout_capture = StringIO()
stderr_capture = StringIO()
success = True
return_value = None
exception = None

try:
    parsed = ast.parse(code, "<repl>", "exec")
    if parsed.body and isinstance(parsed.body[-1], ast.Expr):
        statements = parsed.body[:-1]
        if statements:
            stmt_module = ast.Module(body=statements, type_ignores=[])
            ast.fix_missing_locations(stmt_module)
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                exec(compile(stmt_module, "<repl>", "exec"), globals_ns, globals_ns)

        expr_code = compile(ast.Expression(body=parsed.body[-1].value), "<repl>", "eval")
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            return_value = eval(expr_code, globals_ns, globals_ns)
    else:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(compile(code, "<repl>", "exec"), globals_ns, globals_ns)
except Exception as exc:
    success = False
    exception = {{
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }}

payload = {{
    "success": success,
    "stdout": stdout_capture.getvalue(),
    "stderr": stderr_capture.getvalue(),
    "return_value": None if return_value is None else repr(return_value),
    "exception": exception,
}}

print("__REPL_MCP_JSON_START__")
print(json.dumps(payload))
print("__REPL_MCP_JSON_END__")
""".strip()

        return f"python - <<'PY'\n{runner}\nPY"

    @staticmethod
    def _parse_runner_payload(stdout: str) -> Optional[dict[str, Any]]:
        """Parse structured runner payload from command output."""
        start_marker = "__REPL_MCP_JSON_START__"
        end_marker = "__REPL_MCP_JSON_END__"

        if start_marker not in stdout or end_marker not in stdout:
            return None

        payload_start = stdout.find(start_marker) + len(start_marker)
        payload_end = stdout.find(end_marker, payload_start)
        if payload_end < 0:
            return None

        payload_raw = stdout[payload_start:payload_end].strip()
        if not payload_raw:
            return None

        try:
            return json.loads(payload_raw)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _exec_command(handle: SandboxHandle, command: str, timeout: float, cwd: Optional[str] = None) -> BashResult:
        """Execute shell command via Daytona process API."""
        timeout_seconds = int(max(1, timeout))
        kwargs: dict[str, Any] = {"timeout": timeout_seconds}
        if cwd:
            kwargs["cwd"] = cwd

        response = handle.sandbox.process.exec(command, **kwargs)
        return BashResult(
            exit_code=int(getattr(response, "exit_code", 0)),
            stdout=str(getattr(response, "result", "") or ""),
            stderr="",
        )
