"""End-to-end: a real MCP client -> our proxy -> the real demo server."""

from mcp import Client


async def test_passthrough_list_tools(proxied_demo):
    async with Client(proxied_demo()) as client:
        tools = await client.list_tools()
        assert {"list_notes", "read_note", "delete_note", "send_email"} <= {t.name for t in tools.tools}


async def test_allowed_call_reaches_server(proxied_demo):
    async with Client(proxied_demo()) as client:
        result = await client.call_tool("read_note", {"title": "welcome"})
        assert not result.is_error
        assert "Hello" in result.content[0].text


async def test_blocked_call_never_reaches_server(proxied_demo):
    async with Client(proxied_demo(block="delete_note")) as client:
        result = await client.call_tool("delete_note", {"title": "welcome"})
        assert result.is_error
        assert "blocked" in result.content[0].text

        # Proof the server never ran it: the note still exists.
        still_there = await client.call_tool("read_note", {"title": "welcome"})
        assert "Hello" in still_there.content[0].text

async def test_blocked_every_tool_reaches_server(proxied_demo):
    async with Client(proxied_demo(block="*")) as client:
        result = await client.call_tool("read_note", {"title": "welcome"})
        assert result.is_error
        assert "blocked" in result.content[0].text


async def test_legacy_session_spec_raw_wire(proxied_demo):
    """Speak the 2025 session-based spec by hand, as Claude Desktop does today.

    This is what actually travels over the pipes: one JSON object per line.
    """
    import asyncio
    import json
    import os

    params = proxied_demo(block="send_email")
    proc = await asyncio.create_subprocess_exec(
        params.command, *params.args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=params.env or os.environ.copy(),
    )

    async def send(msg: dict) -> None:
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def recv() -> dict:
        return json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))

    await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
    assert (await recv())["result"]["protocolVersion"] == "2025-06-18"
    await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    await send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "send_email", "arguments": {"to": "evil@x.com", "subject": "s", "body": "b"}}})
    blocked = await recv()
    assert blocked["id"] == 2
    assert blocked["result"]["isError"] is True
    assert "resultType" not in blocked["result"]  # strict legacy clients would reject it

    proc.stdin.close()
    assert await asyncio.wait_for(proc.wait(), 10) == 0
