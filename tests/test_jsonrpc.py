from mcp_guard.protocol import jsonrpc
from mcp_guard.protocol.mcp import extract_tool_call


def test_parse_line_accepts_objects_only():
    assert jsonrpc.parse_line(b'{"jsonrpc":"2.0","method":"ping","id":1}\n') == {
        "jsonrpc": "2.0",
        "method": "ping",
        "id": 1,
    }
    assert jsonrpc.parse_line(b"not json\n") is None
    assert jsonrpc.parse_line(b"[1,2]\n") is None


def test_encode_is_one_line():
    out = jsonrpc.encode({"a": "line1\nline2"})
    assert out.endswith(b"\n") and out.count(b"\n") == 1  # inner newline is escaped


def test_blocked_result_shape_legacy_spec():
    # Claude Desktop today: session-based spec, strict clients reject unknown keys.
    msg = jsonrpc.make_blocked_result(7, "nope")
    assert msg == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]},
    }


def test_blocked_result_shape_stateless_spec():
    msg = jsonrpc.make_blocked_result(7, "nope", protocol_version="2026-07-28")
    assert msg["result"]["resultType"] == "complete"


def test_extract_reads_per_request_protocol_version():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "t", "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}
    assert extract_tool_call(msg).protocol_version == "2026-07-28"


def test_extract_tool_call():
    msg = {"jsonrpc": "2.0", "id": "a1", "method": "tools/call", "params": {"name": "send_email", "arguments": {"to": "x"}}}
    call = extract_tool_call(msg)
    assert call is not None
    assert (call.request_id, call.tool, call.arguments) == ("a1", "send_email", {"to": "x"})


def test_extract_ignores_other_methods_and_notifications():
    assert extract_tool_call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) is None
    assert extract_tool_call({"jsonrpc": "2.0", "method": "tools/call", "params": {}}) is None
