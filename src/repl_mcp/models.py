"""Data models for REPL MCP server."""

from typing import Optional, Literal, Any
from pydantic import BaseModel, Field, ConfigDict


class TruncationInfo(BaseModel):
    """Metadata about truncated output."""

    truncated: bool = Field(description="Whether content was truncated")
    original_size: int = Field(description="Original size in characters")
    truncated_size: int = Field(description="Size after truncation")
    truncation_type: Literal["hard", "smart"] = Field(
        default="hard",
        description="'hard' = simple cutoff, 'smart' = attempt to preserve structure"
    )


class WarningInfo(BaseModel):
    """Non-fatal execution warnings."""

    category: str = Field(description="Warning category (e.g., 'large_output', 'slow_execution')")
    message: str = Field(description="Human-readable warning message")
    suggestion: Optional[str] = Field(
        default=None,
        description="Actionable suggestion to address the warning"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context (e.g., actual_size, threshold)"
    )


class ExceptionInfo(BaseModel):
    """Information about an exception that occurred during execution."""

    type: str = Field(description="Exception type name")
    message: str = Field(description="Exception message")
    traceback: str = Field(description="Full traceback string")

    # Python 3.10+ style hints
    hints: list[str] = Field(
        default_factory=list,
        description="Helpful hints for fixing this error (e.g., 'Did you mean workspace.read()?')"
    )
    similar_names: list[str] = Field(
        default_factory=list,
        description="For NameError/AttributeError: suggested similar names"
    )
    context_line: Optional[str] = Field(
        default=None,
        description="The line of code that caused the error"
    )


class ExecutionResult(BaseModel):
    """Result of executing Python code in the REPL."""

    success: bool = Field(description="Whether execution succeeded without exceptions")

    # Output fields with truncation metadata
    stdout: str = Field(default="", description="Standard output captured during execution")
    stdout_info: Optional[TruncationInfo] = Field(
        default=None,
        description="Truncation metadata for stdout"
    )

    stderr: str = Field(default="", description="Standard error captured during execution")
    stderr_info: Optional[TruncationInfo] = Field(
        default=None,
        description="Truncation metadata for stderr"
    )

    return_value: Optional[str] = Field(default=None, description="String representation of return value")
    return_value_info: Optional[TruncationInfo] = Field(
        default=None,
        description="Truncation metadata for return value"
    )

    exception: Optional[ExceptionInfo] = Field(default=None, description="Exception info if execution failed")
    execution_time_ms: float = Field(description="Execution time in milliseconds")

    namespace_vars: dict[str, str] = Field(default_factory=dict, description="Current namespace variables")
    namespace_vars_info: dict[str, TruncationInfo] = Field(
        default_factory=dict,
        description="Truncation info for each namespace variable"
    )

    # Warnings system
    warnings: list[WarningInfo] = Field(
        default_factory=list,
        description="Non-fatal warnings about execution"
    )

    def __str__(self) -> str:
        """Format as human-readable plain text output."""
        parts = []

        # Error case
        if not self.success and self.exception:
            exc = self.exception
            parts.append(f"{exc.type}: {exc.message}")
            if exc.traceback:
                # Show abbreviated traceback (skip the wrapper frames)
                parts.append(exc.traceback.strip())
            if exc.hints:
                parts.append("")
                for hint in exc.hints:
                    parts.append(f"Hint: {hint}")
            return "\n".join(parts)

        # Success case
        if self.stdout:
            parts.append(self.stdout.rstrip())

        if self.stderr:
            parts.append(f"[stderr] {self.stderr.rstrip()}")

        if self.return_value is not None and self.return_value != "None":
            if parts:
                parts.append("")  # blank line before return value
            parts.append(f"→ {self.return_value}")

        # Warnings as footer
        if self.warnings:
            if parts:
                parts.append("")
            for w in self.warnings:
                parts.append(f"⚠ {w.message}")

        # Nothing to show
        if not parts:
            return "(executed successfully)"

        return "\n".join(parts)


class ServerConfig(BaseModel):
    """Configuration for an MCP server connection."""

    model_config = ConfigDict(extra="allow")

    # Claude Code / MCP configs use "type" to pick the transport
    # ("stdio" | "sse" | "http"). Optional — inferred when absent.
    type: Optional[str] = Field(default=None, description="Transport type hint: stdio, sse, or http")

    command: Optional[str] = Field(default=None, description="Command to run for stdio transport")
    args: Optional[list[str]] = Field(default=None, description="Arguments for stdio command")
    url: Optional[str] = Field(default=None, description="URL for HTTP/SSE transport")
    env: Optional[dict[str, str]] = Field(default=None, description="Environment variables for the server")
    headers: Optional[dict[str, str]] = Field(
        default=None,
        description="HTTP headers for sse/http transports (values support ${VAR} env expansion)",
    )
    timeout_s: Optional[float] = Field(
        default=None,
        description="Optional per-server connect timeout in seconds",
    )

    @property
    def transport_type(self) -> Literal["stdio", "sse", "http"]:
        """Determine transport type from config."""
        if self.type in ("http", "streamable-http", "streamable_http"):
            return "http"
        if self.type == "sse":
            return "sse"
        if self.url:
            # Legacy default for bare-url configs
            return "sse"
        return "stdio"
