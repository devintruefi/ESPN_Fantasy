#!/usr/bin/env python3
"""Call the hosted ESPN Fantasy MCP server's READ-ONLY tools from a shell.

    python3 scripts/gm_call.py my_leagues
    python3 scripts/gm_call.py season_outlook '{"league": "football"}'
    python3 scripts/gm_call.py --list

Used by scheduled GM briefings. Write tools (set_lineup, add_player, drop_player,
cancel_claim, propose_trade) are refused here, whatever is asked.

Authentication: the server takes "Authorization: Bearer <token>". In a Claude cloud
environment the token is saved as a network secret for the server's host, and the
environment's proxy adds the header, so the token never appears in a command, a file
or a prompt. Elsewhere set GM_TOKEN in the environment. Server URL: GM_SERVER_URL
(default https://espnfantasy1.vercel.app/mcp). Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("GM_SERVER_URL", "https://espnfantasy1.vercel.app/mcp")
WRITE_TOOLS = {"set_lineup", "add_player", "drop_player", "cancel_claim", "propose_trade"}


def rpc(method: str, params: dict) -> dict:
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    token = os.environ.get("GM_TOKEN")
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=115) as r:
            text = r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit("The server refused the request (no valid access token). In a cloud environment, save the "
                     "server's access token as a network secret for its host; elsewhere set GM_TOKEN.")
        sys.exit(f"Server error: HTTP {e.code}")
    except urllib.error.URLError as e:
        sys.exit(f"Couldn't reach the server: {e.reason}")
    if text.lstrip().startswith("event:") or text.lstrip().startswith("data:"):
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


def call(tool: str, args: dict) -> str:
    if tool in WRITE_TOOLS:
        sys.exit(f"Refusing to call {tool}: this client is read-only.")
    resp = rpc("tools/call", {"name": tool, "arguments": args})
    if "error" in resp:
        sys.exit(f"Error: {resp['error'].get('message')}")
    result = resp.get("result") or {}
    text = "".join(c.get("text", "") for c in result.get("content") or [])
    if result.get("isError"):
        sys.exit(f"Tool error: {text}")
    return text


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    if argv[0] == "--list":
        tools = rpc("tools/list", {}).get("result", {}).get("tools", [])
        for t in tools:
            ro = (t.get("annotations") or {}).get("readOnlyHint")
            print(f"{t['name']}{'' if ro else '  (write: refused here)'}")
        return
    args = json.loads(argv[1]) if len(argv) > 1 else {}
    out = call(argv[0], args)
    try:
        print(json.dumps(json.loads(out), indent=1, ensure_ascii=False))
    except ValueError:
        print(out)


if __name__ == "__main__":
    main(sys.argv[1:])
