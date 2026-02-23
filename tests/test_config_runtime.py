"""Tests for runtime/startup configuration resolution."""

import argparse
from pathlib import Path

from repl_mcp.config import RuntimeConfig, ServerConfig


def _make_args(**overrides) -> argparse.Namespace:
    defaults = {
        "transport": None,
        "host": None,
        "port": None,
        "config": None,
        "no_autoconnect": False,
        "debug": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_runtime_config_defaults(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_URL", raising=False)
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("FEATURE_BASH_ENABLED", raising=False)
    monkeypatch.delenv("FEATURE_UPLOAD_ENABLED", raising=False)
    monkeypatch.delenv("SESSION_IDLE_ACTION", raising=False)
    monkeypatch.delenv("SESSION_IDLE_TIMEOUT_MINUTES", raising=False)

    cfg = RuntimeConfig.from_env()

    assert cfg.daytona_enabled is False
    assert cfg.feature_bash_enabled is False
    assert cfg.feature_upload_enabled is True
    assert cfg.session_idle_action == "stop"
    assert cfg.session_idle_timeout_minutes == 30


def test_runtime_config_env_overrides(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_URL", "https://api.daytona.example")
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    monkeypatch.setenv("DAYTONA_SNAPSHOT", "python-default")
    monkeypatch.setenv("SESSION_IDLE_ACTION", "archive")
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_MINUTES", "45")
    monkeypatch.setenv("FEATURE_BASH_ENABLED", "1")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "2048")
    monkeypatch.setenv("EXEC_TIMEOUT_SECONDS", "180")

    cfg = RuntimeConfig.from_env()

    assert cfg.daytona_enabled is True
    assert cfg.daytona_api_url == "https://api.daytona.example"
    assert cfg.daytona_api_key == "test-key"
    assert cfg.daytona_snapshot == "python-default"
    assert cfg.session_idle_action == "archive"
    assert cfg.session_idle_timeout_minutes == 45
    assert cfg.feature_bash_enabled is True
    assert cfg.max_upload_bytes == 2048
    assert cfg.exec_timeout_seconds == 180.0


def test_runtime_config_requires_url_and_key_for_daytona(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_URL", "https://api.daytona.example")
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    cfg_no_key = RuntimeConfig.from_env()
    assert cfg_no_key.daytona_enabled is False

    monkeypatch.delenv("DAYTONA_API_URL", raising=False)
    monkeypatch.setenv("DAYTONA_API_KEY", "test-key")
    cfg_no_url = RuntimeConfig.from_env()
    assert cfg_no_url.daytona_enabled is False


def test_startup_config_uses_env_when_cli_omitted(monkeypatch):
    monkeypatch.setenv("REPL_MCP_TRANSPORT", "sse")
    monkeypatch.setenv("REPL_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("REPL_MCP_PORT", "9000")
    monkeypatch.setenv("REPL_MCP_CONFIG", "/tmp/custom.json")
    monkeypatch.setenv("REPL_MCP_AUTOCONNECT", "false")
    monkeypatch.setenv("REPL_MCP_DEBUG", "true")

    cfg = ServerConfig.from_sources(_make_args())

    assert cfg.transport == "sse"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9000
    assert cfg.config_path == Path("/tmp/custom.json")
    assert cfg.autoconnect is False
    assert cfg.debug is True


def test_startup_config_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("REPL_MCP_TRANSPORT", "sse")
    monkeypatch.setenv("REPL_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("REPL_MCP_PORT", "9000")
    monkeypatch.setenv("REPL_MCP_CONFIG", "/tmp/custom.json")

    args = _make_args(
        transport="stdio",
        host="0.0.0.0",
        port=7777,
        config=Path(".mcp.json"),
        no_autoconnect=True,
        debug=True,
    )
    cfg = ServerConfig.from_sources(args)

    assert cfg.transport == "stdio"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 7777
    assert cfg.config_path == Path(".mcp.json")
    assert cfg.autoconnect is False
    assert cfg.debug is True
