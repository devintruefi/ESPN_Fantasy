import pytest

from espn_fantasy_hub import writes
from espn_fantasy_hub.config import LeagueConfig
from espn_fantasy_hub.service import UserError
from espn_fantasy_hub.views import player_of

from conftest import FOOTBALL_NOW, make_hub
from test_football import entry_named

SWID = "{00000000-0000-0000-0000-000000000004}"  # owner of Team 1 in the fixture


def writable_hub(data, monkeypatch, posted):
    cfg = LeagueConfig(sport="football", league_id=1086064, season=2026, team_id=1)
    hub = make_hub([(cfg, data)], FOOTBALL_NOW, swid=SWID)
    hub.settings.allow_writes = True

    def fake_post(sport, season, league_id, cookies, payload):
        posted.append(payload)
        return {"id": "tx-1", "status": "EXECUTED", "isPending": False}

    monkeypatch.setattr(writes, "post_transaction", fake_post)
    return hub


def test_plan_start_and_bench(football_hub):
    h = football_hub.handles()[0]
    view = football_hub.view(h)
    entries = view.roster_entries(1)
    plan, steps, warnings = writes.plan_lineup(view, entries, start=["Jared Goff"], bench=["Tyler Shough"])
    ids = {player_of(e)["fullName"]: player_of(e)["id"] for e in entries}
    assert sorted(plan) == sorted([(ids["Jared Goff"], 20, 0), (ids["Tyler Shough"], 0, 20)])
    assert any("Jared Goff: BE → QB" == s for s in steps)


def test_plan_flex_and_errors(football_hub):
    h = football_hub.handles()[0]
    view = football_hub.view(h)
    entries = view.roster_entries(1)
    plan, steps, _ = writes.plan_lineup(view, entries, start=["Tank Dell"], bench=["Zach Charbonnet"])
    assert any("Tank Dell: BE → FLEX" == s for s in steps)  # WR slots full, so FLEX
    with pytest.raises(writes.MoveError, match="No open slot"):
        writes.plan_lineup(view, entries, start=["Jared Goff"])
    with pytest.raises(writes.MoveError, match="isn't on your roster"):
        writes.plan_lineup(view, entries, start=["Patrick Mahomes"])
    with pytest.raises(writes.MoveError, match="already your lineup"):
        writes.plan_lineup(view, entries, start=["Tyler Shough"])


def test_locked_player_cannot_move(football_data):
    entry_named(football_data, "Jared Goff")["playerPoolEntry"]["lineupLocked"] = True
    cfg = LeagueConfig(sport="football", league_id=1086064, season=2026, team_id=1)
    hub = make_hub([(cfg, football_data)], FOOTBALL_NOW)
    view = hub.view(hub.handles()[0])
    with pytest.raises(writes.MoveError, match="locked"):
        writes.plan_lineup(view, view.roster_entries(1), start=["Jared Goff"], bench=["Tyler Shough"])


def test_payload_shapes():
    p = writes.lineup_payload(1, SWID, 10, [(5, 20, 0)])
    assert p["type"] == "ROSTER" and p["items"][0] == {"playerId": 5, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 0}
    assert p["memberId"] == SWID and p["executionType"] == "EXECUTE" and p["isLeagueManager"] is False
    fa = writes.add_drop_payload(1, SWID, 10, add_id=7, drop_id=8)
    assert fa["type"] == "FREEAGENT" and [i["type"] for i in fa["items"]] == ["ADD", "DROP"] and "bidAmount" not in fa
    wv = writes.add_drop_payload(1, SWID, 10, add_id=7, waiver=True, bid=12)
    assert wv["type"] == "WAIVER" and wv["bidAmount"] == 12
    tr = writes.trade_payload(1, SWID, 10, 2, [5], [9], now_ms=1000)
    assert tr["type"] == "TRADE_PROPOSAL" and {(i["fromTeamId"], i["toTeamId"]) for i in tr["items"]} == {(1, 2), (2, 1)}


def test_set_lineup_preview_then_confirm(football_data, monkeypatch):
    posted = []
    hub = writable_hub(football_data, monkeypatch, posted)
    preview = hub.set_lineup(start=["Jared Goff"], bench=["Tyler Shough"])
    assert preview["status"].startswith("PREVIEW") and not posted
    done = hub.set_lineup(start=["Jared Goff"], bench=["Tyler Shough"], confirm=True)
    assert done["status"] == "SUBMITTED" and len(posted) == 1
    assert posted[0]["type"] == "ROSTER" and posted[0]["teamId"] == 1 and posted[0]["scoringPeriodId"] == 10
    assert done["transaction_id"] == "tx-1"


def test_writes_disabled_by_default(football_data, monkeypatch):
    posted = []
    hub = writable_hub(football_data, monkeypatch, posted)
    hub.settings.allow_writes = False
    with pytest.raises(UserError, match="turned off"):
        hub.set_lineup(start=["Jared Goff"], bench=["Tyler Shough"], confirm=True)
    assert not posted


def test_add_player_rules(football_data, monkeypatch):
    posted = []
    hub = writable_hub(football_data, monkeypatch, posted)
    # The card fixture is Tee Higgins, who is on Team 1 already.
    with pytest.raises(UserError, match="Use the trade tools"):
        hub.add_player(add="Tee Higgins")
    # Make him a free agent and try with a full roster and no drop.
    football_data["card"]["players"][0]["onTeamId"] = 0
    football_data["card"]["players"][0]["status"] = "FREEAGENT"
    with pytest.raises(UserError, match="roster is full"):
        hub.add_player(add="Tee Higgins")
    prev = hub.add_player(add="Tee Higgins", drop="Tank Dell")
    assert prev["changes"] == ["Add free agent: Tee Higgins", "Drop: Tank Dell"] and not posted
    hub.add_player(add="Tee Higgins", drop="Tank Dell", confirm=True)
    assert posted[-1]["type"] == "FREEAGENT"
    # On waivers -> waiver claim
    football_data["card"]["players"][0]["status"] = "WAIVERS"
    hub.add_player(add="Tee Higgins", drop="Tank Dell", confirm=True)
    assert posted[-1]["type"] == "WAIVER"


def test_post_transaction_errors(monkeypatch):
    from espn_fantasy_hub.sports import FOOTBALL_SPORT

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    class Sess:
        def __init__(self, resp):
            self.resp, self.calls = resp, []

        def post(self, url, **kw):
            self.calls.append((url, kw))
            return self.resp

    sess = Sess(Resp(409, {"messages": ["Player is not on waivers"], "details": [{"message": "Roster full"}]}))
    monkeypatch.setattr(writes.http, "session", lambda: sess)
    with pytest.raises(writes.MoveError, match="Player is not on waivers; Roster full"):
        writes.post_transaction(FOOTBALL_SPORT, 2026, 1, {"espn_s2": "a", "SWID": SWID}, {"x": 1})
    url, kw = sess.calls[0]
    assert url.startswith("https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/")
    assert kw["allow_redirects"] is False and kw["headers"]["x-fantasy-platform"] == "espn-fantasy-web"


def test_ir_capacity_enforced(football_hub):
    view = football_hub.view(football_hub.handles()[0])
    entries = view.roster_entries(1)  # IR has 2 spots, both used (Monangai, Harvey)
    with pytest.raises(writes.MoveError, match="IR slot is full"):
        writes.plan_lineup(view, entries, moves={"Tank Dell": "IR"})
    # Swapping someone out of IR at the same time is fine.
    plan, steps, _ = writes.plan_lineup(view, entries, moves={"Tank Dell": "IR", "RJ Harvey": "BE"})
    assert len(plan) == 2


def test_slot_swap_between_starters(football_hub):
    view = football_hub.view(football_hub.handles()[0])
    entries = view.roster_entries(1)
    plan, steps, warnings = writes.plan_lineup(view, entries, moves={"Zach Charbonnet": "RB", "Bijan Robinson": "FLEX"})
    assert sorted(steps) == ["Bijan Robinson: RB → FLEX", "Zach Charbonnet: FLEX → RB"]
    assert not any("empty" in w for w in warnings)


def test_add_player_warns_when_dropping_better_player(football_data, monkeypatch):
    posted = []
    hub = writable_hub(football_data, monkeypatch, posted)
    football_data["card"]["players"][0]["onTeamId"] = 0
    football_data["card"]["players"][0]["status"] = "FREEAGENT"
    # Adding Tee Higgins (~17 pts/game) while dropping Bijan Robinson (~18.3) -> caution
    prev = hub.add_player(add="Tee Higgins", drop="Bijan Robinson")
    assert prev["warnings"][0].startswith("CAUTION: Bijan Robinson has MORE season value")
    assert prev["value_check"]["dropping"]["season_value"] > prev["value_check"]["adding"]["season_value"]
    # Dropping a weaker player -> no caution
    prev = hub.add_player(add="Tee Higgins", drop="Tank Dell")
    assert not any(w.startswith("CAUTION") for w in prev.get("warnings", []))
    assert not posted


def test_cancel_claim_payload():
    p = writes.cancel_claim_payload(1, SWID, 11, "abc-123")
    assert p["type"] == "WAIVER" and p["executionType"] == "CANCEL"
    assert p["relatedTransactionId"] == "abc-123" and p["items"] == [] and p["teamId"] == 1


def _claim(tid, team, add, drop, bid, ms, status="PENDING", related=None, kind="WAIVER"):
    tx = {"id": tid, "type": kind, "status": status, "teamId": team, "bidAmount": bid, "proposedDate": ms,
          "items": [{"type": "ADD", "playerId": add}] + ([{"type": "DROP", "playerId": drop}] if drop else [])}
    if related:
        tx["relatedTransactionId"] = related
    return tx


def test_pending_claims_and_cancel(football_data, monkeypatch):
    posted = []
    hub = writable_hub(football_data, monkeypatch, posted)
    h = hub.handles()[0]
    view = hub.view(h)
    me = view.my_team_id
    roster_ids = [player_of(e)["id"] for e in view.roster_entries(me)]
    a, b, c = 9001, 9002, 9003
    queue = {"pendingTransactions": [
        _claim("live-1", me, a, roster_ids[-1], 9, 1_700_000_000_000),
        _claim("old-1", me, b, roster_ids[-1], 3, 1_699_000_000_000),
        _claim("theirs", me + 1, c, None, 4, 1_700_000_000_000),
    ]}
    log = {"transactions": [_claim("cxl", me, b, None, 0, 1_700_100_000_000, status="CANCELED", related="old-1")]}
    monkeypatch.setattr(h.client, "pending_transactions", lambda: queue, raising=False)
    monkeypatch.setattr(h.client, "transactions", lambda sp, final=False: log)
    monkeypatch.setattr(hub, "_names", lambda h_, ids, sp: {a: "Chiefs D/ST", b: "Bills D/ST", c: "Jets D/ST"})
    out = hub.pending_claims()
    assert [x["id"] for x in out["pending_claims"]] == ["live-1"]   # cancelled and other teams' claims excluded
    assert out["pending_claims"][0]["add"] == ["Chiefs D/ST"]
    assert out["pending_claims"][0].get("bid") == (9 if view.uses_faab else None)
    with pytest.raises(UserError, match="No pending claim"):
        hub.cancel_claim(claim="Bills D/ST")
    prev = hub.cancel_claim(claim="chiefs d/st")
    assert prev["claim_id"] == "live-1" and not posted
    res = hub.cancel_claim(claim="live-1", confirm=True)
    assert posted[-1]["executionType"] == "CANCEL" and posted[-1]["relatedTransactionId"] == "live-1"
    assert res["status"] == "SUBMITTED" and res["verified_in_espn"] is False  # fake queue still lists it


def test_cancel_claim_needs_writes(football_hub):
    with pytest.raises(UserError, match="turned off"):
        football_hub.cancel_claim(claim="anything")
