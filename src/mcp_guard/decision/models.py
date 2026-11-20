"""Data contract between interceptors (proxy, gateway) and the decision maker.

pydantic validates data at runtime, like zod. We use it for anything that
crosses a process boundary (in M3 these travel over a Unix socket), because
we can't trust bytes coming from another process.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


class ApprovalRequest(BaseModel):
    approval_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    source: str  # "stdio-proxy" | "http-gateway"
    server: str  # MCP server name, e.g. "demo-notes"
    tool: str
    arguments: dict[str, Any]
    created_at: float = Field(default_factory=time.time)


class Decision(BaseModel):
    approval_id: str
    verdict: Verdict
    reason: str = ""
