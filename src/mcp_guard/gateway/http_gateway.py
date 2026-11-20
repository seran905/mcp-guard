"""HTTP gateway for remote MCP (Claude Web / claude.ai custom connectors).

For remote connectors, Claude never talks to your machine directly: Anthropic's
cloud calls the connector URL. So we stand a reverse proxy in front of the
real MCP server and expose *that* as the connector:

    Anthropic cloud --HTTPS--> tunnel --> gateway :8787 --HTTP--> real MCP server
                                             |
                                             +--> daemon --> popup

Streamable HTTP transport, in one paragraph: the client POSTs each JSON-RPC
message to one endpoint; the server answers with either a JSON body or an SSE
stream. GET opens a stream for server-initiated messages, DELETE ends a
session. Headers like Mcp-Session-Id and MCP-Protocol-Version must survive the
hop. The gateway only needs to understand POSTed tools/call; everything else is
streamed through byte-for-byte.

Exposure: the endpoint is /mcp/<token>, with a random token. Anyone without the
URL gets 404. (Production would use OAuth; see README trade-offs.)
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from collections.abc import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from mcp_guard.decision.client import request_decision
from mcp_guard.decision.models import ApprovalRequest, Verdict
from mcp_guard.protocol import jsonrpc
from mcp_guard.protocol.mcp import BATCH_REJECTED, extract_tool_call, split_batch

log = logging.getLogger(__name__)

# Headers that describe one TCP hop, not the message; never forward them.
# Host/Origin are rewritten so the upstream's DNS-rebinding checks see localhost.
HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate", "origin",
}
MAX_BODY = 4 * 1024 * 1024


def create_app(upstream: str, server_name: str, token: str) -> Starlette:
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10))

    async def endpoint(request: Request) -> Response:
        if not secrets.compare_digest(request.path_params["token"], token):  # constant-time compare
            return Response(status_code=404)

        body = await request.body()
        if request.method == "POST":
            if len(body) > MAX_BODY:
                return Response(status_code=413)
            if request.headers.get("content-encoding", "identity") != "identity":
                # We can't inspect a compressed body, so we refuse it rather than let it pass unchecked.
                return Response("compressed request bodies are not supported", status_code=415)
            blocked = await _gate(body, request.headers.get("mcp-protocol-version"))
            if blocked is not None:
                return blocked
        return await _forward(request, body)

    async def _gate(body: bytes, header_version: str | None) -> Response | None:
        batch = jsonrpc.parse_batch(body)
        if batch is not None:
            calls, _ = split_batch(batch)
            if calls:
                log.warning("BLOCK %d tool call(s) smuggled in a JSON-RPC batch", len(calls))
                return JSONResponse([jsonrpc.make_blocked_result(c.request_id, BATCH_REJECTED, c.protocol_version or header_version) for c in calls])
            return None

        msg = jsonrpc.parse_line(body)
        call = extract_tool_call(msg) if msg is not None else None
        if call is None:
            return None

        decision = await request_decision(
            ApprovalRequest(source="http-gateway", server=server_name, tool=call.tool, arguments=call.arguments)
        )
        if decision.verdict is Verdict.ALLOW:
            log.info("ALLOW %s.%s id=%s", server_name, call.tool, call.request_id)
            return None
        log.warning("BLOCK %s.%s id=%s", server_name, call.tool, call.request_id)
        reason = decision.reason or f"mcp-guard: '{call.tool}' was blocked by the user."
        return JSONResponse(jsonrpc.make_blocked_result(call.request_id, reason, call.protocol_version or header_version))

    async def _forward(request: Request, body: bytes) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        upstream_request = client.build_request(
            request.method, upstream, headers=headers, content=body, params=request.query_params
        )
        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            log.error("upstream %s unreachable: %s", upstream, exc)
            return Response("upstream MCP server unreachable", status_code=502)

        out_headers = {k: v for k, v in upstream_response.headers.items() if k.lower() not in HOP_BY_HOP}
        # Stream (don't buffer): SSE responses stay open and deliver events as they happen.
        return StreamingResponse(
            upstream_response.aiter_raw(),
            status_code=upstream_response.status_code,
            headers=out_headers,
            background=BackgroundTask(upstream_response.aclose),
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield  # app runs here; code after yield is shutdown (like a finally)
        await client.aclose()

    return Starlette(
        routes=[Route("/mcp/{token}", endpoint, methods=["GET", "POST", "DELETE"])],
        lifespan=lifespan,
    )


def run_gateway(upstream: str, server_name: str, host: str, port: int, token: str | None) -> int:
    import uvicorn

    token = token or secrets.token_urlsafe(24)
    print(f"\nmcp-guard gateway for {server_name!r} -> {upstream}")
    print(f"  local endpoint:  http://{host}:{port}/mcp/{token}")
    print(f"  expose it:       cloudflared tunnel --url http://localhost:{port}")
    print(f"  connector URL:   https://<your-tunnel>.trycloudflare.com/mcp/{token}\n")
    uvicorn.run(create_app(upstream, server_name, token), host=host, port=port, log_level="warning")
    return 0
