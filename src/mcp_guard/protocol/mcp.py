"""MCP-specific knowledge: which messages need approval and what's inside them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_guard.protocol.jsonrpc import META_PROTOCOL_VERSION, JsonDict

# Only tool invocations change the world. Everything else (initialize,
# tools/list, pings, notifications) passes straight through.
INTERCEPTED_METHODS = frozenset({"tools/call"})


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The parts of a tools/call request a human needs to make a decision.

    `frozen=True` makes it immutable (like `Readonly<T>`); `slots=True` makes
    it smaller and faster. A dataclass is fine here because this data comes
    from our own parsing. Untrusted data crossing a process boundary gets
    pydantic instead (see decision/models.py).
    """

    request_id: str | int
    tool: str
    arguments: dict[str, Any]
    protocol_version: str | None = None  # set only by 2026+ stateless requests


def extract_tool_call(msg: JsonDict) -> ToolCall | None:
    """Return a ToolCall if `msg` is a request we must gate, else None."""
    if msg.get("method") not in INTERCEPTED_METHODS or "id" not in msg:
        return None

    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}

    name = params.get("name")
    args = params.get("arguments") or {}
    meta = params.get("_meta")
    version = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
    return ToolCall(
        request_id=msg["id"],
        tool=str(name) if name is not None else "<unknown>",
        arguments=args if isinstance(args, dict) else {"_raw": args},
        protocol_version=version if isinstance(version, str) else None,
    )
