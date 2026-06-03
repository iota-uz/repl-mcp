---
name: python-repl
description: Use INSTEAD of running `python3 -c`, a `python3 - <<EOF` heredoc, or a `cmd | python3` pipeline via Bash — and for batch codebase operations, cross-referencing git with code, aggregating data across files, multi-step analysis where steps depend on each other, or orchestrating multiple MCP tools in one script. Guides effective use of the Python REPL MCP server (`execute_python`).
---

# Python REPL MCP — When and How

## Loading the tool

`execute_python` is an MCP tool and may be deferred (name visible, schema not loaded). Load it once, then it stays available for the whole session:

```
ToolSearch query: "select:mcp__python-repl__execute_python"
```

## Why over Bash python

- **Warm and persistent**: ~0.1s per call vs ~3s per fresh `python3` spawn. Variables, imports, and functions survive across calls — build analysis incrementally instead of re-parsing in every command.
- **Shell composition built in**: `sh()` replaces `cmd | python3 -c` pipelines (below).
- **Codebase utilities pre-loaded**: structured `git`, AST search, 100+ language parsing.

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

Full filesystem access. `open()`, `Path().read_text()`, absolute paths, and `~` all work — in plain Python and in `workspace.*` helpers alike. Relative paths resolve against the workspace root (the project directory). `workspace.glob()` accepts absolute patterns like `"/tmp/*.json"` and `"~/logs/**/*.txt"`.

## Data passing

Use the `inject` parameter to pass structured data from your context into the REPL. Do not serialize data into the code string — `inject` avoids quoting/escaping bugs with nested strings, dicts, and lists.

## Output truncation

stdout truncates at **50KB**, return values at **20KB**. Structure your code to aggregate and summarize in-REPL rather than dumping raw results. Slice large return values (`results[:10]`).

## Git defaults that surprise

- `git.log()` returns **empty `files_changed`** by default. Pass `include_files=True` explicitly when you need changed file lists (it's expensive, so opt-in).
- `git.diff()` returns **no patch text** by default. Pass `include_patch=True` when you need actual diff content.
- `git.*` stays anchored to the workspace repo regardless of `sh()` cwd.

## Pattern matching

`name_pattern` and `module_pattern` on `ast_utils` and `code` utilities are **regex**, not globs. Write `"handle.*"` not `"handle*"`.

## Directory recursion is built-in

`ast_utils.find_*()` and `code.find_*()` accept a **directory path** and recurse by default. Do not loop over `workspace.glob()` results and call them per-file — pass the directory and let the utility walk.
