"""Shell command helper for the REPL.

Provides sh(), a pre-injected helper that runs shell commands and returns
stdout as a str subclass, enabling one-call shell+Python composition:

    data = json.loads(sh("gh pr view 2822 --json statusCheckRollup"))
    count = int(sh("rg -c TODO src/", check=False) or 0)
"""

import os
import subprocess
from pathlib import Path
from typing import Optional, Union


class ShellError(RuntimeError):
    """Raised when a shell command exits nonzero and check=True.

    Carries the full result so failures stay inspectable:
        try:
            sh("false")
        except ShellError as e:
            print(e.returncode, e.stderr)
    """

    def __init__(self, cmd: str, returncode: int, stdout: str, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        summary = stderr.strip() or stdout.strip()
        if len(summary) > 500:
            summary = summary[:500] + "..."
        super().__init__(
            f"Command failed with exit code {returncode}: {cmd}"
            + (f"\n{summary}" if summary else "")
        )


class ShellResult(str):
    """Command stdout as a str, with execution metadata attached.

    Behaves exactly like the stdout string (json.loads(result), result.strip(),
    slicing all work), plus:
        .returncode  - process exit code
        .stderr      - captured stderr
        .ok          - True if returncode == 0
        .stdout      - same as str(self), for explicitness

    Note: str operations (slicing, strip, etc.) return plain str.
    """

    returncode: int
    stderr: str

    def __new__(cls, stdout: str, *, returncode: int, stderr: str) -> "ShellResult":
        obj = super().__new__(cls, stdout)
        obj.returncode = returncode
        obj.stderr = stderr
        return obj

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def stdout(self) -> str:
        return str(self)


def sh(
    cmd: str,
    *,
    check: bool = True,
    timeout: float = 120.0,
    cwd: Optional[Union[str, Path]] = None,
    env: Optional[dict] = None,
    _default_cwd: Optional[Path] = None,
) -> ShellResult:
    """Run a shell command and return its stdout as a ShellResult (str subclass).

    Args:
        cmd: Shell command (pipes, globs, && all work — runs with shell=True)
        check: Raise ShellError on nonzero exit (default True).
               Pass check=False to inspect .returncode/.ok/.stderr instead.
        timeout: Max seconds before subprocess.TimeoutExpired (default 120)
        cwd: Working directory (default: workspace root)
        env: Extra environment variables, merged over os.environ
        _default_cwd: Internal — workspace root bound by the engine

    Returns:
        ShellResult: stdout string with .returncode, .stderr, .ok attached

    Examples:
        data = json.loads(sh("gh pr list --json number,title"))
        files = sh("git ls-files '*.py'").splitlines()
        r = sh("pytest -q", check=False)
        if not r.ok: print(r.stderr[-500:])
    """
    merged_env = None
    if env:
        merged_env = {**os.environ, **env}

    proc = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else (str(_default_cwd) if _default_cwd else None),
        env=merged_env,
    )

    if check and proc.returncode != 0:
        raise ShellError(cmd, proc.returncode, proc.stdout, proc.stderr)

    return ShellResult(proc.stdout, returncode=proc.returncode, stderr=proc.stderr)


def make_sh(default_cwd: Optional[Path] = None):
    """Create an sh() bound to a default working directory (workspace root).

    Returns a function with the same signature and docstring as sh(), so
    help queries (sh?) and inspect work naturally in the REPL.
    """
    import functools

    @functools.wraps(sh)
    def bound_sh(cmd, *, check=True, timeout=120.0, cwd=None, env=None):
        return sh(
            cmd, check=check, timeout=timeout,
            cwd=cwd, env=env, _default_cwd=default_cwd,
        )

    return bound_sh
