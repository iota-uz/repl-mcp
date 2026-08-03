# Stateful Python REPL MCP Server

A Model Context Protocol (MCP) server giving AI agents a **persistent Python REPL** with honest execution semantics. Code runs in a subprocess kernel (Jupyter-style): variables survive across calls, runaway code is interruptible without losing state, and crashes never take the server down. Your other MCP servers — project, global and plugin alike — are callable in-code via a pre-injected `mcp` bridge.

## Features

- **Persistent State**: variables, imports, and functions survive across calls (~0.1s warm calls vs ~3s per fresh `python3` spawn)
- **Real Timeouts**: runaway code (sync *or* async) is interrupted at `timeout` seconds — KeyboardInterrupt, **namespace state preserved**. Cells that swallow the interrupt are killed and the kernel respawns with an explicit "variables cleared" notice
- **Crash Isolation**: a segfault/OOM in REPL code kills only the kernel child; the server respawns it instantly
- **Top-level `await`**: `await client.get(url)` directly — no `asyncio.run()` wrapper
- **Shell Composition**: pre-injected `sh()` helper — `json.loads(sh("gh pr view 1 --json title"))` replaces `cmd | python3 -c` pipelines
- **Full Filesystem Access**: `open()`, absolute paths, and `~` all work; cwd is your project
- **MCP Bridge**: `mcp.call("server", "tool", **args)` reaches every MCP server Claude Code knows — project (`./.mcp.json`), **user/global** (`~/.claude.json`) and plugin-provided — each connected **on demand**, the first time you name it. Failures stay visible in `mcp.failed` / `mcp.help()`
- **Claude Code Plugin**: one install bundles the server, a usage skill, and a Bash-nudge hook

## Installation

### Claude Code (plugin — recommended)

```bash
# In Claude Code:
/plugin marketplace add iota-uz/repl-mcp
/plugin install python-repl@repl-mcp
```

Restart the session and all three components are active. Portable across machines — nothing is hand-edited in `~/.claude.json`.

> **Migrating from a `claude mcp add` install?** Remove the old entry first: `claude mcp remove python-repl -s user`. Keeping both registers two REPL server processes with duplicate tools and can skew versions between them.

**What the plugin bundles:**

| Component | What it does |
|---|---|
| **MCP server** | `execute_python` tool, launched via `uvx` pinned to the release tag (cached after first run; the REPL's working directory is your project, not the plugin cache) |
| **Skill** (`python-repl`) | Teaches Claude when to reach for the REPL (instead of `python3 -c` / heredocs via Bash) and its gotchas — truncation limits, the on-demand mcp bridge, package installs |
| **Nudge hook** (PostToolUse) | When Claude runs inline Python through Bash (`python3 -c`, `python3 - <<EOF`, `cmd \| python3`), injects a non-blocking reminder to use `execute_python`. Silent on `python3 script.py`, `python3 -m ...`, `pytest` |

To update later: `/plugin marketplace update repl-mcp` then `/plugin update python-repl@repl-mcp`.

### Claude Code (MCP server only)

```bash
claude mcp add python-repl -- uvx --from git+https://github.com/iota-uz/repl-mcp@v2.1.0 repl-mcp
```

Pin to a tag (as above) so `uvx` caches the build instead of fetching GitHub on every session start.

### Codex CLI

```bash
codex mcp add python-repl -- uvx --from git+https://github.com/iota-uz/repl-mcp@v2.1.0 repl-mcp
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "python-repl": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/iota-uz/repl-mcp@v2.1.0", "repl-mcp"]
    }
  }
}
```

### Manual (development)

```bash
git clone https://github.com/iota-uz/repl-mcp && cd repl-mcp
uv sync --extra dev
uv run repl-mcp                  # stdio transport (the only transport)
```

## Usage

One tool: `execute_python(code, reset=False, timeout=120)`.

```python
# State persists across calls
execute_python(code="import httpx; data = (await httpx.AsyncClient().get(url)).json()")
execute_python(code="len(data['items'])")          # → 42

# Shell composition
execute_python(code="prs = json.loads(sh('gh pr list --json number,title'))")

# MCP bridge — see what's reachable (free, connects nothing)
execute_python(code="print(mcp.help())")
# Any scope: project, user/global, plugin. The named server starts on first call.
execute_python(code="mcp.call('github', 'create_issue', owner='me', repo='proj', title='Bug')")
execute_python(code="for f in files: mcp.call('telegram-mcp', 'download_media', **f)")

# Runaway code? Interrupted at timeout, state survives:
execute_python(code="while True: pass", timeout=5)
# → KeyboardInterrupt: execution interrupted. Namespace state ... preserved.

# Missing package? Install into the running env:
execute_python(code="sh('uv pip install openpyxl')")
```

Notes:
- The `mcp` bridge sees claude.ai host connectors (Notion/Gmail/Drive/chrome) **not at all** — those are server-managed with nothing on disk, so call their tools directly. Everything configured locally is reachable; see Scopes below.
- `mcp.call` arguments must be JSON-serializable (they cross the kernel process boundary).
- Output truncates at 50KB (stdout) / 20KB (return values) — aggregate in-REPL.
- `reset=True` clears variables but keeps `sh`/`mcp`.

## MCP bridge scopes

Discovery mirrors Claude Code's own config layout. On a name collision the highest-precedence
scope wins; the loser stays reachable as `project:name` / `user:name` / `plugin:id:name`.

| Precedence | Scope | Source |
|---|---|---|
| 1 | local | `~/.claude.json` → `projects["<cwd>"].mcpServers` |
| 2 | project | `<cwd>/.mcp.json` → `mcpServers` (or `--config`) |
| 3 | user (global) | `~/.claude.json` → `mcpServers` |
| 4 | plugin | each enabled plugin's `.claude-plugin/plugin.json` → `mcpServers` |

Servers listed in `disabledMcpjsonServers` are skipped. This REPL server itself is always excluded,
so `mcp.call` can never fork a nested bridge.

Discovery runs at startup and spawns nothing — a server process starts only when you name it in
`mcp.call()` (~1-3s the first time, warm after). `print(mcp.help())` shows every available server
with its scope and status without connecting anything.

**Security**: in-REPL code can now start any of your configured MCP servers with your credentials.
Narrow it with `--mcp-scope project,local` (or `--mcp-scope none` to disable the bridge entirely).

## Architecture (v2: subprocess kernel)

```
MCP client ── stdio ──► PARENT (FastMCP, pure async)        CHILD (owns namespace)
                          execute_python ── EXECUTE ──────►  exec / await cell
                                       ◄──── RESULT ──────   captured output
                          timeout: SIGINT ────────────────►  KeyboardInterrupt
                          crash: respawn + clear notice      (state survives)
                     MCP sessions (on demand)  ◄─ MCP_CALL ─ in-code mcp.* proxy
```

The server's event loop never blocks on REPL code; in-cell `mcp.*` calls are serviced on an independent channel while the cell runs. See `CLAUDE.md` for the full development guide.

## v2.1.0 changes

- **Global MCP servers are reachable**: the bridge merges user-scope (`~/.claude.json`), local, project
  and plugin configs instead of only `./.mcp.json`
- **On-demand connect**: naming a server starts that one server; a session that never touches `mcp.*`
  still spawns zero child processes. Failed connects are remembered briefly so a loop over a dead
  server doesn't pay the timeout each iteration
- **`mcp.servers` now lists what is *available*** (any scope), not just what happens to be connected
- `${VAR}` expansion applies to `command`/`args`/`url` too; unset vars fail the connect with a clear
  reason instead of exec'ing an empty command
- New `--mcp-scope` flag (`all` by default)

## v2.0.0 breaking changes

- **Removed** (zero observed usage across real agent transcripts): `workspace`/`git`/`ast_utils`/`code` pre-injected utilities (use `open()`/`pathlib`/`sh('git …')`), `%magic` commands and `object?` queries, the `inject` parameter, `mcp.tools.<server>.<tool>` dot-style access and `discover_tools()` (use `mcp.call`/`mcp.list_tools`), SSE transport (stdio only)
- **Changed**: execution moved to a subprocess kernel — `timeout` is now actually enforced; kernel restarts are reported explicitly
- **Added**: top-level `await`, `mcp.failed`, lazy MCP connect
- Install footprint dropped ~350MB (tree-sitter removed)

## Development

```bash
uv run pytest tests/ -v          # full suite
```

See `CLAUDE.md` for architecture details, test map, gotchas, and the release process.

## License

MIT
