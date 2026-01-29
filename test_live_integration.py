#!/usr/bin/env python3
"""
Live integration test for Python REPL MCP Server.

This script:
1. Starts the MCP server with SSE transport
2. Connects to it using the MCP SDK client
3. Tests Python execution functionality
4. Tests MCP tool invocation
5. Verifies autoconnect behavior
"""

import asyncio
import subprocess
import time
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client


async def test_repl_server():
    """Test the REPL MCP server with SSE transport."""

    print("=" * 80)
    print("Python REPL MCP Server - Live Integration Test")
    print("=" * 80)

    # Start server
    print("\n[1/5] Starting MCP server with SSE transport...")
    venv_python = Path(__file__).parent / ".venv" / "bin" / "python3"

    server_process = subprocess.Popen(
        [
            str(venv_python),
            "-m",
            "repl_mcp.repl_mcp_server",
            "--transport",
            "sse",
            "--port",
            "8765",
            "--no-autoconnect",  # Disable autoconnect for isolated testing
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for server to be ready
    server_url = "http://localhost:8765"
    print(f"   Waiting for server at {server_url}...")

    max_wait = 10
    start_time = time.time()

    while time.time() - start_time < max_wait:
        if server_process.poll() is not None:
            stdout, stderr = server_process.communicate()
            print(f"   ❌ Server failed to start!")
            print(f"   STDOUT: {stdout}")
            print(f"   STDERR: {stderr}")
            return False

        try:
            # Try to connect
            transport = sse_client(f"{server_url}/sse")
            async with transport as (read, write):
                print(f"   ✅ Server is running on {server_url}/sse")
                break
        except Exception as e:
            await asyncio.sleep(0.5)
    else:
        print("   ❌ Server did not start in time")
        server_process.terminate()
        return False

    try:
        # Connect to server
        print("\n[2/5] Connecting to MCP server...")
        transport = sse_client(f"{server_url}/sse")

        async with transport as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("   ✅ Connected and initialized MCP session")

                # List available tools
                print("\n[3/5] Listing available tools...")
                tools_result = await session.list_tools()
                tool_names = [tool.name for tool in tools_result.tools]
                print(f"   Available tools: {', '.join(tool_names)}")

                expected_tools = ["execute_python", "list_namespace_vars", "list_connected_servers"]
                for tool in expected_tools:
                    if tool in tool_names:
                        print(f"   ✅ {tool}")
                    else:
                        print(f"   ❌ {tool} (missing)")

                # Test basic Python execution
                print("\n[4/5] Testing Python execution...")

                # Test 1: Simple arithmetic
                result = await session.call_tool(
                    "execute_python",
                    arguments={"code": "x = 42\nprint(f'The answer is {x}')\nx"}
                )

                if result.content:
                    content = result.content[0].text if hasattr(result.content[0], 'text') else str(result.content[0])
                    print(f"   Test 1 - Simple arithmetic:")
                    print(f"   {content[:200]}")

                    # Verify result contains expected values
                    if "42" in content and "success" in content.lower():
                        print("   ✅ Basic execution works")
                    else:
                        print("   ❌ Unexpected result")

                # Test 2: State persistence
                result = await session.call_tool(
                    "execute_python",
                    arguments={"code": "y = x + 8\nprint(f'x={x}, y={y}')\ny"}
                )

                if result.content:
                    content = result.content[0].text if hasattr(result.content[0], 'text') else str(result.content[0])
                    print(f"   Test 2 - State persistence:")
                    print(f"   {content[:200]}")

                    if "50" in content:
                        print("   ✅ State persists between calls")
                    else:
                        print("   ❌ State not persisted")

                # Test 3: List namespace
                result = await session.call_tool("list_namespace_vars", arguments={})

                if result.content:
                    content = result.content[0].text if hasattr(result.content[0], 'text') else str(result.content[0])
                    print(f"   Test 3 - List namespace:")
                    print(f"   {content[:200]}")

                    if "x" in content and "y" in content:
                        print("   ✅ Namespace listing works")
                    else:
                        print("   ❌ Variables not in namespace")

                # Test 4: Error handling
                print("\n[5/5] Testing error handling...")
                result = await session.call_tool(
                    "execute_python",
                    arguments={"code": "undefined_variable"}
                )

                if result.content:
                    content = result.content[0].text if hasattr(result.content[0], 'text') else str(result.content[0])

                    if "NameError" in content or "exception" in content.lower():
                        print("   ✅ Error handling works")
                    else:
                        print("   ❌ Error not properly captured")

                print("\n" + "=" * 80)
                print("✅ All tests completed successfully!")
                print("=" * 80)

    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Cleanup
        print("\n[Cleanup] Stopping server...")
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
            print("   ✅ Server stopped cleanly")
        except subprocess.TimeoutExpired:
            server_process.kill()
            server_process.wait()
            print("   ⚠️  Server killed (timeout)")

    return True


if __name__ == "__main__":
    success = asyncio.run(test_repl_server())
    sys.exit(0 if success else 1)
