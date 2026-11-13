"""Re-capture test fixtures from two public ESPN leagues, with people scrubbed.

    python tests/tools/capture_fixtures.py

Member names, account ids (SWIDs), team names and logos are replaced with
placeholders, so fixtures contain only public pro-player data.
"""

import gzip
import json
import re
import sys
from pathlib import Path

from espn_fantasy_hub.espn import LeagueClient
from espn_fantasy_hub.sports import BASKETBALL_SPORT, FOOTBALL_SPORT

OUT = Path(__file__).resolve().parents[1] / "fixtures"
GUID = re.compile(r"\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}?")
PERSONAL_KEYS = {"displayName", "firstName", "lastName"}
DROP_KEYS = {"notificationSettings", "logo", "logoType"}


class Scrubber:
    def __init__(self):
        self.guids = {}
        self.team_names = {}

    def guid(self, value: str) -> str:
        core = value.strip("{}").upper()
        if core not in self.guids:
            n = len(self.guids) + 1
            self.guids[core] = f"{{00000000-0000-0000-0000-{n:012d}}}"
        fake = self.guids[core]
        return fake if value.startswith("{") else fake.strip("{}")

    def __call__(self, obj, parent_key=None):
        if isinstance(obj, dict):
            out = {}
            is_team = "abbrev" in obj and "id" in obj and ("record" in obj or "owners" in obj)
            for k, v in obj.items():
                if k in DROP_KEYS:
                    continue
                is_member = isinstance(obj.get("id"), str) and GUID.fullmatch(obj["id"])
                if k in PERSONAL_KEYS and isinstance(v, str) and is_member:
                    out[k] = f"Person{int(self.guid(obj['id']).strip('{}')[-12:])}"
                    continue
                if is_team and k in ("name", "location", "nickname"):
                    out[k] = f"Team {obj['id']}"
                    continue
                if is_team and k == "abbrev":
                    out[k] = f"T{obj['id']}"
                    continue
                out[k] = self(v, k)
            return out
        if isinstance(obj, list):
            return [self(v, parent_key) for v in obj]
        if isinstance(obj, str) and GUID.fullmatch(obj):
            return self.guid(obj)
        return obj


BULKY_KEYS = {"outlooks", "seasonOutlook", "draftRanksByRankType", "rankings"}


def trim(obj):
    """Drop long text blobs the tests don't need (keeps fixtures small)."""
    if isinstance(obj, dict):
        return {k: trim(v) for k, v in obj.items() if k not in BULKY_KEYS}
    if isinstance(obj, list):
        return [trim(v) for v in obj]
    return obj


def save(name, data):
    data = {k: (v if k in ("card", "news") else trim(v)) for k, v in data.items()}
    if isinstance(data.get("news"), dict):
        feed = (data["news"].get("news") or {}).get("feed") or []
        data["news"] = {"news": {"feed": feed[:5]}}
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / f"{name}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"saved {name}")


def main():
    scrub = Scrubber()
    ff = LeagueClient(FOOTBALL_SPORT, 1086064, 2026, None)
    core = scrub(ff.core())
    core["settings"]["name"] = "Test Football League"
    week = core["scoringPeriodId"]
    mp = core["status"]["currentMatchupPeriod"]
    ids = set()
    football = {
        "core": core,
        "scoreboard_current": scrub(ff.scoreboard(mp, week)),
        "scoreboard_prev": scrub(ff.scoreboard(mp - 1, week - 1)),
        "pro_schedule": ff.pro_schedule(),
        "pool": scrub(ff.player_pool(week, limit=150)),
        "pool_rb": scrub(ff.player_pool(week, slot_ids=(2,), limit=150)),
        "card": scrub(ff.player_cards([4239993], 17, (f"11{ff.season}{week}",))),
        "transactions": scrub(ff.transactions(week)),
        "pos_ratings": ff.positional_ratings(week),
        "draft": scrub(ff.draft()),
        "news": ff.news(4239993),
    }
    for t in core["teams"]:
        ids |= {e["playerId"] for e in t["roster"]["entries"]}
    for tx in football["transactions"]["transactions"]:
        ids |= {it["playerId"] for it in tx.get("items") or []}
    ids |= {p["playerId"] for p in football["draft"]["draftDetail"].get("picks") or []}
    football["player_index"] = [p for p in ff.player_index() if p["id"] in ids or p["id"] == 4239993]
    save("football", football)

    bb = LeagueClient(BASKETBALL_SPORT, 1631984064, 2023, None)
    bcore = scrub(bb.core())
    bcore["settings"]["name"] = "Test Basketball League"
    basketball = {
        "core": bcore,
        "scoreboard_10": scrub(bb.scoreboard(10, 80)),
        "pro_schedule": bb.pro_schedule(),
        "pool": scrub(bb.player_pool(bcore["scoringPeriodId"], limit=150)),
        "card": scrub(bb.player_cards([3992], 175, ("012023", "022023", "032023"))),
    }
    bids = {e["playerId"] for t in bcore["teams"] for e in t["roster"]["entries"]}
    basketball["player_index"] = [p for p in bb.player_index() if p["id"] in bids or p["id"] == 3992]
    save("basketball", basketball)


if __name__ == "__main__":
    sys.exit(main())
