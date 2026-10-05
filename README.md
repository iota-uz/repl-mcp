# REPL MCP 3.0

A native Rust MCP server with a persistent CPython worker. Python remains the language
of your cells; Rust owns the protocol, MCP broker, deadlines and process lifecycle.
The worker starts on the first execution. An idle connection does not start Python
or a multiprocessing resource tracker.

Execution has full access to the host filesystem, environment and network. The user
explicitly chose this mode; the process boundary provides crash recovery, not a sandbox.
Supported hosts: macOS and Linux. Windows job ownership is not implemented.

## Installation

Download the wheel matching your host from the [v3.0.0 GitHub release](https://github.com/iota-uz/repl-mcp/releases/tag/v3.0.0):
macOS arm64, macOS x86_64 or Linux x86_64 (glibc 2.28+). Python 3.10+ is required;
binary wheels do not require a Rust compiler. Install the downloaded wheel in a
dedicated environment:

```sh
uv venv ~/.local/share/repl-mcp/venv --python 3.12
uv pip install --python ~/.local/share/repl-mcp/venv/bin/python /absolute/path/repl_mcp-3.0.0-*.whl
~/.local/share/repl-mcp/venv/bin/repl-mcp --help
```

Use that absolute executable path in your client registration. Releases are
distributed through GitHub; an unqualified PyPI install is not the release path.
Source installation requires Rust 1.88+ and CPython 3.10+:

```sh
uv sync --extra dev --locked
uv run repl-mcp --help
uv run repl-mcp --transport stdio --mcp-scope none
```

For a client launch configuration, use the absolute installed `repl-mcp` executable
and an explicit project/config directory. The default working directory is the
client's launch directory. `--python /absolute/venv/bin/python` selects a runtime;
otherwise the binary prefers a sibling Python interpreter, then `python3` on PATH.
This makes wheel/uvx installations use their own environment. `python_health`
reports the selected interpreter and, after lazy startup, its actual version.

The plugin launcher selects the published wheel for the host and runs it through
`uvx`; its default launch does not compile Rust. It requires `uv` on PATH. The first
launch downloads the wheel and Python dependencies, so allow enough startup time
for that initial network setup. Subsequent launches reuse uv's cache. For a client
with a short startup deadline, prewarm it with
`sh /path/to/plugin/scripts/repl-mcp-launch.sh --help`, or register the installed
binary above. Deliberate Git/source launches require Rust and a longer cold-start
budget for compilation.
Do not leave old MCP registrations enabled alongside the native server. The Python
2.x server and engine modules remain source regression fixtures, not the shipped
entrypoint; the binary wheel contains the Rust executable with an embedded worker.
For changes to broker discovery, result envelopes and helper APIs, follow the
[3.0 migration checklist](CHANGELOG.md#migration-from-2x).

## Execution tools

| Tool | Purpose |
|---|---|
| `execute_python(code, reset=False, timeout=120, session_id=None)` | Execute one cell; top-level await works; state persists |
| `python_start(code, reset=False, timeout=120, session_id=None)` | Start a long cell and return its run ID immediately |
| `python_health()` | Inspect server/runtime/broker without starting Python or connecting servers |
| `python_run(run_id=None)` | Inspect active execution or the latest retained result |
| `python_cancel(run_id=None)` | Request cancellation of the active execution and linked MCP calls |

Results contain readable text and structured fields: `run_id`, `success`, `stdout`,
`stderr`, `return_value`, `error`, `elapsed_ms`, `truncated` and `state`. Results also
identify the session and generation. `state` describes the worker after execution;
`state_reset` reports a fresh/reset/recovered namespace before that cell. Python failures
set MCP `isError`; invalid tool arguments are protocol errors. Timing includes
postprocessing. Normal interruption preserves variables; crashes or forced termination
clear them. A reset restarts the worker and restores initial cwd/environment/loop.

Only one cell runs at a time; overlapping executions fail promptly. Health and cancel
remain available. For current MCP 2026-07-28 stateless requests, first call health,
then pass its `server.session_id` to execution/start; a foreign session ID is rejected.
Legacy clients can omit it. `python_start` avoids client request deadlines: poll
`python_run(run_id)` until completion, or cancel it explicitly. History retains the
last 64 results. Output is bounded during writes
in UTF-8 bytes, not after accumulating an unlimited buffer. Safe previews inspect
small builtin values; custom `repr` is not executed implicitly. Native fd output is
drained separately and never mixed into MCP stdout.

```python
import asyncio
import httpx
client = httpx.AsyncClient()
# A later cell can reuse the client on the persistent loop:
data = (await client.get(url)).json()
```

Cell-owned background tasks and delayed callbacks are cancelled at completion.
Unjoined user threads or still-running executor work require termination of the
worker and report cleared state; finished/default DNS executor threads may remain
idle. Keep resource objects and await their work. Use top-level await instead of
nested `asyncio.run()`.

```python
from pathlib import Path
Path('~/data.json').expanduser().read_text()  # open() does not expand ~
json_text = sh('gh pr list --json number,title')
print(json_text.returncode, json_text.stderr, json_text.ok)
```

`sh()` captures bounded stdout/stderr, follows Python's current cwd, and owns its
process group. Standard `subprocess.Popen` children are tracked and polled to avoid
retained zombies while preserving `.wait()` results. Explicit process detachment,
`os.fork`, native process APIs and arbitrary custom signal handling are trusted code;
they are not an OS containment guarantee.

`checkpoint(path)` writes a bounded, atomic JSON checkpoint of supported builtin
namespace values and reports skipped values. `restore(path)` validates the complete
checkpoint before loading it. Imports, functions, native objects and active resources
are not serializable through this contract. No automatic code replay or pickle loading.
`repl_history()` lists source IDs/line counts; `repl_source(run_id)` explicitly retrieves
retained code. Source/linecache storage is capped at 64 runs and 1 MiB, never journalled.

## MCP broker

The default registry is the launch project's `.mcp.json`. Use `--config /path/registry.json`
for an independent broker registry, or `--mcp-scope none` to disable it. Client-managed
cloud connectors cannot be discovered by a local server. Claude, Codex and t3 can all
use the same explicit registry; another client's credentials and approvals are not
inherited automatically.

```json
{
  "mcpServers": {
    "service": {
      "type": "stdio",
      "command": "/absolute/path/service-mcp",
      "args": [],
      "allowedTools": ["read_record", "update_record"]
    }
  }
}
```

A dedicated registry is a grant to the broker. `allowedTools` narrows that grant and
is checked on each call. Foreign Claude `user`/`local` scopes are opt-in and require
independent `brokerAllowed: true` entries; plugin registries require an explicit
exported configuration. This avoids reinterpreting a client's interactive approvals
as blanket authorization. `REPL_MCP_NO_BRIDGE=1` blocks recursive brokers.

```python
print(mcp.help())                         # names only, connects nothing
print(mcp.help('service', 'read_record'))  # full input/output schema
response = mcp.call('service', 'read_record', arguments={'id': '123'})
print(response.get('structuredContent'))  # complete envelope retained
print(mcp.text(response))                 # all text blocks, explicit extraction
response = await mcp.acall('service', 'read_record', arguments={'id': '456'})
```

`mcp.call` is synchronous; `mcp.acall` supports concurrent async work. Legacy
`await mcp.call(...)` succeeds after a single synchronous call, so it cannot fail
only after applying a write. Use `arguments={...}` for tool arguments named `timeout`,
`server`, `tool` or `arguments`. MCP errors raise `ToolError` with `.result` retaining
the full envelope. Listing follows bounded pagination; failures are not empty success.

Sessions connect lazily, equivalent aliases share a transport, and broken transports
are invalidated. Refresh does not retry writes. `mcp.refresh()` reloads configuration;
`mcp.journal()` shows bounded call metadata and outcomes, including `outcome_unknown`
when a dispatched call is abandoned. No arguments or response payloads enter this
journal. `--journal /private/path/journal.json` optionally persists those metadata
records atomically with an exclusive process lock. Prior unfinished dispatches become
`outcome_unknown` on recovery. An execution is not a transaction: cancellation cannot undo a committed write.
Reconcile an unknown outcome with the target before retrying.

Configuration errors omit secret values. `${VAR}` must exist and be nonempty;
transport mismatches, unknown fields and empty Bearer headers fail explicitly.
HTTP uses Streamable HTTP, with explicit headers or independently configured OAuth.
`--oauth-login SERVER` starts the configured broker's authorization flow; it does
not borrow browser credentials from another client. Invalid broker configuration
leaves health and ordinary Python available; broker operations fail closed until a
successful refresh. See the configuration contract
in [migration design](docs/rust-migration.md).

For an independent authorization-code grant:

```json
{"mcpServers":{"service":{"type":"http","url":"https://service.example/mcp",
  "oauth":{"grantType":"authorization_code",
    "credentialFile":"/private/directory/service.json","redirectPort":0}}}}
```

Run `repl-mcp --config /absolute/registry.json --oauth-login service`, then open
the printed authorization URL. Credential directories/files require private
permissions; symlinks are rejected. `redirectPort: 0` uses an ephemeral localhost
port; configure a fixed port when the provider requires a pre-registered callback.
Client-credentials grants use `grantType: "client_credentials"`, `clientId` and
`clientSecret`; environment substitutions keep secrets out of checked-in config.

## Packages and resource limits

Runtime dependencies are pinned; Rust's transitive graph is committed in `Cargo.lock`,
and Python's environment in `uv.lock`. To add a package to the actual running
interpreter, use an explicit target:

```python
import shlex, sys
sh('uv pip install --python ' + shlex.quote(sys.executable) + ' numpy==2.2.6')
```

Imports may need a worker reset after an install. Prefer a reproducible configured
venv for long-lived use. `openpyxl` and `httpx` are included; scientific packages
are not silently installed.

Limits include 256 KiB code, 1 MiB protocol/worker frames, 50,000-byte captured streams,
20,000-byte previews, bounded RPC concurrency and execution timeout up to 3600 seconds.
`--max-memory-mib` defaults to 2048; zero disables RSS enforcement. This sampled
worker RSS budget is not a hard whole-tree quota. A native C call may not process
SIGINT; after the grace period Rust terminates the worker and reports cleared state.
Large output should be written to an explicitly chosen file and summarized in the cell.

## Verification

```sh
make verify
make wheel
make sdist
```

CI runs strict Rust/Python checks and the complete regression suite on macOS/Linux,
Python 3.10/3.12/3.14 and MCP SDK 2.2/2.3. Stdio contract tests cover errors, schemas,
cancellation late effects, fatal exits, asynchronous state and full broker envelopes.
These fixtures verify wire behavior; they do not claim a live Claude/Codex/t3 UI test.
See [audit coverage](docs/audit-coverage.md) for per-finding evidence and residual limits.

[MIT license](LICENSE).
