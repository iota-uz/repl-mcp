# Python REPL MCP Server - Development Guide

## Project Overview

MCP server (FastMCP) exposing one tool — `execute_python`, a persistent Python REPL running in a **subprocess kernel** (Jupyter-style). Built for AI-agent use:

- **Persistent state** — variables/imports/functions survive across calls (the killer feature)
- **Honest timeouts** — runaway code gets SIGINT → KeyboardInterrupt at `timeout`; namespace state survives the interrupt. Cells that swallow the interrupt are hard-killed and the kernel respawns with a clear "variables cleared" notice
- **Crash isolation** — a segfault/OOM in REPL code kills only the kernel child; the MCP server survives and respawns it
- **Top-level `await`** — cells compile with `PyCF_ALLOW_TOP_LEVEL_AWAIT` (IPython autoawait-style); async cells are cancellable at `timeout`
- **`sh()` helper** — shell composition replacing `cmd | python3 -c` pipelines
- **Full filesystem access** — `open()`, absolute paths, `~` all work; cwd = the user's project
- **MCP bridge** — `mcp.call/servers/failed/list_tools/help` reach servers from **every Claude Code config scope** (local, project `.mcp.json`, user `~/.claude.json`, enabled plugins — see `mcp_config.py`); connection is **per server, on demand** (naming it in `mcp.call` starts it), so sessions that never touch the bridge spawn zero child MCP servers
- **Claude Code plugin** — `.claude-plugin/` + `skills/` + `hooks/` bundle the server, a usage skill, and a Bash-nudge hook

Transport: **stdio only** (SSE was removed in v2.0.0).

## Architecture

```
PARENT (MCP server process, pure async)      CHILD (kernel process, owns namespace)
  repl_mcp_server.py  FastMCP + lifespan       kernel/child_main.py
  kernel/supervisor.py KernelSupervisor          REPLEngine (repl_engine.py)
    execute ── EXECUTE ───────────────────────►  exec / await cell
            ◄── RESULT (ExecutionResult json) ─  stdout/stderr captured per call
    timeout: SIGINT ──────────────────────────►  KeyboardInterrupt (state survives)
    grace-fail/crash: kill + respawn + notice
    rpc task ◄── MCP_CALL ─────────────────────  mcp.* proxy (ChildMcpProxy)
             ── MCP_REPLY ────────────────────►
  mcp_client_wrapper.py MCPClientWrapper — real MCP sessions live in the PARENT,
    loop-affine to the server loop; RPC handlers call it from executor threads
    via run_coroutine_threadsafe (no nest_asyncio anywhere)
```

Key invariants:
- The server event loop is **never blocked** by REPL code (it awaits IPC)
- Two IPC channels (control + rpc-back) so in-cell `mcp.*` calls are serviced while the parent awaits the cell result — single-channel would deadlock
- Child stdout is rerouted to stderr at spawn (protects the stdio JSON-RPC channel)
- `ExecutionResult` crosses the boundary as `model_dump(mode="json")` — keep it JSON-clean
- `mcp.call` kwargs must be JSON-serializable (validated at the proxy with a clear error)

### Source map

```
src/repl_mcp/
├── repl_mcp_server.py    # FastMCP server, lifespan, execute_python tool
├── repl_engine.py        # Engine: cell compile/exec, capture, hints, truncation
├── mcp_client_wrapper.py # Parent-side MCP sessions + registry, on-demand connect
├── mcp_config.py         # Multi-scope discovery (local/project/user/plugin), self-exclusion
├── models.py             # ExecutionResult/ExceptionInfo/ServerConfig/...
├── kernel/
│   ├── protocol.py       # Frame + message kinds, JSON-only payloads
│   ├── supervisor.py     # Parent: spawn/respawn, SIGINT timeouts, RPC service
│   └── child_main.py     # Child: control loop + ChildMcpProxy
└── utilities/shell.py    # sh() helper (ShellResult/ShellError)
```

## Development Setup

```bash
git clone <repo-url> && cd repl_mcp
uv sync --extra dev
```

Run the server (stdio is the only transport):

```bash
uv run repl-mcp                        # all scopes: local/project/user/plugin (on demand)
uv run repl-mcp --mcp-scope project    # only ./.mcp.json (pre-2.1 behaviour)
uv run repl-mcp --mcp-scope none       # disable the mcp bridge (== --no-autoconnect)
uv run repl-mcp --config .mcp.dev.json # override the *project*-scope file only
```

Quick protocol smoke test:

```bash
echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}},"id":1}' | uv run repl-mcp
```

## Testing

```bash
uv run pytest tests/ -v                 # full suite
uv run pytest tests/test_kernel.py tests/test_kernel_bridge.py -v   # kernel/IPC
```

Test map:
- `test_repl_engine.py` — engine in-process (incl. top-level await, traceback stripping)
- `test_kernel_protocol.py` — IPC frame loopback (no subprocess)
- `test_kernel.py` — kernel acceptance: state round-trip, interrupt-survives, grace-kill restart, segfault isolation, restart notice
- `test_mcp_config.py` — multi-scope discovery: precedence, deny lists, plugins, self-exclusion, `${VAR}` expansion (hermetic — fake `home`/`cwd`, never reads the real `~/.claude.json`)
- `test_kernel_bridge.py` — mcp bridge over IPC incl. on-demand connect, negative cache and interrupt-mid-call
- `test_cross_thread_bridge.py` — parent-side wrapper loop-affinity contract
- `test_claude_code_integration.py` — spawns the real server over stdio
- fixtures: `tests/fixtures/child_mcp_server.py` (echo + slow tools)

## Configuration Files

**`.mcp.dev.json`** — dev-only MCP config for working *in this repo* (renamed from `.mcp.json` so it isn't auto-discovered as the plugin's MCP bundle); pass it via `--config` to override the project scope. At runtime the server merges the user project's `.mcp.json` (cwd-relative) with `~/.claude.json` (user + local scope) and enabled plugins, then connects each server on first use.

**`.claude-plugin/plugin.json`** — Claude Code plugin manifest
- Bundles the MCP server (`uvx --from git+...@vX.Y.Z` — pinned tag so uvx caches the build and cwd stays at the user's project; do NOT use `uv run --directory`, it chdirs), the `skills/python-repl` skill, and the `hooks/` nudge hook
- `.claude-plugin/marketplace.json` makes the repo installable via `/plugin marketplace add iota-uz/repl-mcp`

## Release Process

Version lives in THREE places — bump all, then tag:

1. `pyproject.toml` `version`
2. `.claude-plugin/plugin.json` `version` AND the `@vX.Y.Z` ref in `mcpServers.python-repl.args`
3. `src/repl_mcp/__init__.py` `__version__`

```bash
uv run pytest tests/ -q          # green gate
git commit -am "release: vX.Y.Z"
git tag vX.Y.Z && git push && git push --tags
# then: /plugin update python-repl in Claude Code
```

## Gotchas

- **stdout discipline**: parent logs to stderr only (stdio carries JSON-RPC); the kernel child's fd 1 is rerouted to stderr at spawn — keep it that way
- **Engine timeout semantics**: `REPLEngine.execute(timeout=...)` self-cancels only async cells; sync-cell enforcement belongs to the supervisor (SIGINT). Don't "fix" one without the other
- **Supervisor recv**: the pending control-read future is shielded across the timeout → interrupt → grace sequence; a cancelled wait must not abandon the read or the RESULT frame is lost (see `execute()`)
- **multiprocessing start method is spawn** (darwin-safe); child entrypoint must stay importable as `repl_mcp.kernel.child_main.child_serve`
- **Bridge recursion**: discovery must never hand back an entry that starts this server. `is_self_server()` is the heuristic; `REPL_MCP_NO_BRIDGE=1` (injected into every spawned server's env) is the backstop that caps nesting at depth 1. Don't drop either
- **Loop affinity**: `ensure_connected_async` must be awaited on the owner loop — the supervisor calls it directly in `_handle_rpc`, never through `run_in_executor`. `_pin_loop()` raises rather than silently rebinding, which is what made the old `mcp.help()` hang possible
- **Connect budget**: on-demand connects are clamped to `MAX_ON_DEMAND_CONNECT_S` (30s) because the kernel child only waits `call timeout + 45s` for connect+call. Raising one without the other makes the child report a bogus "no reply from parent"
- **Secrets**: `~/.claude.json` carries OAuth state and tokens. Read only `.mcpServers` / `.projects[cwd]`, and route anything user-facing through `redact()` — a config value must never reach logs or `mcp.help()`
- **`reset=True`** clears the namespace in-place (keeps `sh`/`mcp`); a kernel *restart* is the heavyweight path and always carries the variables-cleared notice
- Killing a wedged server: `pkill -f repl-mcp` (kernel children are daemonic and die with the parent)

## Contributing Guidelines

1. Tests first; keep the full suite green at every commit
2. Type hints on public functions; docstrings on tools and public methods
3. Error messages must be actionable for an AI agent (hints > stack noise)
4. Anything crossing the kernel IPC boundary stays JSON-serializable
5. Resist tool/API surface growth — transcript evidence drives additions (v2.0.0 deleted every unused feature; don't re-grow them without usage data)
