# Changelog

## 3.0.1 — 2026-10-06

- Restore Claude Code tool catalogue compatibility by including top-level
  `ttlMs: 0` and `cacheScope: "private"` fields in `tools/list` responses for
  all supported protocol versions. Tool execution semantics, persistence and
  cancellation are unchanged.
- Update native wheel/plugin launch URLs and package metadata to 3.0.1. Add
  Cargo repository, homepage and documentation links.

Upgrade the runtime and plugin together. The platform wheel launcher continues
to use published GitHub assets and does not compile Rust on its default path.

## 3.0.0 — 2026-10-06

The MCP server, broker and process supervisor now run in Rust. Cells still execute
in a persistent CPython worker, started lazily on first use. Native binary wheels
are available for macOS arm64, macOS x86_64 and Linux x86_64 (glibc 2.28+); source
installation requires Rust 1.88+ and Python 3.10+.

### Changes

- Add independent `python_health`, `python_start`, `python_run` and `python_cancel`
  tools. Python errors set MCP `isError` and retain structured execution results.
- Preserve the asynchronous loop across cells; cancel cell-owned tasks, callbacks
  and MCP requests. Reset restarts the worker; forced termination reports cleared
  state. Explicit bounded JSON checkpoints support recovery of builtin values.
- Own worker, MCP peer and managed subprocess lifecycles through native guardians;
  enforce byte limits during capture and framing, with bounded concurrency/caches.
- Preserve complete MCP envelopes, schemas and paginated catalogues. Equivalent
  aliases share connections while retaining independent allowlists.
- Add independent OAuth PKCE/client-credentials grants and a private credential
  store. Optional durable effect metadata records unknown outcomes without retrying
  writes or claiming rollback.
- Pin the runtime dependency closure and commit Rust/Python lockfiles. Binary
  wheels contain the Rust executable and embedded stdlib worker; Python MCP SDK
  and FastMCP are development dependencies, not the shipped server runtime.

### Migration from 2.x

1. Replace the runtime and installed skill/plugin instructions together. Disable
   duplicate old server registrations. The plugin selects a published native
   wheel through `uvx`. Prewarm its first download when startup deadlines are short,
   or configure an absolute installed executable. Deliberate Git/source installs
   require a Rust compiler and compile on first use.
2. Use the persistent injected `mcp` object inside cells. Do not `import mcp` to
   access the broker: that imports the unrelated Python SDK. Removed historical
   `workspace`/`inject` APIs are not restored; package installation and shell work
   use the documented `sys.executable` target and `sh()` helper.
3. `mcp.call()` returns the full envelope rather than the first text block. Use
   `mcp.text(result)` for explicit extraction and inspect `structuredContent`.
   Errors raise `ToolError`, preserving `.result`. Prefer `await mcp.acall()` for
   async calls; legacy `await mcp.call()` remains supported without double dispatch.
4. Broker discovery defaults to the launch project's `.mcp.json`. Supply `--config`
   for an independent registry. Foreign Claude scopes require explicit opt-in and
   `brokerAllowed: true`; plugin registries require exported configuration.
   Credentials and interactive client approvals are not automatically inherited.
5. Keep reusable resources idle between cells and await their work. Unjoined threads
   and unfinished executor work clear the worker. Arbitrary objects/resources are
   not checkpointed automatically. Current stateless MCP callers pass the session
   ID from `python_health` to execution; legacy clients may omit it.

Execution retains full host access. Windows ownership, arbitrary state restoration,
transactional cancellation and live agent UI acceptance are outside these claims.
See [audit coverage](docs/audit-coverage.md) for evidence and limits.
