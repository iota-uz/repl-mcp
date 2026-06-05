---
name: python-repl
description: Use INSTEAD of running `python3 -c`, a `python3 - <<EOF` heredoc, or a `cmd | python3` pipeline via Bash — and for batch file operations, data aggregation, multi-step analysis where steps depend on each other, or orchestrating the project's MCP tools in one script. Guides effective use of the Python REPL MCP server (`execute_python`).
---

# Python REPL MCP — When and How

## Loading the tool

`execute_python` is an MCP tool and may be deferred (name visible, schema not loaded). Load it once, then it stays available for the whole session:

```
ToolSearch query: "execute_python"
```

(The exact tool name depends on install method: `mcp__plugin_python-repl_python-repl__execute_python` via the plugin, `mcp__python-repl__execute_python` via `claude mcp add` — keyword search matches both.)

## Why over Bash python

- **Warm and persistent**: ~0.1s per call vs ~3s per fresh `python3` spawn. Variables, imports, and functions survive across calls — build analysis incrementally instead of re-parsing in every command.
- **Honest timeouts**: runaway code is interrupted at `timeout` seconds (KeyboardInterrupt) and your variables survive the interrupt. A crash (segfault/OOM) only restarts the REPL kernel — you get a clear "variables cleared" notice, never a hung server.
- **Top-level `await` works**: `await client.get(url)` directly, no `asyncio.run()` wrapper.
- **Shell composition built in**: `sh()` replaces `cmd | python3 -c` pipelines (below).

## Shell composition with sh()

`sh(cmd)` runs a shell command and returns stdout as a `str` subclass — usable directly:

```python
data = json.loads(sh("gh pr view 2822 --json statusCheckRollup"))
files = sh("git ls-files '*.py'").splitlines()

r = sh("pytest -q", check=False)   # check=False: don't raise on nonzero exit
if not r.ok:
    print(r.returncode, r.stderr[-500:])
```

Pipes, globs, and `&&` all work (`shell=True`). On nonzero exit it raises `ShellError` (carrying `.returncode`/`.stdout`/`.stderr`) unless `check=False`.

## File access

Full filesystem access. `open()`, `Path().read_text()`, absolute paths, and `~` all work. Relative paths resolve against the project directory.

## MCP bridge

`mcp` reaches the MCP servers configured in the *project's* `.mcp.json` (it connects lazily on first use — expect the first call to take a few seconds):

```python
mcp.servers                                  # connected server names
mcp.failed                                   # connection failures with reasons
print(mcp.help())                            # servers + tools overview
mcp.call('github', 'create_issue', owner='me', repo='proj', title='Bug')
```

Host-level connectors (claude.ai Notion/GitHub, user-scope `claude mcp add` servers) are **not** reachable here — call those tools directly. Arguments must be JSON-serializable.

## Missing packages

The REPL env is ephemeral per release build. Install into the *running* env:

```python
sh('uv pip install openpyxl')
```

## Output truncation

stdout truncates at **50KB**, return values at **20KB**. Aggregate and summarize in-REPL rather than dumping raw results. Slice large return values (`results[:10]`).
