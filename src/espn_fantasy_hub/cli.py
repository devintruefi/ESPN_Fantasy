"""Command line: `espn-fantasy-hub` (MCP server), `setup`, `doctor`."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_settings, normalize_swid
from .espn import EspnError
from .service import Hub


def _mask(value: str | None) -> str:
    if not value:
        return "(not set)"
    return value[:6] + "…" + value[-4:] if len(value) > 12 else "set"


def doctor() -> int:
    """Check the configuration against live ESPN data and print what Claude will see."""
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"✗ Config problem: {e}")
        return 1
    print(f"Config file: {settings.config_path or '(none — using environment variables)'}")
    print(f"espn_s2: {_mask(settings.espn_s2)}   SWID: {_mask(settings.swid)}")
    hub = Hub(settings)
    handles = hub.handles()
    if hub.discovery_error:
        print(f"! Automatic league lookup failed: {hub.discovery_error}")
    if not handles:
        print("✗ No leagues found. " + hub.no_leagues_message())
        return 1
    ok = True
    for h in handles:
        src = "found automatically" if h.cfg.discovered else "from config"
        try:
            view = hub.view(h)
            me = view.my_team_id
            mine = f"your team: {view.team_name(me)} ({view.record_str(me)})" if me else "your team: NOT IDENTIFIED (set team_id)"
            print(f"✓ {h.sport.key:10} {view.name} — id {h.cfg.league_id}, season {view.season}, {len(view.teams)} teams, {mine} [{src}]")
            if me is None:
                ok = False
        except EspnError as e:
            ok = False
            print(f"✗ {h.sport.key:10} league {h.cfg.league_id} (season {h.season}) [{src}]: {e}")
    return 0 if ok else 2


def setup() -> int:
    """Interactive setup that writes the config file."""
    path = Path(os.environ.get("ESPN_FANTASY_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    print("ESPN Fantasy Hub setup\n")
    print("1. Log in at https://www.espn.com/fantasy/ in your browser.")
    print("2. Open developer tools → Application (Chrome) / Storage (Firefox/Safari) → Cookies → https://www.espn.com")
    print("3. Copy the values of the cookies named espn_s2 and SWID.\n")
    s2 = getpass.getpass("espn_s2 (input hidden): ").strip()
    swid = input("SWID (looks like {XXXXXXXX-XXXX-...}): ").strip()
    try:
        swid = normalize_swid(swid) or ""
    except ConfigError as e:
        print(f"✗ {e}")
        return 1
    print("\nYour leagues are normally found automatically. To add them by hand, enter league ids")
    print("(the leagueId=... number in the league's URL). Leave blank to skip.")
    leagues = []
    for sport in ("football", "basketball"):
        ids = input(f"{sport} league ids, comma separated: ").strip()
        for lid in filter(None, (x.strip() for x in ids.split(","))):
            if not lid.isdigit():
                print(f"  skipping {lid!r} (not a number)")
                continue
            leagues.append((sport, lid))
    lines = [
        "# ESPN Fantasy Hub config. Keep this file private: the cookies give access to your ESPN account.",
        f'espn_s2 = "{s2}"',
        f'swid = "{swid}"',
        "auto_discover = true",
        "",
    ]
    for sport, lid in leagues:
        lines += ["[[league]]", f'sport = "{sport}"', f"id = {lid}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print(f"\nSaved {path}\n")
    return doctor()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="espn-fantasy-hub", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="Run the MCP server over stdio (default)")
    sub.add_parser("setup", help="Interactive setup: save your ESPN cookies and leagues")
    sub.add_parser("doctor", help="Check your config against live ESPN data")
    args = parser.parse_args(argv)
    if args.cmd == "setup":
        sys.exit(setup())
    if args.cmd == "doctor":
        sys.exit(doctor())
    from .server import serve

    serve()


if __name__ == "__main__":
    main()
