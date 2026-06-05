"""Minimal MCP stdio server used by cross-thread bridge tests."""
from fastmcp import FastMCP

mcp = FastMCP("child")


@mcp.tool()
def echo(text: str) -> str:
    """Echo text back."""
    return text


if __name__ == "__main__":
    mcp.run(transport="stdio")
