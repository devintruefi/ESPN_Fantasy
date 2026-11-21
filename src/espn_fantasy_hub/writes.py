"""Roster moves: lineups, adds/drops, waiver claims, trade proposals.

Every move works in two steps:
  1. called without confirm -> validates and returns a preview (nothing sent)
  2. called again with confirm=True -> sent to ESPN, then re-read to verify

Writes are disabled unless ESPN_ALLOW_WRITES=1 (or allow_writes = true in the
config file).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import requests

from . import http
from .espn import EspnError, LeagueAccessError
from .sports import OUT_STATUSES, Sport
from .views import LeagueView, norm_name, player_of, ppe_of

WRITES_BASE = "https://lm-api-writes.fantasy.espn.com/apis/v3/games"
WRITE_HEADERS = {
    "x-fantasy-platform": "espn-fantasy-web",
    "x-fantasy-source": "kona",
    "content-type": "application/json",
    "accept": "application/json",
}


class MoveError(Exception):
    """The requested move is invalid; message explains why."""


# --------------------------------------------------------------------------
# payloads
# --------------------------------------------------------------------------


def base_payload(team_id: int, swid: str, scoring_period: int) -> dict:
    return {
        "isLeagueManager": False,
        "teamId": team_id,
        "memberId": swid,
        "scoringPeriodId": scoring_period,
        "executionType": "EXECUTE",
    }


def lineup_payload(team_id, swid, scoring_period, moves: list[tuple[int, int, int]], future: bool = False) -> dict:
    """A lineup change; a later day than today is a FUTURE_ROSTER transaction (ESPN refuses ROSTER for it)."""
    return base_payload(team_id, swid, scoring_period) | {
        "type": "FUTURE_ROSTER" if future else "ROSTER",
        "items": [
            {"playerId": pid, "type": "LINEUP", "fromLineupSlotId": frm, "toLineupSlotId": to}
            for pid, frm, to in moves
        ],
    }


def add_drop_payload(team_id, swid, scoring_period, add_id=None, drop_id=None, waiver=False, bid=None) -> dict:
    items = []
    if add_id:
        items.append({"playerId": add_id, "type": "ADD", "toTeamId": team_id})
    if drop_id:
        items.append({"playerId": drop_id, "type": "DROP", "fromTeamId": team_id})
    payload = base_payload(team_id, swid, scoring_period) | {
        "type": "WAIVER" if waiver else "FREEAGENT",
        "items": items,
    }
    if waiver:
        payload["bidAmount"] = int(bid or 0)
    return payload


def cancel_claim_payload(team_id, swid, scoring_period, claim_id: str) -> dict:
    """Withdraw a pending waiver claim: same transaction type, executionType CANCEL, pointing at the claim."""
    return base_payload(team_id, swid, scoring_period) | {
        "type": "WAIVER",
        "executionType": "CANCEL",
        "relatedTransactionId": claim_id,
        "items": [],
    }


def trade_payload(team_id, swid, scoring_period, partner_id, give_ids, get_ids, now_ms: int, days_valid=2) -> dict:
    items = [
        {"playerId": pid, "type": "TRADE", "fromTeamId": team_id, "toTeamId": partner_id} for pid in give_ids
    ] + [
        {"playerId": pid, "type": "TRADE", "fromTeamId": partner_id, "toTeamId": team_id} for pid in get_ids
    ]
    return base_payload(team_id, swid, scoring_period) | {
        "type": "TRADE_PROPOSAL",
        "comment": "",
        "expirationDate": now_ms + days_valid * 24 * 3600 * 1000,
        "items": items,
    }


# --------------------------------------------------------------------------
# posting
# --------------------------------------------------------------------------


def post_transaction(sport: Sport, season: int, league_id: int, cookies: dict, payload: dict) -> dict:
    if not cookies:
        raise LeagueAccessError("Making moves needs your ESPN login cookies (espn_s2 and SWID).")
    url = f"{WRITES_BASE}/{sport.game}/seasons/{season}/segments/0/leagues/{league_id}/transactions/"
    try:
        resp = http.session().post(
            url,
            data=json.dumps(payload),
            headers=WRITE_HEADERS,
            cookies=cookies,
            allow_redirects=False,  # never forward the session cookie elsewhere
            timeout=25,
        )
    except requests.exceptions.RequestException as e:
        raise EspnError(f"Couldn't reach ESPN to submit the move ({type(e).__name__}). Check the ESPN app before retrying.") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code in (401, 403):
        raise LeagueAccessError("ESPN rejected the move: your login cookies are missing, expired, or don't own this team.")
    if resp.status_code >= 300:
        raise MoveError("ESPN rejected the move: " + _espn_messages(body, resp.status_code))
    return body if isinstance(body, dict) else {"response": body}


def _espn_messages(body, status) -> str:
    msgs = []
    if isinstance(body, dict):
        for m in body.get("messages") or []:
            if isinstance(m, str):
                msgs.append(m)
        for d in body.get("details") or []:
            if isinstance(d, dict) and d.get("message"):
                msgs.append(str(d["message"]))
    return "; ".join(dict.fromkeys(msgs)) or f"HTTP {status}"


# --------------------------------------------------------------------------
# lineup planning
# --------------------------------------------------------------------------


def find_on_roster(view: LeagueView, entries: list[dict], name: str) -> dict:
    q = norm_name(name)
    exact = [e for e in entries if norm_name(player_of(e).get("fullName", "")) == q]
    if len(exact) == 1:
        return exact[0]
    partial = [e for e in entries if q and q in norm_name(player_of(e).get("fullName", ""))]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(player_of(e).get("fullName", "?") for e in entries)
    if not partial:
        raise MoveError(f"{name!r} isn't on your roster. Your players: {names}")
    raise MoveError(f"{name!r} matches several players: " + ", ".join(player_of(e).get("fullName") for e in partial))


def _slot_id(sport: Sport, view: LeagueView, slot_name: str) -> int:
    name = slot_name.strip().upper()
    aliases = {"BENCH": "BE", "DST": "D/ST", "DEF": "D/ST", "UTIL": "UT", "UTL": "UT"}
    name = aliases.get(name, name)
    for sid in list(view.slot_counts) + [sport.bench_slot, sport.ir_slot]:
        if sport.slot(sid).upper() == name:
            return sid
    raise MoveError(f"Unknown lineup slot {slot_name!r}. This league uses: " + ", ".join(
        sorted({sport.slot(s) for s in view.slot_counts})))


def plan_lineup(
    view: LeagueView,
    entries: list[dict],
    start: list[str] | None = None,
    bench: list[str] | None = None,
    moves: dict[str, str] | None = None,
    future: bool = False,
) -> tuple[list[tuple[int, int, int]], list[str], list[str]]:
    """Work out the ESPN slot moves for 'start these, bench those'.

    Returns (moves as (playerId, fromSlot, toSlot), human-readable steps, warnings).
    """
    sport = view.sport
    slot_of = {player_of(e).get("id"): e.get("lineupSlotId") for e in entries}
    by_id = {player_of(e).get("id"): e for e in entries}
    original = dict(slot_of)
    warnings: list[str] = []
    capacity = {sid: c for sid, c in view.slot_counts.items()}

    def check_movable(e):
        p = player_of(e)
        if ppe_of(e).get("lineupLocked") and not future:
            raise MoveError(f"{p.get('fullName')} is locked (his game has started), so he can't be moved now.")

    def occupants(sid):
        return [pid for pid, s in slot_of.items() if s == sid]

    for name in bench or []:
        e = find_on_roster(view, entries, name)
        check_movable(e)
        slot_of[player_of(e)["id"]] = sport.bench_slot

    for name, slot_name in (moves or {}).items():
        e = find_on_roster(view, entries, name)
        check_movable(e)
        sid = _slot_id(sport, view, slot_name)
        if sid not in (player_of(e).get("eligibleSlots") or []) and sid != sport.bench_slot:
            raise MoveError(f"{player_of(e).get('fullName')} isn't eligible for {sport.slot(sid)}.")
        slot_of[player_of(e)["id"]] = sid

    # Most specific slots first (e.g. WR before FLEX, C before UT).
    def specificity(sid):
        return sum(1 for e in entries if sid in (player_of(e).get("eligibleSlots") or []))

    for name in start or []:
        e = find_on_roster(view, entries, name)
        check_movable(e)
        p = player_of(e)
        pid = p["id"]
        if sport.is_starting_slot(slot_of.get(pid)):
            warnings.append(f"{p.get('fullName')} is already starting ({sport.slot(slot_of[pid])}).")
            continue
        options = [
            sid for sid in sorted(p.get("eligibleSlots") or [], key=specificity)
            if sport.is_starting_slot(sid) and sid in capacity and len(occupants(sid)) < capacity[sid]
        ]
        if not options:
            elig = ", ".join(sport.slot(s) for s in p.get("eligibleSlots") or [] if s in capacity and sport.is_starting_slot(s))
            raise MoveError(
                f"No open slot for {p.get('fullName')} (eligible: {elig}). Also say who to bench."
            )
        slot_of[pid] = options[0]

    for sid, cap in capacity.items():
        if sport.is_starting_slot(sid) and len(occupants(sid)) > cap:
            raise MoveError(f"Too many players in {sport.slot(sid)} ({len(occupants(sid))}/{cap}).")
    ir_cap = capacity.get(sport.ir_slot, 0)
    in_ir = occupants(sport.ir_slot)
    if len(in_ir) > ir_cap:
        names = ", ".join(player_of(by_id[p]).get("fullName", "?") for p in in_ir if original.get(p) == sport.ir_slot)
        raise MoveError(
            f"The IR slot is full ({ir_cap} spot{'s' if ir_cap != 1 else ''}"
            + (f", used by {names}" if names else "")
            + "). Move someone out of IR in the same request."
        )

    result, steps = [], []
    for pid, new in slot_of.items():
        if new != original.get(pid):
            e = by_id[pid]
            result.append((pid, original[pid], new))
            steps.append(f"{player_of(e).get('fullName')}: {sport.slot(original[pid])} → {sport.slot(new)}")
            injury = view._injury(e, player_of(e))
            if sport.is_starting_slot(new) and injury in OUT_STATUSES:
                warnings.append(f"{player_of(e).get('fullName')} is {injury} but would be starting.")
    for sid, cap in capacity.items():
        if sport.is_starting_slot(sid) and len(occupants(sid)) < cap:
            warnings.append(f"{sport.slot(sid)} slot would be left empty.")
    if not result:
        raise MoveError("Nothing to change — that's already your lineup.")
    return result, steps, warnings


def auto_lineup(view: LeagueView, entries: list[dict], period: int, value, future: bool = False):
    """Best lineup for one day: as many healthy players with a game as the slots allow, higher
    value first, each in a slot he's eligible for. Locked players (game started) stay put.

    Greedy by value with augmenting paths = a maximum-value matching of players to slots.
    Returns (moves as (playerId, fromSlot, toSlot), steps, warnings) like plan_lineup."""
    sport = view.sport
    capacity = {sid: c for sid, c in view.slot_counts.items() if sport.is_starting_slot(sid)}
    slots = [sid for sid, c in sorted(capacity.items()) for _ in range(c)]
    by_id = {player_of(e)["id"]: e for e in entries}
    original = {pid: e.get("lineupSlotId") for pid, e in by_id.items()}
    locked = {pid for pid, e in by_id.items() if ppe_of(e).get("lineupLocked") and not future}

    def plays(e):
        p = player_of(e)
        if e.get("lineupSlotId") == sport.ir_slot or view._injury(e, p) in OUT_STATUSES:
            return False
        g = view.pro.game(p.get("proTeamId"), period)
        return bool(g) and (future or g["start"] is None or g["start"] > view.pro.now)

    owner: dict[int, int] = {}            # slot index -> player id
    # locked starters keep their slots
    for pid in locked:
        sid = original[pid]
        if sport.is_starting_slot(sid):
            i = next((k for k, s in enumerate(slots) if s == sid and k not in owner), None)
            if i is not None:
                owner[i] = pid
    fixed = set(owner)

    def eligible(pid):
        el = set(player_of(by_id[pid]).get("eligibleSlots") or [])
        return [k for k, s in enumerate(slots) if s in el and k not in fixed]

    def augment(pid, seen):
        for k in eligible(pid):
            if k in seen:
                continue
            seen.add(k)
            if k not in owner or augment(owner[k], seen):
                owner[k] = pid
                return True
        return False

    playing = sorted((pid for pid, e in by_id.items() if pid not in locked and plays(e)),
                     key=lambda pid: value(by_id[pid]) or -99, reverse=True)
    for pid in playing:
        augment(pid, set())
    assigned = {pid: slots[k] for k, pid in owner.items()}
    # Fill leftover slots with players already there (no game either way), else leave empty.
    for k, s in enumerate(slots):
        if k in owner:
            continue
        keep = next((pid for pid, sid in original.items() if sid == s and pid not in assigned and pid not in locked
                     and s in (player_of(by_id[pid]).get("eligibleSlots") or [])), None)
        if keep is not None:
            assigned[keep] = s
            owner[k] = keep
    result, steps, warnings = [], [], []
    for pid, e in by_id.items():
        if pid in locked or original[pid] == sport.ir_slot:
            continue
        new = assigned.get(pid, sport.bench_slot)
        if new != original[pid]:
            result.append((pid, original[pid], new))
            steps.append(f"{player_of(e).get('fullName')}: {sport.slot(original[pid])} → {sport.slot(new)}")
    benched_playing = [player_of(by_id[pid]).get("fullName") for pid in playing if pid not in assigned]
    if benched_playing:
        warnings.append("More players have games than slots fit; sitting: " + ", ".join(benched_playing))
    empty = len(slots) - len(owner)
    if empty:
        warnings.append(f"{empty} starting slot(s) have nobody with a game.")
    if not result:
        raise MoveError("Nothing to change — every healthy player with a game is already starting.")
    return result, steps, warnings


def roster_limit(view: LeagueView) -> int:
    return sum(c for sid, c in view.slot_counts.items() if sid != view.sport.ir_slot)


def now_ms(now: dt.datetime) -> int:
    return int(now.timestamp() * 1000)
