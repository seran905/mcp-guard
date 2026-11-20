"""Shared pytest fixtures (like a jest setup file)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from mcp_guard.config import Settings
from mcp_guard.daemon.approver import ScriptedApprover
from mcp_guard.daemon.audit import AuditLog
from mcp_guard.daemon.server import GuardDaemon

ROOT = Path(__file__).resolve().parents[1]
DEMO_SERVER = ROOT / "examples" / "demo_server.py"


@dataclass
class RunningDaemon:
    settings: Settings
    approver: ScriptedApprover


@pytest.fixture
async def daemon():
    """A real GuardDaemon on a temp socket, with a scriptable approver instead of popups."""
    # macOS limits Unix socket paths to ~104 chars; pytest's tmp_path is too long there.
    home = Path(tempfile.mkdtemp(prefix="mg-", dir="/tmp"))
    settings = Settings(home=home, socket_path=home / "g.sock", audit_path=home / "audit.jsonl", decision_timeout=5)
    approver = ScriptedApprover()
    ready = asyncio.Event()
    task = asyncio.create_task(GuardDaemon(settings, approver, AuditLog(settings.audit_path)).serve(ready))
    await asyncio.wait_for(ready.wait(), 5)
    yield RunningDaemon(settings, approver)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def proxied_demo(daemon):
    """Factory: StdioServerParameters that start the demo server *through* the proxy.

    The test client plays the role of Claude Desktop.
    """

    def make(block: str = "", socket: Path | None = None) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_guard", "proxy", "--name", "demo", "--", sys.executable, str(DEMO_SERVER)],
            env={
                **os.environ,
                "MCP_GUARD_BLOCK": block,
                "MCP_GUARD_SOCKET": str(socket or daemon.settings.socket_path),
                "MCP_GUARD_TIMEOUT": str(daemon.settings.decision_timeout),
            },
        )

    return make
