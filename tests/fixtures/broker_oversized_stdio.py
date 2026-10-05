"""Oversized peer frame fixture; intentionally ignores output BrokenPipe cleanup."""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "oversized", "version": "1"}}
    elif request["method"] == "tools/list":
        result = {"tools": [{"name": "large", "inputSchema": {"type": "object"}, "description": "x" * 2_000_000}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                          "error": {"code": -32601, "message": "unknown"}}), flush=True)
        continue
    result["resultType"] = "complete"
    try:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    except BrokenPipeError:
        break
