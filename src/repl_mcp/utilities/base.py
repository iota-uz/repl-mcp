"""Base classes and protocols for REPL utilities."""

from typing import Protocol, runtime_checkable


# =============================================================================
# Utility Protocol
# =============================================================================


@runtime_checkable
class REPLUtility(Protocol):
    """Protocol for REPL-injectable utilities.

    All utilities injected into the REPL namespace should implement this protocol
    to ensure consistent behavior and discoverability.
    """

    def __repr__(self) -> str:
        """Return string representation for REPL display."""
        ...


# =============================================================================
# Custom Exceptions
# =============================================================================


class UtilityError(Exception):
    """Base exception for all utility errors."""

    pass


class WorkspaceError(UtilityError):
    """Exception raised for workspace-related errors."""

    pass


class PathEscapeError(WorkspaceError, PermissionError):
    """Exception raised when a path attempts to escape the workspace.

    Inherits from PermissionError for backwards compatibility with code
    that catches PermissionError for security violations.
    """

    def __init__(self, path: str, workspace_root: str = ""):
        self.path = path
        self.workspace_root = workspace_root
        msg = f"Path escapes workspace: {path}"
        if workspace_root:
            msg += f" (root: {workspace_root})"
        PermissionError.__init__(self, msg)
        WorkspaceError.__init__(self, msg)


class WriteDisabledError(WorkspaceError, PermissionError):
    """Exception raised when write operations are attempted on read-only workspace.

    Inherits from PermissionError for backwards compatibility.
    """

    def __init__(self):
        msg = "Write operations are disabled for this workspace"
        PermissionError.__init__(self, msg)
        WorkspaceError.__init__(self, msg)


class GitUtilsError(UtilityError):
    """Exception raised for git-related errors."""

    pass


class NotAGitRepoError(GitUtilsError):
    """Exception raised when path is not in a git repository."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Not a git repository: {path}")


class GitRefNotFoundError(GitUtilsError):
    """Exception raised when a git ref (branch, tag, commit) is not found."""

    def __init__(self, ref: str):
        self.ref = ref
        super().__init__(f"Git ref not found: {ref}")


class ASTError(UtilityError):
    """Exception raised for AST analysis errors."""

    pass


class ParseError(ASTError):
    """Exception raised when a Python file cannot be parsed."""

    def __init__(self, path: str, error: str):
        self.path = path
        self.error = error
        super().__init__(f"Failed to parse {path}: {error}")
