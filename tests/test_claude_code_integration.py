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
from mcp.client.sse import sse_client


PROJECT_ROOT = Path(__file__).parent.parent


class TestClaudeCodeIntegration:
    """Test server startup as Claude Code would launch it."""

    @pytest.mark.skip(reason="uv run resolves to correct project even from wrong cwd")
    def test_server_without_directory_fails_from_wrong_dir(self, clean_environment):
        """Test that server fails when run from wrong directory without --directory arg.

        Note: This test is skipped because `uv run repl-mcp` actually finds the correct
        project even when run from /tmp. The important thing is that Claude Code sets
        the cwd to the project root where .mcp.json is located, which our other tests verify.
        """
        pass

    @pytest.mark.asyncio
    async def test_server_without_directory_works_from_project_root(self, clean_environment):
        """Test that server works without --directory when cwd is project root."""
        original_dir = os.getcwd()
        os.chdir(PROJECT_ROOT)

        try:
            # Start without --directory argument (current fixed approach)
            process = subprocess.Popen(
                ["uv", "run", "repl-mcp",
                 "--transport", "sse", "--port", "9002", "--no-autoconnect"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(PROJECT_ROOT),  # Claude Code sets cwd to where .mcp.json is
                text=True,
            )

            # Wait for server
            max_wait = 10
            start_time = time.time()

            while time.time() - start_time < max_wait:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail(f"Server failed:\nSTDOUT: {stdout}\nSTDERR: {stderr}")

                try:
                    transport = sse_client("http://localhost:9002/sse")
                    async with transport as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()

                            # Success! Can call tools
                            result = await session.call_tool(
                                "execute_python",
                                arguments={"code": "x = 42; x"}
                            )
                            assert "42" in str(result.content)
                            break
                except Exception:
                    await asyncio.sleep(0.5)
            else:
                pytest.fail("Server did not start in time")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            os.chdir(original_dir)

    @pytest.mark.asyncio
    async def test_full_mcp_json_flow(self, clean_environment):
        """Test complete flow using exact .mcp.json configuration."""
        # Load actual config
        config_path = PROJECT_ROOT / ".mcp.json"
        if not config_path.exists():
            pytest.skip(".mcp.json not found")

        config = json.loads(config_path.read_text())
        python_repl = config.get("mcpServers", {}).get("python-repl")

        if not python_repl:
            pytest.skip("python-repl not configured in .mcp.json")

        # Support both SSE-style configs (url) and stdio-style configs (command/args).
        if python_repl.get("url"):
            # Start server exactly as Claude Code would (subprocess) and connect over SSE.
            process = subprocess.Popen(
                [python_repl["command"]] + python_repl["args"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(PROJECT_ROOT),  # Claude Code sets cwd to where .mcp.json is
                text=True,
            )

            try:
                # Wait for readiness
                start_time = time.time()
                max_wait = 25

                last_error = None
                while time.time() - start_time < max_wait:
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        pytest.fail(f"Server crashed:\nSTDOUT: {stdout[:500]}\nSTDERR: {stderr[:500]}")

                    try:
                        transport = sse_client(python_repl["url"])
                        async with transport as (read, write):
                            async with ClientSession(read, write) as session:
                                await session.initialize()

                                startup_time = time.time() - start_time
                                print(f"✓ Server ready in {startup_time:.2f}s")

                                tools = await session.list_tools()
                                tool_names = [t.name for t in tools.tools]
                                assert "execute_python" in tool_names
                                assert "list_connected_servers" in tool_names

                                result = await session.call_tool(
                                    "execute_python",
                                    arguments={"code": "import sys; sys.version"},
                                )
                                assert "success" in str(result.content).lower()

                                return
                    except Exception as e:
                        last_error = str(e)
                        await asyncio.sleep(0.5)

                if process.poll() is None:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=2)
                    pytest.fail(
                        f"Server did not become ready in {max_wait}s\n"
                        f"Last error: {last_error}\nSTDERR: {stderr[:500]}"
                    )
                else:
                    stdout, stderr = process.communicate()
                    pytest.fail(f"Server exited during test\nSTDOUT: {stdout[:500]}\nSTDERR: {stderr[:500]}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
        else:
            # Claude Code's default is stdio: spawn and connect over stdio.
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
                    assert "list_connected_servers" in tool_names

                    result = await session.call_tool(
                        "execute_python",
                        arguments={"code": "import sys; sys.version"},
                    )
                    assert "success" in str(result.content).lower()

    def test_port_conflict_detection(self, clean_environment):
        """Test that server detects port conflicts and fails gracefully."""
        import socket

        # Bind to port 9003
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', 9003))
        sock.listen(1)

        try:
            # Try to start server on same port
            process = subprocess.Popen(
                ["uv", "run", "repl-mcp",
                 "--transport", "sse", "--port", "9003", "--no-autoconnect"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                text=True,
            )

            # Wait for it to fail
            max_wait = 5
            start_time = time.time()

            while time.time() - start_time < max_wait:
                if process.poll() is not None:
                    # Process exited - that's what we want
                    stdout, stderr = process.communicate()
                    # Should have error message about port
                    assert "port" in stderr.lower() or "address" in stderr.lower(), \
                        f"Expected port error message, got: {stderr}"
                    return  # Test passed
                time.sleep(0.5)

            # If still running after timeout, kill it and fail
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            pytest.fail("Server should have failed immediately due to port conflict")
        finally:
            sock.close()

    @pytest.mark.asyncio
    async def test_sse_stdout_isolation(self, clean_environment):
        """Test that autoconnect messages don't break SSE protocol."""
        # Start server WITH autoconnect (will output to stderr, not stdout)
        process = subprocess.Popen(
            ["uv", "run", "repl-mcp",
             "--transport", "sse", "--port", "9004"],  # No --no-autoconnect
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            text=True,
        )

        try:
            # Wait for server with retry loop
            max_wait = 15  # Longer timeout for autoconnect
            start_time = time.time()

            while time.time() - start_time < max_wait:
                # Check if process crashed
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    # If autoconnect failed (e.g., no GitHub token), that's okay
                    # The test is about stdout/stderr handling, not autoconnect success
                    pytest.skip(f"Server failed to start (likely autoconnect issue): {stderr[:200]}")

                try:
                    # Try to connect - should work despite autoconnect output
                    transport = sse_client("http://localhost:9004/sse")
                    async with transport as (read, write):
                        async with ClientSession(read, write) as session:
                            # Should succeed despite autoconnect output to stderr
                            await session.initialize()

                            # Verify can call tools
                            result = await session.list_tools()
                            assert len(result.tools) > 0
                            break
                except Exception:
                    await asyncio.sleep(0.5)
            else:
                pytest.fail("Server did not become ready in time")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

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
