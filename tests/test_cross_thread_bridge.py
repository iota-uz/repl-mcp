"""
Regression tests for the cross-event-loop deadlock (mcp.help() hang).

Production execution model: lifespan autoconnect creates sessions on the
server's MAIN event loop, while execute_python offloads REPL execution to a
WORKER thread (anyio.to_thread). Before the loop-affinity fix, any wrapper
call from that thread awaited loop-bound sessions on a foreign loop and
deadlocked forever (observed in the wild as a 17-minute mcp.help() hang).

These tests replicate that exact topology: connect on a running loop, then
call the sync wrapper API from a worker thread with a hard timeout guard.
"""
import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from repl_mcp.mcp_client_wrapper import MCPClientWrapper

CHILD_SERVER = Path(__file__).parent / "fixtures" / "child_mcp_server.py"

GUARD_S = 20  # generous guard; pre-fix these calls hang forever


@pytest_asyncio.fixture
async def connected_wrapper():
    """Wrapper with a child server connected on the running (main) loop."""
    wrapper = MCPClientWrapper()
    results = await wrapper.connect_async(
        {"child": {"command": sys.executable, "args": [str(CHILD_SERVER)]}},
        timeout_s=30.0,
    )
    assert results == {"child": True}
    yield wrapper
    loop = asyncio.get_running_loop()
    await asyncio.wait_for(loop.run_in_executor(None, wrapper.disconnect), timeout=GUARD_S)


@pytest.mark.asyncio
async def test_list_tools_from_worker_thread(connected_wrapper):
    """list_tools() must work from a worker thread (FastMCP sync-tool model)."""
    loop = asyncio.get_running_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(None, connected_wrapper.list_tools), timeout=GUARD_S
    )
    assert result == {"child": ["echo"]}


@pytest.mark.asyncio
async def test_help_from_worker_thread(connected_wrapper):
    """help() — the call that hung 17 minutes in the wild — must return."""
    loop = asyncio.get_running_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(None, connected_wrapper.help), timeout=GUARD_S
    )
    assert "child (1 tools)" in result
    assert ".mcp.json" in result  # scope note present


@pytest.mark.asyncio
async def test_call_from_worker_thread(connected_wrapper):
    """mcp.call() round-trip from a worker thread."""
    loop = asyncio.get_running_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(
            None, lambda: connected_wrapper.call("child", "echo", text="hi")
        ),
        timeout=GUARD_S,
    )
    assert result == "hi"


@pytest.mark.asyncio
async def test_failed_server_is_reported(connected_wrapper):
    """Connect failures surface in .failed and help() instead of vanishing."""
    results = await connected_wrapper.connect_async(
        {"broken": {"command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}},
        timeout_s=5.0,
    )
    assert results == {"broken": False}
    assert "broken" in connected_wrapper.failed

    loop = asyncio.get_running_loop()
    help_text = await asyncio.wait_for(
        loop.run_in_executor(None, connected_wrapper.help), timeout=GUARD_S
    )
    assert "Failed to connect:" in help_text
    assert "broken" in help_text
