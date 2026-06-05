"""
IPC protocol between the kernel supervisor (parent / MCP server process) and
the kernel child (interpreter process owning the REPL namespace).

Transport: a pair of multiprocessing.Pipe duplex Connections carrying plain
dicts (JSON-shaped payloads only — Connection pickles, but we never rely on
pickle beyond builtins so frames stay debuggable and version-stable).

Two channels, deliberately:

  control:  parent → child  EXECUTE / RESET / PING / SHUTDOWN
            child  → parent RESULT / RESET_OK / PONG
  rpc:      child  → parent MCP_CALL / MCP_LIST / MCP_HELP / MCP_STATE
            parent → child  MCP_REPLY

While the child is mid-EXECUTE and issues an mcp.* call, it blocks on the rpc
channel for MCP_REPLY. The parent's rpc-servicing task is independent of the
task awaiting RESULT, so the round trip cannot deadlock. Correlation ids on
every frame keep replies matched.
"""

import itertools
import json
from dataclasses import dataclass, field
from typing import Any

# Control channel kinds
EXECUTE = "execute"        # payload: {code, timeout, reset}
RESULT = "result"          # payload: ExecutionResult.model_dump(mode="json")
RESET = "reset"            # payload: {}
RESET_OK = "reset_ok"      # payload: {}
PING = "ping"              # payload: {}
PONG = "pong"              # payload: {}
SHUTDOWN = "shutdown"      # payload: {}

# RPC channel kinds (child-initiated, parent replies with MCP_REPLY)
MCP_CALL = "mcp_call"      # payload: {server, tool, kwargs, timeout}
MCP_LIST = "mcp_list"      # payload: {server | None}
MCP_HELP = "mcp_help"      # payload: {server | None, tool | None}
MCP_STATE = "mcp_state"    # payload: {} → reply {servers, failed}
MCP_REPLY = "mcp_reply"    # payload: {ok: bool, value | error}

CONTROL_KINDS = frozenset({EXECUTE, RESULT, RESET, RESET_OK, PING, PONG, SHUTDOWN})
RPC_KINDS = frozenset({MCP_CALL, MCP_LIST, MCP_HELP, MCP_STATE, MCP_REPLY})

_ids = itertools.count(1)


def next_id(prefix: str) -> str:
    """Monotonic correlation id (unique within one process)."""
    return f"{prefix}-{next(_ids)}"


def ensure_json(value: Any, *, what: str) -> Any:
    """
    Validate that a value is JSON-serializable; raise a clear TypeError if not.

    Used at the proxy boundary: mcp.call kwargs cross the process boundary
    and must be plain data.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError) as e:
        raise TypeError(
            f"{what} must be JSON-serializable to cross the kernel boundary: {e}"
        ) from None
    return value


@dataclass
class Frame:
    """One message on either channel."""

    kind: str
    id: str
    payload: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {"kind": self.kind, "id": self.id, "payload": self.payload}

    @classmethod
    def from_wire(cls, data: dict) -> "Frame":
        if not isinstance(data, dict):
            raise ValueError(f"Malformed frame (not a dict): {data!r}")
        kind = data.get("kind")
        if kind not in CONTROL_KINDS and kind not in RPC_KINDS:
            raise ValueError(f"Unknown frame kind: {kind!r}")
        return cls(kind=kind, id=data.get("id", ""), payload=data.get("payload") or {})
