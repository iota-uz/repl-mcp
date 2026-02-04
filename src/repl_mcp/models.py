"""Data models for REPL MCP server."""

from datetime import datetime
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


class ServerConfig(BaseModel):
    """Configuration for an MCP server connection."""

    model_config = ConfigDict(extra="allow")

    # Claude Code / MCP configs sometimes include auxiliary keys like "type".
    type: Optional[str] = Field(default=None, description="Optional transport type hint (ignored)")

    command: Optional[str] = Field(default=None, description="Command to run for stdio transport")
    args: Optional[list[str]] = Field(default=None, description="Arguments for stdio command")
    url: Optional[str] = Field(default=None, description="URL for HTTP/SSE transport")
    env: Optional[dict[str, str]] = Field(default=None, description="Environment variables for the server")
    timeout_s: Optional[float] = Field(
        default=None,
        description="Optional per-server connect timeout in seconds",
    )

    @property
    def transport_type(self) -> Literal["stdio", "sse"]:
        """Determine transport type from config."""
        if self.url:
            return "sse"
        return "stdio"


# =============================================================================
# Git Models
# =============================================================================


class CommitInfo(BaseModel):
    """Information about a git commit."""

    hash: str = Field(description="Full commit hash")
    short_hash: str = Field(description="Short commit hash (7 chars)")
    message: str = Field(description="Commit message (first line)")
    full_message: str = Field(default="", description="Full commit message")
    author_name: str = Field(description="Author name")
    author_email: str = Field(description="Author email")
    authored_date: datetime = Field(description="Author date")
    committer_name: str = Field(default="", description="Committer name")
    committer_email: str = Field(default="", description="Committer email")
    committed_date: Optional[datetime] = Field(default=None, description="Commit date")
    files_changed: list[str] = Field(default_factory=list, description="List of changed files")


class FileDiff(BaseModel):
    """Information about a file change in a diff."""

    path: str = Field(description="File path")
    change_type: Literal["added", "modified", "deleted", "renamed", "copied"] = Field(
        description="Type of change"
    )
    old_path: Optional[str] = Field(default=None, description="Old path if renamed/copied")
    additions: int = Field(default=0, description="Lines added")
    deletions: int = Field(default=0, description="Lines deleted")
    patch: Optional[str] = Field(default=None, description="Unified diff patch")


class BlameLine(BaseModel):
    """Information about a line from git blame."""

    line_num: int = Field(description="Line number (1-indexed)")
    content: str = Field(description="Line content")
    commit_hash: str = Field(description="Commit hash that last modified this line")
    short_hash: str = Field(description="Short commit hash")
    author: str = Field(description="Author name")
    author_email: str = Field(description="Author email")
    date: datetime = Field(description="Date of the commit")


class GitStatus(BaseModel):
    """Git repository status."""

    branch: str = Field(description="Current branch name")
    tracking_branch: Optional[str] = Field(default=None, description="Remote tracking branch")
    staged: list[str] = Field(default_factory=list, description="Staged files")
    unstaged: list[str] = Field(default_factory=list, description="Modified but unstaged files")
    untracked: list[str] = Field(default_factory=list, description="Untracked files")
    ahead: int = Field(default=0, description="Commits ahead of tracking branch")
    behind: int = Field(default=0, description="Commits behind tracking branch")
    is_dirty: bool = Field(default=False, description="Whether working tree has changes")


class BranchInfo(BaseModel):
    """Information about a git branch."""

    name: str = Field(description="Branch name")
    is_current: bool = Field(default=False, description="Whether this is the current branch")
    is_remote: bool = Field(default=False, description="Whether this is a remote branch")
    tracking_branch: Optional[str] = Field(default=None, description="Remote tracking branch")
    commit_hash: str = Field(description="Commit hash at branch tip")
    commit_message: str = Field(description="Commit message at branch tip")


# =============================================================================
# AST Models
# =============================================================================


class CallSite(BaseModel):
    """Information about a function call site."""

    file: str = Field(description="File path")
    line: int = Field(description="Line number (1-indexed)")
    column: int = Field(description="Column offset")
    function_name: str = Field(description="Name of the function being called")
    full_call: str = Field(description="Full call expression (e.g., 'module.func')")
    context: str = Field(default="", description="Surrounding code context")


class FunctionDef(BaseModel):
    """Information about a function definition."""

    file: str = Field(description="File path")
    line: int = Field(description="Line number (1-indexed)")
    end_line: Optional[int] = Field(default=None, description="End line number")
    name: str = Field(description="Function name")
    params: list[str] = Field(default_factory=list, description="Parameter names")
    return_annotation: Optional[str] = Field(default=None, description="Return type annotation")
    docstring: Optional[str] = Field(default=None, description="Function docstring")
    is_async: bool = Field(default=False, description="Whether this is an async function")
    is_method: bool = Field(default=False, description="Whether this is a method")
    decorators: list[str] = Field(default_factory=list, description="Decorator names")


class ClassDef(BaseModel):
    """Information about a class definition."""

    file: str = Field(description="File path")
    line: int = Field(description="Line number (1-indexed)")
    end_line: Optional[int] = Field(default=None, description="End line number")
    name: str = Field(description="Class name")
    bases: list[str] = Field(default_factory=list, description="Base class names")
    methods: list[str] = Field(default_factory=list, description="Method names")
    class_variables: list[str] = Field(default_factory=list, description="Class variable names")
    docstring: Optional[str] = Field(default=None, description="Class docstring")
    decorators: list[str] = Field(default_factory=list, description="Decorator names")


class ImportInfo(BaseModel):
    """Information about an import statement."""

    file: str = Field(description="File path")
    line: int = Field(description="Line number (1-indexed)")
    module: str = Field(description="Imported module name")
    names: list[str] = Field(default_factory=list, description="Imported names (for 'from' imports)")
    alias: Optional[str] = Field(default=None, description="Import alias")
    is_from_import: bool = Field(default=False, description="Whether this is a 'from' import")
    is_relative: bool = Field(default=False, description="Whether this is a relative import")


class DependencyGraph(BaseModel):
    """Dependency graph for a set of modules."""

    nodes: list[str] = Field(default_factory=list, description="List of module/file paths")
    edges: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Edges as (importer, imported) tuples"
    )
    external_deps: list[str] = Field(
        default_factory=list,
        description="External dependencies (not found in codebase)"
    )


class UsageInfo(BaseModel):
    """Information about a name usage in code."""

    file: str = Field(description="File path")
    line: int = Field(description="Line number (1-indexed)")
    column: int = Field(description="Column offset")
    context: str = Field(default="", description="Surrounding code context")


class ComplexityMetrics(BaseModel):
    """Code complexity metrics for a file."""

    file: str = Field(description="File path")
    lines: int = Field(default=0, description="Total lines of code")
    functions: int = Field(default=0, description="Number of functions")
    classes: int = Field(default=0, description="Number of classes")
    imports: int = Field(default=0, description="Number of import statements")
    max_nesting_depth: int = Field(default=0, description="Maximum nesting depth")


class TagInfo(BaseModel):
    """Information about a git tag."""

    name: str = Field(description="Tag name")
    commit_hash: str = Field(description="Commit hash the tag points to")
    commit_message: str = Field(description="Commit message")
    date: datetime = Field(description="Tag/commit date")
