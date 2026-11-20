"""JSON-RPC 2.0 helpers.

Pure functions only: no I/O, no asyncio. That makes this the easiest module to
unit-test, so as much logic as possible lives here.

MCP's stdio transport sends one JSON object per line (newline-delimited JSON).
"""

from __future__ import annotations

import json
from typing import Any

JsonDict = dict[str, Any]  # type alias, like `type JsonDict = Record<string, unknown>`


def parse_line(line: bytes) -> JsonDict | None:
    """Decode one line into a JSON object.

    Returns None for anything we don't understand (bad JSON, arrays, numbers).
    The caller passes those through untouched: a proxy should never break
    traffic it can't read.
    """
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


def parse_batch(line: bytes) -> list[Any] | None:
    """Return the items if `line` is a JSON-RPC batch (a JSON array), else None."""
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, list) else None


def encode(msg: JsonDict | list[Any]) -> bytes:
    """Serialise a message as one newline-terminated line."""
    return (json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def is_request(msg: JsonDict) -> bool:
    """Requests have a method and an id and expect a response."""
    return "method" in msg and "id" in msg


def is_notification(msg: JsonDict) -> bool:
    """Notifications have a method but no id: fire-and-forget."""
    return "method" in msg and "id" not in msg


# MCP 2026-07-28 made the protocol stateless: there is no initialize handshake,
# and every request carries its version in params._meta under this key.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
STATELESS_SPEC = "2026-07-28"


def make_blocked_result(request_id: str | int, reason: str, protocol_version: str | None = None) -> JsonDict:
    """Build the response the proxy sends to Claude when a tool call is blocked.

    We answer with a *successful* JSON-RPC response whose MCP result is a tool
    error (isError: true), not a JSON-RPC `error` object. Claude then shows the
    reason to the model, which can explain to the user that the call was blocked.

    The shape must match the spec version of the request we're answering:
      - 2026-07-28+ requires   "resultType": "complete"
      - older versions forbid unknown keys in strict clients, so omit it.
    Version strings are ISO dates, so plain string comparison orders them.
    """
    result: JsonDict = {"isError": True, "content": [{"type": "text", "text": reason}]}
    if protocol_version is not None and protocol_version >= STATELESS_SPEC:
        result["resultType"] = "complete"
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
