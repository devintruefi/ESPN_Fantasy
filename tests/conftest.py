import copy
import datetime as dt
import gzip
import json
from pathlib import Path

import pytest

from espn_fantasy_hub.config import LeagueConfig, Settings
from espn_fantasy_hub.service import Hub, LeagueHandle

FIXTURES = Path(__file__).parent / "fixtures"

# Fixture snapshot times (UTC): football captured Fri Nov 13 2026 (NFL week 10);
# basketball is a completed 2022-23 season.
FOOTBALL_NOW = dt.datetime(2026, 11, 13, 22, 0, tzinfo=dt.timezone.utc)
BASKETBALL_NOW = dt.datetime(2023, 4, 12, 18, 0, tzinfo=dt.timezone.utc)


def load(name):
    with gzip.open(FIXTURES / f"{name}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


_CACHE = {}


def fixture_data(name):
    if name not in _CACHE:
        _CACHE[name] = load(name)
    return copy.deepcopy(_CACHE[name])


class FakeClient:
    """Stands in for LeagueClient, serving saved ESPN responses."""

    def __init__(self, data, sport):
        self.d = data
        self.sport = sport
        self.calls = []

    def core(self):
        return self.d["core"]

    def roster_for_period(self, scoring_period):
        self.calls.append(("roster", scoring_period))
        return self.d.get("roster_override", self.d["core"])

    def scoreboard(self, matchup_period, scoring_period):
        self.calls.append(("scoreboard", matchup_period, scoring_period))
        if "scoreboard_current" in self.d:
            cur = self.d["core"]["status"]["currentMatchupPeriod"]
            return self.d["scoreboard_current"] if matchup_period == cur else self.d["scoreboard_prev"]
        return self.d["scoreboard_10"]

    def player_pool(self, scoring_period, statuses=("FREEAGENT", "WAIVERS"), slot_ids=(), limit=100):
        if slot_ids and "pool_rb" in self.d and slot_ids == (2,):
            return self.d["pool_rb"]
        return self.d["pool"]

    def player_cards(self, player_ids, final_scoring_period, extra_splits=()):
        self.calls.append(("cards", tuple(extra_splits)))
        return self.d["card"]

    def transactions(self, scoring_period):
        if scoring_period == self.d["core"]["scoringPeriodId"] and "transactions" in self.d:
            return self.d["transactions"]
        return {"transactions": []}

    def positional_ratings(self, scoring_period):
        return self.d.get("pos_ratings", {})

    def draft(self):
        return self.d.get("draft", {"draftDetail": {"drafted": False}})

    def pro_schedule(self):
        return self.d["pro_schedule"]

    def player_index(self):
        return self.d["player_index"]

    def news(self, player_id):
        return self.d.get("news", {})

    def clear(self):
        pass


def make_hub(leagues, now, swid=None):
    """leagues: list of (LeagueConfig, data dict)."""
    settings = Settings(espn_s2="x" if swid else None, swid=swid, leagues=[c for c, _ in leagues], auto_discover=False)
    hub = Hub(settings, tz=dt.timezone.utc, now_fn=lambda: now)
    handles = []
    for cfg, data in leagues:
        h = LeagueHandle(cfg, settings)
        h.client = FakeClient(data, h.sport)
        handles.append(h)
    hub._handles = handles
    return hub


@pytest.fixture
def football_data():
    return fixture_data("football")


@pytest.fixture
def basketball_data():
    return fixture_data("basketball")


@pytest.fixture
def football_hub(football_data):
    cfg = LeagueConfig(sport="football", league_id=1086064, season=2026, team_id=1)
    return make_hub([(cfg, football_data)], FOOTBALL_NOW)


@pytest.fixture
def basketball_hub(basketball_data):
    cfg = LeagueConfig(sport="basketball", league_id=1631984064, season=2023, team_id=1)
    return make_hub([(cfg, basketball_data)], BASKETBALL_NOW)
