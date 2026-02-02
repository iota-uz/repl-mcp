# Python REPL MCP Server - Development Guide

## Project Overview

This is a Model Context Protocol (MCP) server that provides a Python REPL environment with the ability to connect to other MCP servers. Built with FastMCP, it allows:

- **Interactive Python execution** via `execute_python` tool
- **MCP server connections** - Connect to and use tools from other MCP servers
- **Dual transport modes** - stdio (for Claude Desktop) and SSE (for Claude Code CLI)
- **Auto-connect** - Automatically connects to MCP servers listed in `.mcp.json`

**Key Use Case**: Acts as a "meta-server" that can orchestrate multiple MCP tools in a single Python environment.

## Development Setup

### Prerequisites
- Python 3.10+
- `uv` package manager (recommended) or pip
- Git

### Installation
```bash
# Clone and install dependencies
git clone <repo-url>
cd repl_mcp
uv sync  # or: pip install -e .
```

### Running the Server

**For Claude Code (SSE transport):**
```bash
uv run repl-mcp --transport sse
# Server starts on http://localhost:8000/sse
```

**For Claude Desktop (stdio transport):**
```bash
uv run repl-mcp --transport stdio
```

**Disable autoconnect during development:**
```bash
uv run repl-mcp --transport sse --no-autoconnect
```

## Testing

### Run All Tests
```bash
uv run pytest tests/ -v
```

### Run Specific Test Suites
```bash
# Unit tests only
uv run pytest tests/test_*.py -v -k "not integration"

# Integration tests (requires server startup)
uv run pytest tests/test_claude_code_integration.py -v

# Live end-to-end test
uv run python test_live_integration.py
```

### Test Coverage
```bash
uv run pytest tests/ --cov=src/repl_mcp --cov-report=html
open htmlcov/index.html
```

## Architecture

### Core Components

1. **`src/repl_mcp/repl_mcp_server.py`** - Main server implementation
   - FastMCP server setup with tools
   - Python REPL context management
   - MCP client connections (autoconnect)
   - Lifespan management (startup/shutdown)

2. **`src/repl_mcp/context.py`** - REPL execution context
   - Manages Python execution environment
   - Handles variable persistence between executions
   - Captures stdout/stderr and return values

3. **`src/repl_mcp/mcp_client.py`** - MCP client for connecting to other servers
   - Manages connections to external MCP servers
   - Tool forwarding and proxying
   - Connection lifecycle management

### Transport Modes

**stdio** (Standard Input/Output):
- Used by Claude Desktop
- Bidirectional JSON-RPC over stdin/stdout
- Single persistent connection
- Keep stdout clean - log to stderr only

**SSE** (Server-Sent Events):
- Used by Claude Code CLI
- HTTP-based streaming protocol
- Client connects to `http://localhost:8000/sse`
- Allows multiple concurrent clients
- CRITICAL: All logging must go to stderr to avoid protocol interference

### Configuration Files

**`.mcp.json`** - MCP server configuration for Claude Code
- Defines how Claude Code launches this server
- Lists other MCP servers for autoconnect
- IMPORTANT: Currently uses `--no-autoconnect` due to startup timeout issues with autoconnect in SSE mode

**`pyproject.toml`** - Python project configuration
- Dependencies and build settings
- Entry points: `repl-mcp` command

## Common Development Tasks

### Adding a New Tool

1. Add tool function in `src/repl_mcp/repl_mcp_server.py`:
```python
@mcp.tool()
def my_new_tool(param: str) -> str:
    """Tool description for Claude."""
    # Implementation
    return result
```

2. Add tests in `tests/test_repl_mcp_server.py`
3. Run tests to verify

### Modifying REPL Execution

Edit `src/repl_mcp/context.py`:
- `execute()` method handles code execution
- Modify variable persistence logic in `get_context()`
- Update output capture in `_capture_output()`

### Debugging Connection Issues

**SSE mode not connecting:**
1. Check port availability: `lsof -i:8000`
2. Test SSE endpoint: `curl -N http://localhost:8000/sse`
3. Check Claude Code logs: `~/.claude/debug/latest`
4. Look for "Connection timeout" or "Port already in use" errors

**stdio mode issues:**
1. Check that stdout is clean (no print statements)
2. Verify JSON-RPC messages are properly formatted
3. Test with: `echo '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}' | uv run repl-mcp --transport stdio`

### Working with Autoconnect

**Current Status**: Autoconnect is disabled in `.mcp.json` due to startup timeout issues when connecting to GitHub MCP server in SSE mode.

**To re-enable after fixing:**
1. Remove `--no-autoconnect` from `.mcp.json`
2. Ensure GitHub token is set: `export GH_TOKEN=your_token`
3. Test startup time stays under 30 seconds
4. Verify via: `uv run pytest tests/test_claude_code_integration.py::test_full_mcp_json_flow`

## Known Issues & Gotchas

### SSE Mode Startup Timeout
**Issue**: Server hangs during autoconnect in SSE mode, causing 30-second timeout
**Workaround**: Using `--no-autoconnect` flag in `.mcp.json`
**Root Cause**: Under investigation - GitHub MCP connection may block SSE initialization
**Tracking**: See plan file at `~/.claude/plans/lazy-enchanting-bengio.md`

### Port Conflicts
**Issue**: "Port 8000 is already in use"
**Solution**: Kill existing server: `kill $(lsof -ti:8000)`
**Prevention**: Port conflict detection is implemented and will exit cleanly with error message

### Logging in SSE Mode
**Rule**: ALWAYS log to stderr in SSE mode
**Why**: SSE uses stdout for protocol messages - any print() statements will corrupt the stream
**How**: Use `logging.info()` which is configured to use stderr in SSE mode

## File Structure

```
repl_mcp/
├── .mcp.json                    # Claude Code MCP configuration
├── CLAUDE.md                    # This file - project guide
├── pyproject.toml               # Python project config
├── src/repl_mcp/
│   ├── __init__.py
│   ├── repl_mcp_server.py      # Main server (FastMCP)
│   ├── context.py              # Python REPL execution context
│   └── mcp_client.py           # MCP client for connections
├── tests/
│   ├── test_context.py         # REPL context tests
│   ├── test_repl_mcp_server.py # Server unit tests
│   ├── test_mcp_client.py      # MCP client tests
│   └── test_claude_code_integration.py  # Integration tests
└── test_live_integration.py    # Live E2E test

```

## Development Workflow

### Making Changes

1. **Read existing code first** - Understand current implementation before modifying
2. **Write tests** - Add test cases before implementing features
3. **Run tests frequently** - `uv run pytest tests/ -v`
4. **Check logs** - Monitor stderr output for errors
5. **Test both transports** - Verify stdio and SSE modes both work

### Before Committing

```bash
# Run full test suite
uv run pytest tests/ -v

# Verify server starts in both modes
uv run repl-mcp --transport stdio --no-autoconnect &
# Test, then kill

uv run repl-mcp --transport sse --no-autoconnect &
# Test, then kill

# Check code quality (if configured)
uv run ruff check src/
uv run mypy src/
```

### Deployment Checklist

- [ ] All tests passing (52+ tests)
- [ ] Both stdio and SSE transports tested
- [ ] Port conflict detection working
- [ ] Logging goes to stderr in SSE mode
- [ ] `.mcp.json` configuration tested with Claude Code
- [ ] Integration tests pass
- [ ] Documentation updated

## Troubleshooting

### "Server not found" when running `uv run repl-mcp`
- Check virtual environment: `uv sync`
- Verify installation: `uv run python -c "import repl_mcp; print(repl_mcp.__file__)"`

### Tests failing with "Connection refused"
- Port may be in use: `kill $(lsof -ti:8000)`
- Check test isolation - each test should use unique port or cleanup properly

### "Module not found" errors in tests
- Reinstall in editable mode: `uv pip install -e .`
- Check Python path: `uv run python -c "import sys; print(sys.path)"`

### SSE endpoint returns 404
- Server may not have started: Check logs for startup errors
- Wrong URL: Should be `http://localhost:8000/sse` (note `/sse` path)
- Port conflict: Server failed to bind to port 8000

## Resources

- **FastMCP Documentation**: https://gofastmcp.com
- **MCP Specification**: https://modelcontextprotocol.io
- **Claude Code MCP Guide**: Run `/help` in Claude Code CLI
- **Project Issues**: See git commit history and plan files in `~/.claude/plans/`

## Contributing Guidelines

1. **Test Coverage**: Maintain >90% coverage for new code
2. **Type Hints**: Use type annotations for all public functions
3. **Documentation**: Add docstrings for tools and public methods
4. **Error Handling**: Provide clear error messages for common failures
5. **Logging**: Use appropriate log levels (DEBUG/INFO/ERROR)
6. **Backwards Compatibility**: Don't break existing tool interfaces without migration plan

## Quick Reference

**Start server for Claude Code:**
```bash
uv run repl-mcp --transport sse --no-autoconnect
```

**Run tests:**
```bash
uv run pytest tests/ -v
```

**Debug connection:**
```bash
curl -N http://localhost:8000/sse
```

**Check logs:**
```bash
tail -f ~/.claude/debug/latest
```

**Kill server:**
```bash
kill $(lsof -ti:8000)
```
