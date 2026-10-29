"""Load stored API-Tennis fixtures into the database under permanent IDs.

* Players map onto existing internal IDs only on full name plus matching
  birthdate (or an exact, unique name when no birthdate is on record); anyone
  else gets a new internal ID. The mapping is checked against TML on the
  January 2026 overlap.
* ATP matches inside TML's coverage are not inserted again (TML stays the
  record there); later ones and all Challenger matches are added.
* After inserting, every match is resequenced so the replay order interleaves
  both sources chronologically.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from . import api_tennis, config, db, registry, venues
from .ingest_charting import NameIndex, find_links, tour_match_index
from .normalize import SURFACES, norm_name, round_order

ROUNDS = {"1/64-finals": "R128", "1/32-finals": "R64", "1/16-finals": "R32", "1/8-finals": "R16",
          "quarter-finals": "QF", "semi-finals": "SF", "final": "F"}
SLAMS = ("australian open", "french open", "roland garros", "wimbledon", "us open")
OUTCOMES = {"Finished": "completed", "Retired": "retired", "Walk Over": "walkover"}


def _bday(s: str | None) -> str | None:
    # "12.09.1998" -> "19980912", the TML format
    if not s or s.count(".") != 2:
        return None
    d, m, y = s.split(".")
    return f"{y}{m}{d}"


def _near(a: str, b: str) -> bool:
    """Same year and at most 31 days apart (YYYYMMDD strings)."""
    try:
        da, db_ = date(int(a[:4]), int(a[4:6]), int(a[6:])), date(int(b[:4]), int(b[4:6]), int(b[6:]))
    except ValueError:
        return False
    return da.year == db_.year and abs((da - db_).days) <= 31


def _surface(raw: str | None) -> tuple[str | None, str | None]:
    """API-Tennis "Hard (Indoor)" -> ("Hard", "I"); labels that are not surfaces ("- Play Offs") -> (None, None)."""
    s = (raw or "").strip()
    name = s.split("(")[0].strip().capitalize()
    return (name if name in SURFACES else None), ("I" if "(indoor)" in s.lower() else None)


def _clean_name(s: str) -> str:
    s = (s or "").strip()
    return s[4:] if s.upper().startswith("ATP ") else s


def _stat(stats: list, player_key: int, name: str) -> tuple[int, int] | None:
    for s in stats or ():
        if s.get("player_key") == player_key and s.get("stat_period") == "match" and s.get("stat_name") == name:
            if s.get("stat_won") is not None and s.get("stat_total"):
                return int(s["stat_won"]), int(s["stat_total"])
    return None


def _serve(stats, player_key) -> tuple[int | None, ...]:
    """(serve points, 1st serve in, 1st serve won, 2nd serve won), TML's columns, from the match stat lines."""
    first, second = _stat(stats, player_key, "1st serve points won"), _stat(stats, player_key, "2nd serve points won")
    if not first or not second:
        return None, None, None, None
    return first[1] + second[1], first[1], first[0], second[0]


def _score(scores: list, winner_first: bool) -> str:
    sets = []
    for s in sorted(scores or (), key=lambda x: int(x.get("score_set") or 0)):
        a, b = str(s.get("score_first") or ""), str(s.get("score_second") or "")
        if not winner_first:
            a, b = b, a
        ga, _, ta = a.partition(".")
        gb, _, tb = b.partition(".")
        if not ga or not gb:
            continue
        tb_pts = min(int(ta), int(tb)) if ta and tb else None
        sets.append(f"{ga}-{gb}" + (f"({tb_pts})" if tb_pts is not None else ""))
    return " ".join(sets)


def map_players(conn: sqlite3.Connection, api_players: dict[int, dict], preg) -> tuple[dict[int, str], Counter, list]:
    """API player key -> internal player ID; returns the map, method counts and new player rows."""
    names = NameIndex.from_db(conn)
    bdays = {r[0]: r[1] for r in conn.execute("SELECT player_id, birthdate FROM player")}
    bday_index: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    for r in conn.execute("SELECT player_id, name_norm, birthdate FROM player WHERE birthdate IS NOT NULL"):
        bday_index[r[2]].append((r[0], {t for t in r[1].split() if len(t) > 2}))
    out, how, new_rows = {}, Counter(), []
    keys = sorted(api_players)
    for k in keys:
        p = api_players[k]
        existing = preg.lookup("api_tennis", str(k))
        full = (p.get("player_full_name") or p.get("player_name") or "").strip()
        bday = _bday(p.get("player_bday"))
        if existing:
            out[k] = existing
            how["registry"] += 1
            if existing not in bdays:  # minted by an earlier API-Tennis ingest; the TML step rebuilt the player table
                new_rows.append((existing, full or str(k), norm_name(full), None, None, bday, None, None, 0))
            continue
        cands = names.candidates(full) or names.fuzzy_candidates(full)
        by_bday = {c for c in cands if bday and bdays.get(c) == bday}
        exact = names.candidates(full)
        pid = None
        tokens = {t for t in norm_name(full).split() if len(t) > 2}
        same_day = [pid_ for pid_, toks in bday_index.get(bday, ()) if toks & tokens] if bday else []
        only = next(iter(exact)) if len(exact) == 1 else None
        if len(by_bday) == 1:
            pid, method = next(iter(by_bday)), "name+birthdate"
        elif only and not (bday and bdays.get(only) and bdays[only] != bday):
            pid, method = only, "exact_name"
        elif only and bday and bdays.get(only) and _near(bdays[only], bday):
            pid, method = only, "exact_name+near_birthdate"  # a typo in one source's birthdate
        elif len(same_day) == 1:
            pid, method = same_day[0], "birthdate+surname_token"  # reordered names ("John Wolf Jeffrey")
        if pid:
            preg.alias(pid, "api_tennis", str(k))
            how[method] += 1
        else:
            pid = preg.get_or_mint("api_tennis", str(k))
            new_rows.append((pid, full or str(k), norm_name(full), None, None, bday, None, None, 0))
            how["new_player"] += 1
        out[k] = pid
    return out, how, new_rows


def ingest(conn: sqlite3.Connection, registry_dir: Path | None = None, log=print) -> dict:
    fixtures = [f for f in api_tennis.fixtures() if f.get("event_status") in OUTCOMES
                and f.get("first_player_key") and f.get("second_player_key")]
    tournaments = {t["tournament_key"]: t for t in api_tennis.load(api_tennis.RAW / "tournaments.json.gz")["result"]}
    api_players = api_tennis.players()
    for f in fixtures:  # players missing from tournament lists still need an identity
        for side in ("first", "second"):
            api_players.setdefault(f[f"{side}_player_key"], {"player_full_name": None, "player_name": f[f"event_{side}_player"]})
    tml_end = db.get_meta(conn, "tml_coverage_end") or db.get_meta(conn, "latest_event_date")
    collected_at = config.utcnow()

    preg, treg = registry.players(registry_dir), registry.tournaments(registry_dir)
    ereg, mreg = registry.editions(registry_dir), registry.matches(registry_dir)
    pmap, how, new_players = map_players(conn, api_players, preg)

    # TML editions by name, for level / indoor / best-of of the same event.
    tml_by_name: dict[str, sqlite3.Row] = {}
    for r in conn.execute("SELECT name, level, indoor, year FROM tournament_edition ORDER BY year"):
        tml_by_name[norm_name(r["name"])] = r
    vindex = venues.VenueIndex(venues.load())

    editions: dict[tuple, list[dict]] = defaultdict(list)
    for f in fixtures:
        editions[(f["tournament_key"], f["tournament_season"])].append(f)

    # Overlap check: ATP fixtures inside TML coverage should land on a TML match with the mapped IDs.
    idx = tour_match_index(conn)
    overlap = Counter()
    for f in fixtures:
        if f["event_type_type"] == "Atp Singles" and f["event_date"] <= tml_end and f.get("event_qualification") != "True":
            a, b = pmap[f["first_player_key"]], pmap[f["second_player_key"]]
            rnd = ROUNDS.get((f.get("tournament_round") or "").split(" - ")[-1].strip().lower())
            hit = find_links({a}, {b}, date.fromisoformat(f["event_date"]), rnd, idx, 2, 2)
            overlap["linked_to_tml" if hit else "no_tml_match"] += 1

    ed_rows, match_rows, mext = [], [], []
    tour_seen = {}
    for (tkey, season), fs in editions.items():
        t = tournaments.get(tkey, {})
        name = _clean_name(fs[0]["tournament_name"])
        is_chall = fs[0]["event_type_type"] == "Challenger Men Singles"
        if not is_chall and max(f["event_date"] for f in fs) <= tml_end:
            continue  # TML holds this edition
        year = int(season) if str(season).isdigit() else int(fs[0]["event_date"][:4])
        tml = tml_by_name.get(norm_name(name))
        slam = any(s in name.lower() for s in SLAMS)
        level = "C" if is_chall else ("G" if slam else (tml["level"] if tml else "A"))
        tour_id = treg.get_or_mint("api_tennis", str(tkey), collected_at)
        tour_seen[tour_id] = name
        ed_id = ereg.get_or_mint("api_tennis", f"{tkey}:{season}", collected_at)
        dates = sorted(f["event_date"] for f in fs)
        v = vindex.lookup(name, year)
        surface, indoor = _surface(t.get("tournament_sourface"))
        ed_rows.append((ed_id, tour_id, f"api:{tkey}:{season}", year, name, level, surface,
                        indoor or (tml["indoor"] if tml and not is_chall else None), None, dates[0], dates[-1], "day",
                        v.city if v else None, v.latitude if v else None, v.longitude if v else None,
                        v.elevation_m if v else None, venues.VENUE_SOURCE if v else None, "api_tennis"))
        for f in fs:
            if not is_chall and f["event_date"] <= tml_end:
                continue
            first_won = f.get("event_winner") == "First Player"
            if f.get("event_winner") not in ("First Player", "Second Player"):
                continue
            wk, lk = (f["first_player_key"], f["second_player_key"]) if first_won else (f["second_player_key"], f["first_player_key"])
            rnd = "Q" if f.get("event_qualification") == "True" else ROUNDS.get((f.get("tournament_round") or "").split(" - ")[-1].strip().lower())
            w_sv, w_in, w_1st, w_2nd = _serve(f.get("statistics"), wk)
            l_sv, l_in, l_1st, l_2nd = _serve(f.get("statistics"), lk)
            start = f"{f['event_date']}T{f['event_time']}:00Z" if f.get("event_time") else None
            mid = mreg.get_or_mint("api_tennis", str(f["event_key"]), collected_at)
            best_of = 5 if (slam and rnd != "Q") else 3
            match_rows.append({
                "match_id": mid, "edition_id": ed_id, "seq": 0, "round": rnd, "round_order": round_order(rnd or ""),
                "best_of": best_of, "winner_id": pmap[wk], "loser_id": pmap[lk], "winner_src_id": str(wk), "loser_src_id": str(lk),
                "winner_src_name": f["event_first_player" if first_won else "event_second_player"],
                "loser_src_name": f["event_second_player" if first_won else "event_first_player"],
                "score": _score(f.get("scores"), first_won), "outcome": OUTCOMES[f["event_status"]],
                "surface": surface, "indoor": None, "level": level,
                "w_svpt": w_sv, "w_1stin": w_in, "w_1stwon": w_1st, "w_2ndwon": w_2nd,
                "l_svpt": l_sv, "l_1stin": l_in, "l_1stwon": l_1st, "l_2ndwon": l_2nd,
                "event_date_lo": f["event_date"], "event_date_hi": f["event_date"], "start_time": start,
                "event_time_precision": "minute" if start else "day", "published_at": None, "collected_at": collected_at,
                "available_at": f"{f['event_date']}T23:59:59Z", "availability_basis": "event_date_upper_bound",
                "identity_status": "ok", "source": "api_tennis", "source_key": str(f["event_key"]), "snapshot_id": "api_tennis",
            })
            mext.append(("api_tennis", str(f["event_key"]), mid))

    with conn:
        conn.execute("DELETE FROM match_external_id WHERE source = 'api_tennis'")
        conn.execute("DELETE FROM match WHERE source = 'api_tennis'")
        conn.execute("DELETE FROM tournament_edition WHERE snapshot_id = 'api_tennis'")
        conn.executemany("INSERT OR IGNORE INTO player VALUES (?,?,?,?,?,?,?,?,?)", new_players)
        conn.executemany("INSERT OR IGNORE INTO player_external_id VALUES (?,?,?)",
                         [("api_tennis", str(k), pid) for k, pid in pmap.items()])
        # API full names become lookup variants (helps chart and market linking).
        conn.executemany("INSERT OR IGNORE INTO player_name VALUES (?,?,?,?,?)",
                         [(pid, n, norm_name(n), "api_tennis", 1) for k, pid in pmap.items()
                          if (n := (api_players[k].get("player_full_name") or "").strip())])
        conn.executemany("INSERT OR IGNORE INTO tournament VALUES (?,?,?)", [(tid, None, n) for tid, n in tour_seen.items()])
        conn.executemany(f"INSERT INTO tournament_edition VALUES ({','.join('?' * 18)})", ed_rows)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(match)")]
        rows = [tuple(m.get(c) for c in cols) for m in match_rows]
        conn.executemany(f"INSERT INTO match VALUES ({','.join('?' * len(cols))})", rows)
        conn.executemany("INSERT OR IGNORE INTO match_external_id VALUES (?,?,?)", mext)
    for reg in (preg, treg, ereg, mreg):
        reg.save()
    resequence(conn)
    latest = conn.execute("SELECT MAX(event_date_hi) FROM match").fetchone()[0]
    report = {"fixtures": len(fixtures), "matches_added": len(match_rows), "editions_added": len(ed_rows),
              "player_mapping": dict(how), "overlap_check": dict(overlap),
              "with_serve_stats": sum(1 for m in match_rows if m["w_svpt"] and m["l_svpt"]),
        "challenger_matches": sum(1 for m in match_rows if m["level"] == "C"),
              "with_start_time": sum(1 for m in match_rows if m["start_time"]), "latest_event_date": latest}
    with conn:
        db.set_meta(conn, "latest_event_date", latest)
        db.set_meta(conn, "api_tennis_ingest_report", json.dumps(report))
    log(json.dumps(report))
    return report


def resequence(conn: sqlite3.Connection) -> None:
    """Global replay order across sources: edition start, edition, round, match day, start time, old order."""
    rows = conn.execute(
        """SELECT m.match_id, e.start_date, e.source_key, m.round_order, m.event_date_lo, m.start_time, m.seq
           FROM match m JOIN tournament_edition e USING (edition_id)"""
    ).fetchall()
    rows.sort(key=lambda r: (r[1] or "0000", r[2], r[3] or 0, r[4] or "", r[5] or "", r[6]))
    with conn:
        conn.executemany("UPDATE match SET seq = ? WHERE match_id = ?", [(i, r[0]) for i, r in enumerate(rows, start=1)])
