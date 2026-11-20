"""Stdio interception proxy.

Claude Desktop launches this process instead of the real MCP server:

    Claude Desktop <--stdio--> StdioProxy <--stdio--> real MCP server

Every message is relayed byte-for-byte, except `tools/call` requests, which
are held until a Decision arrives:
  - ALLOW: the original bytes are forwarded to the server.
  - BLOCK: the server never sees the call; the proxy answers Claude itself.

Concurrency model: one asyncio event loop, two long-running "pump" tasks
(client->server and server->client), plus one short-lived task per pending
tool call. The pumps never wait on a human, so other traffic (pings,
tools/list, more tool calls) keeps flowing while a popup is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence

from mcp_guard.decision.client import request_decision
from mcp_guard.decision.models import ApprovalRequest, Decision, Verdict
from mcp_guard.protocol import jsonrpc
from mcp_guard.protocol.mcp import ToolCall, extract_tool_call

log = logging.getLogger(__name__)

# asyncio's default line limit is 64 KiB. Tool results (file contents,
# screenshots as base64) easily exceed that, so raise it.
STREAM_LIMIT = 16 * 1024 * 1024
CHILD_EXIT_GRACE_SECONDS = 3.0


class StdioProxy:
    def __init__(self, server_name: str, command: Sequence[str]) -> None:
        self.server_name = server_name
        self.command = list(command)
        self._child: asyncio.subprocess.Process | None = None
        # Two writers can race (pump + gate tasks), so each output gets a lock.
        self._to_client_lock = asyncio.Lock()
        self._to_server_lock = asyncio.Lock()
        # request id -> task waiting for a human decision
        self._pending: dict[str | int, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ run

    async def run(self) -> int:
        """Run until either side disconnects. Returns the exit code."""
        try:
            self._child = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,  # inherit: server logs flow to our stderr
                limit=STREAM_LIMIT,
            )
        except (FileNotFoundError, PermissionError) as exc:
            log.error("cannot start MCP server %r: %s", self.command, exc)
            return 127

        log.info("proxying server %r (pid %s): %s", self.server_name, self._child.pid, " ".join(self.command))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
                loop.add_signal_handler(sig, stop.set)

        client_reader = await _open_stdin()
        upstream = asyncio.create_task(self._pump_client_to_server(client_reader), name="upstream")
        downstream = asyncio.create_task(self._pump_server_to_client(), name="downstream")
        stopper = asyncio.create_task(stop.wait(), name="signal")

        # Like Promise.race: wake up when the first of these finishes.
        done, _ = await asyncio.wait({upstream, downstream, stopper}, return_when=asyncio.FIRST_COMPLETED)

        if downstream in done:
            log.info("server %r closed its stdout", self.server_name)
        elif upstream in done:
            log.info("client disconnected")
        else:
            log.info("received shutdown signal")

        await self._shutdown(upstream, downstream, stopper)
        return self._child.returncode if self._child.returncode is not None else 0

    async def _shutdown(self, *tasks: asyncio.Task[object]) -> None:
        for task in self._pending.values():
            task.cancel()
        child = self._child
        assert child is not None
        if child.stdin and not child.stdin.is_closing():
            child.stdin.close()  # polite: EOF tells a stdio server to exit
        try:
            await asyncio.wait_for(child.wait(), CHILD_EXIT_GRACE_SECONDS)
        except TimeoutError:
            log.warning("server did not exit, terminating")
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), CHILD_EXIT_GRACE_SECONDS)
            except TimeoutError:
                child.kill()
                await child.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *self._pending.values(), return_exceptions=True)

    # ---------------------------------------------------------------- pumps

    async def _pump_client_to_server(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # a single line exceeded STREAM_LIMIT
                log.error("message from client exceeded %d bytes; closing", STREAM_LIMIT)
                return
            if not line:  # b"" means EOF: Claude closed our stdin
                return

            msg = jsonrpc.parse_line(line)
            call = extract_tool_call(msg) if msg is not None else None

            if call is None:
                if msg is not None and msg.get("method") == "notifications/cancelled":
                    self._cancel_pending(msg)
                await self._send_to_server(line)
                continue

            # Don't await the decision here, or every other message would stall
            # behind the popup. Spawn a task and keep reading.
            task = asyncio.create_task(self._gate(call, line), name=f"gate-{call.request_id}")
            self._pending[call.request_id] = task
            task.add_done_callback(lambda t, rid=call.request_id: self._forget(rid, t))

    async def _pump_server_to_client(self) -> None:
        assert self._child is not None and self._child.stdout is not None
        while True:
            try:
                line = await self._child.stdout.readline()
            except ValueError:
                log.error("message from server exceeded %d bytes; closing", STREAM_LIMIT)
                return
            if not line:
                return
            await self._send_to_client(line)

    # ----------------------------------------------------------------- gate

    async def _gate(self, call: ToolCall, raw: bytes) -> None:
        req = ApprovalRequest(
            source="stdio-proxy",
            server=self.server_name,
            tool=call.tool,
            arguments=call.arguments,
        )
        try:
            decision = await request_decision(req)
        except Exception:  # noqa: BLE001 - any failure must fail closed
            # Note: CancelledError is a BaseException, so it is NOT caught here.
            log.exception("decision failed for %s; blocking", call.tool)
            decision = Decision(
                approval_id=req.approval_id,
                verdict=Verdict.BLOCK,
                reason="mcp-guard could not get a decision, so the call was blocked (fail-closed).",
            )

        if decision.verdict is Verdict.ALLOW:
            log.info("ALLOW %s.%s id=%s", self.server_name, call.tool, call.request_id)
            await self._send_to_server(raw)
        else:
            log.warning("BLOCK %s.%s id=%s", self.server_name, call.tool, call.request_id)
            reason = decision.reason or f"mcp-guard: '{call.tool}' was blocked by the user."
            await self._send_to_client(jsonrpc.encode(jsonrpc.make_blocked_result(call.request_id, reason, call.protocol_version)))

    def _cancel_pending(self, msg: jsonrpc.JsonDict) -> None:
        """Claude gave up on a request: drop its popup instead of running it later."""
        params = msg.get("params") or {}
        rid = params.get("requestId") if isinstance(params, dict) else None
        task = self._pending.get(rid) if rid is not None else None
        if task is not None:
            log.info("client cancelled pending call id=%s", rid)
            task.cancel()

    def _forget(self, request_id: str | int, task: asyncio.Task[None]) -> None:
        if self._pending.get(request_id) is task:
            del self._pending[request_id]

    # --------------------------------------------------------------- output

    async def _send_to_server(self, line: bytes) -> None:
        assert self._child is not None and self._child.stdin is not None
        async with self._to_server_lock:
            try:
                self._child.stdin.write(line)
                await self._child.stdin.drain()  # backpressure, like stream.write()'s callback
            except (BrokenPipeError, ConnectionResetError):
                log.warning("server stdin closed; dropping message")

    async def _send_to_client(self, line: bytes) -> None:
        async with self._to_client_lock:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()


async def _open_stdin() -> asyncio.StreamReader:
    """Wrap our stdin pipe in an asyncio StreamReader (Python has no async stdin built in)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=STREAM_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    return reader
