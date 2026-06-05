"""Minimal MCP stdio server used by kernel bridge tests."""
import time

from fastmcp import FastMCP

mcp = FastMCP("child")


@mcp.tool()
def echo(text: str) -> str:
    """Echo text back."""
    return text


@mcp.tool()
def slow(seconds: float) -> str:
    """Sleep then return (for interrupt-during-call tests)."""
    time.sleep(seconds)
    return f"slept {seconds}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
