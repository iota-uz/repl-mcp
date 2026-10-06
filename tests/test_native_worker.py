"""Real stdio worker acceptance without an installed Rust binary or MCP SDK."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

import pytest


WORKER = Path(__file__).parents[1] / "src/repl_mcp/native_worker.py"


class Client:
    def __init__(self):
        self.process = subprocess.Popen([sys.executable, "-u", str(WORKER)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
        self.events = queue.Queue()
        self.backlog = []
        self.serial = 0
        threading.Thread(target=self.read, daemon=True).start()
        ready = self.next("ready")
        assert ready["pid"] == self.process.pid
        assert Path(ready["python_executable"]).resolve() == Path(sys.executable).resolve()

    def read(self):
        for line in self.process.stdout:
            self.events.put(json.loads(line))

    def send(self, frame):
        self.process.stdin.write(json.dumps(frame).encode() + b"\n")
        self.process.stdin.flush()

    def next(self, kind, timeout=5):
        for index, item in enumerate(self.backlog):
            if item["type"] == kind:
                return self.backlog.pop(index)
        deadline = time.monotonic() + timeout
        while True:
            item = self.events.get(timeout=max(0.001, deadline - time.monotonic()))
            if item["type"] == kind:
                return item
            self.backlog.append(item)

    def start(self, code):
        self.serial += 1
        identifier = f"test-{self.serial}"
        self.send({"type": "execute", "id": identifier, "code": code})
        return identifier

    def execute(self, code):
        identifier = self.start(code)
        item = self.next("result")
        assert item["id"] == identifier
        return item["result"]

    def close(self):
        if self.process.poll() is None:
            try:
                self.send({"type": "shutdown"})
            except BrokenPipeError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.stderr.close()


@pytest.fixture
def client():
    instance = Client()
    try:
        yield instance
    finally:
        instance.close()


def test_persistent_async_loop_and_state(client):
    assert client.execute("import asyncio\nloop = asyncio.get_running_loop() if False else asyncio.get_event_loop()\nevent = asyncio.Event()\nx = 41")["success"]
    result = client.execute("await asyncio.sleep(0)\nevent.set()\n[loop is asyncio.get_running_loop() if False else loop is asyncio.get_event_loop(), event.is_set(), x + 1]")
    assert result["value"] == [True, True, 42]
    assert client.execute("async def answer():\n    return asyncio.get_running_loop() is loop\nawait answer()")["value"] is True


def test_namespace_metadata_bypasses_all_custom_hooks(client):
    assert client.execute("effects = []\nclass Meta(type):\n    @property\n    def __name__(cls):\n        effects.append('descriptor')\n        return 'bad'\n    def __eq__(cls, other):\n        effects.append('equality')\n        return False\n    def __getattribute__(cls, name):\n        effects.append('attribute')\n        return super().__getattribute__(name)\nclass Hostile(metaclass=Meta):\n    def __repr__(self):\n        effects.append('repr')\n        return 'bad'\nx = Hostile()")["success"]
    client.serial += 1
    client.send({"type": "execute", "id": f"test-{client.serial}", "inventory": True})
    result = client.next("result")["result"]
    assert result["success"]
    assert next(item for item in result["value"]["variables"] if item["name"] == "x")["type"] == "Hostile"
    assert client.execute("effects")["value"] == []
    assert client.execute("x")["success"]
    assert client.execute("effects")["value"] == []


def test_saved_file_real_source_import_encoding_and_argument_restoration(client, tmp_path):
    (tmp_path / "helper.py").write_text("answer = 42\n")
    source = tmp_path / "task.py"
    source.write_bytes("# coding: latin-1\nimport sys, helper\nseen = [__name__, __file__, sys.argv, helper.answer, 'café']\nseen\n".encode("latin-1"))
    assert client.execute("import sys\nold_argv = sys.argv\nold_path = sys.path")["success"]
    client.serial += 1
    client.send({"type": "execute", "id": f"test-{client.serial}", "source_path": str(source), "argv": ["hello"]})
    result = client.next("result")["result"]
    assert result["value"] == ["__main__", str(source), [str(source), "hello"], 42, "café"]
    assert client.execute("[sys.argv is old_argv, sys.path is old_path, __name__, '__file__' in globals(), seen[3]]")["value"] == [True, True, "__repl__", False, 42]
    source.write_text("raise ValueError('saved-source-error')\n")
    client.serial += 1
    client.send({"type": "execute", "id": f"test-{client.serial}", "source_path": str(source), "argv": []})
    error = client.next("result")["result"]
    assert not error["success"] and str(source) in error["error"] and "line 1" in error["error"]
    assert client.execute("[sys.argv is old_argv, sys.path is old_path]")["value"] == [True, True]


def test_saved_file_bounded_input_validation(client, tmp_path):
    source = tmp_path / "large.py"
    source.write_bytes(b" " * (256 * 1024 + 1))
    client.serial += 1
    client.send({"type": "execute", "id": f"test-{client.serial}", "source_path": str(source)})
    result = client.next("result")["result"]
    assert not result["success"] and "256 KiB" in result["error"]
    assert client.execute("40 + 2")["value"] == 42


@pytest.mark.parametrize("expression,status,stderr", [("None", 0, ""), ("0", 0, ""), ("2", 2, ""), ("'stopped'", 1, "stopped\n")])
def test_saved_file_exit_status_and_next_cell(client, tmp_path, expression, status, stderr):
    source = tmp_path / "exit.py"
    source.write_text(f"import sys\nmarker = 42\nsys.exit({expression})\nmarker = 99\n")
    client.serial += 1
    client.send({"type": "execute", "id": f"test-{client.serial}", "source_path": str(source)})
    result = client.next("result")["result"]
    assert result["success"] is (status == 0)
    assert result["exit_code"] == status and result["stderr"] == stderr
    assert client.execute("marker")["value"] == 42
    assert not client.execute("raise SystemExit(0)")["success"]


def test_oversized_result_artifact_rpc_and_temporary_cleanup(client):
    client.serial += 1
    identifier = f"test-{client.serial}"
    client.send({"type": "execute", "id": identifier, "code": "'x' * 100000", "artifact_enabled": True})
    rpc = client.next("rpc")
    assert rpc["op"] == "artifact.begin" and rpc["run_id"] == identifier
    assert rpc["params"]["format"] == "text"
    client.send({"type": "rpc_result", "id": rpc["id"], "result": {"upload_id": "upload-1"}})
    received = bytearray()
    chunks = 0
    import base64
    while True:
        rpc = client.next("rpc")
        assert "path" not in rpc["params"]
        if rpc["op"] == "artifact.commit":
            break
        assert rpc["op"] == "artifact.append"
        assert rpc["params"]["offset"] == len(received)
        chunk = base64.b64decode(rpc["params"]["data"])
        assert len(chunk) <= 32768
        received.extend(chunk)
        chunks += 1
        client.send({"type": "rpc_result", "id": rpc["id"], "result": {"size": len(received)}})
    assert received == b"x" * 100000 and chunks == 4
    reference = {"id": "artifact-1", "size": 100000, "format": "text", "sha256": "test"}
    client.send({"type": "rpc_result", "id": rpc["id"], "result": reference})
    result = client.next("result")["result"]
    assert result["success"] and result["artifact"] == reference and result["truncated"]["return"]
    assert len(result["return_value"].encode()) <= 20000


def test_artifact_upload_error_aborts_before_reporting_failure(client):
    client.start("artifact(b'hello')")
    rpc = client.next("rpc")
    assert rpc["op"] == "artifact.begin"
    client.send({"type": "rpc_result", "id": rpc["id"], "result": {"upload_id": "failed-upload"}})
    rpc = client.next("rpc")
    assert rpc["op"] == "artifact.append"
    client.send({"type": "rpc_result", "id": rpc["id"], "error": "upload capacity error"})
    abort = client.next("rpc")
    assert abort["op"] == "artifact.abort"
    assert abort["params"]["upload_id"] == "failed-upload"
    client.send({"type": "rpc_result", "id": abort["id"], "result": {"aborted": True}})
    result = client.next("result")["result"]
    assert not result["success"] and "upload capacity error" in result["error"]


def test_artifact_custom_objects_fail_without_repr_and_do_not_dispatch(client):
    result = client.execute("effects = []\nclass Evil:\n    def __repr__(self):\n        effects.append(1)\n        return 'evil'\nartifact({'nested': Evil()})")
    assert not result["success"] and "exact JSON" in result["error"]
    assert client.execute("effects")["value"] == []


def test_output_bound_during_writes_and_unicode(client):
    result = client.execute("print('Ж' * 2000000)")
    assert len(result["stdout"].encode()) == 50000
    assert result["truncated"]["stdout"]
    outputs = [item for item in client.backlog if item["type"] == "output"]
    assert sum(len(item["text"].encode()) for item in outputs) == 50000
    assert client.execute("print('\\ud800')")["stdout"] == "?\n"


def test_custom_repr_and_hidden_namespace_never_called(client):
    result = client.execute("class Evil:\n    def __repr__(self):\n        raise RuntimeError('repr called')\nevil = Evil()\nevil")
    assert result["success"] and result["return_value"].startswith("<Evil")
    assert client.execute("2 + 2")["value"] == 4


def test_traceback_source_run_and_line(client):
    result = client.execute("x = 1\n1 / 0")
    assert not result["success"]
    assert '<repl:test-1>' in result["error"] and "line 2" in result["error"]
    assert "native_worker.py" not in result["error"]


def test_sync_await_and_full_envelope_once(client):
    client.start("await mcp.call('fixture', 'echo', arguments={'timeout': 7})")
    request = client.next("rpc")
    assert request["params"]["arguments"] == {"timeout": 7}
    envelope = {"isError": False, "content": [{"type": "text", "text": "one"},
                                                {"type": "text", "text": "two"}],
                "structuredContent": {"number": 7}}
    client.send({"type": "rpc_result", "id": request["id"], "result": envelope})
    result = client.next("result")["result"]
    assert result["success"]
    assert result["value"] == envelope
    assert client.execute("1")["value"] == 1
    assert not any(item["type"] == "rpc" for item in client.backlog)


def test_multiplexed_async_calls_and_tool_error(client):
    client.start("import asyncio\nresponses = await asyncio.gather(mcp.acall('fixture','a'), mcp.acall('fixture','b'))\n[dict(item) for item in responses]")
    requests = [client.next("rpc"), client.next("rpc")]
    assert {item["params"]["tool"] for item in requests} == {"a", "b"}
    for item in reversed(requests):
        client.send({"type": "rpc_result", "id": item["id"], "result": {
            "content": [], "structuredContent": {"tool": item["params"]["tool"]}}})
    result = client.next("result")["result"]
    assert result["success"] and len(result["value"]) == 2
    client.start("mcp.call('fixture', 'fail')")
    request = client.next("rpc")
    client.send({"type": "rpc_result", "id": request["id"], "result": {
        "isError": True, "content": [{"type": "text", "text": "bad arguments"}]}})
    result = client.next("result")["result"]
    assert not result["success"] and "ToolError: MCP tool failed: bad arguments" in result["error"]


@pytest.mark.parametrize("code", ["import time\nx = 10\ntime.sleep(30)\nx = 99",
                                 "import asyncio\nx = 10\nawait asyncio.sleep(30)\nx = 99"])
def test_interrupt_preserves_state_and_loop(client, code):
    client.start(code)
    time.sleep(0.1)
    client.process.send_signal(signal.SIGINT)
    result = client.next("result")["result"]
    assert not result["success"] and result["state"] == "preserved"
    assert client.execute("x")["value"] == 10
    assert client.execute("import asyncio\nawait asyncio.sleep(0)\n42")["value"] == 42


def test_shell_bound_cwd_and_timeout(client, tmp_path):
    result = client.execute(f"import os\nos.chdir({str(tmp_path)!r})\nr = sh('pwd')\n[r.stdout.strip(), r.ok]")
    assert result["value"] == [str(tmp_path), True]
    result = client.execute("r = sh(\"yes x | head -c 1000000\")\n[len(r), r.truncated]")
    assert result["value"] == [50000, True]
    result = client.execute("sh('sleep 30', timeout=0.1)")
    assert not result["success"] and "TimeoutExpired" in result["error"]
    result = client.execute("r = sh(r\"yes | tr y '\\377' | head -c 100000\")\n[len(r.encode('utf-8')), r.truncated]")
    assert result["value"][0] <= 50000 and result["value"][1]


def test_parent_eof_stops_executing_worker(client):
    client.start("import time\ntime.sleep(30)")
    time.sleep(0.1)
    client.process.stdin.close()
    assert client.process.wait(timeout=3) is not None


def test_direct_fd_output_never_corrupts_frames(client):
    result = client.execute("import os\nos.write(1, b'not-json\\n')\n42")
    assert result["value"] == 42


def test_invalid_bridge_timeout_has_no_side_effects(client):
    result = client.execute("mcp.call('fixture', 'write', timeout=-1)")
    assert not result["success"] and "timeout" in result["error"]
    assert not any(item["type"] == "rpc" for item in client.backlog)


def test_cancelled_rpc_late_result_does_not_poison_next_run(client):
    client.start("mcp.call('fixture','slow')")
    request = client.next("rpc")
    client.process.send_signal(signal.SIGINT)
    assert not client.next("result")["result"]["success"]
    client.send({"type": "rpc_result", "id": request["id"], "result": {"content": []}})
    assert client.execute("42")["value"] == 42


def test_cancelling_one_async_rpc_signals_only_its_origin(client):
    run = client.start("import asyncio\nfirst = asyncio.create_task(mcp.acall('fixture', 'slow'))\nawait asyncio.sleep(0.05)\nfirst.cancel()\ntry:\n    await first\nexcept asyncio.CancelledError:\n    pass\nawait mcp.acall('fixture', 'second')")
    first = client.next("rpc")
    cancelled = client.next("rpc_cancel")
    assert cancelled == {"type": "rpc_cancel", "id": first["id"], "run_id": run}
    second = client.next("rpc")
    assert second["id"] != first["id"] and second["run_id"] == run
    client.send({"type": "rpc_result", "id": second["id"], "result": {"content": [], "structuredContent": 42}})
    assert client.next("result")["result"]["value"]["structuredContent"] == 42
    # A late reply to the cancelled call cannot poison the namespace or trigger
    # another cancellation frame for the completed second call.
    client.send({"type": "rpc_result", "id": first["id"], "result": {"content": []}})
    assert client.execute("42")["value"] == 42
    assert not any(item["type"] == "rpc_cancel" for item in client.backlog)


def test_explicit_checkpoint_safe_atomic_and_recovery(client, tmp_path):
    target = str(tmp_path / "state.json")
    result = client.execute(f"x = {{'a': [1,2,3]}}\nclass Evil:\n    def __repr__(self):\n        raise RuntimeError('repr called')\nevil = Evil()\ncheckpoint({target!r})")
    assert result["success"]
    snapshot = json.loads(Path(target).read_text())
    assert snapshot["values"]["x"] == {"a": [1, 2, 3]}
    assert "evil" in snapshot["skipped"] and "Evil" in snapshot["skipped"]
    assert Path(target).stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.part"))
    recovered = Client()
    try:
        assert recovered.execute(f"restore({target!r})\nx")["value"] == {"a": [1, 2, 3]}
    finally:
        recovered.close()


def test_checkpoint_restore_rejects_before_mutation(client, tmp_path):
    target = tmp_path / "malicious.json"
    target.write_text(json.dumps({"format": "repl-checkpoint-v1", "values": {"x": 100, "sh": "bad"}}))
    result = client.execute(f"x = 1\nrestore({str(target)!r})")
    assert not result["success"]
    assert client.execute("x")["value"] == 1


def test_cell_background_tasks_cancelled_before_next_run(client):
    result = client.execute("import asyncio\nx = 0\nasync def late():\n    global x\n    await asyncio.sleep(0.1)\n    x = 99\ntask = asyncio.create_task(late())\nawait asyncio.sleep(0)")
    assert result["success"] and result["background_tasks_cancelled"] == 1
    assert client.execute("await asyncio.sleep(0.2)\n[x, task.cancelled()]")["value"] == [0, True]


def test_saved_popen_reaped_without_losing_wait_result(client):
    result = client.execute("import subprocess, time\np = subprocess.Popen(['true'])\ntime.sleep(0.2)\n[p.pid, p.returncode]")
    pid, returncode = result["value"]
    assert returncode == 0
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    assert not status.stdout.strip()
    assert client.execute("p.wait()")["value"] == 0


def test_background_task_refusing_cancel_clears_worker(client):
    result = client.execute("import asyncio\nasync def rogue():\n    while True:\n        try:\n            await asyncio.sleep(10)\n        except asyncio.CancelledError:\n            pass\ntask = asyncio.create_task(rogue())\nawait asyncio.sleep(0)")
    assert not result["success"] and result["state"] == "cleared"
    assert "refused cancellation" in result["error"]
    assert client.process.wait(timeout=3) == 70


def test_shell_and_large_string_trailing_preview_useful(client):
    result = client.execute("sh('printf hello')")
    assert result["value"] == "hello" and result["return_value"] == '"hello"'
    result = client.execute("'x' * 3000")
    assert result["value"] == "x" * 3000 and not result["truncated"]["return"]
    result = client.execute("'x' * 1000000")
    assert result["return_value"].startswith('"xxx')
    assert len(result["return_value"].encode()) <= 20000 and result["truncated"]["return"]


def test_cancelled_live_thread_cannot_write_later(client, tmp_path):
    marker = str(tmp_path / "late.txt")
    client.start(f"import threading, time\nfrom pathlib import Path\ndef late():\n    time.sleep(0.8)\n    Path({marker!r}).write_text('late')\nthreading.Thread(target=late).start()\ntime.sleep(20)")
    time.sleep(0.1)
    client.process.send_signal(signal.SIGINT)
    result = client.next("result")["result"]
    assert not result["success"] and result["state"] == "cleared"
    assert client.process.wait(timeout=3) == 70
    time.sleep(0.9)
    assert not Path(marker).exists()


def test_normal_background_thread_is_explicitly_rejected(client):
    result = client.execute("import threading, time\nthreading.Thread(target=lambda: time.sleep(20)).start()")
    assert not result["success"] and result["state"] == "cleared"
    assert "Join threads" in result["error"]
    assert client.process.wait(timeout=3) == 70


def test_joined_thread_bridge_cannot_borrow_cell_context(client):
    result = client.execute("import threading\nerrors = []\ndef request():\n    try:\n        mcp.call('fixture', 'write')\n    except RuntimeError as exc:\n        errors.append(str(exc))\nt = threading.Thread(target=request)\nt.start()\nt.join()\nerrors")
    assert result["success"]
    assert "originating active cell" in result["value"][0]
    assert not any(item["type"] == "rpc" for item in client.backlog)


def test_old_context_cannot_borrow_later_cell(client):
    assert client.execute("import contextvars\nold_context = contextvars.copy_context()")["success"]
    result = client.execute("old_context.run(mcp.call, 'fixture', 'write')")
    assert not result["success"] and "originating active cell" in result["error"]
    assert not any(item["type"] == "rpc" for item in client.backlog)


def test_cancelled_to_thread_cannot_write_later(client, tmp_path):
    marker = str(tmp_path / "late-async-thread.txt")
    client.start(f"import asyncio, time\nfrom pathlib import Path\ndef late():\n    time.sleep(0.8)\n    Path({marker!r}).write_text('late')\nawait asyncio.to_thread(late)")
    time.sleep(0.1)
    client.process.send_signal(signal.SIGINT)
    result = client.next("result")["result"]
    assert not result["success"] and result["state"] == "cleared"
    assert client.process.wait(timeout=3) == 70
    time.sleep(0.9)
    assert not Path(marker).exists()


def test_timer_callbacks_cannot_write_under_next_cell(client, tmp_path):
    marker = str(tmp_path / "timer-late.txt")
    result = client.execute(f"import asyncio\nfrom pathlib import Path\nasyncio.get_running_loop().call_later(0.1, lambda: Path({marker!r}).write_text('late'))\nawait asyncio.sleep(0)")
    assert result["success"]
    assert client.execute("await asyncio.sleep(0.2)\n42")["value"] == 42
    assert not Path(marker).exists()


def test_bounded_source_history_attributes_old_function(client):
    source = "def fail_later():\n    raise ValueError('old source failure')"
    assert client.execute(source)["success"]
    result = client.execute("fail_later()")
    assert not result["success"]
    assert '<repl:test-1>' in result["error"] and 'line 2' in result["error"]
    assert "raise ValueError('old source failure')" in result["error"]
    assert client.execute("repl_source('test-1')")["value"] == source
    history = client.execute("repl_history()")["value"]
    assert history[0] == {"run_id": "test-1", "lines": 2}
    assert all(set(item) == {"run_id", "lines"} for item in history)
    for _ in range(64):
        assert client.execute("x = 1")["success"]
    result = client.execute("repl_source('test-1')")
    assert not result["success"] and "unknown or evicted" in result["error"]
    assert len(client.execute("repl_history()")["value"]) == 64
    assert client.execute("import linecache\nlen([key for key in linecache.cache if key.startswith('<repl:')])")["value"] <= 64


def test_helper_shadowing_cannot_replace_history_access(client):
    assert client.execute("repl_source = 'shadow'; repl_history = 'shadow'")["success"]
    assert client.execute("callable(repl_source) and callable(repl_history)")["value"] is True


def test_source_history_memory_bound_evicts_before_run_limit(client):
    for _ in range(8):
        assert client.execute("blob = '" + "x" * 100000 + "'")["success"]
    result = client.execute("repl_source('test-1')")
    assert not result["success"] and "evicted" in result["error"]
    result = client.execute("sum(len(repl_source(item['run_id']).encode()) for item in repl_history())")
    assert result["value"] <= 1024 * 1024


def test_named_help_uses_single_catalogue_deadline(client):
    client.start("mcp.help('fixture', 'echo', timeout=0.5)")
    request = client.next("rpc")
    assert request["params"] == {"server": "fixture", "timeout": 0.5}
    definition = {"name": "echo", "inputSchema": {"type": "object", "required": ["text"]}}
    client.send({"type": "rpc_result", "id": request["id"], "result": [definition]})
    assert client.next("result")["result"]["value"] == definition


def test_idle_dns_and_default_executor_preserve_loop_state(client):
    result = client.execute("import asyncio\nx = 42\nlock = asyncio.Lock()\naddresses = await asyncio.get_running_loop().getaddrinfo('localhost', 80)\nlen(addresses) > 0")
    assert result["success"] and result["value"] is True and result["state"] == "preserved"
    result = client.execute("await lock.acquire()\nlock.release()\nawait asyncio.to_thread(lambda: x + 1)")
    assert result["success"] and result["value"] == 43 and result["state"] == "preserved"
    assert client.execute("x")["value"] == 42
