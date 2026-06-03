"""Workspace file access for REPL MCP with full filesystem support."""

import os
from pathlib import Path
from typing import Union

from .base import REPLUtility, WriteDisabledError, WorkspaceError


class Workspace(REPLUtility):
    """
    File access rooted at the workspace, with full filesystem support.

    Relative paths resolve against the workspace root. Absolute paths and
    ~-prefixed paths are used as-is — the whole filesystem is accessible
    (matching the REPL itself, where open() is unrestricted).

    Methods:
        read(path) -> str           Read file contents
        read_bytes(path) -> bytes   Read file as bytes
        read_lines(path) -> list    Read file as list of lines
        write(path, content)        Write file contents
        write_bytes(path, content)  Write bytes to file
        append(path, content)       Append to file
        glob(pattern) -> list[str]  Find files matching glob pattern
        exists(path) -> bool        Check if path exists
        is_file(path) -> bool       Check if path is a file
        is_dir(path) -> bool        Check if path is a directory
        listdir(path) -> list[str]  List directory contents
        stat(path) -> dict          Get file statistics
        mkdir(path)                 Create directory
        remove(path)                Remove file
        rmdir(path)                 Remove directory
        copy(src, dst)              Copy file
        move(src, dst)              Move/rename file
        search(pattern, path)       Search for pattern in files
        tree(path, max_depth)       Show directory tree

    Examples:
        >>> workspace.read("src/main.py")
        '#!/usr/bin/env python\\n...'

        >>> workspace.glob("**/*.py")
        ['src/main.py', 'src/utils.py', 'tests/test_main.py']

        >>> workspace.exists("config.json")
        True

        >>> workspace.read("/tmp/data.json")          # absolute paths work
        >>> workspace.glob("~/Projects/**/*.py")      # ~ expansion works

        >>> workspace.write("output.txt", "Hello, World!")

        >>> for result in workspace.search("TODO", "src/"):
        ...     print(f"{result['file']}:{result['line_num']}: {result['line']}")

    Notes:
        - Full filesystem access: absolute and ~ paths supported everywhere
        - Write operations can be disabled via allow_write=False
        - Results under the workspace root are reported as relative paths;
          results outside it are reported as absolute paths
    """

    def __init__(self, root: Union[str, Path], allow_write: bool = True):
        """Initialize workspace with a root directory.

        Args:
            root: Root directory path. All operations are sandboxed within this directory.
            allow_write: If False, write/mkdir/remove operations raise PermissionError.
        """
        self.root = Path(root).resolve()
        self.allow_write = allow_write

        if not self.root.exists():
            raise ValueError(f"Workspace root does not exist: {self.root}")
        if not self.root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {self.root}")

    def _resolve_safe(self, path: str) -> Path:
        """Resolve a path: relative against workspace root, absolute/~ as-is.

        Args:
            path: Relative, absolute, or ~-prefixed path

        Returns:
            Resolved absolute Path
        """
        p = Path(path).expanduser()
        if p.is_absolute():
            return p.resolve()
        return (self.root / p).resolve()

    def _display_path(self, path: Path) -> str:
        """Render a path relative to root when inside it, absolute otherwise."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def _check_write_allowed(self) -> None:
        """Check if write operations are allowed."""
        if not self.allow_write:
            raise WriteDisabledError()

    # -------------------------------------------------------------------------
    # Reading operations
    # -------------------------------------------------------------------------

    def read(self, path: str, encoding: str = "utf-8") -> str:
        """Read file contents as text.

        Args:
            path: Relative path to file
            encoding: Text encoding (default: utf-8)

        Returns:
            File contents as string

        Raises:
            FileNotFoundError: If file doesn't exist
        """
        resolved = self._resolve_safe(path)
        return resolved.read_text(encoding=encoding)

    def read_bytes(self, path: str) -> bytes:
        """Read file contents as bytes.

        Args:
            path: Relative path to file

        Returns:
            File contents as bytes
        """
        resolved = self._resolve_safe(path)
        return resolved.read_bytes()

    def read_lines(self, path: str, encoding: str = "utf-8") -> list[str]:
        """Read file as list of lines (without newlines).

        Args:
            path: Relative path to file
            encoding: Text encoding (default: utf-8)

        Returns:
            List of lines
        """
        content = self.read(path, encoding=encoding)
        return content.splitlines()

    # -------------------------------------------------------------------------
    # Writing operations
    # -------------------------------------------------------------------------

    def write(self, path: str, content: Union[str, bytes], encoding: str = "utf-8") -> None:
        """Write content to file.

        Args:
            path: Relative path to file
            content: Text or bytes to write
            encoding: Text encoding for string content (default: utf-8)

        Raises:
            PermissionError: If writes are disabled (allow_write=False)
        """
        self._check_write_allowed()
        resolved = self._resolve_safe(path)

        # Ensure parent directory exists
        resolved.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(content, bytes):
            resolved.write_bytes(content)
        else:
            resolved.write_text(content, encoding=encoding)

    def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        """Append content to file.

        Args:
            path: Relative path to file
            content: Text to append
            encoding: Text encoding (default: utf-8)
        """
        self._check_write_allowed()
        resolved = self._resolve_safe(path)

        # Ensure parent directory exists
        resolved.parent.mkdir(parents=True, exist_ok=True)

        with open(resolved, "a", encoding=encoding) as f:
            f.write(content)

    # -------------------------------------------------------------------------
    # Discovery operations
    # -------------------------------------------------------------------------

    def glob(self, pattern: str) -> list[str]:
        """Find files matching glob pattern.

        Args:
            pattern: Glob pattern, relative ("**/*.py", "src/*.ts"),
                     absolute ("/tmp/*.json"), or ~-prefixed ("~/logs/*.txt")

        Returns:
            List of matching paths — relative when under workspace root,
            absolute otherwise
        """
        expanded = os.path.expanduser(pattern)
        if os.path.isabs(expanded):
            import glob as glob_module
            paths = (Path(p) for p in glob_module.glob(expanded, recursive=True))
        else:
            paths = self.root.glob(expanded)
        return sorted(self._display_path(p) for p in paths)

    def exists(self, path: str) -> bool:
        """Check if path exists.

        Args:
            path: Relative path to check

        Returns:
            True if path exists
        """
        return self._resolve_safe(path).exists()

    def is_file(self, path: str) -> bool:
        """Check if path is a file.

        Args:
            path: Relative path to check

        Returns:
            True if path is a file
        """
        resolved = self._resolve_safe(path)
        return resolved.is_file()

    def is_dir(self, path: str) -> bool:
        """Check if path is a directory.

        Args:
            path: Relative path to check

        Returns:
            True if path is a directory
        """
        resolved = self._resolve_safe(path)
        return resolved.is_dir()

    def listdir(self, path: str = ".") -> list[str]:
        """List directory contents.

        Args:
            path: Relative path to directory (default: workspace root)

        Returns:
            List of filenames in directory
        """
        resolved = self._resolve_safe(path)
        if not resolved.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        return sorted([p.name for p in resolved.iterdir()])

    def stat(self, path: str) -> dict:
        """Get file statistics.

        Args:
            path: Relative path to file

        Returns:
            Dict with size, mtime, ctime, is_file, is_dir
        """
        resolved = self._resolve_safe(path)
        st = resolved.stat()
        return {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "ctime": st.st_ctime,
            "is_file": resolved.is_file(),
            "is_dir": resolved.is_dir(),
        }

    def tree(self, path: str = ".", max_depth: int = 3, pattern: str = "*") -> list[str]:
        """Get directory tree structure.

        Args:
            path: Relative path to start from (default: workspace root)
            max_depth: Maximum depth to traverse (default: 3)
            pattern: Glob pattern to filter files (default: "*")

        Returns:
            List of relative paths in tree order
        """
        resolved = self._resolve_safe(path)
        if not resolved.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        results = []

        def walk(current: Path, depth: int) -> None:
            if depth > max_depth:
                return

            try:
                items = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name))
            except PermissionError:
                return

            for item in items:
                display = self._display_path(item)
                if item.is_file():
                    if item.match(pattern):
                        results.append(display)
                else:
                    results.append(display + "/")
                    walk(item, depth + 1)

        walk(resolved, 1)
        return results

    # -------------------------------------------------------------------------
    # Filesystem operations
    # -------------------------------------------------------------------------

    def mkdir(self, path: str, parents: bool = True) -> None:
        """Create directory.

        Args:
            path: Relative path for new directory
            parents: Create parent directories if needed (default: True)
        """
        self._check_write_allowed()
        resolved = self._resolve_safe(path)
        resolved.mkdir(parents=parents, exist_ok=True)

    def remove(self, path: str) -> None:
        """Remove file.

        Args:
            path: Relative path to file to remove

        Raises:
            IsADirectoryError: If path is a directory (use rmdir)
        """
        self._check_write_allowed()
        resolved = self._resolve_safe(path)
        if resolved.is_dir():
            raise IsADirectoryError(f"Cannot remove directory with remove(), use rmdir(): {path}")
        resolved.unlink()

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove directory.

        Args:
            path: Relative path to directory
            recursive: If True, remove non-empty directories recursively
        """
        self._check_write_allowed()
        resolved = self._resolve_safe(path)

        if not resolved.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")

        if recursive:
            import shutil
            shutil.rmtree(resolved)
        else:
            resolved.rmdir()

    def copy(self, src: str, dst: str) -> None:
        """Copy file.

        Args:
            src: Source path
            dst: Destination path
        """
        self._check_write_allowed()
        src_resolved = self._resolve_safe(src)
        dst_resolved = self._resolve_safe(dst)

        # Ensure parent directory exists
        dst_resolved.parent.mkdir(parents=True, exist_ok=True)

        import shutil
        shutil.copy2(src_resolved, dst_resolved)

    def move(self, src: str, dst: str) -> None:
        """Move/rename file or directory.

        Args:
            src: Source path
            dst: Destination path
        """
        self._check_write_allowed()
        src_resolved = self._resolve_safe(src)
        dst_resolved = self._resolve_safe(dst)

        # Ensure parent directory exists
        dst_resolved.parent.mkdir(parents=True, exist_ok=True)

        import shutil
        shutil.move(str(src_resolved), str(dst_resolved))

    # -------------------------------------------------------------------------
    # Convenience methods
    # -------------------------------------------------------------------------

    def search(self, pattern: str, path: str = ".", recursive: bool = True) -> list[dict]:
        """Search for pattern in files.

        Args:
            pattern: Regex pattern to search for
            path: Directory to search in (default: workspace root)
            recursive: Search recursively (default: True)

        Returns:
            List of dicts with file, line_num, line content
        """
        import re
        regex = re.compile(pattern)
        results = []

        glob_pattern = "**/*" if recursive else "*"
        resolved = self._resolve_safe(path)

        for file_path in resolved.glob(glob_pattern):
            if not file_path.is_file():
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue

            for line_num, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    results.append({
                        "file": self._display_path(file_path),
                        "line_num": line_num,
                        "line": line.strip(),
                    })

        return results

    def __repr__(self) -> str:
        return f"Workspace(root={self.root!r}, allow_write={self.allow_write})"
