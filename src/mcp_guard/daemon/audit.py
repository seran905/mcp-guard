"""Append-only audit trail: one JSON object per line (JSONL).

Every decision is recorded with who/what decided it. This is the evidence a
security or compliance team asks for: "show me every tool call the AI made
last week and who approved it".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mcp_guard.decision.models import ApprovalRequest, Decision

MAX_ARGS_CHARS = 4000


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def record(self, req: ApprovalRequest, decision: Decision, decided_by: str) -> None:
        args = json.dumps(req.arguments, ensure_ascii=False, default=str)
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "approval_id": req.approval_id,
            "source": req.source,
            "server": req.server,
            "tool": req.tool,
            "arguments": args if len(args) <= MAX_ARGS_CHARS else args[:MAX_ARGS_CHARS] + "...(truncated)",
            "verdict": decision.verdict.value,
            "decided_by": decided_by,  # user | timeout | requester-gone | error
            "latency_ms": round((time.time() - req.created_at) * 1000),
        }
        # os.open with 0o600: only your user can read the log (it may contain secrets).
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
