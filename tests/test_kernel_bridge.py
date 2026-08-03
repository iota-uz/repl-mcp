"""
MCP bridge over the kernel IPC boundary.

Ports the historical cross-thread-bridge scenarios (list_tools / help / call /
failed-server reporting — incl. the 17-minute mcp.help() hang case) onto the
v2 topology: REPL code runs in the kernel CHILD process; its `mcp` proxy RPCs
to the parent where the real sessions live; connection is LAZY (first mcp.*
use triggers connect).

Also covers the hard case: SIGINT fired while a bridge call is in flight must
neither deadlock nor corrupt the rpc channel.
"""

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from repl_mcp.kernel.supervisor import KernelSupervisor
from repl_mcp.mcp_client_wrapper import MCPClientWrapper

CHILD_SERVER = Path(__file__).parent / "fixtures" / "child_mcp_server.py"
GUARD_S = 45


def _spy_connects(wrapper):
    """Count real transport connects (registry hits are free)."""
    calls = {"n": 0}
    original = wrapper._connect_server

    async def counting(name, config):
        calls["n"] += 1
        return await original(name, config)

    wrapper._connect_server = counting
    return calls


@pytest_asyncio.fixture
async def bridged_kernel():
    """Kernel + parent wrapper with the fixture server DISCOVERED, not connected."""
    wrapper = MCPClientWrapper()
    wrapper.register_raw(
        {"child": {"command": sys.executable, "args": [str(CHILD_SERVER)]}}
    )
    connect_calls = _spy_connects(wrapper)

    sup = KernelSupervisor(mcp_wrapper=wrapper)
    await asyncio.wait_for(sup.start(), timeout=GUARD_S)
    yield sup, wrapper, connect_calls
    await asyncio.wait_for(sup.shutdown(), timeout=GUARD_S)
    loop = asyncio.get_running_loop()
    await asyncio.wait_for(
        loop.run_in_executor(None, wrapper.disconnect), timeout=GUARD_S
    )


async def run(sup, code, **kw):
    return await asyncio.wait_for(sup.execute(code, **kw), timeout=GUARD_S)


@pytest.mark.asyncio
class TestBridgeOverIPC:
    async def test_lazy_connect_and_list_tools(self, bridged_kernel):
        sup, wrapper, calls = bridged_kernel
        # Discovered but not started until REPL code touches mcp.*
        assert wrapper.servers == ["child"]
        assert wrapper.connected == []
        assert calls["n"] == 0

        r = await run(sup, "mcp.list_tools()")
        assert r.success, r.exception
        assert "'echo'" in r.return_value
        assert calls["n"] == 1
        assert wrapper.connected == ["child"]

    async def test_help_lists_idle_servers_without_connecting(self, bridged_kernel):
        """Discovery must be free: help() shows what exists, spawns nothing."""
        sup, wrapper, calls = bridged_kernel
        r = await run(sup, "print(mcp.help())")
        assert r.success, r.exception
        assert "child" in r.stdout
        assert "not connected yet" in r.stdout
        assert calls["n"] == 0
        assert wrapper.connected == []

    async def test_help_returns_promptly(self, bridged_kernel):
        """The historical 17-minute hang case, now across two processes."""
        sup, _, _ = bridged_kernel
        r = await run(sup, "mcp.call('child', 'echo', text='warm')\nprint(mcp.help())")
        assert r.success, r.exception
        assert "child" in r.stdout and "2 tools" in r.stdout
        assert ".mcp.json" in r.stdout  # scope note survives the RPC

    async def test_call_round_trip(self, bridged_kernel):
        sup, _, _ = bridged_kernel
        r = await run(sup, "mcp.call('child', 'echo', text='hi from kernel')")
        assert r.success, r.exception
        assert r.return_value == "'hi from kernel'"

    async def test_servers_and_failed_properties(self, bridged_kernel):
        sup, wrapper, _ = bridged_kernel
        r = await run(sup, "(mcp.servers, mcp.failed)")
        assert r.success, r.exception
        assert r.return_value == "(['child'], {})"

    async def test_failed_server_reported_through_proxy(self, bridged_kernel):
        sup, wrapper, _ = bridged_kernel
        # Force-connect a broken server on the parent side
        await wrapper.connect_async(
            {"broken": {"command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}},
            timeout_s=5.0,
        )
        r = await run(sup, "print(mcp.help())")
        assert r.success
        assert "broken" in r.stdout
        assert "Connection closed" in r.stdout

    async def test_unknown_server_error_lists_available(self, bridged_kernel):
        sup, _, calls = bridged_kernel
        r = await run(sup, "mcp.call('telegram-mcp', 'get_me')")
        assert not r.success
        assert "not configured" in r.exception.message
        assert "child" in r.exception.message  # tells the agent what it CAN call
        assert calls["n"] == 0

    async def test_failed_connect_is_negative_cached(self, bridged_kernel):
        """A dead server must not cost a full connect timeout every iteration."""
        sup, wrapper, calls = bridged_kernel
        wrapper.register_raw(
            {"dead": {"command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}}
        )
        for _ in range(3):
            r = await run(sup, "mcp.call('dead', 'echo')")
            assert not r.success
        assert calls["n"] == 1  # connected once, then served from the failure memo

    async def test_non_json_kwargs_clear_error(self, bridged_kernel):
        sup, _, _ = bridged_kernel
        r = await run(sup, "mcp.call('child', 'echo', text=object())")
        assert not r.success
        assert "JSON-serializable" in r.exception.message

    async def test_state_queries_never_connect(self, bridged_kernel):
        sup, _, calls = bridged_kernel
        await run(sup, "mcp.servers")
        await run(sup, "mcp.failed")
        assert calls["n"] == 0

    async def test_connect_happens_once_per_server(self, bridged_kernel):
        sup, _, calls = bridged_kernel
        for _ in range(3):
            r = await run(sup, "mcp.call('child', 'echo', text='x')")
            assert r.success, r.exception
        assert calls["n"] == 1

    async def test_only_the_named_server_starts(self, bridged_kernel):
        """The whole point: naming one server must not spawn the others."""
        sup, wrapper, _ = bridged_kernel
        wrapper.register_raw(
            {"spare": {"command": sys.executable, "args": [str(CHILD_SERVER)]}}
        )
        r = await run(sup, "mcp.call('child', 'echo', text='x')")
        assert r.success, r.exception
        assert wrapper.connected == ["child"]
        assert sorted(wrapper.servers) == ["child", "spare"]


@pytest.mark.asyncio
class TestInterruptDuringBridgeCall:
    async def test_interrupt_mid_call_no_deadlock_no_corruption(self, bridged_kernel):
        """SIGINT while an MCP_CALL is in flight: the cell aborts cleanly,
        state survives, and the rpc channel still works afterwards."""
        sup, _, _ = bridged_kernel
        await run(sup, "mcp.servers")  # connect eagerly so timing is clean
        await run(sup, "marker = 'pre-interrupt'")

        r = await run(
            sup,
            "mcp.call('child', 'slow', seconds=30)",
            timeout=2.0,
        )
        assert not r.success
        assert r.exception.type == "KeyboardInterrupt"

        # Same kernel: state intact, channel not corrupted (stale reply drained)
        r2 = await run(sup, "marker")
        assert r2.return_value == "'pre-interrupt'"
        assert sup.generation == 1

        r3 = await run(sup, "mcp.call('child', 'echo', text='channel ok')")
        assert r3.success, r3.exception
        assert r3.return_value == "'channel ok'"
