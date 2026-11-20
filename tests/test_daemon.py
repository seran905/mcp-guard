"""Proxy + daemon together: human decisions, failures, timeouts, concurrency."""

from __future__ import annotations

import asyncio
import json
import time

from mcp import Client

from mcp_guard.config import Settings
from mcp_guard.daemon.approver import ScriptedApprover
from mcp_guard.daemon.audit import AuditLog
from mcp_guard.daemon.server import GuardDaemon
from mcp_guard.decision.client import request_decision
from mcp_guard.decision.models import ApprovalRequest, Verdict


async def test_user_block_is_enforced(proxied_demo, daemon):
    daemon.approver.verdicts["delete_note"] = Verdict.BLOCK
    async with Client(proxied_demo()) as client:
        result = await client.call_tool("delete_note", {"title": "welcome"})
        assert result.is_error and "blocked" in result.content[0].text
        assert not (await client.call_tool("read_note", {"title": "welcome"})).is_error


async def test_daemon_down_fails_closed(proxied_demo, tmp_path):
    async with Client(proxied_demo(socket=tmp_path / "nobody-home.sock")) as client:
        result = await client.call_tool("read_note", {"title": "welcome"})
        assert result.is_error and "not running" in result.content[0].text


async def _start_daemon(settings: Settings, approver: ScriptedApprover) -> asyncio.Task[None]:
    ready = asyncio.Event()
    task = asyncio.create_task(GuardDaemon(settings, approver, AuditLog(settings.audit_path)).serve(ready))
    await asyncio.wait_for(ready.wait(), 5)
    return task


async def test_no_answer_times_out_to_block(daemon):
    home = daemon.settings.home
    fast = Settings(home, home / "fast.sock", home / "fast.jsonl", decision_timeout=0.5)
    task = await _start_daemon(fast, ScriptedApprover(default=None))  # human never clicks
    try:
        req = ApprovalRequest(source="test", server="demo", tool="send_email", arguments={})
        decision = await request_decision(req, fast)
        assert decision.verdict is Verdict.BLOCK and "no decision within" in decision.reason
        assert json.loads(fast.audit_path.read_text().splitlines()[-1])["decided_by"] == "timeout"
    finally:
        task.cancel()


async def test_requester_cancel_dismisses_pending_decision(daemon):
    """Claude cancels (or the proxy dies) while the popup is open: daemon gives up on it."""
    daemon.approver.verdicts["send_email"] = None
    req = ApprovalRequest(source="test", server="demo", tool="send_email", arguments={})
    asking = asyncio.create_task(request_decision(req, daemon.settings))
    await asyncio.sleep(0.3)
    asking.cancel()  # closes the socket, like a cancelled tools/call
    await asyncio.gather(asking, return_exceptions=True)
    await asyncio.sleep(0.3)
    entry = json.loads(daemon.settings.audit_path.read_text().splitlines()[-1])
    assert (entry["verdict"], entry["decided_by"]) == ("block", "requester-gone")


async def test_concurrent_calls_decided_out_of_order(proxied_demo, daemon):
    """Five calls in flight at once. The slow one is approved last; nothing blocks anything else."""
    daemon.approver.delays["read_note"] = 1.0  # human takes a while on this one
    daemon.approver.verdicts["delete_note"] = Verdict.BLOCK
    async with Client(proxied_demo()) as client:
        started = time.monotonic()
        results = await asyncio.gather(
            client.call_tool("read_note", {"title": "welcome"}),
            client.call_tool("delete_note", {"title": "secrets"}),
            client.call_tool("list_notes", {}),
            client.call_tool("delete_note", {"title": "welcome"}),
            client.call_tool("send_email", {"to": "a@b.c", "subject": "s", "body": "b"}),
        )
        elapsed = time.monotonic() - started

    read, del1, listed, del2, email = results
    assert not read.is_error and "Hello" in read.content[0].text  # welcome survived both deletes
    assert del1.is_error and del2.is_error
    assert not listed.is_error and not email.is_error
    assert elapsed < 2.5  # ran in parallel, not 5 sequential waits
    assert len(daemon.approver.seen) == 5


async def test_every_decision_is_audited(proxied_demo, daemon):
    daemon.approver.verdicts["send_email"] = Verdict.BLOCK
    async with Client(proxied_demo()) as client:
        await client.call_tool("read_note", {"title": "welcome"})
        await client.call_tool("send_email", {"to": "evil@x.com", "subject": "s", "body": "b"})
    lines = [json.loads(line) for line in daemon.settings.audit_path.read_text().splitlines()]
    assert [(e["tool"], e["verdict"], e["decided_by"]) for e in lines] == [
        ("read_note", "allow", "user"),
        ("send_email", "block", "user"),
    ]
    assert "evil@x.com" in lines[1]["arguments"]
    assert (daemon.settings.audit_path.stat().st_mode & 0o777) == 0o600
