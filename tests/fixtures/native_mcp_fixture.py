"""Dependency-free MCP peer for native transport contract acceptance tests."""

import json
import ctypes
import os
import sys
import threading
from pathlib import Path

WRITE_LOCK = threading.Lock()
CANCELLED = {}


def send(payload):
    with WRITE_LOCK:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def handle(request):
    request_id = request["id"]
    cancelled = CANCELLED.setdefault(str(request_id), threading.Event())
    method = request["method"]
    params = request.get("params", {})
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-11-25"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "native-acceptance-fixture", "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        names = ["echo", "multi", "fail"] if not params.get("cursor") else ["slow_write", "collision", "native_hold"]
        result = {"tools": [
            {"name": name, "description": name,
             "inputSchema": {"type": "object", "properties": {
                 "text": {"type": "string"}, "marker": {"type": "string"},
                 "timeout": {"type": "integer"}, "server": {"type": "string"},
                 "tool": {"type": "string"}}, "additionalProperties": False}}
            for name in names
        ]}
        if not params.get("cursor"):
            result["nextCursor"] = "page-two"
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments", {})
        if name == "native_hold":
            Path(args["marker"]).write_text(str(os.getpid()))
            ctypes.PyDLL(None).sleep(8)
        if name == "slow_write":
            if cancelled.wait(2):
                return
            Path(args["marker"]).write_text("committed")
        result = {
            "content": [{"type": "text", "text": args.get("text", name)}],
            "structuredContent": {"name": name, "arguments": args},
            "isError": name == "fail",
        }
        if name == "multi":
            result["content"].append({"type": "text", "text": "second"})
    else:
        send({"jsonrpc": "2.0", "id": request_id,
              "error": {"code": -32601, "message": "Unknown method"}})
        return
    result["resultType"] = "complete"
    send({"jsonrpc": "2.0", "id": request_id, "result": result})
    CANCELLED.pop(str(request_id), None)


for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "notifications/cancelled":
        key = str(request.get("params", {}).get("requestId"))
        CANCELLED.setdefault(key, threading.Event()).set()
    elif "id" in request:
        threading.Thread(target=handle, args=(request,), daemon=True).start()
