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


def _clean(value: str | None) -> str:
    """Trim whitespace/quotes people often paste along with a secret."""
    return (value or "").strip().strip('"').strip("'").strip()


def config_status(token: str, environ=None) -> dict:
    """What the server can see, as yes/no only (never values)."""
    env = os.environ if environ is None else environ
    s2, swid = _clean(env.get("ESPN_S2")), _clean(env.get("ESPN_SWID"))
    if not token:
        tok = "missing"
    elif len(token) < 16:
        tok = f"too short ({len(token)} characters; expected 16+)"
    else:
        tok = "set"
    login = "set" if s2 and swid else ("missing" if not (s2 or swid) else ("ESPN_SWID missing" if s2 else "ESPN_S2 missing"))
    return {
        "access_token": tok,
        "espn_login": login,
        "writes_enabled": _clean(env.get("ESPN_ALLOW_WRITES")).lower() in ("1", "true", "yes", "on"),
        "timezone": _clean(env.get("ESPN_TIMEZONE")) or "server default (UTC)",
        "leagues_listed": len([x for x in _clean(env.get("ESPN_LEAGUES")).split(",") if x.strip()]),
        "vercel_env": env.get("VERCEL_ENV"),
    }


def create_app(token: str | None = None, hub=None):
    token = _clean(token if token is not None else os.environ.get("MCP_ACCESS_TOKEN", ""))
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
        if path in ("/", "/health"):
            await _send_json(send, 200, {
                "ok": True,
                "service": "espn-fantasy-hub",
                "mcp": "/mcp/<access token>",
                "config": config_status(token),
            })
            return
        if path.startswith("/.well-known/"):
            await _send_json(send, 404, {"error": "Not found."})  # no OAuth here
            return
        if not token or len(token) < 16:
            await _send_json(send, 503, {"error": "MCP_ACCESS_TOKEN is missing or too short on the server; refusing requests. See /health."})
            return
        presented = _presented_token(scope)
        if not presented or not hmac.compare_digest(presented.encode(), token.encode()):
            # 404 rather than 401: a 401 makes MCP clients (e.g. claude.ai) start
            # an OAuth login flow, which this server doesn't use.
            await _send_json(send, 404, {"error": "Not found."})
            return
        method = scope.get("method", "GET").upper()
        if method != "POST":
            # Stateless server: no standalone event stream (GET) and no sessions
            # to end (DELETE). The MCP spec says to answer 405 so clients fall
            # back to plain POST; an open GET stream would also tie up a
            # serverless function until it times out.
            await send({
                "type": "http.response.start",
                "status": 405,
                "headers": [(b"allow", b"POST"), (b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": b'{"error": "Use POST for MCP requests."}'})
            return
        manager = StreamableHTTPSessionManager(app=mcp._mcp_server, event_store=None, json_response=True, stateless=True)
        async with manager.run():
            await manager.handle_request(scope, receive, send)

    return app


app = create_app()
