#!/usr/bin/env python3
"""
Demonstration of autoconnect functionality.

This script starts the server WITH autoconnect enabled and shows
that it automatically connects to configured MCP servers.
"""

import asyncio
import subprocess
import time
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client


async def test_autoconnect():
    """Test autoconnect functionality."""

    print("=" * 80)
    print("Python REPL MCP Server - Autoconnect Demonstration")
    print("=" * 80)

    # Start server WITH autoconnect (reads .mcp.json)
    print("\n[1/3] Starting MCP server with autoconnect enabled...")
    venv_python = Path(__file__).parent / ".venv" / "bin" / "python3"

    server_process = subprocess.Popen(
        [
            str(venv_python),
            "-m",
            "repl_mcp.repl_mcp_server",
            "--transport",
            "sse",
            "--port",
            "8766",
            # Note: NOT using --no-autoconnect, so it will read .mcp.json
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for server to be ready
    server_url = "http://localhost:8766"
    print(f"   Waiting for server at {server_url}...")

    max_wait = 15  # Give more time for autoconnect
    start_time = time.time()

    while time.time() - start_time < max_wait:
        if server_process.poll() is not None:
            stdout, stderr = server_process.communicate()
            print(f"   ❌ Server failed to start!")
            print(f"   STDOUT: {stdout}")
            print(f"   STDERR: {stderr}")
            return False

        try:
            transport = sse_client(f"{server_url}/sse")
            async with transport as (read, write):
                print(f"   ✅ Server is running on {server_url}/sse")
                break
        except Exception:
            await asyncio.sleep(0.5)
    else:
        print("   ❌ Server did not start in time")
        server_process.terminate()
        return False

    try:
        # Connect to server
        print("\n[2/3] Connecting to MCP server and checking autoconnect...")
        transport = sse_client(f"{server_url}/sse")

        async with transport as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("   ✅ Connected to MCP session")

                # Check which servers are connected
                print("\n[3/3] Checking connected MCP servers...")
                result = await session.call_tool(
                    "list_connected_servers",
                    arguments={}
                )

                if result.content:
                    content = result.content[0].text if hasattr(result.content[0], 'text') else str(result.content[0])
                    print(f"   Connected servers: {content}")

                    # Parse the JSON to see what's connected
                    import json
                    try:
                        servers = json.loads(content)
                        if servers:
                            print(f"\n   ✅ Autoconnect worked! Connected to {len(servers)} server(s):")
                            for server_name, tools in servers.items():
                                print(f"      - {server_name}: {len(tools)} tools available")
                        else:
                            print("\n   ⚠️  No servers connected (check .mcp.json configuration)")
                    except json.JSONDecodeError:
                        print(f"   Result: {content}")

                # Test using the mcp object in Python code
                print("\n[Bonus] Testing mcp object access in REPL...")
                result = await session.call_tool(
                    "execute_python",
                    arguments={
                        "code": """
# The 'mcp' object is pre-injected into the REPL namespace
import json

# List connected servers using the mcp object
servers = mcp.discover_tools()
print(f"Servers available via mcp object: {list(servers.keys())}")

# Show the result
json.dumps(servers, indent=2)
"""
                    }
                )

                if result.content:
                    content = result.content[0].text if hasattr(result.content[0], 'text') else str(result.content[0])
                    print(f"   {content[:500]}")

                print("\n" + "=" * 80)
                print("✅ Autoconnect demonstration complete!")
                print("=" * 80)
                print("\nNote: If no servers connected, ensure:")
                print("  1. .mcp.json exists and has valid server configurations")
                print("  2. Server commands are accessible (e.g., 'npx' for GitHub)")
                print("  3. Required environment variables are set (e.g., GITHUB_TOKEN)")

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
    success = asyncio.run(test_autoconnect())
    sys.exit(0 if success else 1)
