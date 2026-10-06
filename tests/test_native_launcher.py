"""Published-wheel selector tests; no release download or real uvx execution."""

import json
import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "repl-mcp-launch.sh"


def fake_environment(tmp_path, os_name, architecture, include_uvx=True):
    uname = tmp_path / "uname"
    uname.write_text('#!/bin/sh\ncase "$1" in -s) echo "$FAKE_OS";; -m) echo "$FAKE_ARCH";; esac\n')
    uname.chmod(0o755)
    if include_uvx:
        uvx = tmp_path / "uvx"
        uvx.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        uvx.chmod(0o755)
    return {**os.environ, "PATH": str(tmp_path), "FAKE_OS": os_name, "FAKE_ARCH": architecture}


@pytest.mark.parametrize(("os_name", "architecture", "platform"), [
    ("Darwin", "arm64", "macosx_11_0_arm64"),
    ("Darwin", "x86_64", "macosx_11_0_x86_64"),
    ("Linux", "x86_64", "manylinux_2_28_x86_64"),
])
def test_published_wheel_launch(tmp_path, os_name, architecture, platform):
    result = subprocess.run(
        ["/bin/sh", str(LAUNCHER), "--transport", "stdio", "--config", "a file.json"],
        env=fake_environment(tmp_path, os_name, architecture),
        cwd=tmp_path, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "--from", f"https://github.com/iota-uz/repl-mcp/releases/download/v3.1.0/"
        f"repl_mcp-3.1.0-py3-none-{platform}.whl",
        "repl-mcp", "--transport", "stdio", "--config", "a file.json",
    ]
    assert not result.stderr


@pytest.mark.parametrize(("os_name", "architecture"), [("Linux", "aarch64"), ("Windows_NT", "x86_64")])
def test_unsupported_host(tmp_path, os_name, architecture):
    result = subprocess.run(
        ["/bin/sh", str(LAUNCHER)], env=fake_environment(tmp_path, os_name, architecture),
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 1
    assert "unsupported host" in result.stderr and "Linux x86_64" in result.stderr
    assert not result.stdout


def test_missing_uvx(tmp_path):
    result = subprocess.run(
        ["/bin/sh", str(LAUNCHER)], env=fake_environment(tmp_path, "Darwin", "arm64", False),
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 1
    assert "uvx is required" in result.stderr
    assert not result.stdout


def test_manifest_launches_native_wheel_selector():
    manifest = json.loads((LAUNCHER.parents[1] / ".claude-plugin" / "plugin.json").read_text())
    server = manifest["mcpServers"]["python-repl"]
    assert server["command"] == "sh"
    assert server["args"] == ["-c", LAUNCHER.read_text(), "repl-mcp-plugin", "--transport", "stdio"]


def test_manifest_preserves_arguments_without_plugin_root(tmp_path):
    manifest = json.loads((LAUNCHER.parents[1] / ".claude-plugin" / "plugin.json").read_text())
    server = manifest["mcpServers"]["python-repl"]
    result = subprocess.run(
        ["/bin/sh", *server["args"]], env=fake_environment(tmp_path, "Darwin", "arm64"),
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-3:] == ["repl-mcp", "--transport", "stdio"]
