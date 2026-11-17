import asyncio
import json

import httpx
import pytest

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


def test_health_reports_config_without_values(football_hub):
    from espn_fantasy_hub.remote import config_status
    st = config_status("x" * 20, {"ESPN_S2": "secret-s2", "ESPN_SWID": "{A}", "ESPN_ALLOW_WRITES": "1"})
    assert st["access_token"] == "set" and st["espn_login"] == "set" and st["writes_enabled"] is True
    assert "secret" not in str(st)
    assert config_status("short", {})["access_token"].startswith("too short")
    assert config_status("", {"ESPN_S2": "a"})["espn_login"] == "ESPN_SWID missing"
    app = create_app("  " + TOKEN + "\n", football_hub)  # pasted with whitespace
    assert call(app, f"/mcp/{TOKEN}", INIT).status_code == 200
    body = call(app, "/health").json()
    assert body["config"]["access_token"] == "set"


def test_non_post_methods_get_405(football_hub):
    app = create_app(TOKEN, football_hub)

    async def run(method):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://x.test") as c:
            return await c.request(method, f"/mcp/{TOKEN}", headers={"accept": "text/event-stream"})

    for method in ("GET", "DELETE", "PUT"):
        r = asyncio.run(run(method))
        assert r.status_code == 405 and r.headers["allow"] == "POST"


def test_bad_requests_are_handled(football_hub):
    app = create_app(TOKEN, football_hub)
    r = call(app, f"/mcp/{TOKEN}", None)  # GET without accept header
    assert r.status_code == 405

    async def raw(body):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://x.test") as c:
            return await c.post(f"/mcp/{TOKEN}", content=body, headers=HEADERS)

    assert asyncio.run(raw("{not json")).status_code in (400, 422)
    r = asyncio.run(raw(json.dumps(rpc("nope/nope"))))
    assert "error" in r.json()


@pytest.mark.parametrize("value", [
    "Bearer {t}", "bearer {t}", "Bearer Bearer {t}", "Bearer  {t} ", 'Bearer "{t}"',
    "Bearer https://example.vercel.app/mcp/{t}", "Bearer MCP_ACCESS_TOKEN={t}", "{t}",
])
def test_bearer_header_forgives_paste_formats(football_hub, value):
    app = create_app(TOKEN, football_hub)
    r = call(app, "/mcp", rpc("tools/list", {}, 1), HEADERS | {"authorization": value.format(t=TOKEN)})
    assert r.status_code == 200, value


@pytest.mark.parametrize("value", ["Bearer ", "Bearer Bearer", "Bearer x{t}", "Bearer {t}x",
                                   "Bearer https://example.vercel.app/mcp/wrong-token-0123456789"])
def test_bearer_header_still_needs_the_exact_token(football_hub, value):
    app = create_app(TOKEN, football_hub)
    r = call(app, "/mcp", rpc("tools/list", {}, 1), HEADERS | {"authorization": value.format(t=TOKEN)})
    assert r.status_code == 404, value
