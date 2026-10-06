"""Session ownership, state-generation and selected-environment wire regressions."""
import json
import os
import shlex
import sys
import time

from tests.test_native_protocol import binary as binary_fixture, native as native_fixture

binary = binary_fixture
native = native_fixture


def tool(client, tool_name, **args):
    return client.request("tools/call", {"name": tool_name, "arguments": args})


def opened(client, project, name="session", **kwargs):
    result = tool(client, "python_session_open", name=name, project=str(project),
                  **({"python": sys.executable} if not kwargs else {k: v for k, v in kwargs.items() if v is not None}))
    assert not result["isError"], result
    return result["structuredContent"]


def wait_result(client, session, run, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = tool(client, "python_run", session_id=session, run_id=run)["structuredContent"]
        if "success" in result:
            return result
        time.sleep(.03)
    raise AssertionError(f"run did not finish: {result}")


def test_independent_concurrent_sessions_and_close(native, tmp_path):
    a, b = (opened(native, tmp_path, "same-label") for _ in range(2))
    assert a["session_id"] != b["session_id"]
    assert a["server_id"] == b["server_id"]
    first = tool(native, "python_start", session_id=a["session_id"],
                 code="import asyncio; print('running'); await asyncio.sleep(30)")
    run = first["structuredContent"]["run_id"]
    other = native.execute("answer = 99; answer", session_id=b["session_id"])
    assert other["structuredContent"]["value"] == 99
    closed = tool(native, "python_session_close", session_id=a["session_id"])
    assert closed["structuredContent"]["closed"] is True
    sessions = tool(native, "python_session_list")["structuredContent"]["sessions"]
    assert a["session_id"] not in [s["session_id"] for s in sessions]
    assert native.execute("answer + 1", session_id=b["session_id"])["structuredContent"]["value"] == 100
    assert run


def test_generation_guard_after_worker_crash_blocks_external_effect(native, tmp_path):
    session = opened(native, tmp_path)["session_id"]
    first = native.execute("answer = 42; answer", session_id=session)["structuredContent"]
    native.execute("import os; os._exit(1)", session_id=session)
    marker = tmp_path / "must-not-exist"
    guarded = native.execute(f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
                             session_id=session, expected_generation=first["generation"])
    assert guarded["isError"], guarded
    assert "STATE_CHANGED" in guarded["structuredContent"]["error"]
    assert not marker.exists()
    refreshed = tool(native, "python_session_inspect", session_id=session)["structuredContent"]
    assert refreshed["generation"] > first["generation"]
    assert native.execute("42", session_id=session,
                          expected_generation=refreshed["generation"])["structuredContent"]["value"] == 42


def test_retained_result_keeps_original_generation(native, tmp_path):
    session = opened(native, tmp_path)["session_id"]
    first = native.execute("42", session_id=session)["structuredContent"]
    reset = native.execute("99", session_id=session, reset=True,
                           expected_generation=first["generation"])["structuredContent"]
    assert reset["generation"] > first["generation"]
    record = tool(native, "python_run", session_id=session, run_id=first["run_id"])["structuredContent"]
    assert record["generation"] == first["generation"]
    assert record["server_id"] == first["server_id"]


def test_fresh_file_cancel_preserves_persistent_namespace(native, tmp_path):
    session = opened(native, tmp_path)["session_id"]
    first = native.execute("marker = 42; marker", session_id=session)["structuredContent"]
    script = tmp_path / "slow.py"
    script.write_text("import time\nmarker = 99\nprint('started', flush=True)\ntime.sleep(30)\n")
    request = native.start("tools/call", {"name": "python_execute_file", "arguments": {
        "session_id": session, "path": str(script), "mode": "fresh"}})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        progress = tool(native, "python_run", session_id=session)["structuredContent"]
        if "started" in progress.get("stdout", ""):
            break
        time.sleep(.03)
    else:
        raise AssertionError(progress)
    tool(native, "python_cancel", session_id=session, run_id=progress["run_id"])
    result = native.receive(request)["structuredContent"]
    assert not result["success"]
    assert result["state"] == "preserved"
    assert result["generation"] == first["generation"]
    assert native.execute("marker", session_id=session)["structuredContent"]["value"] == 42


def test_explicit_broken_venv_never_falls_back(native, tmp_path):
    project = tmp_path / "broken"
    (project / ".venv" / "bin").mkdir(parents=True)
    response = tool(native, "python_session_open", name="broken", project=str(project))
    assert response["isError"], response
    assert "Python executable" in response["structuredContent"]["error"]
    missing = tool(native, "python_session_open", name="explicit", project=str(project),
                   python=str(project / "missing-python"))
    assert missing["isError"], missing


def test_project_venv_report_and_initial_generation(native, tmp_path):
    project = tmp_path / "venv"
    (project / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, project / ".venv" / "bin" / "python")
    session = opened(native, project, python=None)
    assert session["environment"]["source"] == "project_venv"
    assert session["generation"] == 0
    result = native.execute("42", session_id=session["session_id"], expected_generation=0)
    assert result["structuredContent"]["value"] == 42
    assert result["structuredContent"]["generation"] == 1


def test_default_session_close_is_explicitly_rejected(native):
    default = tool(native, "python_health")["structuredContent"]["server"]["session_id"]
    response = tool(native, "python_session_close", session_id=default)
    assert response["isError"]
    assert "default" in response["structuredContent"]["error"]
    assert native.execute("42")["structuredContent"]["value"] == 42


def test_inventory_metadata_does_not_start_user_hooks(native, tmp_path):
    session = opened(native, tmp_path)["session_id"]
    marker = tmp_path / "hook"
    code = f"""class Trap:
    def __repr__(self):
        open({str(marker)!r}, 'w').write('repr')
        raise AssertionError('repr')
trap = Trap()
"""
    native.execute(code, session_id=session)
    inventory = tool(native, "python_session_inspect", session_id=session, include_namespace=True)
    assert not inventory["isError"], inventory
    assert "trap" in json.dumps(inventory["structuredContent"]["namespace"])
    assert not marker.exists()


def test_inspection_does_not_replace_or_evict_user_run_history(native, tmp_path):
    session = opened(native, tmp_path)["session_id"]
    first = native.execute("42", session_id=session)["structuredContent"]
    for _ in range(65):
        inventory = tool(native, "python_session_inspect", session_id=session, include_namespace=True)
        assert not inventory["isError"], inventory
    latest = tool(native, "python_run", session_id=session)["structuredContent"]
    assert latest["run_id"] == first["run_id"]
    assert latest["value"] == 42


def test_probe_valid_json_then_hang_is_bounded_and_reaped(native, tmp_path):
    pid_file = tmp_path / "probe-pid"
    wrapper = tmp_path / "fake-python"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {shlex.quote(str(pid_file))}\n"
        "printf '%s\\n' '{\"version\":\"3.14\",\"major\":3,\"minor\":14}'\n"
        "sleep 30\n"
    )
    wrapper.chmod(0o700)
    started = time.monotonic()
    response = tool(native, "python_session_open", name="hung-probe", project=str(tmp_path),
                    python=str(wrapper))
    assert response["isError"], response
    assert "timed out" in response["structuredContent"]["error"]
    assert time.monotonic() - started < 13
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.02)
    else:
        raise AssertionError(f"owned interpreter probe {pid} survived timeout")


def test_environment_metadata_reports_actual_runtime_state(native, tmp_path):
    session = opened(native, tmp_path)
    assert session["environment"]["runtime_status"] == "lazy"
    assert session["environment"]["version"] == sys.version.split()[0]
    native.execute("42", session_id=session["session_id"])
    metadata = tool(native, "python_session_inspect", session_id=session["session_id"])["structuredContent"]
    assert metadata["environment"]["runtime_status"] == "running"
    assert metadata["environment"]["validated"] is True
    assert metadata["environment"]["version"] == sys.version.split()[0]
    assert metadata["environment"]["python"] == session["environment"]["python"]


def test_cancelled_session_open_never_publishes_and_reaps_probe(native, tmp_path):
    pid_file = tmp_path / "cancelled-probe-pid"
    wrapper = tmp_path / "cancelled-python"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {shlex.quote(str(pid_file))}\n"
        "printf '%s\\n' '{\"version\":\"3.14\",\"major\":3,\"minor\":14}'\n"
        "sleep 30\n"
    )
    wrapper.chmod(0o700)
    request = native.start("tools/call", {"name": "python_session_open", "arguments": {
        "name": "cancelled-open", "project": str(tmp_path), "python": str(wrapper),
    }})
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert pid_file.exists(), "interpreter probe never started"
    pid = int(pid_file.read_text())
    native.send({"method": "notifications/cancelled", "params": {"requestId": request}})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.02)
    else:
        raise AssertionError(f"cancelled interpreter probe {pid} survived")
    sessions = tool(native, "python_session_list")["structuredContent"]["sessions"]
    assert "cancelled-open" not in [session["name"] for session in sessions]
    assert native.execute("42")["structuredContent"]["value"] == 42


def test_open_capacity_and_health_remain_available_during_probes(native, tmp_path):
    requests, pid_files = [], []
    for index in range(4):
        pid_file = tmp_path / f"probe-{index}.pid"
        wrapper = tmp_path / f"python-{index}"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"echo $$ > {shlex.quote(str(pid_file))}\n"
            "printf '%s\\n' '{\"version\":\"3.14\",\"major\":3,\"minor\":14}'\n"
            "sleep 30\n"
        )
        wrapper.chmod(0o700)
        requests.append(native.start("tools/call", {"name": "python_session_open", "arguments": {
            "name": f"pending-{index}", "project": str(tmp_path), "python": str(wrapper),
        }}))
        pid_files.append(pid_file)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not all(path.exists() for path in pid_files):
        time.sleep(.02)
    assert all(path.exists() for path in pid_files), "owned probes never all started"
    health_started = time.monotonic()
    assert not tool(native, "python_health")["isError"]
    assert time.monotonic() - health_started < 3
    fifth = tool(native, "python_session_open", name="over-capacity", project=str(tmp_path),
                 python=sys.executable)
    assert fifth["isError"], fifth
    assert "capacity" in fifth["structuredContent"]["error"]
    for request in requests:
        native.send({"method": "notifications/cancelled", "params": {"requestId": request}})
    deadline = time.monotonic() + 5
    remaining = {int(path.read_text()) for path in pid_files}
    while time.monotonic() < deadline and remaining:
        for pid in remaining.copy():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.remove(pid)
        time.sleep(.02)
    assert not remaining, f"cancelled probes survived: {remaining}"
    session = opened(native, tmp_path, "after-cancellation")
    assert session["environment"]["validated"] is True


def test_broken_venv_symlink_is_not_treated_as_absent(native, tmp_path):
    project = tmp_path / "broken-venv-symlink"
    project.mkdir()
    (project / ".venv").symlink_to(project / "missing-environment")
    response = tool(native, "python_session_open", name="broken-link", project=str(project))
    assert response["isError"], response
    assert "Python executable" in response["structuredContent"]["error"]
    explicit = opened(native, project, "override-broken-link", python=sys.executable)
    assert explicit["environment"]["source"] == "explicit"
