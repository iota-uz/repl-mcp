"""Data models for REPL MCP server."""

from typing import Optional, Literal, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator

# Exception text caps — exceptions bypass the engine's stdout/return-value
# truncation, so they get their own (a 1MB assertion diff must not flood the
# agent's context in one response).
MAX_EXCEPTION_MESSAGE_SIZE = 10_000
MAX_EXCEPTION_TRACEBACK_SIZE = 20_000


def utf8_safe(text: str) -> str:
    """
    Make a string strictly UTF-8 encodable (replace lone surrogates etc.).

    Critical invariant: any non-encodable character that reaches the MCP
    response kills the stdio writer task (PydanticSerializationError inside
    mcp's stdout_writer) and permanently wedges the server — every later tool
    call hangs. Surrogates arrive via surrogateescape decoding (os.fsdecode,
    weird filenames), broken UTF-16 handling, or malformed JSON.
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def _cap(text: str, max_size: int) -> str:
    """Hard-cap a string with an explicit omitted-count marker."""
    if len(text) <= max_size:
        return text
    marker = f"\n... [TRUNCATED — {len(text) - max_size} chars omitted]"
    return text[: max_size - len(marker)] + marker


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

    @field_validator("message", mode="after")
    @classmethod
    def _sanitize_message(cls, v: str) -> str:
        return _cap(utf8_safe(v), MAX_EXCEPTION_MESSAGE_SIZE)

    @field_validator("traceback", mode="after")
    @classmethod
    def _sanitize_traceback(cls, v: str) -> str:
        return _cap(utf8_safe(v), MAX_EXCEPTION_TRACEBACK_SIZE)

    @field_validator("context_line", mode="after")
    @classmethod
    def _sanitize_context(cls, v: Optional[str]) -> Optional[str]:
        return utf8_safe(v) if v is not None else None

    @field_validator("hints", "similar_names", mode="after")
    @classmethod
    def _sanitize_lists(cls, v: list[str]) -> list[str]:
        return [utf8_safe(item) for item in v]


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

    @field_validator("stdout", "stderr", mode="after")
    @classmethod
    def _sanitize_streams(cls, v: str) -> str:
        return utf8_safe(v)

    @field_validator("return_value", mode="after")
    @classmethod
    def _sanitize_return(cls, v: Optional[str]) -> Optional[str]:
        return utf8_safe(v) if v is not None else None

    @field_validator("namespace_vars", mode="after")
    @classmethod
    def _sanitize_namespace(cls, v: dict[str, str]) -> dict[str, str]:
        return {k: utf8_safe(val) for k, val in v.items()}

    def __str__(self) -> str:
        """Format as human-readable plain text output."""
        parts = []

        # Output captured before a failure is still valuable (debug prints) —
        # show it in both the success and error cases.
        if self.stdout:
            parts.append(self.stdout.rstrip())

        if self.stderr:
            parts.append(f"[stderr] {self.stderr.rstrip()}")

        # Error case
        if not self.success and self.exception:
            exc = self.exception
            summary = f"{exc.type}: {exc.message}" if exc.message else exc.type
            if parts:
                parts.append("")
            parts.append(summary)

            # Skip tracebacks that add nothing beyond the summary line
            # (KeyboardInterrupt, SystemExit, KernelRestarted, ...) — they
            # would render as a bare duplicate of the message.
            def _norm(s: str) -> str:
                return s.rstrip(": \n")

            tb = (exc.traceback or "").strip()
            if tb and _norm(tb) not in (_norm(summary), exc.type) \
                    and not _norm(summary).startswith(_norm(tb)):
                parts.append(tb)

            if exc.hints:
                parts.append("")
                for hint in exc.hints:
                    parts.append(f"Hint: {hint}")

            for w in self.warnings:
                parts.append(f"⚠ {w.message}")
            return "\n".join(parts)

        # Success case
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
