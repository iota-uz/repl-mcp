"""Tests for the sh() shell helper."""

import json
import subprocess

import pytest

from repl_mcp.utilities.shell import sh, make_sh, ShellResult, ShellError
from repl_mcp.repl_engine import REPLEngine, RESERVED_NAMES


class TestShellResult:
    def test_basic_echo(self):
        result = sh("echo hi")
        assert result == "hi\n"
        assert result.ok is True
        assert result.returncode == 0
        assert result.stderr == ""

    def test_is_str_subclass(self):
        result = sh("echo hi")
        assert isinstance(result, str)
        assert isinstance(result, ShellResult)

    def test_json_loads_roundtrip(self):
        result = sh("printf '{\"a\": 1, \"b\": [2, 3]}'")
        data = json.loads(result)
        assert data == {"a": 1, "b": [2, 3]}

    def test_stdout_property(self):
        result = sh("echo hi")
        assert result.stdout == str(result) == "hi\n"

    def test_str_methods_return_plain_str(self):
        result = sh("echo hi")
        assert result.strip() == "hi"
        # Sliced/derived values are plain str (documented behavior)
        assert isinstance(result.strip(), str)

    def test_stderr_captured(self):
        result = sh("echo err >&2")
        assert result == ""
        assert result.stderr == "err\n"
        assert result.ok is True


class TestShellErrors:
    def test_nonzero_raises_shell_error(self):
        with pytest.raises(ShellError) as exc_info:
            sh("echo out; echo err >&2; exit 3")
        err = exc_info.value
        assert err.returncode == 3
        assert err.stdout == "out\n"
        assert err.stderr == "err\n"
        assert "exit code 3" in str(err)

    def test_check_false_returns_result(self):
        result = sh("exit 4", check=False)
        assert result.ok is False
        assert result.returncode == 4

    def test_timeout_raises(self):
        with pytest.raises(subprocess.TimeoutExpired):
            sh("sleep 5", timeout=0.2)


class TestShellCwd:
    def test_explicit_cwd(self, tmp_path):
        result = sh("pwd", cwd=tmp_path)
        assert result.strip() == str(tmp_path.resolve())

    def test_default_cwd_from_factory(self, tmp_path):
        bound = make_sh(tmp_path)
        assert bound("pwd").strip() == str(tmp_path.resolve())

    def test_explicit_cwd_overrides_default(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        bound = make_sh(tmp_path)
        assert bound("pwd", cwd=sub).strip() == str(sub.resolve())

    def test_env_merges_over_os_environ(self):
        result = sh("echo $MY_TEST_VAR-$HOME", env={"MY_TEST_VAR": "xyz"})
        # Custom var present AND inherited environment (HOME) survives
        assert result.startswith("xyz-/")


class TestShellInEngine:
    def test_sh_injected(self):
        engine = REPLEngine()
        result = engine.execute("sh('echo from-repl').strip()")
        assert result.success
        assert "from-repl" in result.return_value

    def test_sh_is_reserved(self):
        assert "sh" in RESERVED_NAMES

    def test_sh_survives_reset(self):
        engine = REPLEngine()
        engine.execute("x = 1")
        engine.reset_namespace()
        result = engine.execute("sh('echo still-here').strip()")
        assert result.success
        assert "still-here" in result.return_value

    def test_sh_help_query_shows_docstring(self):
        engine = REPLEngine()
        result = engine.execute("sh?")
        assert result.success
        assert "shell command" in result.stdout.lower() or "shell command" in (result.return_value or "").lower()

    def test_sh_default_cwd_is_workspace_root(self, tmp_path):
        engine = REPLEngine(workspace_root=tmp_path)
        result = engine.execute("sh('pwd').strip()")
        assert result.success
        assert str(tmp_path.resolve()) in result.return_value
