"""Git utilities for REPL MCP."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from git import Repo, InvalidGitRepositoryError, GitCommandError, Commit
from git.diff import Diff

from .base import REPLUtility, NotAGitRepoError, GitRefNotFoundError, GitUtilsError
from ..models import (
    CommitInfo,
    FileDiff,
    BlameLine,
    GitStatus,
    BranchInfo,
    TagInfo,
)


class GitUtils(REPLUtility):
    """Git repository utilities for code analysis.

    Example usage in REPL:
        git.log(n=5)
        git.status()
        git.diff("main", "feature")
        git.blame("src/main.py")
    """

    def __init__(self, repo_path: Union[str, Path]):
        """Initialize git utilities for a repository.

        Args:
            repo_path: Path to git repository (or any path within it)

        Raises:
            InvalidGitRepositoryError: If path is not in a git repository
        """
        self.repo_path = Path(repo_path).resolve()
        self.repo = Repo(self.repo_path, search_parent_directories=True)
        self.working_dir = Path(self.repo.working_dir)

    def _commit_to_info(self, commit: Commit, include_files: bool = False) -> CommitInfo:
        """Convert GitPython commit to CommitInfo model.

        Args:
            commit: GitPython Commit object
            include_files: Include list of changed files (slower)

        Returns:
            CommitInfo model instance
        """
        return CommitInfo(
            hash=commit.hexsha,
            short_hash=commit.hexsha[:7],
            message=commit.message.split("\n")[0],
            full_message=commit.message,
            author_name=commit.author.name,
            author_email=commit.author.email,
            authored_date=datetime.fromtimestamp(commit.authored_date, tz=timezone.utc),
            committer_name=commit.committer.name,
            committer_email=commit.committer.email,
            committed_date=datetime.fromtimestamp(commit.committed_date, tz=timezone.utc),
            files_changed=list(commit.stats.files.keys()) if include_files else [],
        )

    def log(
        self,
        n: int = 10,
        since: Optional[str] = None,
        until: Optional[str] = None,
        path: Optional[str] = None,
        author: Optional[str] = None,
        branch: Optional[str] = None,
        grep: Optional[str] = None,
        include_files: bool = False,
    ) -> list[CommitInfo]:
        """Get commit log.

        Args:
            n: Maximum number of commits to return (default: 10)
            since: Only commits after this date (e.g., "2024-01-01", "1 week ago")
            until: Only commits before this date
            path: Only commits affecting this path
            author: Filter by author name/email
            branch: Branch to get log from (default: current branch)
            grep: Filter by commit message pattern
            include_files: Include list of changed files (slower)

        Returns:
            List of CommitInfo objects
        """
        kwargs = {"max_count": n}

        if since:
            kwargs["since"] = since
        if until:
            kwargs["until"] = until
        if author:
            kwargs["author"] = author
        if grep:
            kwargs["grep"] = grep

        # Determine what to iterate
        rev = branch if branch else "HEAD"
        if path:
            commits = self.repo.iter_commits(rev, paths=path, **kwargs)
        else:
            commits = self.repo.iter_commits(rev, **kwargs)

        return [self._commit_to_info(commit, include_files) for commit in commits]

    def show(self, ref: str = "HEAD", include_files: bool = True) -> CommitInfo:
        """Get details of a specific commit.

        Args:
            ref: Commit reference (hash, branch, tag, or HEAD)
            include_files: Include list of changed files

        Returns:
            CommitInfo for the specified commit
        """
        commit = self.repo.commit(ref)
        return self._commit_to_info(commit, include_files)

    def diff(
        self,
        from_ref: str = "HEAD",
        to_ref: Optional[str] = None,
        path: Optional[str] = None,
        include_patch: bool = False,
    ) -> list[FileDiff]:
        """Get diff between refs or working tree.

        Args:
            from_ref: Base commit/branch (default: HEAD)
            to_ref: Target commit/branch (default: working tree)
            path: Only show diff for this path
            include_patch: Include unified diff patch (can be large)

        Returns:
            List of FileDiff objects
        """
        from_commit = self.repo.commit(from_ref)

        if to_ref:
            to_commit = self.repo.commit(to_ref)
            diffs = from_commit.diff(to_commit, paths=path if path else None)
        else:
            # Diff against working tree (staged + unstaged)
            diffs = from_commit.diff(None, paths=path if path else None)

        results = []
        for d in diffs:
            change_type = self._diff_change_type(d)

            file_diff = FileDiff(
                path=d.b_path or d.a_path,
                change_type=change_type,
                old_path=d.a_path if change_type in ("renamed", "copied") else None,
                additions=0,  # GitPython doesn't provide this easily
                deletions=0,
                patch=d.diff.decode("utf-8", errors="replace") if include_patch and d.diff else None,
            )
            results.append(file_diff)

        return results

    def _diff_change_type(self, d: Diff) -> str:
        """Convert GitPython diff change type to our enum."""
        if d.new_file:
            return "added"
        elif d.deleted_file:
            return "deleted"
        elif d.renamed_file:
            return "renamed"
        elif d.copied_file:
            return "copied"
        else:
            return "modified"

    def blame(
        self,
        path: str,
        rev: str = "HEAD",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> list[BlameLine]:
        """Get git blame for a file.

        Args:
            path: Path to file (relative to repo root)
            rev: Revision to blame (default: HEAD)
            start_line: Start line number (1-indexed, optional)
            end_line: End line number (1-indexed, optional)

        Returns:
            List of BlameLine objects
        """
        # Build blame command args
        kwargs = {}
        if start_line and end_line:
            kwargs["L"] = f"{start_line},{end_line}"

        blame_data = self.repo.blame(rev, path, **kwargs)

        results = []
        line_num = start_line or 1

        for commit, lines in blame_data:
            for line in lines:
                blame_line = BlameLine(
                    line_num=line_num,
                    content=line,
                    commit_hash=commit.hexsha,
                    short_hash=commit.hexsha[:7],
                    author=commit.author.name,
                    author_email=commit.author.email,
                    date=datetime.fromtimestamp(commit.authored_date, tz=timezone.utc),
                )
                results.append(blame_line)
                line_num += 1

        return results

    def status(self) -> GitStatus:
        """Get current repository status.

        Returns:
            GitStatus with branch, staged, unstaged, untracked files
        """
        # Get current branch
        try:
            branch = self.repo.active_branch.name
        except TypeError:
            # Detached HEAD
            branch = f"HEAD detached at {self.repo.head.commit.hexsha[:7]}"

        # Get tracking branch info
        tracking_branch = None
        ahead = 0
        behind = 0

        try:
            tracking = self.repo.active_branch.tracking_branch()
            if tracking:
                tracking_branch = tracking.name
                # Count ahead/behind
                ahead = len(list(self.repo.iter_commits(f"{tracking.name}..HEAD")))
                behind = len(list(self.repo.iter_commits(f"HEAD..{tracking.name}")))
        except (TypeError, GitCommandError):
            pass

        # Get file status
        staged = []
        unstaged = []
        untracked = list(self.repo.untracked_files)

        # Staged changes (index vs HEAD)
        for item in self.repo.index.diff("HEAD"):
            staged.append(item.a_path or item.b_path)

        # Unstaged changes (working tree vs index)
        for item in self.repo.index.diff(None):
            unstaged.append(item.a_path or item.b_path)

        return GitStatus(
            branch=branch,
            tracking_branch=tracking_branch,
            staged=staged,
            unstaged=unstaged,
            untracked=untracked,
            ahead=ahead,
            behind=behind,
            is_dirty=self.repo.is_dirty(untracked_files=True),
        )

    def branches(self, include_remote: bool = False) -> list[BranchInfo]:
        """List all branches.

        Args:
            include_remote: Include remote tracking branches

        Returns:
            List of BranchInfo objects
        """
        results = []

        # Local branches
        for branch in self.repo.branches:
            tracking = None
            try:
                if branch.tracking_branch():
                    tracking = branch.tracking_branch().name
            except (TypeError, AttributeError):
                pass

            info = BranchInfo(
                name=branch.name,
                is_current=branch == self.repo.active_branch,
                is_remote=False,
                tracking_branch=tracking,
                commit_hash=branch.commit.hexsha,
                commit_message=branch.commit.message.split("\n")[0],
            )
            results.append(info)

        # Remote branches
        if include_remote:
            for ref in self.repo.remote().refs:
                info = BranchInfo(
                    name=ref.name,
                    is_current=False,
                    is_remote=True,
                    tracking_branch=None,
                    commit_hash=ref.commit.hexsha,
                    commit_message=ref.commit.message.split("\n")[0],
                )
                results.append(info)

        return results

    def file_history(
        self,
        path: str,
        n: int = 10,
    ) -> list[CommitInfo]:
        """Get commit history for a specific file.

        Args:
            path: Path to file (relative to repo root)
            n: Maximum number of commits

        Returns:
            List of CommitInfo objects
        """
        commits = self.repo.iter_commits("HEAD", paths=path, max_count=n)
        return [self._commit_to_info(commit) for commit in commits]

    def changed_files(
        self,
        from_ref: str = "HEAD~1",
        to_ref: str = "HEAD",
    ) -> list[str]:
        """Get list of files changed between two refs.

        Args:
            from_ref: Base reference (default: previous commit)
            to_ref: Target reference (default: HEAD)

        Returns:
            List of file paths that changed
        """
        from_commit = self.repo.commit(from_ref)
        to_commit = self.repo.commit(to_ref)

        diffs = from_commit.diff(to_commit)
        return [d.b_path or d.a_path for d in diffs]

    def tags(self, n: int = 20) -> list[TagInfo]:
        """Get list of tags.

        Args:
            n: Maximum number of tags

        Returns:
            List of TagInfo objects
        """
        results = []
        for tag in sorted(self.repo.tags, key=lambda t: t.commit.committed_date, reverse=True)[:n]:
            results.append(TagInfo(
                name=tag.name,
                commit_hash=tag.commit.hexsha,
                commit_message=tag.commit.message.split("\n")[0],
                date=datetime.fromtimestamp(tag.commit.committed_date, tz=timezone.utc),
            ))
        return results

    def __repr__(self) -> str:
        return f"GitUtils(repo={self.working_dir})"
