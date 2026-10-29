"""API-Tennis (api-tennis.com): recent ATP and Challenger results with start times and serve stats.

Fills the gap after the TML mirror ends (Jan 2026) and adds Challenger
singles, which TML does not carry. Raw responses are stored per month with
their collection time; parsing happens from the stored files.

The key is read from ``API_TENNIS_KEY`` (never committed).
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import date, timedelta
from pathlib import Path

from . import config, http

BASE = "https://api.api-tennis.com/tennis/"
EVENT_TYPES = {"atp": 265, "challenger": 281}
RAW = config.RAW_DIR / "api_tennis"


def _call(method: str, **params) -> list:
    key = os.environ.get("API_TENNIS_KEY")
    if not key:
        raise RuntimeError("Set API_TENNIS_KEY (e.g. `set -a; . data/secrets.env; set +a`).")
    r = http.get(BASE, {"method": method, "APIkey": key, "timezone": "UTC", **params}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"{method}: {r.error}")
    d = r.json()
    if str(d.get("success")) != "1":
        raise RuntimeError(f"{method}: {d.get('error')} {d.get('result')}")
    return d.get("result") or []


def _store(path: Path, params: dict, result) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"collected_at": config.utcnow(), "params": params, "result": result}, fh)


def load(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _months(start: date, end: date):
    d = start.replace(day=1)
    while d <= end:
        nxt = (d + timedelta(days=32)).replace(day=1)
        yield d, min(nxt - timedelta(days=1), end)
        d = nxt


def fetch_fixtures(kind: str, start: date, end: date, refresh_days: int = 10, log=print) -> int:
    """One file per month; months that ended more than ``refresh_days`` ago are fetched once."""
    n = 0
    for lo, hi in _months(start, end):
        path = RAW / kind / f"{lo:%Y-%m}.json.gz"
        if path.exists() and (date.today() - hi).days > refresh_days:
            continue
        params = {"event_type_key": EVENT_TYPES[kind], "date_start": lo.isoformat(), "date_stop": hi.isoformat()}
        result = _call("get_fixtures", **params)
        _store(path, params, result)
        n += 1
        log(f"{kind} {lo:%Y-%m}: {len(result)} fixtures")
    return n


def fetch_tournaments() -> int:
    result = _call("get_tournaments")
    _store(RAW / "tournaments.json.gz", {}, result)
    return len(result)


def fetch_players(log=print) -> int:
    """Player records (full name, birthdate) per tournament seen in the stored fixtures."""
    keys = sorted({f["tournament_key"] for f in fixtures()})
    n = 0
    for k in keys:
        path = RAW / "players" / f"{k}.json.gz"
        if path.exists():
            continue
        try:
            result = _call("get_players", tournament_key=k)
        except RuntimeError as e:  # some tournaments have no player list; keep going
            result = {"error": str(e)}
        _store(path, {"tournament_key": k}, result)
        n += 1
        if n % 100 == 0:
            log(f"players: {n} tournaments fetched")
    return n


def fixtures() -> list[dict]:
    out = []
    for kind in EVENT_TYPES:
        for path in sorted((RAW / kind).glob("*.json.gz")):
            out += load(path)["result"]
    return out


def players() -> dict[int, dict]:
    out: dict[int, dict] = {}
    for path in sorted((RAW / "players").glob("*.json.gz")):
        res = load(path)["result"]
        for p in res if isinstance(res, list) else []:
            out[p["player_key"]] = {k: p.get(k) for k in ("player_key", "player_name", "player_full_name", "player_country", "player_bday")}
    return out
