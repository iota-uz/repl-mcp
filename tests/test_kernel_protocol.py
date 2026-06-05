"""Loopback unit tests for the kernel IPC protocol (no subprocess)."""

import pytest

from repl_mcp.kernel.protocol import (
    EXECUTE, RESULT, MCP_CALL, MCP_REPLY,
    Frame, ensure_json, next_id,
)


class TestFrame:
    def test_round_trip(self):
        f = Frame(EXECUTE, "exec-1", {"code": "x=1", "timeout": 5.0, "reset": False})
        wire = f.to_wire()
        back = Frame.from_wire(wire)
        assert back == f

    def test_all_kinds_round_trip(self):
        for kind in (EXECUTE, RESULT, MCP_CALL, MCP_REPLY):
            assert Frame.from_wire(Frame(kind, "id-1").to_wire()).kind == kind

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="Unknown frame kind"):
            Frame.from_wire({"kind": "bogus", "id": "1", "payload": {}})

    def test_malformed_rejected(self):
        with pytest.raises(ValueError):
            Frame.from_wire("not a dict")

    def test_missing_payload_defaults_empty(self):
        f = Frame.from_wire({"kind": EXECUTE, "id": "1"})
        assert f.payload == {}


class TestCorrelationIds:
    def test_ids_unique_and_prefixed(self):
        a, b = next_id("exec"), next_id("rpc")
        assert a != b
        assert a.startswith("exec-")
        assert b.startswith("rpc-")


class TestEnsureJson:
    def test_plain_data_passes(self):
        assert ensure_json({"a": [1, "x", None]}, what="args") == {"a": [1, "x", None]}

    def test_non_serializable_raises_clear_error(self):
        with pytest.raises(TypeError, match="JSON-serializable"):
            ensure_json({"conn": object()}, what="mcp.call() arguments")
