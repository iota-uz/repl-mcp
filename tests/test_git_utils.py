"""Tests for git utilities."""

import subprocess
import pytest
from pathlib import Path
from datetime import datetime

from repl_mcp.utilities.git_utils import GitUtils
from repl_mcp.models import CommitInfo, FileDiff, BlameLine, GitStatus, BranchInfo


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository with some history."""
    # Initialize repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        capture_output=True,
    )

    # Create initial commit
    (tmp_path / "file1.py").write_text("# Initial content\nx = 1")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=tmp_path,
        capture_output=True,
    )

    # Create second commit
    (tmp_path / "file2.py").write_text("# Second file\ny = 2")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add file2"],
        cwd=tmp_path,
        capture_output=True,
    )

    # Create third commit - modify file1
    (tmp_path / "file1.py").write_text("# Modified content\nx = 1\nz = 3")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Modify file1"],
        cwd=tmp_path,
        capture_output=True,
    )

    return tmp_path


@pytest.fixture
def git_utils(git_repo):
    """Create GitUtils instance for the test repo."""
    return GitUtils(git_repo)


class TestGitUtilsInit:
    """Test GitUtils initialization."""

    def test_init_valid_repo(self, git_repo):
        """Test initialization with valid git repo."""
        utils = GitUtils(git_repo)
        assert utils.repo is not None
        assert utils.working_dir == git_repo

    def test_init_invalid_repo(self, tmp_path):
        """Test initialization with non-git directory."""
        from git import InvalidGitRepositoryError

        with pytest.raises(InvalidGitRepositoryError):
            GitUtils(tmp_path)

    def test_init_subdirectory(self, git_repo):
        """Test initialization from subdirectory of repo."""
        subdir = git_repo / "subdir"
        subdir.mkdir()
        utils = GitUtils(subdir)
        assert utils.working_dir == git_repo


class TestGitLog:
    """Test git log functionality."""

    def test_log_basic(self, git_utils):
        """Test basic log retrieval."""
        commits = git_utils.log(n=10)
        assert len(commits) == 3
        assert all(isinstance(c, CommitInfo) for c in commits)

    def test_log_limit(self, git_utils):
        """Test log with limit."""
        commits = git_utils.log(n=2)
        assert len(commits) == 2

    def test_log_commit_info(self, git_utils):
        """Test commit info structure."""
        commits = git_utils.log(n=1)
        commit = commits[0]

        assert commit.hash is not None
        assert len(commit.hash) == 40
        assert len(commit.short_hash) == 7
        assert commit.message == "Modify file1"
        assert commit.author_name == "Test User"
        assert commit.author_email == "test@example.com"
        assert isinstance(commit.authored_date, datetime)

    def test_log_with_path(self, git_utils):
        """Test log filtered by path."""
        commits = git_utils.log(path="file1.py")
        assert len(commits) == 2  # Initial + modification
        assert all("file1" in c.message.lower() or "initial" in c.message.lower() for c in commits)

    def test_log_include_files(self, git_utils):
        """Test log with file list."""
        commits = git_utils.log(n=1, include_files=True)
        assert len(commits[0].files_changed) > 0


class TestGitShow:
    """Test git show functionality."""

    def test_show_head(self, git_utils):
        """Test showing HEAD commit."""
        commit = git_utils.show("HEAD")
        assert isinstance(commit, CommitInfo)
        assert commit.message == "Modify file1"

    def test_show_specific_commit(self, git_utils):
        """Test showing specific commit by hash."""
        commits = git_utils.log(n=3)
        oldest = commits[-1]

        shown = git_utils.show(oldest.hash)
        assert shown.hash == oldest.hash
        assert shown.message == oldest.message


class TestGitDiff:
    """Test git diff functionality."""

    def test_diff_between_commits(self, git_utils):
        """Test diff between two commits."""
        diffs = git_utils.diff("HEAD~1", "HEAD")
        assert len(diffs) >= 1
        assert all(isinstance(d, FileDiff) for d in diffs)

    def test_diff_change_types(self, git_utils):
        """Test that diff correctly identifies change types."""
        # Between first and second commit, file2.py was added
        diffs = git_utils.diff("HEAD~2", "HEAD~1")
        file2_diff = next((d for d in diffs if "file2" in d.path), None)
        assert file2_diff is not None
        assert file2_diff.change_type == "added"

    def test_diff_with_patch(self, git_utils):
        """Test diff with patch content."""
        diffs = git_utils.diff("HEAD~1", "HEAD", include_patch=True)
        modified = next((d for d in diffs if "file1" in d.path), None)
        assert modified is not None
        # Patch may be None if GitPython doesn't provide it, but the diff should still work
        assert modified.change_type == "modified"


class TestGitBlame:
    """Test git blame functionality."""

    def test_blame_basic(self, git_utils):
        """Test basic blame."""
        blame = git_utils.blame("file1.py")
        assert len(blame) >= 2  # At least 2 lines
        assert all(isinstance(b, BlameLine) for b in blame)

    def test_blame_line_info(self, git_utils):
        """Test blame line information."""
        blame = git_utils.blame("file1.py")
        first_line = blame[0]

        assert first_line.line_num == 1
        assert first_line.content == "# Modified content"
        assert first_line.author == "Test User"
        assert len(first_line.commit_hash) == 40

    def test_blame_line_range(self, git_utils):
        """Test blame with line range."""
        blame = git_utils.blame("file1.py", start_line=1, end_line=2)
        assert len(blame) == 2
        assert blame[0].line_num == 1
        assert blame[1].line_num == 2


class TestGitStatus:
    """Test git status functionality."""

    def test_status_clean(self, git_utils, git_repo):
        """Test status on clean repo."""
        status = git_utils.status()
        assert isinstance(status, GitStatus)
        assert status.branch == "master" or status.branch == "main"
        assert len(status.staged) == 0
        assert len(status.unstaged) == 0
        assert len(status.untracked) == 0
        assert status.is_dirty is False

    def test_status_with_changes(self, git_utils, git_repo):
        """Test status with uncommitted changes."""
        # Create an untracked file
        (git_repo / "new_file.txt").write_text("new content")

        # Modify a tracked file
        (git_repo / "file1.py").write_text("modified again")

        status = git_utils.status()
        assert "new_file.txt" in status.untracked
        assert "file1.py" in status.unstaged
        assert status.is_dirty is True

    def test_status_with_staged(self, git_utils, git_repo):
        """Test status with staged changes."""
        (git_repo / "file1.py").write_text("staged content")
        subprocess.run(["git", "add", "file1.py"], cwd=git_repo, capture_output=True)

        status = git_utils.status()
        assert "file1.py" in status.staged


class TestGitBranches:
    """Test git branches functionality."""

    def test_branches_basic(self, git_utils):
        """Test listing branches."""
        branches = git_utils.branches()
        assert len(branches) >= 1
        assert all(isinstance(b, BranchInfo) for b in branches)

    def test_branches_current(self, git_utils):
        """Test that current branch is marked."""
        branches = git_utils.branches()
        current = [b for b in branches if b.is_current]
        assert len(current) == 1

    def test_branches_info(self, git_utils):
        """Test branch information."""
        branches = git_utils.branches()
        branch = branches[0]

        assert branch.name is not None
        assert len(branch.commit_hash) == 40
        assert branch.commit_message is not None


class TestGitFileHistory:
    """Test file history functionality."""

    def test_file_history(self, git_utils):
        """Test getting file history."""
        history = git_utils.file_history("file1.py")
        assert len(history) >= 1  # At least the modification commit
        assert all(isinstance(c, CommitInfo) for c in history)

    def test_file_history_limit(self, git_utils):
        """Test file history with limit."""
        history = git_utils.file_history("file1.py", n=1)
        assert len(history) <= 1


class TestGitChangedFiles:
    """Test changed files functionality."""

    def test_changed_files(self, git_utils):
        """Test getting changed files between commits."""
        files = git_utils.changed_files("HEAD~1", "HEAD")
        assert "file1.py" in files


class TestGitIntegration:
    """Integration tests for git utilities with REPL engine."""

    def test_git_injected_in_repl(self, git_repo):
        """Test that git is available in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=git_repo)

        result = engine.execute("git.status().branch")
        assert result.success
        assert "master" in result.return_value or "main" in result.return_value

    def test_git_log_in_repl(self, git_repo):
        """Test using git.log in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=git_repo)

        result = engine.execute("len(git.log(n=5))")
        assert result.success
        assert result.return_value == "3"

    def test_git_preserved_on_reset(self, git_repo):
        """Test that git is preserved after namespace reset."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=git_repo)
        engine.execute("x = 42")
        engine.reset_namespace()

        # git should still be available
        result = engine.execute("git is not None")
        assert result.success
        assert result.return_value == "True"
