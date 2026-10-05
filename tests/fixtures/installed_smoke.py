"""Dependency-free smoke test of an installed binary and its sibling Python."""

import argparse
import json
import signal
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", default="3.0.0")
    args = parser.parse_args()
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
        tools = request(2, "tools/list", {})["tools"]
        assert {tool["name"] for tool in tools} == {
            "execute_python", "python_start", "python_health", "python_run", "python_cancel",
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
        print(json.dumps({"version": args.expected_version, "installed_binary": str(binary),
                          "worker": value, "persistent_state": True, "semantic_error": True}))
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
    assert process.returncode == 0, process.returncode


if __name__ == "__main__":
    main()
