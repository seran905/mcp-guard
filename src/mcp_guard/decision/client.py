"""How interceptors ask "may this tool call run?".

M1 version: a stub policy driven by an environment variable, so the proxy can
be built and tested before the daemon exists:

    MCP_GUARD_BLOCK="send_email,delete_note"

M3 replaces the body with a call to the daemon (which shows the popup).
The function signature stays the same, so the proxy doesn't change.
"""

from __future__ import annotations

import os

from mcp_guard.decision.models import ApprovalRequest, Decision, Verdict


async def request_decision(req: ApprovalRequest) -> Decision:
    blocked = {t.strip() for t in os.environ.get("MCP_GUARD_BLOCK", "").split(",") if t.strip()}
    if req.tool in blocked or "*" in blocked:
        return Decision(
            approval_id=req.approval_id,
            verdict=Verdict.BLOCK,
            reason=f"mcp-guard: tool '{req.tool}' on server '{req.server}' was blocked.",
        )
    return Decision(approval_id=req.approval_id, verdict=Verdict.ALLOW)
