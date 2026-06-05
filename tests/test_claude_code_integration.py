"""Integration tests simulating Claude Code connection flow."""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).parent.parent


class TestClaudeCodeIntegration:
    """Test server startup as Claude Code would launch it."""

    @pytest.mark.asyncio
    async def test_full_mcp_json_flow(self, clean_environment):
        """Test complete flow using exact dev MCP configuration."""
        # Load actual config (.mcp.dev.json — renamed from .mcp.json so it
        # isn't auto-discovered as the plugin's MCP bundle)
        config_path = PROJECT_ROOT / ".mcp.dev.json"
        if not config_path.exists():
            pytest.skip(".mcp.dev.json not found")

        config = json.loads(config_path.read_text())
        python_repl = config.get("mcpServers", {}).get("python-repl")

        if not python_repl:
            pytest.skip("python-repl not configured in .mcp.json")

        # Claude Code launches stdio servers: spawn and connect over stdio.
        server_params = StdioServerParameters(
            command=python_repl["command"],
            args=python_repl.get("args", []),
            env=None,
            cwd=PROJECT_ROOT,
        )
        transport = stdio_client(server_params)
        async with transport as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                tool_names = [t.name for t in tools.tools]
                assert "execute_python" in tool_names

                result = await session.call_tool(
                    "execute_python",
                    arguments={"code": "import sys; sys.version"},
                )
                # Plain text output includes version string with "→" prefix
                output = str(result.content)
                assert "3." in output, f"Expected Python version, got: {output}"

    @pytest.mark.asyncio
    async def test_stdio_autoconnect_does_not_pollute_stdout(self, tmp_path, clean_environment):
        """Test that autoconnect doesn't corrupt stdio JSON-RPC stream."""
        # Create a config with an intentionally failing server to exercise autoconnect paths.
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "bad-server": {
                            "type": "stdio",
                            "command": "false",
                            "args": [],
                            "timeout_s": 1.0,
                        }
                    }
                }
            )
        )

        server_params = StdioServerParameters(
            command="uv",
            args=["run", "repl-mcp", "--transport", "stdio"],
            env=None,
            cwd=tmp_path,
        )
        transport = stdio_client(server_params)
        async with transport as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # If stdout is polluted, initialize() or list_tools() typically fails.
                tools = await session.list_tools()
                tool_names = [t.name for t in tools.tools]
                assert "execute_python" in tool_names

                result = await session.call_tool(
                    "execute_python",
                    arguments={"code": "2+2"},
                )
                assert "4" in str(result.content)
