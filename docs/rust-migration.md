# Rust migration contract

Tracking: https://github.com/iota-uz/repl-mcp/issues/9 (63 audit findings).

## Decision

The MCP server, lifecycle supervisor and MCP broker run in a native Rust binary
using the official rmcp SDK (3.5.0, MCP 2026-07-28 with legacy compatibility).
Python remains a separate, lazily started CPython worker. No Python interpreter
is embedded in the server. A subprocess provides crash isolation, not a security
sandbox. The user explicitly rejected sandboxing on 2026-10-05; full host access
is intentional. Trusted local execution is explicit; arbitrary full state restoration
and exactly-once external writes cannot be promised.

## Components

- `native/src/{main,server,supervisor}.rs`: CLI, MCP server and process supervisor.
- `native/src/{broker,config}.rs`: downstream sessions, policy and configuration.
- `src/repl_mcp/native_worker.py`: embedded standalone Python evaluation worker.
- `tests/test_native*`: worker, protocol and broker acceptance.

## Worker wire contract v1

UTF-8 newline JSON over child stdin/stdout, maximum frame 1 MiB. Worker duplicates
protocol stdout before rerouting OS fd 1 to stderr. No user output enters protocol.
Rust serializes writes; worker has one background reader routing control commands
and RPC responses, so bridge calls and parent death remain observable during cells.

Parent commands:
`{"type":"execute","id":"run-id","code":"...","reset":false,"timeout":120}`,
`{"type":"shutdown"}` and
`{"type":"rpc_result","id":"rpc-id","result":{},"error":null}`.

Worker events:
`{"type":"ready","python_version":"...","pid":123}`,
`{"type":"result","id":"run-id","result":{...}}`,
`{"type":"output","id":"run-id","stream":"stdout","text":"..."}` and
`{"type":"rpc","id":"rpc-id","run_id":"run-id","op":"call","params":{...}}`.

Execution result: `run_id`, `session_id`, `generation`, `state_reset`, `success`, `stdout`, `stderr`, `return_value`,
`error` (nullable string), `elapsed_ms`, `truncated` (stdout/stderr/return booleans),
`state` (`preserved` or `cleared`). JSON scalar/container trailing values may
add `value`; other values use bounded, safe previews without enumerating globals.
`state` describes the namespace after the run. `state_reset` reports a new
namespace before the run, including recovery from a previous crash.

Broker request operations: `call` params `{server,tool,arguments,timeout}`, `tools`
params `{server}`, `servers` params `{}`, `refresh` params `{}`. Successful `call`
returns the complete MCP CallToolResult envelope; tool errors remain errors.
All RPC belongs to a run; cancellation aborts outstanding requests and does not
retry writes. RPC transport accepts arbitrary tool argument keys via `arguments`.

## Public behavior

`execute_python(code,reset,timeout,session_id)` stays available and returns text plus a
structured execution result, with MCP isError on failure. Independent health,
run inspection and cancellation expose long execution. Idle health does
not start Python or connect all downstream servers. Reset restarts the worker
to restore cwd/environment/async resources and reports cleared state.

`python_start` reserves the single execution slot and returns its run ID immediately.
The server owns that task until completion/shutdown. `python_run` exposes bounded
live output and retains 64 final results. Current stateless MCP requests supply the
session ID returned by health; legacy clients may omit it.

`repl_history()` lists metadata; `repl_source(run_id)` retrieves code explicitly.
Source/linecache storage is bounded to 64 runs and 1 MiB. Explicit `checkpoint` /
`restore` supports bounded JSON builtin values only. Background tasks/callbacks
belong to their cell and are cancelled at completion. Unjoined user threads and
unfinished executor work clear the worker; idle default executor threads are safe.

Broker configuration is explicit per client: project `.mcp.json` is available;
foreign Claude registries require opt-in. Validate transport, environment and
authorization instead of silently bypassing client approvals. Preserve full
schemas/results/pagination and give actionable, redacted errors. Use lazy sessions,
bounded connect/call deadlines, invalidation and deterministic cleanup.

OAuth grants belong to this broker. Authorization-code login uses PKCE, a localhost
callback and a private atomic credential file. Client credentials require explicit
client ID and secret. Tokens from Claude/Codex/t3 are never silently borrowed.
An optional exclusive, atomic `--journal` records only bounded effect metadata,
and recovers unfinished dispatches as `outcome_unknown`. A remote write cannot be
rolled back or inferred solely from cancellation. Invalid registry configuration
leaves plain Python and health available; broker operations fail closed.

## Lifecycle ownership

A native guardian is spawned before each worker or stdio MCP peer. Its private
stdin receives the command specification; credentials never enter guardian argv.
The guardian owns the actual child in its process group and proxies framed I/O.
An independent native pipe watcher observes parent death even while Python holds
the GIL or output stalls. The Rust parent owns and reaps the guardian process.
Detached groups reported by managed worker APIs are registered before forwarding
their events. Arbitrary manual forks/native detachment are trusted host code, not
a containment guarantee.

Worker stdin has one owned writer actor and a four-frame bounded queue. Once a
frame is accepted, cancelling a caller cannot leave a partial JSON frame for the
next run. Write failure/timeout terminates the owned worker group. Frame limits
apply before parsing on worker, stdio peer and streamed HTTP response paths.

The sampled RSS limit measures the actual Python worker, not the guardian and
not the entire descendant tree. Schema validation has structural/size limits;
a deadline does not preempt a running Rust blocking validator. Resource limits
in trusted mode are operational safeguards, not OS security isolation.

## Acceptance

Compile, fmt, strict clippy, full legacy regression suite, new worker suite and
real stdio end-to-end fixtures. Reproduce cancellation late effects, fatal kernel
exit, native hang, output flood, persistent async resources, MCP multiblock/error/
schema/paging, downstream disconnect, process descendants, parent death and idle
resource overhead. Map every audit item to evidence or an explicit residual limit.
Release artifacts are built from the exact tested commit and installed independently
before publication; see [CI release procedure](../CI.md#release-artifacts).
See [3.0 compatibility changes](../CHANGELOG.md#migration-from-2x) before upgrading.
