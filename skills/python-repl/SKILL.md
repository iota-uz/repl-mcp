---
name: python-repl
description: Use the native REPL MCP for persistent Python, batch file analysis, shell composition and calls to explicitly configured MCP servers. Prefer it to inline Python shell commands for multi-step work.
---

# Python REPL MCP 3.x

Load `execute_python` through tool search when deferred. Use `python_health` to
verify server version, selected Python interpreter, limits and broker configuration.
For a long cell use `python_start` to obtain a run ID immediately, then poll or cancel.
For MCP 2026-07-28, take `session_id` from health and pass it to execution; legacy
clients can omit it. One cell runs at a time. `python_run` polls progress/result and
`python_cancel` cancels an active run independently of Python execution.

## Python and files

Variables and imports persist. Top-level `await` uses a persistent event loop;
resource objects can be reused across cells. Do not wrap cells in `asyncio.run()`.
Cell-created tasks and delayed callbacks are cancelled when the cell finishes.
Unjoined user threads or active executor work force a reported worker reset; idle
DNS/default executor threads are safe to keep. Code has full
host filesystem/environment/network access; there is no sandbox.

```python
import json
from pathlib import Path
rows = json.loads(Path('~/data.json').expanduser().read_text())
print(len(rows))
```

`open()` and `Path()` do not expand `~` automatically. Relative paths follow the
worker's current cwd. `reset=True` restarts the worker: variables, imports, cwd,
environment changes and async resources are cleared. Ordinary interruption preserves
state; hard termination/crash clears it and reports that change. Use an explicit
`checkpoint(path)` / `restore(path)` for supported JSON values, not functions,
imports or live resources. `repl_history()` lists source IDs/line counts;
`repl_source(run_id)` retrieves bounded source explicitly. Check checkpoint's skipped list before relying on it.

## Shell composition

```python
r = sh('gh pr list --json number,title', check=False)
if r.ok:
    prs = json.loads(r)
else:
    print(r.returncode, r.stderr)
```

`sh` returns a string with `.returncode`, `.stderr`, `.ok` and `.truncated`. It follows
Python cwd and has bounded capture and process-group cleanup. Standard Popen children
are tracked/reaped; intentional detachment and custom native process APIs remain
trusted code responsibilities.

## MCP bridge

`mcp` is an injected object, not an import from the Python MCP SDK. It reaches only
servers granted through this broker's explicit registry. Host/cloud connectors and
another client's approvals are not automatically available.

```python
print(mcp.help())                         # available names; starts no servers
print(mcp.help('service', 'read_record'))  # full tool schema
response = mcp.call('service', 'read_record', arguments={'id': '123'})
data = response.get('structuredContent')
text = mcp.text(response)                 # explicitly concatenate all text blocks
response = await mcp.acall('service', 'read_record', arguments={'id': '456'})
```

`mcp.call` blocks and returns the complete MCP envelope as a dict. `mcp.acall` is
async and supports bounded fan-out. Legacy `await mcp.call(...)` is tolerated after
one synchronous dispatch; prefer `acall` for async execution. Use the `arguments`
dict for names colliding with `timeout`, `server`, `tool` or `arguments`. Never guess
JSON by automatically parsing the first text block; inspect structuredContent or
explicitly parse known text formats.

A downstream `isError` raises a `ToolError` retaining `.result`. Pagination is
bounded and complete or explicitly fails. `mcp.servers()` lists names;
`mcp.list_tools(server)` returns definitions; `mcp.refresh()` reloads the registry.
Connections are lazy and failures do not become empty successful listings.

`mcp.journal()` records call metadata, not args/responses. Cancel/timeout stops new
work and requests cancellation downstream; an already sent write may have succeeded.
An `outcome_unknown` call must be reconciled with the target before retry. No script
is automatically replayed and cancellation is not a transaction rollback.

## Packages and output

Use the actual worker interpreter, not a guessed shell/uv environment:

```python
import shlex, sys
sh('uv pip install --python ' + shlex.quote(sys.executable) + ' PACKAGE==VERSION')
```

Prefer an explicitly configured reproducible venv. Reset may be needed after an
install. Summarize large results or write them to an explicit file. Streams cap at
50,000 UTF-8 bytes during capture, previews at 20,000 bytes. Structured execution
results report success, run/session/generation IDs, error, truncation and elapsed time.
Custom repr is not implicitly called. Native fd output is separate from protocol.
No fixed latency/SLA is promised.
