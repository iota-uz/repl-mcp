"""Real native artifact ownership, integrity and bounded-context acceptance."""
import hashlib
import json
import os
import sys

import pytest

from tests.test_native_protocol import NativeClient, ROOT, binary as native_binary_fixture

binary = native_binary_fixture


@pytest.fixture
def native(binary, tmp_path):
    client = NativeClient(binary, tmp_path)
    yield client
    client.close()


def call(native, tool_name, **arguments):
    return native.request("tools/call", {"name": tool_name, "arguments": arguments})


def session(native):
    return call(native, "python_health")["structuredContent"]["server"]["session_id"]


def test_auto_large_json_survives_reset_saved_sha_and_bounded_range(native, tmp_path):
    owner = session(native)
    result = native.execute("[{'number': i, 'text': 'hello'} for i in range(3000)]", session_id=owner)
    assert not result["isError"]
    reference = result["structuredContent"]["artifact"]
    assert reference["format"] == "json" and reference["size"] > 20000
    assert "value" not in result["structuredContent"]
    assert len(json.dumps(result).encode()) < 100000
    assert not native.execute("42", session_id=owner, reset=True)["isError"]
    saved = tmp_path / "result.json"
    assert not call(native, "artifact_save", session_id=owner, id=reference["id"], path=str(saved))["isError"]
    content = saved.read_bytes()
    assert hashlib.sha256(content).hexdigest() == reference["sha256"]
    assert len(content) == reference["size"] and json.loads(content)[2999]["number"] == 2999
    range_result = call(native, "artifact_read", session_id=owner, id=reference["id"], offset=0, length=100)["structuredContent"]
    assert range_result["length"] == 100 and range_result["has_more"]
    assert range_result["content"] == content[:100].decode()
    with pytest.raises(AssertionError, match="Invalid arguments at /length"):
        call(native, "artifact_read", session_id=owner, id=reference["id"], length=65537)
    assert call(native, "artifact_save", session_id=owner, id=reference["id"], path=str(saved))["isError"]
    assert not call(native, "artifact_save", session_id=owner, id=reference["id"], path=str(saved), overwrite=True)["isError"]
    assert not call(native, "artifact_delete", session_id=owner, id=reference["id"])["isError"]
    assert call(native, "artifact_read", session_id=owner, id=reference["id"])["isError"]


def test_binary_explicit_ranges_and_forward_without_context_payload(binary, tmp_path):
    config = tmp_path / "registry.json"
    config.write_text(json.dumps({"mcpServers": {"fixture": {"command": sys.executable,
                                 "args": [str(ROOT / "tests/fixtures/native_mcp_fixture.py")],
                                 "allowedTools": ["echo"]}}}))
    native = NativeClient(binary, tmp_path, config)
    try:
        owner = session(native)
        source = tmp_path / "data.bin"
        content = bytes(range(256)) * 100
        source.write_bytes(content)
        reference = call(native, "artifact_create", session_id=owner, path=str(source), format="binary")["structuredContent"]
        assert reference["sha256"] == hashlib.sha256(content).hexdigest()
        source.unlink()
        result = call(native, "artifact_read", session_id=owner, id=reference["id"], offset=250, length=12, encoding="hex")["structuredContent"]
        assert result["content"] == content[250:262].hex()
        assert call(native, "artifact_forward", session_id=owner, id=reference["id"], server="fixture", tool="echo", argument="text", format="text")["isError"]
        result = call(native, "artifact_forward", session_id=owner, id=reference["id"], server="fixture", tool="echo", argument="text", format="base64")
        assert not result["isError"]
        import base64
        forwarded = result["structuredContent"]["structuredContent"]["arguments"]["text"]
        assert base64.b64decode(forwarded) == content
        # Exercise the injected helper routing, not only the public MCP tool.
        helper = native.execute(f"import base64\nreply = artifact_forward({reference['id']!r}, 'fixture', 'echo', 'text', format='base64')\nlen(base64.b64decode(reply['structuredContent']['arguments']['text']))", session_id=owner)
        assert not helper["isError"] and helper["structuredContent"]["value"] == len(content)
    finally:
        native.close()


def test_owner_isolation_close_and_failed_import_keep_prior_artifact(native, tmp_path):
    owner = call(native, "python_session_open", name="artifact-owner", project=str(tmp_path))["structuredContent"]["session_id"]
    source = tmp_path / "small.txt"
    source.write_text("hello")
    first = None
    for _ in range(64):
        reference = call(native, "artifact_create", session_id=owner, path=str(source), format="text")["structuredContent"]
        first = first or reference["id"]
    assert call(native, "artifact_create", session_id=owner, path=str(source), format="json")["isError"]
    assert call(native, "artifact_read", session_id=owner, id=first)["structuredContent"]["content"] == "hello"
    other = call(native, "python_session_open", name="other", project=str(tmp_path))["structuredContent"]["session_id"]
    assert call(native, "artifact_read", session_id=other, id=first)["isError"]
    latest = call(native, "artifact_create", session_id=owner, path=str(source), format="text")["structuredContent"]
    assert call(native, "artifact_read", session_id=owner, id=first)["isError"]
    assert not call(native, "python_session_close", session_id=owner)["isError"]
    with pytest.raises(AssertionError, match="Session not found or closed"):
        call(native, "artifact_read", session_id=owner, id=latest["id"])


@pytest.mark.skipif(os.name != "posix", reason="Supported native targets are Unix")
def test_fifo_import_returns_and_does_not_exhaust_native_jobs(native, tmp_path):
    owner = session(native)
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    for _ in range(5):
        assert call(native, "artifact_create", session_id=owner, path=str(fifo), format="binary")["isError"]
    source = tmp_path / "ok.txt"
    source.write_text("still healthy")
    assert not call(native, "artifact_create", session_id=owner, path=str(source), format="text")["isError"]


@pytest.mark.parametrize("mode", ["persistent", "fresh"])
@pytest.mark.parametrize("expression,status", [("0", 0), ("2", 2), ("'stopped'", 1)])
def test_saved_file_exit_semantics_fresh_and_persistent(native, tmp_path, mode, expression, status):
    owner = session(native)
    assert not native.execute("marker = 7", session_id=owner)["isError"]
    source = tmp_path / "exit.py"
    source.write_text(f"import sys\nmarker = 42\nsys.exit({expression})\nmarker = 99\n")
    result = call(native, "python_execute_file", session_id=owner, path=str(source), mode=mode)
    assert result["isError"] is (status != 0)
    assert result["structuredContent"]["exit_code"] == status
    assert native.execute("marker", session_id=owner)["structuredContent"]["value"] == (42 if mode == "persistent" else 7)
