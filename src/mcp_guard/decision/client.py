"""How interceptors ask "may this tool call run?".

    proxy/gateway --(Unix socket, one JSON line each way)--> daemon --> popup

Every failure path returns BLOCK (fail-closed): daemon not running, socket
refused, garbled reply, timeout. A security control that fails open is a
control an attacker only has to crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from pydantic import ValidationError

from mcp_guard.config import Settings, static_blocklist
from mcp_guard.decision.models import ApprovalRequest, Decision, Verdict

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 2.0
REPLY_MARGIN = 5.0  # daemon answers at decision_timeout; allow a little slack


def _block(req: ApprovalRequest, reason: str) -> Decision:
    return Decision(approval_id=req.approval_id, verdict=Verdict.BLOCK, reason=reason)


async def request_decision(req: ApprovalRequest, settings: Settings | None = None) -> Decision:
    settings = settings or Settings.from_env()

    # 1. Static policy: no popup needed.
    blocked = static_blocklist()
    if req.tool in blocked or "*" in blocked:
        return _block(req, f"mcp-guard: tool '{req.tool}' on server '{req.server}' is blocked by policy.")

    # 2. Ask the daemon (which asks the human).
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(settings.socket_path)), CONNECT_TIMEOUT
        )
    except (OSError, TimeoutError) as exc:
        log.error("daemon unreachable at %s: %s", settings.socket_path, exc)
        return _block(req, "mcp-guard: approval daemon is not running, so the call was blocked (fail-closed).")

    try:
        writer.write(req.model_dump_json().encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), settings.decision_timeout + REPLY_MARGIN)
        if not line:
            raise ConnectionError("daemon closed the connection")
        decision = Decision.model_validate_json(line)
        if decision.approval_id != req.approval_id:
            raise ValueError("decision for a different request")
        return decision
    except (OSError, TimeoutError, ValidationError, ValueError) as exc:
        log.error("no valid decision for %s: %s", req.tool, exc)
        return _block(req, "mcp-guard: lost contact with the approval daemon, so the call was blocked.")
    finally:
        # Also runs on CancelledError (Claude cancelled the request): closing the
        # socket tells the daemon to dismiss the popup.
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
