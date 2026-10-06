"""Historical failure mechanisms exercised through real MCP stdio, without a model.

Fixtures reproduce observed session mechanisms; they do not estimate production
failure rates or replay private user code from transcripts.
"""

import json
import hashlib
import pytest
import sys
import time
from pathlib import Path

from tests import test_native_protocol as protocol
from tests.test_native_protocol import NativeClient


@pytest.fixture
def binary():
    import os
    path = Path(os.environ.get("REPL_MCP_BINARY", protocol.ROOT / "target/debug/repl-mcp"))
    if not path.is_file():
        pytest.skip("build native binary or set REPL_MCP_BINARY")
    return path.resolve()


@pytest.fixture
def native(binary, tmp_path):
    client = NativeClient(binary, tmp_path)
    try:
        yield client
    finally:
        client.close()


def tool(client, tool_name, **arguments):
    return client.request("tools/call", {"name": tool_name, "arguments": arguments})


def opened(client, project, name):
    response = tool(client, "python_session_open", name=name, project=str(project),
                    python=sys.executable)
    assert not response.get("isError"), response
    return response["structuredContent"]["session_id"]


def test_history_shared_variable_and_worktree_paths_are_independent(native, tmp_path):
    a, b = tmp_path / "worktree-a", tmp_path / "worktree-b"
    a.mkdir()
    b.mkdir()
    session_a, session_b = opened(native, a, "agent-a"), opened(native, b, "agent-b")
    native.execute("from pathlib import Path\nroot = Path.cwd()\nmarker = 41", session_id=session_a)
    native.execute("from pathlib import Path\nroot = Path.cwd()\nmarker = 99", session_id=session_b)
    result = native.execute("(root / 'owned.txt').write_text(str(marker))\nstr(root)",
                            session_id=session_a)["structuredContent"]
    assert result["value"] == str(a)
    assert (a / "owned.txt").read_text() == "41"
    assert not (b / "owned.txt").exists()
    tool(native, "python_session_close", session_id=session_a)
    assert native.execute("marker", session_id=session_b)["structuredContent"]["value"] == 99


def test_history_project_dependencies_use_selected_venv(native, tmp_path):
    # Lightweight project venv layout: wrapper selects the current test interpreter
    # and injects a project-only dependency. No network or package installation.
    project = tmp_path / "project"
    bin_path = project / ".venv" / "bin"
    modules = project / "private_modules"
    bin_path.mkdir(parents=True)
    modules.mkdir()
    (modules / "history_project_dependency.py").write_text("answer = 73\n")
    interpreter = bin_path / "python"
    import shlex
    interpreter.write_text("#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(str(modules)) +
                           "\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    interpreter.chmod(0o700)
    response = tool(native, "python_session_open", name="project-env", project=str(project))
    assert not response.get("isError"), response
    session = response["structuredContent"]["session_id"]
    result = native.execute("import history_project_dependency\nhistory_project_dependency.answer",
                            session_id=session)
    assert result["structuredContent"]["value"] == 73, result
    missing = tool(native, "python_session_open", name="missing-explicit-env",
                   project=str(project), python=str(project / "absent-python"))
    assert missing.get("isError"), missing


def test_history_stale_namespace_guard_prevents_external_write(native, tmp_path):
    session = opened(native, tmp_path, "generation-guard")
    initial = native.execute("helper = 42", session_id=session)["structuredContent"]
    old_generation = initial["generation"]
    reset = native.execute("0", session_id=session, reset=True)["structuredContent"]
    assert reset["generation"] != old_generation
    target = tmp_path / "must-not-write"
    rejected = native.execute(f"open({str(target)!r}, 'w').write('bad')",
                              session_id=session, expected_generation=old_generation)
    assert rejected.get("isError"), rejected
    assert "STATE_CHANGED" in json.dumps(rejected)
    assert not target.exists()
    recovered = native.execute("42", session_id=session,
                               expected_generation=reset["generation"])
    assert recovered["structuredContent"]["value"] == 42


def test_inspection_does_not_run_user_repr_or_metaclass(native, tmp_path):
    session = opened(native, tmp_path, "safe-inventory")
    marker = tmp_path / "unexpected-inspection-effect"
    result = native.execute(
        "class Meta(type):\n"
        " def __getattribute__(self, name):\n"
        f"  open({str(marker)!r}, 'w').write(name)\n"
        "  return type.__getattribute__(self, name)\n"
        "class Hostile(metaclass=Meta):\n"
        " def __repr__(self):\n"
        f"  open({str(marker)!r}, 'w').write('repr')\n"
        "  return 'repr'\n"
        "opaque = Hostile()\n42", session_id=session)
    assert not result.get("isError"), result
    inspected = tool(native, "python_session_inspect", session_id=session,
                     include_namespace=True)
    assert not inspected.get("isError"), inspected
    assert "opaque" in json.dumps(inspected)
    assert not marker.exists()


def test_generation_guard_detects_recovery_before_running_dependent_code(native, tmp_path):
    session = opened(native, tmp_path, "crash-generation")
    generation = native.execute("helper = 42", session_id=session)["structuredContent"]["generation"]
    crashed = native.execute("import os\nos._exit(7)", session_id=session)
    assert crashed.get("isError"), crashed
    marker = tmp_path / "stale-code-after-crash"
    rejected = native.execute(f"open({str(marker)!r}, 'w').write('bad')",
                              session_id=session, expected_generation=generation)
    assert rejected.get("isError"), rejected
    assert "STATE_CHANGED" in json.dumps(rejected), rejected
    assert not marker.exists()


def test_parallel_sessions_make_progress_without_sharing_busy_slot(native, tmp_path):
    a, b = opened(native, tmp_path, "parallel-a"), opened(native, tmp_path, "parallel-b")
    script = "import time\nmarker = {}\ntime.sleep(.5)\nmarker"
    first = tool(native, "python_start", session_id=a, code=script.format(41))
    second = tool(native, "python_start", session_id=b, code=script.format(99))
    assert not first.get("isError"), first
    assert not second.get("isError"), second
    deadline = time.monotonic() + 10
    results = {}
    while time.monotonic() < deadline and len(results) < 2:
        for session, started in ((a, first), (b, second)):
            result = tool(native, "python_run", session_id=session,
                          run_id=started["structuredContent"]["run_id"])["structuredContent"]
            if "success" in result:
                results[session] = result
        time.sleep(.02)
    assert results[a]["value"] == 41
    assert results[b]["value"] == 99


def test_parent_death_cleans_workers_from_all_sessions(native, tmp_path):
    sessions = [opened(native, tmp_path, "parent-death") for _ in range(2)]
    pids = [native.execute("import os\nos.getpid()", session_id=session)
            ["structuredContent"]["value"] for session in sessions]
    assert len(set(pids)) == 2
    native.process.kill()
    native.process.wait(timeout=5)
    for pid in pids:
        protocol.wait_process_gone(pid)


def test_history_saved_file_fresh_preserves_live_helpers(native, tmp_path):
    session = opened(native, tmp_path, "script-runner")
    native.execute("marker = 41", session_id=session)
    script = tmp_path / "task script.py"
    script.write_text("import sys\nmarker = 99\nprint(sys.argv[1])\n")
    result = tool(native, "python_execute_file", session_id=session,
                  path=str(script), argv=["argument with spaces"], mode="fresh")
    assert not result.get("isError"), result
    assert "argument with spaces" in result["structuredContent"]["stdout"]
    assert native.execute("marker + 1", session_id=session)["structuredContent"]["value"] == 42
    result = tool(native, "python_execute_file", session_id=session,
                  path=str(script), argv=["persistent"], mode="persistent")
    assert not result.get("isError"), result
    assert native.execute("marker", session_id=session)["structuredContent"]["value"] == 99


def test_history_large_payload_stays_retrievable_after_worker_reset(native, tmp_path):
    session = opened(native, tmp_path, "large-payload")
    result = native.execute("'Ж' * 100000", session_id=session)["structuredContent"]
    assert result["success"], result
    reference = result["artifact"]
    expected = ("Ж" * 100000).encode()
    assert reference["size"] == len(expected)
    assert reference["sha256"] == hashlib.sha256(expected).hexdigest()
    native.execute("0", session_id=session, reset=True)
    partial = tool(native, "artifact_read", session_id=session, id=reference["id"],
                   offset=0, length=10, encoding="text")
    assert not partial.get("isError"), partial
    assert partial["structuredContent"]["content"] == "Ж" * 5
    target = tmp_path / "saved-payload.txt"
    saved = tool(native, "artifact_save", session_id=session,
                 id=reference["id"], path=str(target))
    assert not saved.get("isError"), saved
    assert target.read_bytes() == expected


def test_history_result_lookup_does_not_repeat_completed_write(native, tmp_path):
    session = opened(native, tmp_path, "write-reconciliation")
    target = tmp_path / "writes"
    started = tool(native, "python_start", session_id=session,
                   code=f"from pathlib import Path\np = Path({str(target)!r})\n"
                        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')")
    run_id = started["structuredContent"]["run_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = tool(native, "python_run", session_id=session, run_id=run_id)
        if "success" in result["structuredContent"]:
            break
        time.sleep(.02)
    assert result["structuredContent"]["success"], result
    for _ in range(3):
        retained = tool(native, "python_run", session_id=session, run_id=run_id)
        assert retained["structuredContent"]["run_id"] == run_id
        assert retained["structuredContent"]["session_id"] == session
    assert target.read_text() == "x"


def test_history_broker_cancel_does_not_break_other_session(binary, tmp_path):
    config = tmp_path / "registry.json"
    fixture = Path(__file__).parent / "fixtures/native_mcp_fixture.py"
    config.write_text(json.dumps({"mcpServers": {"fixture": {
        "command": sys.executable, "args": [str(fixture)],
    }}}))
    client = NativeClient(binary, tmp_path, config)
    try:
        a, b = opened(client, tmp_path, "cancelled-agent"), opened(client, tmp_path, "other-agent")
        started = tool(client, "python_start", session_id=a,
                       code="await mcp.acall('fixture', 'slow_write', marker='" +
                            str(tmp_path / "cancelled-write") + "')")
        run_id = started["structuredContent"]["run_id"]
        time.sleep(.2)
        assert tool(client, "python_cancel", session_id=a, run_id=run_id)["structuredContent"]["cancel_requested"]
        result = client.execute("mcp.call('fixture', 'echo', text='other survives')['structuredContent']['arguments']['text']",
                                session_id=b)
        assert result["structuredContent"]["value"] == "other survives", result
        time.sleep(2.1)
        assert not (tmp_path / "cancelled-write").exists()
    finally:
        client.close()


def test_session_project_registries_do_not_borrow_other_projects(binary, tmp_path):
    a, b = tmp_path / "granted-project", tmp_path / "unconfigured-project"
    a.mkdir()
    b.mkdir()
    fixture = Path(__file__).parent / "fixtures/native_mcp_fixture.py"
    (a / ".mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {
        "command": sys.executable, "args": [str(fixture)],
    }}}))
    client = NativeClient(binary, tmp_path, mcp_scope="project")
    try:
        sa, sb = opened(client, a, "project-a"), opened(client, b, "project-b")
        allowed = client.execute("mcp.servers()", session_id=sa)
        assert allowed["structuredContent"]["value"] == ["fixture"], allowed
        unconfigured = client.execute("mcp.servers()", session_id=sb)
        assert unconfigured["structuredContent"]["value"] == [], unconfigured
        diagnosis = client.execute("mcp.explain('fixture')", session_id=sb)
        assert not diagnosis.get("isError"), diagnosis
        assert diagnosis["structuredContent"]["value"]["status"] == "registry_absent", diagnosis
    finally:
        client.close()


def test_project_session_journals_share_durable_writer_and_keep_provenance(binary, tmp_path):
    fixture = Path(__file__).parent / "fixtures/native_mcp_fixture.py"
    projects = [tmp_path / name for name in ("one", "two")]
    for project in projects:
        project.mkdir()
        (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"fixture": {
            "command": sys.executable, "args": [str(fixture)],
        }}}))
    journal = tmp_path / "effects.json"
    client = NativeClient(binary, tmp_path, mcp_scope="project",
                          extra_args=["--journal", str(journal)])
    owners = []
    try:
        for project in projects:
            owner = opened(client, project, "journal-owner")
            owners.append(owner)
            result = client.execute("mcp.call('fixture', 'echo', text='payload-not-metadata')",
                                    session_id=owner)
            assert not result["isError"], result
        records = json.loads(journal.read_text())
        assert len(records) == 2 and all(row["status"] == "completed" for row in records)
        assert {row["session_id"] for row in records} == set(owners)
        assert {Path(row["project"]).resolve() for row in records} == {p.resolve() for p in projects}
        assert "payload-not-metadata" not in journal.read_text()
        for owner in owners:
            assert tool(client, "python_session_close", session_id=owner)["structuredContent"]["closed"]
    finally:
        client.close()
    recovered = NativeClient(binary, tmp_path, mcp_scope="project",
                             extra_args=["--journal", str(journal)])
    try:
        assert len(json.loads(journal.read_text())) == 2
    finally:
        recovered.close()


def test_direct_artifact_forward_cancellation_stops_late_effect(binary, tmp_path):
    fixture = Path(__file__).parent / "fixtures/native_mcp_fixture.py"
    config = tmp_path / "registry.json"
    config.write_text(json.dumps({"mcpServers": {"fixture": {
        "command": sys.executable, "args": [str(fixture)],
    }}}))
    client = NativeClient(binary, tmp_path, config)
    try:
        a, b = opened(client, tmp_path, "forward-owner"), opened(client, tmp_path, "forward-observer")
        marker = tmp_path / "cancelled-forward-write"
        payload = tmp_path / "marker-payload.txt"
        payload.write_text(str(marker))
        reference = tool(client, "artifact_create", session_id=a, path=str(payload),
                         format="text")["structuredContent"]
        request = client.start("tools/call", {"name": "artifact_forward", "arguments": {
            "session_id": a, "id": reference["id"], "server": "fixture",
            "tool": "slow_write", "argument": "marker", "format": "text",
        }})
        time.sleep(.3)
        client.send({"method": "notifications/cancelled", "params": {"requestId": request}})
        response = client.execute("mcp.call('fixture', 'echo', text='alive')['structuredContent']['arguments']['text']",
                                  session_id=b)
        assert response["structuredContent"]["value"] == "alive", response
        time.sleep(2.1)
        assert not marker.exists()
        journal = client.execute("mcp.journal()", session_id=b)["structuredContent"]["value"]
        assert any(entry["tool"] == "slow_write" and entry["status"] == "outcome_unknown"
                   for entry in journal), journal
    finally:
        client.close()


def test_published_output_schemas_match_new_success_and_error_results(native, tmp_path):
    from jsonschema import validate

    schemas = {entry["name"]: entry["outputSchema"]
               for entry in native.request("tools/list", {})["tools"]}

    def checked(tool_name, **arguments):
        response = tool(native, tool_name, **arguments)
        validate(response["structuredContent"], schemas[tool_name])
        return response["structuredContent"]

    opened_session = checked("python_session_open", name="schema-session",
                             project=str(tmp_path), python=sys.executable)
    session = opened_session["session_id"]
    checked("python_session_list")
    checked("python_session_inspect", session_id=session, include_namespace=True)
    checked("execute_python", session_id=session, code="42")
    checked("execute_python", session_id=session, code="1/0")
    payload = tmp_path / "schema-payload"
    payload.write_text("schema")
    reference = checked("artifact_create", session_id=session, path=str(payload), format="text")
    checked("artifact_read", session_id=session, id=reference["id"], length=2)
    checked("artifact_save", session_id=session, id=reference["id"], path=str(tmp_path / "saved"))
    checked("artifact_delete", session_id=session, id=reference["id"])
    checked("artifact_read", session_id=session, id=reference["id"])
    checked("python_session_close", session_id=session)
    checked("python_session_close", session_id=session)
