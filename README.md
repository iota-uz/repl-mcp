# Stateful Python REPL MCP Server

A Model Context Protocol (MCP) server that provides a stateful Python REPL with programmatic access to other MCP tools. Execute Python code persistently and call external MCP servers (like GitHub, Playwright, etc.) directly from your code via a pre-injected `mcp` client object.

## Features

- **Stateful Execution**: Variables, imports, and function definitions persist across executions (~0.1s warm calls vs ~3s per fresh `python3 -c` spawn)
- **Shell Composition**: Pre-injected `sh()` helper — `json.loads(sh("gh pr view 1 --json title"))` replaces `cmd | python3 -c` pipelines
- **Full Filesystem Access**: `open()`, absolute paths, and `~` all work; `workspace.*` helpers accept them too
- **Codebase Utilities**: `workspace` (files), `git` (structured log/diff/blame), `ast_utils` (Python AST), `code` (100+ languages via tree-sitter)
- **MCP Tool Integration**: Call other MCP tools programmatically via `mcp.tools.server.method(...)`
- **Auto-connect**: Automatically connect to MCP servers from `.mcp.json` on startup
- **Output Capture**: Full stdout, stderr, exceptions, and return values
- **HTTP & Stdio Transports**: Default HTTP/SSE on port 8000, with stdio fallback for Claude Desktop
- **No Sandboxing**: Full Python access (intended for local use only)

## Installation

### Claude Code Plugin (Recommended)

The plugin bundles the MCP server, a usage skill, and a nudge hook in one install:

```bash
# In Claude Code:
/plugin marketplace add iota-uz/repl-mcp
/plugin install python-repl@repl-mcp
```

Restart the session and the REPL, skill, and hook are active. Portable across machines — nothing is hand-edited in `~/.claude.json`.

### Claude Code (MCP server only)

```bash
claude mcp add python-repl -- uvx --from git+https://github.com/iota-uz/repl-mcp@v1.1.0 repl-mcp
```

Pin to a tag (as above) so `uvx` caches the build instead of fetching GitHub on every session start.

### Codex CLI

```bash
codex mcp add python-repl -- uvx --from git+https://github.com/iota-uz/repl-mcp@v1.1.0 repl-mcp
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "python-repl": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/iota-uz/repl-mcp@v1.1.0", "repl-mcp"]
    }
  }
}
```

### Manual Installation

<details>
<summary>For development or running standalone</summary>

**Prerequisites:** Install [UV](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Setup:**

```bash
git clone https://github.com/iota-uz/repl-mcp
cd repl-mcp
uv sync
uv run repl-mcp --help
```

</details>

## Quick Start

### 1. Configure MCP Servers (Optional)

Create `.mcp.json` in the project root to auto-connect to MCP servers on startup:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_TOKEN": "${GH_TOKEN}"
      }
    },
    "playwright": {
      "url": "http://localhost:3001/sse"
    }
  }
}
```

### 2. Start the Server

```bash
# Using UV (recommended - handles dependencies automatically)
uv run repl-mcp --port 8000

# Or activate venv first
source .venv/bin/activate
repl-mcp --port 8000

# Stdio mode (for Claude Desktop integration)
uv run repl-mcp --transport stdio
```

### 3. Use from Claude Code

The server exposes one MCP tool:

- `execute_python(code, reset=False, timeout=120, inject=None)` - Execute Python in a persistent REPL

Inside the REPL, use `%help` for documentation, `object?` for quick help (e.g. `sh?`), and `%who` to list variables.

## Usage Examples

### Shell + Python in One Call

The `sh()` helper returns stdout as a `str` subclass with `.returncode`, `.stderr`, and `.ok`:

```python
execute_python(code="""
import json

# Replaces: gh pr view 2822 --json statusCheckRollup | python3 -c "..."
d = json.loads(sh("gh pr view 2822 --json statusCheckRollup"))
for c in d["statusCheckRollup"]:
    print(c["name"], c["conclusion"])

# Inspect failures without raising
r = sh("pytest -q", check=False)
if not r.ok:
    print(r.returncode, r.stderr[-500:])
""")
```

### Full Filesystem Access

```python
execute_python(code="""
import json

# Absolute paths and ~ work everywhere — open(), workspace, glob
raw = json.load(open("/tmp/data.json"))
logs = workspace.glob("~/logs/**/*.txt")
workspace.write("/tmp/summary.json", json.dumps({"n": len(logs)}))
""")
```

### Basic Execution with State Persistence

```python
# First execution
execute_python(code="x = 42")

# Variable persists in next execution
execute_python(code="print(x)")  # Output: 42

# Functions and imports persist too
execute_python(code="""
import json

def greet(name):
    return f"Hello, {name}!"
""")

execute_python(code="print(greet('World'))")  # Output: Hello, World!
```

### Using Auto-connected MCP Servers

If you have `.mcp.json` configured, servers are automatically available:

```python
# GitHub already connected via .mcp.json
execute_python(code="""
# Discover available tools
tools = mcp.discover_tools()
print(f"Connected servers: {list(tools.keys())}")
""")

# Call GitHub tools directly
execute_python(code="""
result = mcp.tools.github.create_issue(
    owner="myuser",
    repo="myrepo",
    title="Test issue from REPL",
    body="This was created programmatically!"
)
print(f"Created issue: {result}")
""")
```

### Runtime Connection to MCP Servers

You can also connect to servers dynamically without `.mcp.json`:

```python
# Connect to GitHub at runtime
connect_mcp_servers(servers={
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {
            "GITHUB_TOKEN": "${GH_TOKEN}"
        }
    }
})

# List what's available
list_connected_servers()

# Use the connected server
execute_python(code="""
issues = mcp.tools.github.list_issues(
    owner="myuser",
    repo="myrepo",
    state="open"
)
print(f"Open issues: {len(issues)}")
""")
```

### Bulk Operations with MCP Tools

```python
# Create multiple GitHub issues programmatically
execute_python(code="""
for i in range(5):
    result = mcp.tools.github.create_issue(
        owner="myuser",
        repo="myrepo",
        title=f"Auto-generated issue {i}",
        body=f"This is issue number {i}"
    )
    print(f"Created: {result['url']}")
""")
```

### Error Handling

```python
# Execution continues after errors
execute_python(code="1 / 0")  # Returns exception info

# Namespace is preserved
execute_python(code="print(x)")  # Still works, x=42 from earlier
```

### Reset Namespace

```python
# Reset clears all variables except 'mcp'
execute_python(code="y = 100", reset=True)

# Previous variables are gone
execute_python(code="print(x)")  # Error: x not defined

# But mcp object is preserved
execute_python(code="print(mcp.discover_tools())")  # Still works
```

## Configuration

### Command Line Options

```bash
repl-mcp --help

Options:
  --transport {stdio,sse}  Transport type (default: sse)
  --port PORT              Port for SSE transport (default: 8000)
  --host HOST              Host for SSE transport (default: 0.0.0.0)
  --config PATH            Path to .mcp.json config (default: .mcp.json)
  --no-autoconnect         Disable auto-connecting to servers from .mcp.json
```

### Environment Variables

- `GH_TOKEN` - GitHub personal access token (if using GitHub MCP)
- Other environment variables as required by your MCP servers

## Architecture

```
Claude ←→ FastMCP Server ←→ REPL Engine ←→ MCP Client Wrapper ←→ External MCP Servers
         (execute_python)    (exec code)    (tool calling)         (github, etc)
```

### Components

- **repl_mcp_server.py** - FastMCP server exposing tools
- **repl_engine.py** - Stateful execution with persistent namespace
- **mcp_client_wrapper.py** - Sync wrapper over async MCP SDK
- **models.py** - Pydantic data models

### Key Design Decisions

1. **UV for Portability** - Single command execution, automatic dependency management
2. **Hybrid MCP Config** - Support `.mcp.json` auto-connect + runtime `connect_mcp_servers()`
3. **Clean Namespace** - Only `mcp` object pre-injected (no library preloading)
4. **HTTP First** - Default SSE transport, stdio as fallback
5. **Sync MCP Wrapper** - Async SDK wrapped with sync API for REPL convenience
6. **Dynamic Tool Access** - Intuitive `mcp.tools.server.method(...)` syntax
7. **Unrestricted Execution** - Full Python access (local use only)
8. **Structured Output** - Always return structured JSON, never throw to MCP layer

## API Reference

### execute_python

Execute Python code in a persistent REPL environment with pre-injected utilities.

**Parameters:**
- `code` (str): Python code to execute
- `reset` (bool, optional): Reset namespace before execution (default: False)
- `timeout` (float, optional): Max execution seconds (default: 120)
- `inject` (dict, optional): Variables to inject into namespace

**Returns:**
```python
{
    "success": bool,
    "stdout": str,
    "stderr": str,
    "return_value": str | None,
    "exception": {
        "type": str,
        "message": str,
        "traceback": str,
        "hints": list[str],         # Helpful suggestions
        "similar_names": list[str]  # For typo detection
    } | None,
    "execution_time_ms": float,
    "namespace_vars": dict[str, str],
    "warnings": list[{
        "category": str,
        "message": str,
        "suggestion": str
    }]
}
```

**REPL Features:**
- `%help` - Full documentation
- `object?` - Quick docstring (IPython-style)
- `%who` / `%whos` - List variables
- `%history` - Execution history
- `%reset` - Clear namespace

## Security Warning

⚠️ **WARNING**: This server executes arbitrary Python code without restrictions.

- **Local use only** - Do not expose to network
- **Single user** - No authentication or isolation
- **Full system access** - Can read/write files, make network calls, run commands
- For production use, run in isolated container with resource limits

## Development

### Running Tests

```bash
# Install dev dependencies
uv sync --extra dev

# Run unit tests
uv run pytest tests/

# Run specific test file
uv run pytest tests/test_repl_engine.py -v

# Run example scripts (integration tests)
bash examples/run_all_examples.sh
```

### Project Structure

```
repl_mcp/
├── src/repl_mcp/
│   ├── __init__.py
│   ├── models.py              # Pydantic data models
│   ├── repl_engine.py         # Stateful execution engine
│   ├── mcp_client_wrapper.py  # Sync MCP client wrapper
│   └── repl_mcp_server.py     # FastMCP server
├── tests/
│   ├── test_repl_engine.py
│   ├── test_mcp_wrapper.py
│   └── test_integration.py
├── examples/
│   ├── 01_basic_execution.py
│   ├── 02_error_handling.py
│   ├── 03_mcp_config_file.py
│   ├── 04_runtime_connection.py
│   ├── 05_github_bulk_ops.py
│   └── run_all_examples.sh
├── .mcp.json                  # MCP server config (auto-connect)
├── config.yaml                # Server settings
├── pyproject.toml             # UV project config
└── README.md
```

## Troubleshooting

### Server won't start

- Check UV is installed: `uv --version`
- Try `uv sync` to reinstall dependencies
- Check port 8000 is not in use: `lsof -i :8000`

### Can't connect to MCP server

- Verify server command is correct in `.mcp.json`
- Check required environment variables are set
- Look for connection errors in server output
- Try connecting manually: `npx -y @modelcontextprotocol/server-github`

### Execution fails

- Check for syntax errors in code
- Verify imports are available (install packages in REPL: `execute_python(code="!pip install requests")`)
- Review exception traceback in result

### Tools not available

- Ensure server connected successfully: `list_connected_servers()`
- Check server is running (for HTTP/SSE servers)
- Verify authentication (e.g., GH_TOKEN for GitHub)

## License

MIT

## Contributing

Contributions welcome! Please open issues for bugs or feature requests.
