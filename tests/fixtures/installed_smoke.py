"""Dependency-free smoke test of an installed binary and its sibling Python."""

import argparse
import json
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", default="3.1.0")
    args = parser.parse_args()
    workspace = tempfile.TemporaryDirectory(prefix="repl-installed-smoke-")
    project = Path(workspace.name)
    binary = Path(sys.executable).parent / "repl-mcp"
    assert binary.is_file(), f"Installed executable missing: {binary}"
    signal.alarm(45)
    process = subprocess.Popen(
        [str(binary), "--mcp-scope", "none"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        def request(number, method, params):
            process.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": number, "method": method, "params": params,
            }) + "\n")
            process.stdin.flush()
            while True:
                line = process.stdout.readline()
                assert line, f"Installed server closed stdout: {process.poll()}"
                message = json.loads(line)
                if message.get("id") == number:
                    assert "error" not in message, message
                    return message["result"]

        initialized = request(1, "initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "installed-artifact-smoke", "version": "1"},
        })
        assert initialized["serverInfo"]["version"] == args.expected_version
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) + "\n")
        process.stdin.flush()
        catalogue = request(2, "tools/list", {})
        assert type(catalogue["ttlMs"]) is int and catalogue["ttlMs"] == 0, catalogue
        assert catalogue["cacheScope"] == "private", catalogue
        tools = catalogue["tools"]
        assert {tool["name"] for tool in tools} >= {
            "execute_python", "python_start", "python_health", "python_run", "python_cancel",
            "python_session_open", "python_session_close", "python_session_list",
            "python_session_inspect", "python_execute_file", "artifact_create",
            "artifact_read", "artifact_save", "artifact_delete", "artifact_forward",
        }
        health = request(3, "tools/call", {
            "name": "python_health", "arguments": {},
        })["structuredContent"]["server"]
        assert health["version"] == args.expected_version, health
        assert health["python"] is None, "Idle installed server started Python eagerly"
        session = health["session_id"]
        result = request(4, "tools/call", {
            "name": "execute_python", "arguments": {
                "session_id": session,
                "code": "import sys, httpx, openpyxl\ninstalled_marker = 41\n"
                        "{'prefix': sys.prefix, 'httpx': httpx.__version__, "
                        "'openpyxl': openpyxl.__version__}",
            },
        })
        assert not result["isError"], result
        value = result["structuredContent"]["value"]
        assert Path(value["prefix"]).resolve() == Path(sys.prefix).resolve(), value
        assert value["httpx"] == "0.28.1" and value["openpyxl"] == "3.1.5", value
        persisted = request(5, "tools/call", {
            "name": "execute_python", "arguments": {
                "session_id": session, "code": "installed_marker + 1",
            },
        })
        assert persisted["structuredContent"]["value"] == 42, persisted
        failed = request(6, "tools/call", {
            "name": "execute_python", "arguments": {
                "session_id": session, "code": "1 / 0",
            },
        })
        assert failed["isError"] and not failed["structuredContent"]["success"], failed
        opened = request(7, "tools/call", {
            "name": "python_session_open", "arguments": {
                "name": "installed-project", "project": str(project),
                "python": sys.executable,
            },
        })
        assert not opened["isError"], opened
        independent = opened["structuredContent"]["session_id"]
        script = project / "task.py"
        script.write_text("file_marker = 7\nfile_marker\n")
        ran = request(8, "tools/call", {
            "name": "python_execute_file", "arguments": {
                "session_id": independent, "path": str(script), "mode": "persistent",
            },
        })
        assert ran["structuredContent"]["value"] == 7, ran
        inspected = request(9, "tools/call", {
            "name": "python_session_inspect", "arguments": {
                "session_id": independent, "include_namespace": True,
            },
        })
        assert not inspected["isError"], inspected
        large = request(10, "tools/call", {
            "name": "execute_python", "arguments": {
                "session_id": independent, "code": "'hello' * 10000",
            },
        })
        reference = large["structuredContent"]["artifact"]
        assert reference["size"] == 50000, reference
        chunk = request(11, "tools/call", {
            "name": "artifact_read", "arguments": {
                "session_id": independent, "id": reference["id"], "length": 5,
            },
        })
        assert chunk["structuredContent"]["content"] == "hello", chunk
        closed = request(12, "tools/call", {
            "name": "python_session_close", "arguments": {"session_id": independent},
        })
        assert closed["structuredContent"]["closed"], closed
        print(json.dumps({"version": args.expected_version, "installed_binary": str(binary),
                          "worker": value, "persistent_state": True, "semantic_error": True,
                          "session_file_inventory_artifact": True}))
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise
        finally:
            process.stdout.close()
            signal.alarm(0)
            workspace.cleanup()
    assert process.returncode == 0, process.returncode


if __name__ == "__main__":
    main()
