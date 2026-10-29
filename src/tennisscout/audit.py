"""First milestone: the data audit (spec, "First milestone: data audit").

Three layers:

1. Population checks over the whole TML backbone: identity problems,
   homonyms, timestamp precision.
2. Cross-source checks: how well Match Charting Project records link to our
   IDs, and where the two sources disagree about conditions or dates.
3. A stratified sample of 150 completed singles matches (2024-2025), each
   checked for everything the richer app would need: both players via internal
   IDs, opponent/score/round, surface/indoor/altitude, outdoor weather,
   market history, timestamps.

External services (Open-Meteo, Kalshi, Polymarket) are *probed live* when the
audit runs, so their status in the report is a measurement, not an assumption.
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
from collections import Counter, defaultdict
from datetime import date

from . import config, db, markets, weather
from .normalize import names_compatible

SAMPLE_YEARS = (2024, 2025)
SAMPLE_SEED = 20261029
STRATA = (  # (label, levels, n)
    ("Grand Slam", ("G",), 40),
    ("Masters 1000", ("M",), 40),
    ("ATP 500", ("500",), 30),
    ("ATP 250", ("250",), 30),
    ("Finals / team / Olympics", ("F", "A", "D", "O"), 10),
)


# -- 1. population checks ------------------------------------------------------

def population_identity(conn: sqlite3.Connection) -> dict:
    status = {r[0]: r[1] for r in conn.execute("SELECT identity_status, COUNT(*) FROM match GROUP BY 1")}
    conflicts = [
        dict(r)
        for r in conn.execute(
            """SELECT m.match_id, e.year, e.name AS tournament, m.round, m.identity_note
               FROM match m JOIN tournament_edition e USING (edition_id)
               WHERE m.identity_status = 'id_name_conflict' ORDER BY e.year"""
        )
    ]
    # Same normalized display name, different players (among players who actually played).
    homonyms = [
        {"name": r["name_norm"], "players": r["n"]}
        for r in conn.execute(
            """SELECT p.name_norm, COUNT(DISTINCT p.player_id) AS n FROM player p
               WHERE p.is_placeholder = 0 AND p.player_id IN (SELECT winner_id FROM match UNION SELECT loser_id FROM match)
               GROUP BY p.name_norm HAVING n > 1 ORDER BY n DESC, p.name_norm"""
        )
    ]
    # One player under several display names: spelling variants vs genuinely different people.
    names_by_player: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT player_id, name FROM player_name"):
        names_by_player[r["player_id"]].add(r["name"])
    variants = sum(1 for ns in names_by_player.values() if len(ns) > 1)
    return {
        "match_rows_by_identity_status": status,
        "id_name_conflicts": conflicts,
        "homonym_names": homonyms,
        "players_with_name_variants": variants,
    }


def population_timestamps(conn: sqlite3.Connection) -> dict:
    precision = {r[0]: r[1] for r in conn.execute("SELECT event_time_precision, COUNT(*) FROM match GROUP BY 1")}
    published = conn.execute("SELECT COUNT(*) FROM match WHERE published_at IS NOT NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM match").fetchone()[0]
    # Editions whose source date is not a plausible start: MCP charts with an exact date
    # land outside the TML edition window.
    outside = conn.execute(
        """SELECT COUNT(DISTINCT m.edition_id) FROM charting_match c JOIN match m USING (match_id)
           WHERE c.link_note LIKE 'MCP date outside TML edition window%'"""
    ).fetchone()[0]
    return {
        "match_rows": total,
        "event_time_precision": precision,
        "rows_with_publication_time": published,
        "editions_with_dates_contradicted_by_mcp": outside,
        "latest_event_date": db.get_meta(conn, "latest_event_date"),
    }


# -- 2. cross-source -------------------------------------------------------------

def cross_source(conn: sqlite3.Connection) -> dict:
    def counts(where: str = "1=1") -> dict:
        return {r[0]: r[1] for r in conn.execute(f"SELECT link_status, COUNT(*) FROM charting_match WHERE {where} GROUP BY 1")}

    player_status = Counter()
    for r in conn.execute("SELECT p1_status, p2_status FROM charting_match WHERE link_status != 'malformed_row'"):
        player_status[r[0]] += 1
        player_status[r[1]] += 1
    surface_disagree = conn.execute(
        "SELECT COUNT(*) FROM charting_match WHERE link_status = 'linked' AND link_note LIKE '%surface disagrees%'"
    ).fetchone()[0]
    carpet = conn.execute(
        "SELECT COUNT(*) FROM charting_match WHERE link_status = 'linked' AND link_note LIKE '%MCP Hard, TML Carpet%'"
    ).fetchone()[0]
    linked = conn.execute("SELECT COUNT(*) FROM charting_match WHERE link_status = 'linked'").fetchone()[0]
    with_time = conn.execute(
        "SELECT COUNT(*) FROM charting_match WHERE link_status = 'linked' AND start_time IS NOT NULL"
    ).fetchone()[0]
    fuzzy = [
        dict(r)
        for r in conn.execute(
            """SELECT name, player_id, COUNT(*) AS charts FROM (
                 SELECT p1_name AS name, p1_player_id AS player_id FROM charting_match WHERE p1_status = 'resolved_via_match_fuzzy'
                 UNION ALL
                 SELECT p2_name, p2_player_id FROM charting_match WHERE p2_status = 'resolved_via_match_fuzzy')
               GROUP BY name, player_id ORDER BY charts DESC"""
        )
    ]
    for f in fuzzy:
        f["tml_name"] = conn.execute("SELECT display_name FROM player WHERE player_id = ?", (f["player_id"],)).fetchone()[0]
    return {
        "charts_all_years": counts(),
        "charts_2019_on": counts("match_date >= '2019-01-01'"),
        "player_name_resolution": dict(player_status),
        "linked": linked,
        "linked_surface_disagreements": surface_disagree,
        "linked_hard_vs_carpet": carpet,
        "linked_with_start_time": with_time,
        "alias_suggestions_from_unique_joins": fuzzy,
    }


# -- 3. the match sample ------------------------------------------------------------

def draw_sample(conn: sqlite3.Connection) -> list[dict]:
    rng = random.Random(SAMPLE_SEED)
    sample = []
    for label, levels, n in STRATA:
        q = f"""SELECT m.match_id FROM match m JOIN tournament_edition e USING (edition_id)
                WHERE e.year BETWEEN ? AND ? AND m.outcome = 'completed'
                  AND COALESCE(m.level, e.level) IN ({','.join('?' * len(levels))})
                ORDER BY m.match_id"""
        ids = [r[0] for r in conn.execute(q, (*SAMPLE_YEARS, *levels))]
        for mid in rng.sample(ids, min(n, len(ids))):
            sample.append({"match_id": mid, "stratum": label})
    return sample


def check_match(conn: sqlite3.Connection, match_id: str, model: str, weather_ok: bool, markets_ok: bool) -> dict:
    m = conn.execute(
        """SELECT m.*, e.name AS tournament, e.year, e.start_date, e.end_date_bound, e.indoor AS ed_indoor,
                  e.surface AS ed_surface, e.latitude, e.longitude, e.elevation_m, e.venue_source, e.venue_city
           FROM match m JOIN tournament_edition e USING (edition_id) WHERE m.match_id = ?""",
        (match_id,),
    ).fetchone()
    chart = conn.execute("SELECT * FROM charting_match WHERE match_id = ?", (match_id,)).fetchone()
    pred = conn.execute("SELECT * FROM prediction WHERE model = ? AND match_id = ?", (model, match_id)).fetchone()

    def name_ok(pid, src_name):
        if not pid:
            return False
        row = conn.execute("SELECT display_name FROM player WHERE player_id = ?", (pid,)).fetchone()
        return bool(row) and names_compatible(row[0], src_name)

    indoor = m["indoor"] or m["ed_indoor"]
    outdoor = indoor == "O"
    has_coords = m["latitude"] is not None and m["longitude"] is not None
    return {
        "match_id": match_id,
        "year": m["year"],
        "tournament": m["tournament"],
        "round": m["round"],
        "level": m["level"],
        # identity
        "both_players_internal_id": bool(m["winner_id"] and m["loser_id"]),
        "identity_status": m["identity_status"],
        "names_agree_with_player_record": name_ok(m["winner_id"], m["winner_src_name"]) and name_ok(m["loser_id"], m["loser_src_name"]),
        # result fields
        "has_score": bool(m["score"]),
        "has_round": bool(m["round"]),
        "has_serve_stats": m["w_svpt"] is not None and m["l_svpt"] is not None,
        "has_pre_match_ranks": m["winner_rank"] is not None and m["loser_rank"] is not None,
        # conditions
        "has_surface": bool(m["surface"] or m["ed_surface"]),
        "has_indoor_flag": indoor in ("I", "O"),
        "indoor_flag_granularity": "tournament" if indoor in ("I", "O") else "missing",
        "has_altitude": m["elevation_m"] is not None,
        "altitude_source": m["venue_source"] if m["elevation_m"] is not None else None,
        "outdoor": outdoor,
        "weather_joinable": (outdoor and has_coords and weather_ok) if outdoor else None,
        "weather_status": None if not outdoor else (
            "no venue coordinates" if not has_coords else ("joinable" if weather_ok else "Open-Meteo blocked from this environment")
        ),
        # timestamps
        "event_time_precision": m["event_time_precision"],
        "event_window_days": _days(m["event_date_lo"], m["event_date_hi"]),
        "has_publication_time": m["published_at"] is not None,
        "has_collection_time": m["collected_at"] is not None,
        "exact_date_from_mcp": chart["match_date"] if chart else None,
        "start_time_from_mcp": chart["start_time"] if chart else None,
        "point_by_point_available": chart is not None,
        # markets
        "market_history": "not checked: hosts blocked" if not markets_ok else "unchecked: matcher not built yet",
        # model
        "pre_match_prediction": pred["p_player1"] if pred else None,
        "pre_match_confidence": pred["confidence"] if pred else None,
    }


def _days(lo: str | None, hi: str | None) -> int | None:
    if not lo or not hi:
        return None
    return (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1


def summarize_sample(rows: list[dict]) -> dict:
    n = len(rows)

    def share(key, val=True):
        return sum(1 for r in rows if r[key] == val) / n if n else 0.0

    outdoor = [r for r in rows if r["outdoor"]]
    return {
        "n": n,
        "by_stratum": dict(Counter(r["stratum"] for r in rows)),
        "both_players_internal_id": share("both_players_internal_id"),
        "identity_ok": share("identity_status", "ok"),
        "names_agree": share("names_agree_with_player_record"),
        "has_score_round": sum(1 for r in rows if r["has_score"] and r["has_round"]) / n,
        "has_serve_stats": share("has_serve_stats"),
        "has_pre_match_ranks": share("has_pre_match_ranks"),
        "has_surface": share("has_surface"),
        "has_indoor_flag": share("has_indoor_flag"),
        "indoor_flag_match_level": 0.0,
        "has_altitude_seed": share("has_altitude"),
        "outdoor_matches": len(outdoor),
        "outdoor_with_coordinates": sum(1 for r in outdoor if r["weather_status"] != "no venue coordinates"),
        "outdoor_weather_joined": sum(1 for r in outdoor if r["weather_joinable"]),
        "event_time_precision": dict(Counter(r["event_time_precision"] for r in rows)),
        "median_event_window_days": sorted(r["event_window_days"] or 0 for r in rows)[n // 2] if n else None,
        "has_publication_time": share("has_publication_time"),
        "has_collection_time": share("has_collection_time"),
        "exact_date_via_mcp": sum(1 for r in rows if r["exact_date_from_mcp"]) / n,
        "start_time_via_mcp": sum(1 for r in rows if r["start_time_from_mcp"]) / n,
        "point_by_point_available": share("point_by_point_available"),
        "has_pre_match_prediction": sum(1 for r in rows if r["pre_match_prediction"] is not None) / n,
        "market_history_checked": 0.0,
    }


def run(conn: sqlite3.Connection) -> dict:
    probes = {"open_meteo": weather.probe(), "markets": markets.probe()}
    weather_ok = all(p["reachable"] for p in probes["open_meteo"])
    markets_ok = all(p["reachable"] for p in probes["markets"])
    model = db.get_meta(conn, "serving_model", "elo_selected")

    sample = draw_sample(conn)
    rows = [{**s, **check_match(conn, s["match_id"], model, weather_ok, markets_ok)} for s in sample]
    result = {
        "run_at": config.utcnow(),
        "snapshots": [dict(r) for r in conn.execute("SELECT snapshot_id, source, revision, revision_date, collected_at FROM source_snapshot")],
        "external_probes": probes,
        "population_identity": population_identity(conn),
        "population_timestamps": population_timestamps(conn),
        "cross_source": cross_source(conn),
        "sample_summary": summarize_sample(rows),
        "sample_match_ids": [r["match_id"] for r in rows],
    }
    out = config.OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "audit_sample.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "audit.json").write_text(json.dumps(result, indent=2, default=str))
    with conn:
        db.set_meta(conn, "audit_result", json.dumps(result, default=str))
    return result
