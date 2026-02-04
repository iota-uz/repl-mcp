"""Tests for workspace file utilities."""

import os
import pytest
from pathlib import Path

from repl_mcp.utilities.workspace import Workspace


class TestWorkspaceBasics:
    """Test basic workspace operations."""

    def test_init_valid_directory(self, tmp_path):
        """Test workspace initialization with valid directory."""
        ws = Workspace(tmp_path)
        assert ws.root == tmp_path.resolve()
        assert ws.allow_write is True

    def test_init_nonexistent_directory(self, tmp_path):
        """Test workspace initialization with nonexistent directory."""
        with pytest.raises(ValueError, match="does not exist"):
            Workspace(tmp_path / "nonexistent")

    def test_init_file_not_directory(self, tmp_path):
        """Test workspace initialization with file instead of directory."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")
        with pytest.raises(ValueError, match="not a directory"):
            Workspace(file_path)

    def test_init_read_only(self, tmp_path):
        """Test workspace initialization with write disabled."""
        ws = Workspace(tmp_path, allow_write=False)
        assert ws.allow_write is False


class TestWorkspaceRead:
    """Test workspace read operations."""

    def test_read_file(self, tmp_path):
        """Test reading a file."""
        (tmp_path / "test.txt").write_text("hello world")
        ws = Workspace(tmp_path)
        content = ws.read("test.txt")
        assert content == "hello world"

    def test_read_nested_file(self, tmp_path):
        """Test reading a nested file."""
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "test.txt").write_text("nested content")
        ws = Workspace(tmp_path)
        content = ws.read("subdir/test.txt")
        assert content == "nested content"

    def test_read_nonexistent_file(self, tmp_path):
        """Test reading a nonexistent file raises error."""
        ws = Workspace(tmp_path)
        with pytest.raises(FileNotFoundError):
            ws.read("nonexistent.txt")

    def test_read_bytes(self, tmp_path):
        """Test reading binary content."""
        (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02")
        ws = Workspace(tmp_path)
        content = ws.read_bytes("binary.bin")
        assert content == b"\x00\x01\x02"

    def test_read_lines(self, tmp_path):
        """Test reading file as lines."""
        (tmp_path / "lines.txt").write_text("line1\nline2\nline3")
        ws = Workspace(tmp_path)
        lines = ws.read_lines("lines.txt")
        assert lines == ["line1", "line2", "line3"]


class TestWorkspaceWrite:
    """Test workspace write operations."""

    def test_write_file(self, tmp_path):
        """Test writing a file."""
        ws = Workspace(tmp_path)
        ws.write("output.txt", "test content")
        assert (tmp_path / "output.txt").read_text() == "test content"

    def test_write_creates_parent_dirs(self, tmp_path):
        """Test that write creates parent directories."""
        ws = Workspace(tmp_path)
        ws.write("deep/nested/output.txt", "content")
        assert (tmp_path / "deep" / "nested" / "output.txt").read_text() == "content"

    def test_write_bytes(self, tmp_path):
        """Test writing binary content."""
        ws = Workspace(tmp_path)
        ws.write("binary.bin", b"\x00\x01\x02")
        assert (tmp_path / "binary.bin").read_bytes() == b"\x00\x01\x02"

    def test_write_disabled(self, tmp_path):
        """Test that write raises when disabled."""
        ws = Workspace(tmp_path, allow_write=False)
        with pytest.raises(PermissionError, match="Write operations are disabled"):
            ws.write("test.txt", "content")

    def test_append_file(self, tmp_path):
        """Test appending to a file."""
        (tmp_path / "append.txt").write_text("first\n")
        ws = Workspace(tmp_path)
        ws.append("append.txt", "second\n")
        assert (tmp_path / "append.txt").read_text() == "first\nsecond\n"


class TestWorkspaceDiscovery:
    """Test workspace discovery operations."""

    def test_glob_pattern(self, tmp_path):
        """Test glob pattern matching."""
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        ws = Workspace(tmp_path)
        py_files = ws.glob("*.py")
        assert set(py_files) == {"a.py", "b.py"}

    def test_glob_recursive(self, tmp_path):
        """Test recursive glob pattern."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("")
        (tmp_path / "src" / "sub").mkdir()
        (tmp_path / "src" / "sub" / "helper.py").write_text("")
        ws = Workspace(tmp_path)
        all_py = ws.glob("**/*.py")
        assert len(all_py) == 2
        assert any("main.py" in f for f in all_py)
        assert any("helper.py" in f for f in all_py)

    def test_exists(self, tmp_path):
        """Test existence check."""
        (tmp_path / "exists.txt").write_text("")
        ws = Workspace(tmp_path)
        assert ws.exists("exists.txt") is True
        assert ws.exists("not_exists.txt") is False

    def test_is_file_is_dir(self, tmp_path):
        """Test file/directory detection."""
        (tmp_path / "file.txt").write_text("")
        (tmp_path / "dir").mkdir()
        ws = Workspace(tmp_path)
        assert ws.is_file("file.txt") is True
        assert ws.is_dir("file.txt") is False
        assert ws.is_file("dir") is False
        assert ws.is_dir("dir") is True

    def test_listdir(self, tmp_path):
        """Test directory listing."""
        (tmp_path / "a.txt").write_text("")
        (tmp_path / "b.txt").write_text("")
        (tmp_path / "subdir").mkdir()
        ws = Workspace(tmp_path)
        contents = ws.listdir()
        assert set(contents) == {"a.txt", "b.txt", "subdir"}

    def test_stat(self, tmp_path):
        """Test file statistics."""
        (tmp_path / "file.txt").write_text("content")
        ws = Workspace(tmp_path)
        stat = ws.stat("file.txt")
        assert stat["size"] == 7  # len("content")
        assert stat["is_file"] is True
        assert stat["is_dir"] is False


class TestWorkspaceFilesystem:
    """Test workspace filesystem operations."""

    def test_mkdir(self, tmp_path):
        """Test directory creation."""
        ws = Workspace(tmp_path)
        ws.mkdir("newdir")
        assert (tmp_path / "newdir").is_dir()

    def test_mkdir_nested(self, tmp_path):
        """Test nested directory creation."""
        ws = Workspace(tmp_path)
        ws.mkdir("a/b/c")
        assert (tmp_path / "a" / "b" / "c").is_dir()

    def test_remove(self, tmp_path):
        """Test file removal."""
        (tmp_path / "to_remove.txt").write_text("")
        ws = Workspace(tmp_path)
        ws.remove("to_remove.txt")
        assert not (tmp_path / "to_remove.txt").exists()

    def test_remove_directory_raises(self, tmp_path):
        """Test that remove raises for directories."""
        (tmp_path / "dir").mkdir()
        ws = Workspace(tmp_path)
        with pytest.raises(IsADirectoryError):
            ws.remove("dir")

    def test_rmdir(self, tmp_path):
        """Test directory removal."""
        (tmp_path / "empty_dir").mkdir()
        ws = Workspace(tmp_path)
        ws.rmdir("empty_dir")
        assert not (tmp_path / "empty_dir").exists()

    def test_rmdir_recursive(self, tmp_path):
        """Test recursive directory removal."""
        (tmp_path / "dir" / "subdir").mkdir(parents=True)
        (tmp_path / "dir" / "file.txt").write_text("")
        ws = Workspace(tmp_path)
        ws.rmdir("dir", recursive=True)
        assert not (tmp_path / "dir").exists()

    def test_copy(self, tmp_path):
        """Test file copy."""
        (tmp_path / "source.txt").write_text("content")
        ws = Workspace(tmp_path)
        ws.copy("source.txt", "dest.txt")
        assert (tmp_path / "dest.txt").read_text() == "content"
        assert (tmp_path / "source.txt").exists()  # Original still exists

    def test_move(self, tmp_path):
        """Test file move."""
        (tmp_path / "source.txt").write_text("content")
        ws = Workspace(tmp_path)
        ws.move("source.txt", "dest.txt")
        assert (tmp_path / "dest.txt").read_text() == "content"
        assert not (tmp_path / "source.txt").exists()  # Original is gone


class TestWorkspaceSecurity:
    """Test workspace security features."""

    def test_path_traversal_blocked(self, tmp_path):
        """Test that path traversal attacks are blocked."""
        ws = Workspace(tmp_path)
        with pytest.raises(PermissionError, match="escapes workspace"):
            ws.read("../../../etc/passwd")

    def test_path_traversal_with_nested_start(self, tmp_path):
        """Test path traversal from nested directory."""
        (tmp_path / "subdir").mkdir()
        ws = Workspace(tmp_path)
        with pytest.raises(PermissionError, match="escapes workspace"):
            ws.read("subdir/../../etc/passwd")

    def test_absolute_path_outside_workspace(self, tmp_path):
        """Test that absolute paths outside workspace are blocked."""
        ws = Workspace(tmp_path)
        with pytest.raises(PermissionError, match="escapes workspace"):
            ws.read("/etc/passwd")

    def test_symlink_escape_blocked(self, tmp_path):
        """Test that symlinks pointing outside workspace are blocked."""
        # Create a symlink pointing outside the workspace
        link_path = tmp_path / "escape_link"
        try:
            link_path.symlink_to("/etc")
        except OSError:
            pytest.skip("Cannot create symlinks (permissions)")

        ws = Workspace(tmp_path)
        with pytest.raises(PermissionError, match="escapes workspace"):
            ws.read("escape_link/passwd")

    def test_write_traversal_blocked(self, tmp_path):
        """Test that write path traversal is blocked."""
        ws = Workspace(tmp_path)
        with pytest.raises(PermissionError, match="escapes workspace"):
            ws.write("../outside.txt", "malicious")

    def test_mkdir_traversal_blocked(self, tmp_path):
        """Test that mkdir path traversal is blocked."""
        ws = Workspace(tmp_path)
        with pytest.raises(PermissionError, match="escapes workspace"):
            ws.mkdir("../outside_dir")


class TestWorkspaceSearch:
    """Test workspace search functionality."""

    def test_search_pattern(self, tmp_path):
        """Test searching for patterns in files."""
        (tmp_path / "a.py").write_text("def hello():\n    print('hello')")
        (tmp_path / "b.py").write_text("def world():\n    print('world')")
        ws = Workspace(tmp_path)
        results = ws.search(r"def \w+\(\)")
        assert len(results) == 2

    def test_search_returns_line_info(self, tmp_path):
        """Test that search returns line information."""
        (tmp_path / "test.py").write_text("line1\nTODO: fix this\nline3")
        ws = Workspace(tmp_path)
        results = ws.search("TODO")
        assert len(results) == 1
        assert results[0]["file"] == "test.py"
        assert results[0]["line_num"] == 2
        assert "TODO" in results[0]["line"]


class TestWorkspaceTree:
    """Test workspace tree functionality."""

    def test_tree_basic(self, tmp_path):
        """Test basic tree output."""
        (tmp_path / "file.txt").write_text("")
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "nested.txt").write_text("")
        ws = Workspace(tmp_path)
        tree = ws.tree()
        assert "file.txt" in tree
        assert any("dir" in item for item in tree)

    def test_tree_max_depth(self, tmp_path):
        """Test tree respects max depth."""
        (tmp_path / "a" / "b" / "c" / "d").mkdir(parents=True)
        (tmp_path / "a" / "b" / "c" / "d" / "deep.txt").write_text("")
        ws = Workspace(tmp_path)
        tree = ws.tree(max_depth=2)
        # Should not include the deepest file
        assert not any("deep.txt" in item for item in tree)


class TestWorkspaceIntegration:
    """Integration tests for workspace with REPL engine."""

    def test_workspace_injected_in_repl(self, tmp_path):
        """Test that workspace is available in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        (tmp_path / "test.txt").write_text("hello from file")
        engine = REPLEngine(workspace_root=tmp_path)

        result = engine.execute("workspace.read('test.txt')")
        assert result.success
        assert "hello from file" in result.return_value

    def test_workspace_glob_in_repl(self, tmp_path):
        """Test using workspace.glob in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        engine = REPLEngine(workspace_root=tmp_path)

        result = engine.execute("len(workspace.glob('*.py'))")
        assert result.success
        assert result.return_value == "2"

    def test_workspace_preserved_on_reset(self, tmp_path):
        """Test that workspace is preserved after namespace reset."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=tmp_path)
        engine.execute("x = 42")
        engine.reset_namespace()

        # workspace should still be available
        result = engine.execute("workspace is not None")
        assert result.success
        assert result.return_value == "True"

        # but x should be gone
        result = engine.execute("x")
        assert not result.success  # x is undefined
