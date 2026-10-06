"""Read-only broker explanations and per-project provenance; no real credentials."""

import json
import sys
from pathlib import Path

import pytest

from tests.test_native_protocol import NativeClient, binary as native_binary_fixture

binary = native_binary_fixture
PEER = Path(__file__).parent / "fixtures" / "native_mcp_fixture.py"


def registry(tmp_path, entries):
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": entries}))
    return config


def explain(client, server, tool=None):
    result = client.execute(f"mcp.explain({server!r}, {tool!r})")
    assert not result["isError"], result
    return result["structuredContent"]["value"]


def test_explanation_distinguishes_absent_registry_and_disabled_broker(binary, tmp_path):
    client = NativeClient(binary, tmp_path, mcp_scope="project")
    try:
        info = explain(client, "host-connector")
        assert info["status"] == "registry_absent", info
        assert info["host_client_route"]["availability"] == "unknown"
        assert info["host_client_route"]["discoverable_by_broker"] is False
        assert info["network_probe_performed"] is False
        assert Path(info["provenance"]["project"]).resolve() == tmp_path.resolve()
    finally:
        client.close()
    disabled = NativeClient(binary, tmp_path, mcp_scope="none")
    try:
        assert explain(disabled, "host-connector")["status"] == "broker_disabled"
    finally:
        disabled.close()


def test_explanation_grants_and_tool_catalogue_without_implicit_connect(binary, tmp_path):
    config = registry(tmp_path, {
        "peer": {"command": sys.executable, "args": [str(PEER.resolve())], "allowedTools": ["echo", "missing"]},
        "disabled": {"disabled": True, "command": sys.executable},
    })
    client = NativeClient(binary, tmp_path, config)
    try:
        assert explain(client, "disabled")["status"] == "not_granted"
        assert explain(client, "unknown")["status"] == "server_absent"
        assert explain(client, "peer", "fail")["status"] == "not_granted"
        assert explain(client, "peer", "echo")["status"] == "disconnected"
        assert client.request("tools/call", {"name": "python_health", "arguments": {}})["structuredContent"]["broker"]["connected_transports"] == 0
        denied = client.execute("mcp.call('peer', 'fail')")
        assert denied["isError"]
        assert client.request("tools/call", {"name": "python_health", "arguments": {}})["structuredContent"]["broker"]["connected_transports"] == 0
        assert not client.execute("mcp.list_tools('peer')")["isError"]
        assert explain(client, "peer", "echo")["status"] == "ready"
        missing = explain(client, "peer", "missing")
        assert missing["status"] == "tool_missing" and missing["evidence"] == "cached_catalogue"
    finally:
        client.close()


def test_explanation_missing_credentials_redacts_environment_and_remains_available(binary, tmp_path):
    secret_marker = "DO_NOT_EXPOSE_credential_placeholder_328129"
    config = registry(tmp_path, {"peer": {"type": "http", "url": "http://127.0.0.1:1/mcp",
                                          "headers": {"Authorization": "Bearer ${" + secret_marker + "}"}}})
    client = NativeClient(binary, tmp_path, config)
    try:
        info = explain(client, "peer")
        assert info["status"] == "credentials_missing"
        assert secret_marker not in json.dumps(info)
        assert not client.execute("2 + 2")["isError"]
    finally:
        client.close()


def test_explanation_unsaved_oauth_grant_has_no_network_or_store_side_effects(binary, tmp_path):
    path = tmp_path / "uncreated" / "tokens.json"
    config = registry(tmp_path, {"peer": {"type": "http", "url": "http://127.0.0.1:1/mcp",
                                          "oauth": {"credentialFile": str(path)}}})
    client = NativeClient(binary, tmp_path, config)
    try:
        info = explain(client, "peer")
        assert info["status"] == "credentials_missing" and not info["network_probe_performed"]
        assert not path.parent.exists(), "Diagnostic created a credential store"
    finally:
        client.close()


@pytest.mark.parametrize("bad_tool", [1, [], {}])
def test_diagnostic_argument_errors_preserve_next_cell(binary, tmp_path, bad_tool):
    config = registry(tmp_path, {})
    client = NativeClient(binary, tmp_path, config)
    try:
        failed = client.execute(f"mcp.explain('peer', {bad_tool!r})")
        assert failed["isError"], failed
        assert client.execute("6 * 7")["structuredContent"]["value"] == 42
    finally:
        client.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix FIFO regression")
def test_fifo_registry_startup_and_refresh_fail_closed_without_blocking_python(binary, tmp_path):
    import os

    config = tmp_path / ".mcp.json"
    os.mkfifo(config)
    client = NativeClient(binary, tmp_path, config)
    try:
        assert client.execute("6 * 7")["structuredContent"]["value"] == 42
        assert explain(client, "peer")["status"] == "config_invalid"
        config.unlink()
        config.write_text('{"mcpServers": {}}')
        assert not client.execute("mcp.refresh()")["isError"]
        config.unlink()
        os.mkfifo(config)
        result = client.execute("mcp.refresh()")
        assert result["isError"]
        assert "regular file" in json.dumps(result)
        assert client.execute("40 + 2")["structuredContent"]["value"] == 42
    finally:
        client.close()
