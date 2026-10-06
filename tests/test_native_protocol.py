"""Real stdio acceptance for the shipped Rust executable, independent of SDK shims."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class NativeClient:
    def __init__(self, binary, cwd, config=None, protocol="2025-11-25", extra_args=(), mcp_scope="none"):
        command = [str(binary), "--python", sys.executable, "--transport", "stdio"]
        command += ["--config", str(config)] if config else ["--mcp-scope", mcp_scope]
        command.extend(extra_args)
        self.protocol = protocol
        self.process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self.messages = queue.Queue()
        self.next_id = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        if protocol != "2026-07-28":
            self.request("initialize", {"protocolVersion": protocol,
                                         "capabilities": {},
                                         "clientInfo": {"name": "acceptance", "version": "1"}})
            self.send({"method": "notifications/initialized"})

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                self.messages.put({"corrupt_stdout": line})
        self.messages.put({"eof": True})

    def send(self, payload):
        if self.protocol == "2026-07-28" and "id" in payload:
            payload = {**payload, "params": {**payload.get("params", {}), "_meta": {
                "io.modelcontextprotocol/protocolVersion": self.protocol,
                "io.modelcontextprotocol/clientInfo": {"name": "acceptance", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }}}
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}) + "\n")
        self.process.stdin.flush()

    def start(self, method, params):
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        return self.next_id

    def receive(self, request_id, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            item = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            assert not item.get("corrupt_stdout"), item
            assert not item.get("eof"), "native process closed stdout"
            if item.get("id") == request_id:
                assert "error" not in item, item
                return item["result"]

    def request(self, method, params):
        return self.receive(self.start(method, params))

    def execute(self, code, **arguments):
        return self.request("tools/call", {"name": "execute_python",
                                           "arguments": {"code": code, **arguments}})

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            pytest.fail("native runtime failed to stop after stdin EOF")
        self.process.stdout.close()
        self.reader.join(timeout=2)


@pytest.fixture
def binary():
    path = Path(os.environ.get("REPL_MCP_BINARY", ROOT / "target/debug/repl-mcp"))
    if not path.is_file():
        pytest.skip("build native binary or set REPL_MCP_BINARY")
    return path.resolve()


@pytest.fixture
def native(binary, tmp_path):
    client = NativeClient(binary, tmp_path)
    yield client
    client.close()


def test_structured_error_and_schema(native):
    tools = native.request("tools/list", {})["tools"]
    execute = next(tool for tool in tools if tool["name"] == "execute_python")
    assert execute["outputSchema"]["type"] == "object"
    assert "mcp" in execute["description"][:400]
    result = native.execute("1 / 0")
    assert result["isError"] is True
    assert result["structuredContent"]["success"] is False
    assert "ZeroDivisionError" in result["structuredContent"]["error"]
    recovered = native.execute("2 + 2")
    assert recovered["structuredContent"]["return_value"] == "4"


@pytest.mark.parametrize("protocol", ["2025-03-26", "2025-11-25", "2026-07-28"])
def test_tools_list_cache_hints_match_protocol(binary, tmp_path, protocol):
    client = NativeClient(binary, tmp_path, protocol=protocol)
    try:
        result = client.request("tools/list", {})
        assert {tool["name"] for tool in result["tools"]} >= {
            "execute_python", "python_start", "python_health", "python_run", "python_cancel",
        }
        assert type(result["ttlMs"]) is int
        assert result["ttlMs"] == 0
        assert result["cacheScope"] == "private"
    finally:
        client.close()


def test_persistent_async_and_no_hidden_repr(native):
    assert not native.execute("import asyncio; lock = asyncio.Lock(); counter = 0")["isError"]
    assert not native.execute("await lock.acquire(); lock.release()")["isError"]
    native.execute("class Expensive:\n def __repr__(self):\n  global counter\n  counter += 1\n  raise RuntimeError('unexpected repr')\nobj = Expensive()")
    result = native.execute("counter")
    assert result["structuredContent"]["return_value"] == "0"


def test_output_bounded_and_fd_output_does_not_corrupt_protocol(native):
    result = native.execute("import os; os.write(1, b'fd output\\n'); print('x' * 2000000)")
    payload = result["structuredContent"]
    assert payload["truncated"]["stdout"]
    assert len(payload["stdout"].encode()) <= 65536
    assert native.execute("40 + 2")["structuredContent"]["return_value"] == "42"


def test_crash_and_interrupt_grace_exit_recover(native):
    result = native.execute("import os; os._exit(3)")
    assert result["isError"]
    assert result["structuredContent"]["state"] == "cleared"
    assert native.execute("42")["structuredContent"]["return_value"] == "42"
    result = native.execute("import time, os\ntry:\n time.sleep(10)\nexcept KeyboardInterrupt:\n os._exit(4)", timeout=.15)
    assert result["isError"]
    assert native.execute("43")["structuredContent"]["return_value"] == "43"


def test_client_cancel_stops_late_effect(native, tmp_path):
    marker = tmp_path / "late-effect"
    request_id = native.start("tools/call", {"name": "execute_python", "arguments": {
        "code": f"import time\ntime.sleep(2)\nopen({str(marker)!r}, 'w').write('late')"}})
    time.sleep(.3)
    native.send({"method": "notifications/cancelled", "params": {"requestId": request_id}})
    recovered = native.execute("42")
    assert recovered["structuredContent"]["return_value"] == "42", recovered
    time.sleep(2.1)
    assert not marker.exists()


def test_bridge_envelopes_pagination_errors_and_argument_collisions(binary, tmp_path):
    config = tmp_path / "registry.json"
    config.write_text(json.dumps({"mcpServers": {"fixture": {
        "command": sys.executable,
        "args": [str(ROOT / "tests/fixtures/native_mcp_fixture.py")],
    }}}))
    client = NativeClient(binary, tmp_path, config)
    try:
        result = client.execute("result = mcp.call('fixture', 'multi'); print(len(result['content'])); print(result['structuredContent']['name'])")
        assert not result["isError"], result
        assert "2" in result["structuredContent"]["stdout"]
        assert "multi" in result["structuredContent"]["stdout"]
        result = client.execute("print([t['name'] for t in mcp.list_tools('fixture')])")
        assert "slow_write" in result["structuredContent"]["stdout"]
        result = client.execute("await mcp.call('fixture', 'echo', text='await succeeds once')")
        assert not result["isError"], result
        result = client.execute("mcp.call('fixture', 'collision', arguments={'timeout': 2, 'server': 'x', 'tool': 'y'})")
        assert not result["isError"], result
        result = client.execute("mcp.call('fixture', 'fail')")
        assert result["isError"]
    finally:
        client.close()


def test_current_stateless_protocol_uses_explicit_session(binary, tmp_path):
    client = NativeClient(binary, tmp_path, protocol="2026-07-28")
    try:
        tools = client.request("tools/list", {})
        assert any(tool["name"] == "execute_python" for tool in tools["tools"])
        health = client.request("tools/call", {"name": "python_health", "arguments": {}})
        session = health["structuredContent"]["server"]["session_id"]
        result = client.execute("x = 40; x + 2", session_id=session)
        assert not result["isError"], result
        assert result["structuredContent"]["return_value"] == "42"
        result = client.execute("x + 3", session_id=session)
        assert result["structuredContent"]["return_value"] == "43"
    finally:
        client.close()


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_process_gone(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not process_alive(pid), f"owned process {pid} survived cleanup"


@pytest.mark.skipif(os.name != "posix", reason="Unix process group acceptance")
def test_parent_death_while_native_code_holds_gil(binary, tmp_path):
    client = NativeClient(binary, tmp_path)
    worker_pid = None
    try:
        worker_pid = int(client.execute("import os; os.getpid()")["structuredContent"]["return_value"])
        client.start("tools/call", {"name": "execute_python", "arguments": {
            "code": "import ctypes; ctypes.PyDLL(None).sleep(8)", "timeout": 30}})
        time.sleep(.3)
        client.process.kill()
        client.process.wait(timeout=5)
        wait_process_gone(worker_pid)
    finally:
        if worker_pid and process_alive(worker_pid):
            os.killpg(worker_pid, signal.SIGKILL)
        if client.process.poll() is None:
            client.process.kill()
            client.process.wait(timeout=5)
        client.process.stdin.close()
        client.process.stdout.close()
        client.reader.join(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="Unix process group acceptance")
def test_detached_popen_and_worker_stop_with_parent(binary, tmp_path):
    client = NativeClient(binary, tmp_path)
    worker_pid = child_pid = None
    try:
        worker_pid = int(client.execute("import os; os.getpid()")["structuredContent"]["return_value"])
        result = client.execute("import subprocess; child = subprocess.Popen(['/bin/sleep', '20'], start_new_session=True); print(child.pid)")
        child_pid = int(result["structuredContent"]["stdout"].strip())
        client.process.kill()
        client.process.wait(timeout=5)
        wait_process_gone(worker_pid)
        wait_process_gone(child_pid)
    finally:
        for pid in (worker_pid, child_pid):
            if pid and process_alive(pid):
                os.killpg(pid, signal.SIGKILL)
        if client.process.poll() is None:
            client.process.kill()
            client.process.wait(timeout=5)
        client.process.stdin.close()
        client.process.stdout.close()
        client.reader.join(timeout=2)


def test_crash_retains_native_output(native):
    result = native.execute("import os; os.write(1, b'before-native-crash\\n'); os._exit(3)")
    assert result["isError"]
    assert "before-native-crash" in result["structuredContent"]["stderr"]


@pytest.mark.skipif(os.name != "posix", reason="Unix process group acceptance")
def test_parent_death_stops_downstream_peer_holding_gil(binary, tmp_path):
    marker = tmp_path / "peer.pid"
    config = tmp_path / "registry.json"
    config.write_text(json.dumps({"mcpServers": {"fixture": {
        "command": sys.executable,
        "args": [str(ROOT / "tests/fixtures/native_mcp_fixture.py")],
    }}}))
    client = NativeClient(binary, tmp_path, config)
    peer_pid = worker_pid = None
    try:
        worker_pid = int(client.execute("import os; os.getpid()")["structuredContent"]["return_value"])
        client.start("tools/call", {"name": "execute_python", "arguments": {
            "code": f"mcp.call('fixture', 'native_hold', marker={str(marker)!r})", "timeout": 30}})
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert marker.exists(), "fixture call did not start"
        peer_pid = int(marker.read_text())
        client.process.kill()
        client.process.wait(timeout=5)
        wait_process_gone(worker_pid)
        wait_process_gone(peer_pid)
    finally:
        for pid in (worker_pid, peer_pid):
            if pid and process_alive(pid):
                os.killpg(pid, signal.SIGKILL)
        if client.process.poll() is None:
            client.process.kill()
            client.process.wait(timeout=5)
        client.process.stdin.close()
        client.process.stdout.close()
        client.reader.join(timeout=2)


def test_background_start_progress_and_cancel(native, tmp_path):
    marker = tmp_path / "background-late-write"
    started = time.monotonic()
    launch = native.request("tools/call", {"name": "python_start", "arguments": {
        "code": f"import time\nprint('started', flush=True)\ntime.sleep(3)\nopen({str(marker)!r}, 'w').write('late')",
        "timeout": 30,
    }})
    assert time.monotonic() - started < 2
    run_id = launch["structuredContent"]["run_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        progress = native.request("tools/call", {"name": "python_run", "arguments": {"run_id": run_id}})
        if "started" in progress["structuredContent"].get("stdout", ""):
            break
        time.sleep(.05)
    assert "started" in progress["structuredContent"].get("stdout", "")
    cancel = native.request("tools/call", {"name": "python_cancel", "arguments": {"run_id": run_id}})
    assert cancel["structuredContent"]["cancel_requested"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = native.request("tools/call", {"name": "python_run", "arguments": {"run_id": run_id}})
        if result["structuredContent"].get("status") != "running":
            break
        time.sleep(.05)
    assert result["structuredContent"]["success"] is False
    time.sleep(3.1)
    assert not marker.exists()


def test_escaped_output_stays_within_serialized_protocol_budget(native):
    result = native.execute("import os; print('\\x00' * 50000); os.write(2, b'\\x00' * 90000)")
    assert not result["isError"], result
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 1024 * 1024
    assert native.execute("42")["structuredContent"]["return_value"] == "42"


def test_spawned_multiprocessing_is_allowed(native):
    result = native.execute(
        "import multiprocessing as mp, time\n"
        "p = mp.get_context('spawn').Process(target=time.sleep, args=(.05,))\n"
        "p.start(); p.join(timeout=3)\n"
        "p.exitcode"
    )
    assert not result["isError"], result
    assert result["structuredContent"]["return_value"] == "0"


def test_sigterm_exits_with_client_stdin_still_open(native):
    result = native.execute("import os; os.getpid()")
    worker_pid = result["structuredContent"]["value"]
    native.process.send_signal(signal.SIGTERM)
    native.process.wait(timeout=5)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(worker_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.02)
    else:
        pytest.fail("worker survived native server SIGTERM")
