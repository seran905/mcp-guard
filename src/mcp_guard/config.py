"""Runtime settings, read from environment variables with safe defaults.

    MCP_GUARD_HOME     state directory            (default ~/.mcp-guard)
    MCP_GUARD_SOCKET   daemon Unix socket path    (default $MCP_GUARD_HOME/guard.sock)
    MCP_GUARD_TIMEOUT  seconds before auto-block  (default 50)
    MCP_GUARD_BLOCK    comma-separated tools always blocked, "*" = all

Why 50s: MCP TypeScript clients (Claude Desktop) give up on a request after
60s by default. Answering at 50s means Claude gets a clear "blocked: timed
out" instead of a generic timeout error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    home: Path
    socket_path: Path
    audit_path: Path
    decision_timeout: float

    @classmethod
    def from_env(cls) -> Settings:
        home = Path(os.environ.get("MCP_GUARD_HOME", Path.home() / ".mcp-guard")).expanduser()
        return cls(
            home=home,
            socket_path=Path(os.environ.get("MCP_GUARD_SOCKET", home / "guard.sock")).expanduser(),
            audit_path=home / "audit.jsonl",
            decision_timeout=float(os.environ.get("MCP_GUARD_TIMEOUT", "50")),
        )


def static_blocklist() -> set[str]:
    return {t.strip() for t in os.environ.get("MCP_GUARD_BLOCK", "").split(",") if t.strip()}
