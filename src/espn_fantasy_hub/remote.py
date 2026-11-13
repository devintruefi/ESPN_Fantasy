"""Hosted (HTTP) mode: the MCP server as an ASGI app for Vercel or any host.

Every request must carry the secret access token, either in the URL path
(/mcp/<token>, which is what claude.ai custom connectors use) or as an
"Authorization: Bearer <token>" header. Without MCP_ACCESS_TOKEN set, the app
refuses all MCP requests rather than exposing your ESPN account.

Runs stateless (a fresh MCP session per request, JSON responses), which suits
serverless platforms.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from urllib.parse import parse_qs

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .server import build_server

TOKEN_IN_PATH = re.compile(r"/mcp/([A-Za-z0-9_\-]{16,})/?$")


async def _send_json(send, status: int, data: dict) -> None:
    body = json.dumps(data).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")],
    })
    await send({"type": "http.response.body", "body": body})


def _presented_token(scope) -> str | None:
    m = TOKEN_IN_PATH.search(scope.get("path") or "")
    if m:
        return m.group(1)
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization" and value.lower().startswith(b"bearer "):
            return value[7:].decode().strip()
    qs = parse_qs((scope.get("query_string") or b"").decode())
    return (qs.get("key") or [None])[0]


def create_app(token: str | None = None, hub=None):
    token = token if token is not None else os.environ.get("MCP_ACCESS_TOKEN", "")
    mcp = build_server(hub)

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path = scope.get("path") or "/"
        if path in ("/", "/health", "/api/index"):
            await _send_json(send, 200, {"ok": True, "service": "espn-fantasy-hub", "mcp": "/mcp/<access token>"})
            return
        if not token or len(token) < 16:
            await _send_json(send, 503, {"error": "Server has no MCP_ACCESS_TOKEN configured; refusing requests."})
            return
        presented = _presented_token(scope)
        if not presented or not hmac.compare_digest(presented.encode(), token.encode()):
            await _send_json(send, 401, {"error": "Missing or wrong access token."})
            return
        manager = StreamableHTTPSessionManager(app=mcp._mcp_server, event_store=None, json_response=True, stateless=True)
        async with manager.run():
            await manager.handle_request(scope, receive, send)

    return app


app = create_app() if os.environ.get("MCP_ACCESS_TOKEN") else None
