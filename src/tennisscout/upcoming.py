"""Upcoming ATP singles from API-Tennis, each with the served pre-match prediction (spec MVP step 3).

Players are matched to internal IDs through the API-Tennis mappings made at ingest; a player who
has never been ingested is listed without a prediction rather than guessed. Surface and indoor come
from the event's recent TML editions (as at ingest), else the API-Tennis tournament list. When Kalshi
lists the match, its current price (mid, margin removed) sits next to the model's.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import api_tennis, http, markets, predict
from .ingest_api_tennis import ROUNDS, SLAMS, _clean_name, _surface, tml_event, tml_history
from .ingest_charting import NameIndex


def kalshi_quotes(conn: sqlite3.Connection) -> dict[frozenset, dict[str, dict]]:
    """Open Kalshi ATP match markets keyed by the pair of internal IDs; names that are not unique are skipped."""
    r = http.get(f"{markets.KALSHI_BASE}/markets", {"series_ticker": "KXATPMATCH", "status": "open", "limit": 1000})
    names = NameIndex.from_db(conn)
    by_event: dict[str, dict[str, dict]] = {}
    for m in (r.json().get("markets") or []) if r.ok else []:
        c = names.candidates(m.get("yes_sub_title") or "")
        if len(c) == 1 and m.get("yes_bid_dollars") and m.get("yes_ask_dollars"):
            by_event.setdefault(m["event_ticker"], {})[next(iter(c))] = {
                "bid": float(m["yes_bid_dollars"]), "ask": float(m["yes_ask_dollars"]), "ticker": m["ticker"]}
    return {frozenset(v): v for v in by_event.values() if len(v) == 2}


def rows(conn: sqlite3.Connection, fixtures: list[dict], tournaments: dict, quotes: dict | None = None) -> list[dict]:
    ext = {r[0]: r[1] for r in conn.execute("SELECT external_id, player_id FROM player_external_id WHERE source = 'api_tennis'")}
    hist = tml_history(conn)
    out = []
    for f in sorted(fixtures, key=lambda f: (f["event_date"], f.get("event_time") or "")):
        if f.get("event_winner") or f.get("event_status") or f.get("event_qualification") == "True":
            continue  # finished, live, cancelled or qualifying
        name = _clean_name(f["tournament_name"])
        surface, indoor = _surface(tournaments.get(f["tournament_key"], {}).get("tournament_sourface"))
        t_surface, _, t_indoor = tml_event(hist, name, int(f["event_date"][:4]))  # same rule as the ingest
        surface, indoor = t_surface or surface, indoor or t_indoor
        best_of = 5 if any(s in name.lower() for s in SLAMS) else 3
        row = {"event_key": f["event_key"], "start": f"{f['event_date']}T{f.get('event_time') or '00:00'}:00Z",
               "tournament": name, "round": ROUNDS.get(label := (f.get("tournament_round") or "").split(" - ")[-1].strip().lower(), label),
               "player_a": f["event_first_player"], "player_b": f["event_second_player"],
               "surface": surface, "indoor": indoor == "I", "best_of": best_of}
        a, b = ext.get(str(f["first_player_key"])), ext.get(str(f["second_player_key"]))
        if not (a and b and surface):
            row["error"] = "player not in the registry yet" if not (a and b) else "surface unknown"
        else:
            p = predict.predict(conn, a, b, surface, indoor == "I", best_of)
            row.update(player_a_id=a, player_b_id=b, p_a=round(p.p_a, 4), model=p.model, confidence=p.confidence)
            q = (quotes or {}).get(frozenset((a, b)))
            if q:  # mid prices, normalised across both contracts to remove the margin
                ma, mb = (q[a]["bid"] + q[a]["ask"]) / 2, (q[b]["bid"] + q[b]["ask"]) / 2
                row.update(kalshi_p_a=round(ma / (ma + mb), 4), kalshi_bid_a=q[a]["bid"], kalshi_ask_a=q[a]["ask"])
        out.append(row)
    return out


def upcoming(conn: sqlite3.Connection, days: int = 2) -> list[dict]:
    today = date.today()
    fixtures = api_tennis._call("get_fixtures", event_type_key=api_tennis.EVENT_TYPES["atp"],
                                date_start=today.isoformat(), date_stop=(today + timedelta(days=days)).isoformat())
    path = api_tennis.RAW / "tournaments.json.gz"
    tournaments = {t["tournament_key"]: t for t in api_tennis.load(path)["result"]} if path.exists() else {}
    return rows(conn, fixtures, tournaments, kalshi_quotes(conn))
