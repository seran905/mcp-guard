"""HTTP gateway end to end: MCP HTTP client -> gateway -> demo server (streamable-http)."""

from __future__ import annotations

import asyncio
import socket
import sys

import httpx
import pytest
import uvicorn
from mcp import Client

from mcp_guard.decision.models import Verdict
from mcp_guard.gateway.http_gateway import create_app

from conftest import DEMO_SERVER

TOKEN = "test-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_port(port: int, timeout: float = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.1)


@pytest.fixture
async def gateway_url(daemon, monkeypatch):
    monkeypatch.setenv("MCP_GUARD_SOCKET", str(daemon.settings.socket_path))
    monkeypatch.setenv("MCP_GUARD_BLOCK", "")

    upstream_port, gateway_port = _free_port(), _free_port()
    upstream = await asyncio.create_subprocess_exec(
        sys.executable, str(DEMO_SERVER), "--http", str(upstream_port),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await _wait_for_port(upstream_port)

    app = create_app(f"http://127.0.0.1:{upstream_port}/mcp", "demo-http", TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=gateway_port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    await _wait_for_port(gateway_port)

    yield f"http://127.0.0.1:{gateway_port}/mcp/{TOKEN}"

    server.should_exit = True
    await serving
    upstream.terminate()
    await upstream.wait()


async def test_gateway_allow_and_block(gateway_url, daemon):
    daemon.approver.verdicts["send_email"] = Verdict.BLOCK
    async with Client(gateway_url) as client:
        assert {"read_note", "send_email"} <= {t.name for t in (await client.list_tools()).tools}
        ok = await client.call_tool("read_note", {"title": "welcome"})
        assert not ok.is_error and "Hello" in ok.content[0].text
        blocked = await client.call_tool("send_email", {"to": "evil@x.com", "subject": "s", "body": "b"})
        assert blocked.is_error and "blocked" in blocked.content[0].text
    assert [r.source for r in daemon.approver.seen] == ["http-gateway", "http-gateway"]


async def test_gateway_hides_endpoint_without_token(gateway_url):
    wrong = gateway_url.rsplit("/", 1)[0] + "/guess"
    async with httpx.AsyncClient() as http:
        assert (await http.post(wrong, json={})).status_code == 404


async def test_gateway_rejects_compressed_bodies(gateway_url):
    async with httpx.AsyncClient() as http:
        r = await http.post(gateway_url, content=b"\x1f\x8b...", headers={"content-encoding": "gzip"})
        assert r.status_code == 415


async def test_gateway_blocks_batched_tool_calls(gateway_url, daemon):
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "delete_note", "arguments": {}}}]
    async with httpx.AsyncClient() as http:
        r = await http.post(gateway_url, json=batch, headers={"accept": "application/json, text/event-stream"})
    assert r.json()[0]["result"]["isError"] is True
    assert daemon.approver.seen == []
