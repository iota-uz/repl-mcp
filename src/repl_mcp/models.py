"""Data models for REPL MCP server."""

from typing import Optional, Literal
from pydantic import BaseModel, Field


class ExceptionInfo(BaseModel):
    """Information about an exception that occurred during execution."""

    type: str = Field(description="Exception type name")
    message: str = Field(description="Exception message")
    traceback: str = Field(description="Full traceback string")


class ExecutionResult(BaseModel):
    """Result of executing Python code in the REPL."""

    success: bool = Field(description="Whether execution succeeded without exceptions")
    stdout: str = Field(default="", description="Standard output captured during execution")
    stderr: str = Field(default="", description="Standard error captured during execution")
    return_value: Optional[str] = Field(default=None, description="String representation of return value")
    exception: Optional[ExceptionInfo] = Field(default=None, description="Exception info if execution failed")
    execution_time_ms: float = Field(description="Execution time in milliseconds")
    namespace_vars: dict[str, str] = Field(default_factory=dict, description="Current namespace variables")


class ServerConfig(BaseModel):
    """Configuration for an MCP server connection."""

    command: Optional[str] = Field(default=None, description="Command to run for stdio transport")
    args: Optional[list[str]] = Field(default=None, description="Arguments for stdio command")
    url: Optional[str] = Field(default=None, description="URL for HTTP/SSE transport")
    env: Optional[dict[str, str]] = Field(default=None, description="Environment variables for the server")

    @property
    def transport_type(self) -> Literal["stdio", "sse"]:
        """Determine transport type from config."""
        if self.url:
            return "sse"
        return "stdio"
