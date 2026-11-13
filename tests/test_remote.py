import asyncio
import json

import httpx

from espn_fantasy_hub.remote import create_app

TOKEN = "test-token-0123456789abcdef"
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def rpc(method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "method": method, "id": id}
    if params is not None:
        msg["params"] = params
    return msg


async def _run(app, path, payload, headers=HEADERS):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://x.test") as c:
        if payload is None:
            return await c.get(path)
        return await c.post(path, content=json.dumps(payload), headers=headers)


def call(app, path, payload=None, headers=HEADERS):
    return asyncio.run(_run(app, path, payload, headers))


INIT = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})


def test_auth_required(football_hub):
    app = create_app(TOKEN, football_hub)
    assert call(app, "/health").status_code == 200
    assert call(app, "/mcp", INIT).status_code == 404
    assert call(app, "/mcp/wrong-token-0123456789", INIT).status_code == 404
    assert call(app, "/.well-known/oauth-protected-resource").status_code == 404
    assert call(create_app("", football_hub), f"/mcp/{TOKEN}", INIT).status_code == 503


def test_mcp_over_http(football_hub):
    app = create_app(TOKEN, football_hub)
    r = call(app, f"/mcp/{TOKEN}", INIT)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "ESPN Fantasy"
    r = call(app, f"/mcp/{TOKEN}", rpc("tools/list", {}, 2))
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"my_leagues", "set_lineup", "power_rankings"} <= names
    r = call(app, f"/mcp/{TOKEN}", rpc("tools/call", {"name": "standings", "arguments": {}}, 3))
    data = json.loads(r.json()["result"]["content"][0]["text"])
    assert len(data["standings"]) == 12
    # bearer header works too (Claude Code / scripts)
    r = call(app, "/mcp", rpc("tools/list", {}, 4), HEADERS | {"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
