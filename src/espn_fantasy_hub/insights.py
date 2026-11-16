"""Turning outside data into decisions (pure: no network). See ``external.py`` for sources.

- id maps: Sleeper / nflverse / PFR ids -> ESPN player ids
- market trends: who managers are adding and dropping, and whether he's available here
- usage: snaps, targets, target share, carries; role changes; TD-dependence (buy/sell signals)
- game environment: Vegas lines -> implied team totals; stadium weather flags; D/ST board
- waiver plan: lineup gain from each free agent, FAAB bid from this league's history, and
  drops limited to rotation spots (never a regular starter)
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .views import compact, norm_name, num, rnd

# ---- ids ---------------------------------------------------------------------------
def _clean(v) -> str | None:
    v = (v or "").strip()
    return None if v in ("", "NA", "None", "nan") else v


def nfl_index(idmap: list[dict] | None) -> dict:
    """{'espn': {espn_id: row}, 'sleeper': {sleeper_id: espn_id}}."""
    by_espn, by_sleeper = {}, {}
    for r in idmap or []:
        e = _clean(r.get("espn_id"))
        if not e or not e.isdigit():
            continue
        e = int(e)
        by_espn[e] = r
        s = _clean(r.get("sleeper_id"))
        if s:
            by_sleeper[s] = e
    return {"espn": by_espn, "sleeper": by_sleeper}


# ---- market trends ---------------------------------------------------------------------
def availability(view, pid, league_players: dict) -> str:
    p = league_players.get(pid)
    if not p:
        return "not in the league's top free agents (check ESPN)"
    owner = p.get("owner")
    if owner in (None, "FA", 0):
        return "on waivers" if p.get("waivers") else "free agent"
    return "your team" if owner == view.my_team_id else f"rostered ({view.team_label(owner)})"


def market_trends(view, adds, drops, to_espn, league_players: dict, limit: int = 15) -> dict:
    """Sleeper's 24-hour adds/drops mapped to this league. ``to_espn``: sleeper id -> ESPN id
    (or a name-based resolver returning one)."""
    def rows(items):
        out = []
        for it in (items or [])[:limit]:
            pid = to_espn(str(it.get("player_id")))
            p = league_players.get(pid) if pid else None
            out.append(compact({
                "player": p["name"] if p else None, "sleeper_id": None if p else it.get("player_id"),
                "pos": p.get("pos") if p else None, "team": p.get("nfl") or p.get("team") if p else None,
                "managers_moving_24h": it.get("count"),
                "here": availability(view, pid, league_players) if pid else "unmatched",
            }))
        return [r for r in out if r.get("player")]
    a, d = rows(adds), rows(drops)
    return {
        "most_added_24h": a or None,
        "most_dropped_24h": d or None,
        "available_and_trending": [r for r in a if r["here"] in ("free agent", "on waivers")] or None,
        "your_players_being_dropped": [r for r in d if r["here"] == "your team"] or None,
        "note": "Counts are adds/drops across Sleeper leagues in the last 24 hours: a read on news and "
                "demand, not on value. Check the reason before acting.",
    }


# ---- usage (football) ------------------------------------------------------------------
NFLVERSE_TEAM = {"LA": "LAR", "WAS": "WSH", "LV": "LV", "JAC": "JAX"}


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def usage_table(weekly: list[dict] | None, snaps: list[dict] | None, index: dict, players: list[dict],
                recent: int = 3) -> list[dict]:
    """Per ESPN player: role and volume this season vs his last ``recent`` games, with flags."""
    if not weekly:
        return []
    by_gsis: dict[str, list[dict]] = defaultdict(list)
    by_name: dict[tuple, list[dict]] = defaultdict(list)
    for r in weekly:
        if r.get("season_type") not in (None, "", "REG"):
            continue
        by_gsis[r.get("player_id")].append(r)
        by_name[(norm_name(r.get("player_display_name") or ""), NFLVERSE_TEAM.get(r.get("team"), r.get("team")))].append(r)
    snap_by: dict[str, dict[int, float]] = defaultdict(dict)
    snap_name: dict[tuple, dict[int, float]] = defaultdict(dict)
    for r in snaps or []:
        if r.get("game_type") not in (None, "", "REG"):
            continue
        wk = int(_f(r.get("week")))
        pct = _f(r.get("offense_pct"))
        snap_by[r.get("pfr_player_id")][wk] = pct
        snap_name[(norm_name(r.get("player") or ""), NFLVERSE_TEAM.get(r.get("team"), r.get("team")))][wk] = pct
    out = []
    for p in players:
        if p.get("pos") not in ("QB", "RB", "WR", "TE"):
            continue
        row = index.get("espn", {}).get(p["id"]) or {}
        gsis, pfr = _clean(row.get("gsis_id")), _clean(row.get("pfr_id"))
        key = (norm_name(p["name"] or ""), p.get("nfl"))
        games = sorted(by_gsis.get(gsis) or by_name.get(key) or [], key=lambda r: int(_f(r.get("week"))))
        if not games:
            continue
        snap = snap_by.get(pfr) or snap_name.get(key) or {}
        last = games[-recent:]

        def avg(rows, k):
            return sum(_f(r.get(k)) for r in rows) / len(rows) if rows else 0.0

        def snap_avg(rows):
            v = [snap[int(_f(r.get("week")))] for r in rows if int(_f(r.get("week"))) in snap]
            return 100 * sum(v) / len(v) if v else None

        ppr = sum(_f(r.get("fantasy_points_ppr")) for r in games)
        tds = sum(_f(r.get("rushing_tds")) + _f(r.get("receiving_tds")) for r in games)
        td_share = 6 * tds / ppr if ppr > 0 else 0.0
        opp_season = avg(games, "targets") + avg(games, "carries")
        s_season, s_last = snap_avg(games), snap_avg(last)
        ts_season, ts_last = 100 * avg(games, "target_share"), 100 * avg(last, "target_share")
        car_season, car_last = avg(games, "carries"), avg(last, "carries")
        flags = []
        if s_season is not None and s_last is not None and len(games) > recent:
            if s_last >= s_season + 10:
                flags.append("snap share rising")
            elif s_last <= s_season - 10:
                flags.append("snap share falling")
        if len(games) > recent and p["pos"] in ("WR", "TE", "RB"):
            if ts_last >= ts_season + 5:
                flags.append("target share rising")
            elif ts_last <= ts_season - 5:
                flags.append("target share falling")
        if len(games) > recent and p["pos"] == "RB":
            if car_last >= car_season + 4:
                flags.append("carries rising")
            elif car_last <= car_season - 4:
                flags.append("carries falling")
        if p["pos"] != "QB" and ppr >= 30:
            if td_share >= 0.4 and opp_season < 12:
                flags.append("TD-dependent: sell-high candidate")
            elif td_share <= 0.15 and (opp_season >= 15 or ts_season >= 22):
                flags.append("volume without TDs: buy-low candidate")
        out.append(compact({
            "player": p["name"], "pos": p["pos"], "team": p.get("nfl"), "games": len(games),
            "snap_pct": f"{s_season:.0f}% season / {s_last:.0f}% last {len(last)}" if s_season is not None and s_last is not None else None,
            "targets_per_game": f"{avg(games, 'targets'):.1f} / {avg(last, 'targets'):.1f}" if p["pos"] != "QB" else None,
            "target_share": f"{ts_season:.0f}% / {ts_last:.0f}%" if p["pos"] != "QB" else None,
            "air_yards_share": f"{100 * avg(games, 'air_yards_share'):.0f}%" if p["pos"] in ("WR", "TE") else None,
            "carries_per_game": f"{car_season:.1f} / {car_last:.1f}" if p["pos"] in ("RB", "QB") else None,
            "pass_attempts_per_game": f"{avg(games, 'attempts'):.1f} / {avg(last, 'attempts'):.1f}" if p["pos"] == "QB" else None,
            "td_share_of_points": f"{100 * td_share:.0f}%" if ppr else None,
            "flags": flags or None,
        }))
    return out


# ---- game environment --------------------------------------------------------------------
# Outdoor stadium coordinates by home team (ESPN abbreviation). Indoor/retractable venues come
# from ESPN's own venue flag, so they don't need an entry.
STADIUMS = {
    "BUF": (42.774, -78.787), "MIA": (25.958, -80.239), "NE": (42.091, -71.264), "NYJ": (40.814, -74.074),
    "NYG": (40.814, -74.074), "BAL": (39.278, -76.623), "CIN": (39.095, -84.516), "CLE": (41.506, -81.700),
    "PIT": (40.447, -80.016), "DEN": (39.744, -105.020), "KC": (39.049, -94.484), "CHI": (41.862, -87.617),
    "GB": (44.501, -88.062), "PHI": (39.901, -75.168), "WSH": (38.908, -76.864), "CAR": (35.226, -80.853),
    "TB": (27.976, -82.503), "SF": (37.403, -121.970), "SEA": (47.595, -122.332), "JAX": (30.324, -81.637),
    "TEN": (36.166, -86.771), "LAR": (33.953, -118.339), "LAC": (33.953, -118.339),
}


def game_lines(sb: dict | None) -> dict:
    """{'games': [...], 'byes': [team ids]} from ESPN's scoreboard."""
    games = []
    for ev in (sb or {}).get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        h, a = teams.get("home") or {}, teams.get("away") or {}
        odds = (comp.get("odds") or [None])[0] or {}
        spread, total = odds.get("spread"), odds.get("overUnder")
        venue = comp.get("venue") or {}
        g = {
            "id": ev.get("id"), "start": ev.get("date"),
            "home_id": int((h.get("team") or {}).get("id") or 0), "home": (h.get("team") or {}).get("abbreviation"),
            "away_id": int((a.get("team") or {}).get("id") or 0), "away": (a.get("team") or {}).get("abbreviation"),
            "spread": num(spread) if spread is not None else None, "total": num(total) if total is not None else None,
            "indoor": bool(venue.get("indoor")), "venue": venue.get("fullName"),
            "country": (venue.get("address") or {}).get("country"),
            "status": ((comp.get("status") or {}).get("type") or {}).get("name"),
        }
        if g["total"] is not None and g["spread"] is not None:
            g["home_implied"] = round((g["total"] - g["spread"]) / 2, 1)
            g["away_implied"] = round((g["total"] + g["spread"]) / 2, 1)
        games.append(g)
    byes = [int(t.get("id")) for t in ((sb or {}).get("week") or {}).get("teamsOnBye") or [] if t.get("id")]
    return {"games": games, "byes": byes}


def weather_points(games: list[dict]) -> list[tuple[str, tuple[float, float]]]:
    """(game id, (lat, lon)) for outdoor US games with a known stadium."""
    out = []
    for g in games:
        if g["indoor"] or (g.get("country") not in (None, "USA")) or g["home"] not in STADIUMS:
            continue
        if g.get("status") not in (None, "STATUS_SCHEDULED"):
            continue   # already started or over
        out.append((g["id"], STADIUMS[g["home"]]))
    return out


def weather_flags(game: dict, forecast: dict | None) -> dict | None:
    """Wind / rain / snow / cold around kickoff (kickoff hour through 3 hours later)."""
    if not forecast or not game.get("start"):
        return None
    try:
        kick = dt.datetime.fromisoformat(game["start"].replace("Z", "+00:00"))
    except ValueError:
        return None
    hourly = forecast.get("hourly") or {}
    times = hourly.get("time") or []
    idx = [i for i, t in enumerate(times)
           if 0 <= (dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc) - kick.replace(minute=0)).total_seconds() <= 3 * 3600]
    if not idx:
        return {"note": "beyond the 7-day forecast"}
    wind = max(num(hourly["wind_speed_10m"][i]) for i in idx)
    rain = max(num(hourly["precipitation_probability"][i]) for i in idx)
    temp = min(num(hourly["temperature_2m"][i]) for i in idx)
    flags = []
    if wind >= 20:
        flags.append("very windy: downgrade passing and kickers")
    elif wind >= 15:
        flags.append("windy: small downgrade for deep passing and kickers")
    if rain >= 60:
        flags.append("snow likely" if temp <= 34 else "rain likely")
    if temp <= 20:
        flags.append("very cold")
    return compact({"wind_mph": round(wind), "precip_chance_pct": round(rain), "temp_f": round(temp), "flags": flags or None})


def dst_board(view, world, lines: dict, week: int, me) -> list[dict]:
    """Free-agent and your D/STs for ``week``: ESPN projection (the best single signal; D/ST
    scoring is mostly noise) and the opponent's Vegas implied total (lower is better)."""
    implied_against = {}
    for g in lines.get("games") or []:
        if "home_implied" in g:
            implied_against[g["home_id"]] = g["away_implied"]   # the home D faces the away offense
            implied_against[g["away_id"]] = g["home_implied"]
    rows = []
    for pid in list(world.free_agents) + list(world.rosters.get(me, [])):
        p = world.players[pid]
        if p["pos"] != "D/ST":
            continue
        if view.pro.game(p["pro_id"], week) is None:
            continue
        proj = p["by_week"].get(week)
        rows.append(compact({
            "dst": p["name"], "yours": True if p["owner"] == me else None,
            "espn_projection": rnd(proj if proj is not None else p["base"], 1),
            "opponent_implied_points": implied_against.get(p["pro_id"]),
        }))
    rows.sort(key=lambda r: (-(r.get("espn_projection") or 0), r.get("opponent_implied_points") or 99))
    return rows[:10]


# ---- waiver plan (football) ----------------------------------------------------------------
def faab_bid(gain: float, hist: dict | None, left: float, contested: bool, pos: str | None = None) -> str:
    """A bid from this league's own winning-bid history, scaled by how much the player helps.
    Defenses and kickers are streamers: $0-1 unless the gain is large."""
    if pos in ("D/ST", "K") and gain < 2.5:
        return "$1-$2" if contested else "$0-$1"
    if not hist:
        p50, p75, p90 = 3, 8, 15
    else:
        p50, p75, p90 = hist.get("median_winning_bid", 3), hist.get("75th_percentile", 8), hist.get("90th_percentile", 15)
    if gain >= 2.5:
        lo, hi = p75, max(p90, p75 + 1)
    elif gain >= 1.0:
        lo, hi = p50, p75
    else:
        lo, hi = 0, 1
    if contested and gain >= 1.0:
        lo, hi = hi, round(hi * 1.3)
    cap = max(1, round(0.4 * left))
    lo, hi = min(lo, cap), min(hi, cap)
    return f"${lo:g}" if lo == hi else f"${lo:g}-${hi:g}"


def waiver_plan_football(world, me, faab: dict | None, faab_left: float | None, trending: set,
                         max_drops: int = 2, top: int = 8) -> dict:
    """Free agents ranked by how much they raise your starting lineup (60% rest of regular season,
    40% playoff weeks, ESPN projections), each paired with the drop that costs least. Drops come
    only from your weakest bench spots, never a regular starter or an injured player worth stashing."""
    from .ffmodel import regular_starters

    view = world.view
    mine = world.team(me)
    core = regular_starters(view, mine)
    base = world.lineup_score(mine)
    droppable = [p for p in mine if p["name"] not in core and not p["in_ir"]
                 and not (p["status"] in ("OUT", "INJURY_RESERVE") and p["base"] >= 12)]
    droppable.sort(key=lambda p: world.lineup_score([q for q in mine if q is not p]) - base, reverse=True)
    rotation = droppable[:max_drops]
    rows = []
    for pid in world.free_agents:
        p = world.players[pid]
        if p["base"] < 3 or p["status"] in ("INJURY_RESERVE",):
            continue
        best = None
        for d in rotation:
            g = world.lineup_score([q for q in mine if q is not d] + [p]) - base
            if best is None or g > best[0]:
                best = (g, d)
        if not best or best[0] <= 0.2:
            continue
        g, d = best
        g_avg = world.lineup_score([q for q in mine if q is not d] + [p], "avg") - world.lineup_score(mine, "avg")
        hot = p["id"] in trending
        rows.append(compact({
            "add": f"{p['name']} ({p['pos']}, {p['nfl']})", "status": p["status"],
            "drop": f"{d['name']} ({d['pos']})",
            "lineup_gain_per_week": rnd(g, 1), "gain_by_season_averages": rnd(g_avg, 1),
            "trending_on_sleeper": True if hot else None,
            "faab_bid": faab_bid(g, faab, faab_left, hot, p["pos"]) if faab_left is not None else None,
            "_g": g,
        }))
    rows.sort(key=lambda r: -r["_g"])
    for r in rows:
        r.pop("_g")
    return {
        "rotation_spots": [f"{p['name']} ({p['pos']})" for p in rotation],
        "protected": sorted(core),
        "best_moves": rows[:top] or "No free agent beats your weakest bench spots by enough to bother.",
        "faab_left": faab_left,
        "faab_history": faab,
        "rules": [
            "Only your weakest bench spots (rotation_spots) are ever suggested as drops; regular starters "
            "and injured players worth stashing are protected.",
            "Bids come from this league's winning bids: a big lineup gain bids near the top of what wins "
            "here, a small one bids $0-1; never more than 40% of what you have left.",
            "Gains are points a week of your starting lineup (ESPN projections), 60% rest of regular "
            "season and 40% playoff weeks.",
        ],
    }
