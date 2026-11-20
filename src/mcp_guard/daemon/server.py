"""The guard daemon: one per machine, owns every decision.

Protocol on the Unix socket (one connection per tool call):
    requester -> daemon : ApprovalRequest as one JSON line
    daemon -> requester : Decision as one JSON line, then close

While the human decides, three things race (asyncio.wait FIRST_COMPLETED):
    1. the approver answers            -> that verdict
    2. the requester disconnects       -> BLOCK, close popup (Claude cancelled)
    3. the decision timeout fires      -> BLOCK, close popup
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time

from pydantic import ValidationError

from mcp_guard.config import Settings
from mcp_guard.daemon.approver import Approver
from mcp_guard.daemon.audit import AuditLog
from mcp_guard.decision.models import ApprovalRequest, Decision, Verdict

log = logging.getLogger(__name__)

READ_REQUEST_TIMEOUT = 5.0
STREAM_LIMIT = 16 * 1024 * 1024


class DaemonAlreadyRunning(RuntimeError):
    pass


class GuardDaemon:
    def __init__(self, settings: Settings, approver: Approver, audit: AuditLog) -> None:
        self.settings = settings
        self.approver = approver
        self.audit = audit

    async def serve(self, ready: asyncio.Event | None = None) -> None:
        path = self.settings.socket_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _remove_stale_socket(path)

        server = await asyncio.start_unix_server(self._handle, path=str(path), limit=STREAM_LIMIT)
        os.chmod(path, 0o600)  # only this user's processes may ask for decisions
        log.info("daemon listening on %s (timeout %ss)", path, self.settings.decision_timeout)
        if ready is not None:
            ready.set()
        try:
            async with server:
                await server.serve_forever()
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), READ_REQUEST_TIMEOUT)
            req = ApprovalRequest.model_validate_json(line)
        except (TimeoutError, ValidationError, ValueError) as exc:
            log.warning("rejected malformed request: %s", exc)
            writer.close()
            return

        decision = await self._decide(req, reader)
        try:
            writer.write(decision.model_dump_json().encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass  # requester already gone; decision is still audited
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _decide(self, req: ApprovalRequest, reader: asyncio.StreamReader) -> Decision:
        timeout = self.settings.decision_timeout
        deadline = time.time() + timeout
        log.info("PENDING %s.%s (%s) id=%s", req.server, req.tool, req.source, req.approval_id[:8])

        ask = asyncio.create_task(self.approver.ask(req, deadline))
        gone = asyncio.create_task(reader.read(1))  # returns b"" when requester disconnects
        done, _ = await asyncio.wait({ask, gone}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)

        if ask in done and ask.exception() is None:
            verdict, decided_by = ask.result(), "user"
            reason = "" if verdict is Verdict.ALLOW else f"mcp-guard: the user blocked '{req.tool}'."
        elif ask in done:
            log.error("approver failed: %r", ask.exception())
            verdict, decided_by = Verdict.BLOCK, "error"
            reason = "mcp-guard: the approval UI failed, so the call was blocked."
        elif gone in done:
            verdict, decided_by = Verdict.BLOCK, "requester-gone"
            reason = "mcp-guard: request was cancelled before a decision."
        else:
            verdict, decided_by = Verdict.BLOCK, "timeout"
            reason = f"mcp-guard: no decision within {timeout:g}s, so '{req.tool}' was blocked."

        for task in (ask, gone):
            task.cancel()  # cancelling `ask` closes the popup
        await asyncio.gather(ask, gone, return_exceptions=True)

        decision = Decision(approval_id=req.approval_id, verdict=verdict, reason=reason)
        log.info("%s %s.%s by %s", verdict.value.upper(), req.server, req.tool, decided_by)
        self.audit.record(req, decision, decided_by)
        return decision


def _remove_stale_socket(path: os.PathLike[str]) -> None:
    """A socket file left by a crashed daemon blocks bind(). Remove it, unless a live daemon owns it."""
    if not os.path.exists(path):
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(os.fspath(path))
    except OSError:
        os.unlink(path)  # nobody listening: stale
    else:
        raise DaemonAlreadyRunning(f"another mcp-guard daemon is already listening on {path}")
    finally:
        probe.close()
