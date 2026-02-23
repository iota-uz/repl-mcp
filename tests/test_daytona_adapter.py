"""Tests for Daytona sandbox adapter behavior."""

from __future__ import annotations

import io
import sys
import types
from contextlib import redirect_stdout

import pytest

from repl_mcp.config import RuntimeConfig
from repl_mcp.daytona_adapter import DaytonaAdapterError, DaytonaSandboxAdapter


class FakeExecResponse:
    def __init__(self, result: str, exit_code: int = 0):
        self.result = result
        self.exit_code = exit_code


class FakeProcess:
    def exec(self, command: str, **_kwargs) -> FakeExecResponse:
        if command.startswith("python - <<'PY'\n") and command.endswith("\nPY"):
            script = command[len("python - <<'PY'\n"):-len("\nPY")]
            buf = io.StringIO()
            with redirect_stdout(buf):
                exec(script, {})
            return FakeExecResponse(buf.getvalue(), exit_code=0)
        return FakeExecResponse(f"ran:{command}", exit_code=0)


class FakeFS:
    def __init__(self):
        self.uploads: list[tuple[bytes, str, int | None]] = []

    def upload_file(self, data: bytes, remote_path: str, timeout: int | None = None):
        self.uploads.append((data, remote_path, timeout))


class FakeSandbox:
    def __init__(self):
        self.process = FakeProcess()
        self.fs = FakeFS()
        self.started = True

    def archive(self):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def delete(self):
        self.started = False


class FakeDaytonaConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeDaytona:
    def __init__(self, _config=None):
        self.created: list[tuple[object | None, int]] = []
        self.deleted = 0
        self.started = 0
        self.stopped = 0

    def create(self, params=None, timeout: int = 60):
        self.created.append((params, timeout))
        return FakeSandbox()

    def start(self, _sandbox, timeout: int = 60):
        self.started += 1

    def stop(self, _sandbox, timeout: int = 60):
        self.stopped += 1

    def delete(self, _sandbox, timeout: int = 60):
        self.deleted += 1


class FakeCreateSandboxFromSnapshotParams:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeCreateSandboxFromImageParams:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def fake_daytona_module(monkeypatch):
    module = types.SimpleNamespace(
        Daytona=FakeDaytona,
        DaytonaConfig=FakeDaytonaConfig,
        CreateSandboxFromSnapshotParams=FakeCreateSandboxFromSnapshotParams,
        CreateSandboxFromImageParams=FakeCreateSandboxFromImageParams,
    )
    monkeypatch.setitem(sys.modules, "daytona", module)
    yield
    monkeypatch.delitem(sys.modules, "daytona", raising=False)


def test_execute_python_persists_chat_state():
    cfg = RuntimeConfig(
        daytona_api_url="https://api.daytona.test",
        daytona_api_key="secret",
    )
    adapter = DaytonaSandboxAdapter(cfg)

    first = adapter.execute_python("chat-a", "x = 2")
    second = adapter.execute_python("chat-a", "x + 5")
    third = adapter.execute_python("chat-b", "x = 10")
    fourth = adapter.execute_python("chat-b", "x + 1")

    assert first.success is True
    assert second.success is True
    assert second.return_value == "7"
    assert third.success is True
    assert fourth.return_value == "11"


def test_upload_bytes_and_path_validation():
    cfg = RuntimeConfig(daytona_api_url="https://api.daytona.test", daytona_api_key="secret")
    adapter = DaytonaSandboxAdapter(cfg)

    adapter.upload_bytes("chat-a", "tmp/file.txt", b"hello")
    handle = adapter.ensure_session("chat-a")
    assert handle.sandbox.fs.uploads[0][1] == "tmp/file.txt"

    with pytest.raises(DaytonaAdapterError):
        adapter.upload_bytes("chat-a", "../escape.txt", b"oops")


def test_bash_and_lifecycle_methods():
    cfg = RuntimeConfig(daytona_api_url="https://api.daytona.test", daytona_api_key="secret")
    adapter = DaytonaSandboxAdapter(cfg)

    bash = adapter.run_bash("chat-a", "echo hi")
    assert bash.exit_code == 0
    assert bash.stdout.startswith("ran:echo hi")

    adapter.stop_session("chat-a")
    assert adapter.get_status("chat-a") == "stopped"

    adapter.start_session("chat-a")
    assert adapter.get_status("chat-a") == "started"

    adapter.archive_session("chat-a")
    assert adapter.get_status("chat-a") == "archived"

    adapter.delete_session("chat-a")
    assert adapter.get_status("chat-a") is None


def test_snapshot_and_image_creation_paths():
    snapshot_cfg = RuntimeConfig(
        daytona_api_url="https://api.daytona.test",
        daytona_api_key="secret",
        daytona_snapshot="snap-1",
    )
    snapshot_adapter = DaytonaSandboxAdapter(snapshot_cfg)
    snapshot_adapter.ensure_session("chat-snap")
    snapshot_daytona = snapshot_adapter._get_daytona_client()
    assert isinstance(snapshot_daytona.created[0][0], FakeCreateSandboxFromSnapshotParams)

    image_cfg = RuntimeConfig(
        daytona_api_url="https://api.daytona.test",
        daytona_api_key="secret",
        daytona_image="python:3.12",
    )
    image_adapter = DaytonaSandboxAdapter(image_cfg)
    image_adapter.ensure_session("chat-img")
    image_daytona = image_adapter._get_daytona_client()
    assert isinstance(image_daytona.created[0][0], FakeCreateSandboxFromImageParams)
