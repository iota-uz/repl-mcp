# REPL MCP development

Read `docs/rust-migration.md` and `docs/audit-coverage.md` first. Version3 is native:
`repl-mcp` Rust binary owns MCP transport, supervisor, broker and OAuth; embedded
`src/repl_mcp/native_worker.py` owns Python evaluation only. Python2.x modules are
historical regression fixtures and are not included in the binary wheel.

## Source map

- `native/src/main.rs`: CLI, bounded stdio, interpreter selection and shutdown.
- `native/src/server.rs`: tool schemas, validation and MCP error/result semantics.
- `native/src/supervisor.rs`: lazy worker/process ownership, cancellation and run records.
- `native/src/broker.rs`: MCP sessions, schemas/envelopes/paging, auth and effect metadata.
- `native/src/config.rs`: explicit registry and validation; never log secret values.
- `src/repl_mcp/native_worker.py`: standalone stdlib worker, persistent loop, captures,
  tracked subprocesses, checkpoint/restore and JSON RPC bindings.
- `tests/test_native*`: shipped runtime acceptance; `tests/fixtures/native_mcp_fixture.py`
  provides a dependency-free downstream peer. Legacy tests cover source regressions.

## Invariants

One writer per component. All stdout belongs to JSON-RPC. Python's OS fd1 is rerouted
before cells; captured user output is returned through bounded frames. Preserve full
MCP envelopes and schemas. Errors must not look successful. Never retry a dispatched
write automatically. Bind every RPC/job to its owner and clean it on cancel/reset/death.
Full host access is intentional; do not claim sandbox or whole-tree OS quotas.
Persistent state must be explicitly identified for current stateless MCP requests.

Use the official stable MCP specification and pinned SDK. Source/client approvals
are not interchangeable: foreign registries need an independent broker grant. Logs
and journals omit raw commands/config values/credentials/results. OAuth uses its own
private credential store, not another client's tokens.

## Checks

`make verify`: fmt, strict clippy, native tests, required worker lint and all Python
regressions. Build before protocol tests so embedded worker changes are included.
Set CARGO_TARGET_DIR/REPL_MCP_BINARY when using an external build directory. Tests
must exercise absence of late effects, process cleanup, full responses and real wire
semantics; matching a success-looking string is insufficient.

Keep Python worker3.10+ stdlib-only. Rust MSRV1.88. macOS/Linux supported; don't add
Windows claims without verified job ownership. Temporary artifacts follow applicable
Workbench instructions. Do not test writes against live services.

## Distribution

Maturin bin wheels contain the native executable and embedded worker. Runtime Python
packages are exact-pinned in pyproject and locked in uv.lock; Cargo.lock is committed.
Binary lookup prefers sibling Python then PATH, with explicit --python override.

Version locations: Cargo.toml, pyproject.toml, src/repl_mcp/__init__.py and plugin
manifest (including its tag). Before publishing: verify wheels and
source distribution, update audit evidence, run matrix, then obtain any required
publication authorization. No release/push/deploy is implicit in local implementation.
