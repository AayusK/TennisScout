"""Load the TML-Database mirror into the database under permanent IDs."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import db, registry, venues
from .normalize import (
    add_days,
    edition_span_days,
    names_compatible,
    norm_name,
    outcome,
    parse_yyyymmdd,
    round_order,
    to_int,
)
from .sources import Snapshot

STAT_COLS = ("ace", "df", "svpt", "1stIn", "1stWon", "2ndWon", "SvGms", "bpSaved", "bpFaced")
PLACEHOLDER_NAMES = {"unknown unknown", "unknown"}


@dataclass
class IngestReport:
    players: int
    tournaments: int
    editions: int
    matches: int
    identity_status: dict[str, int]
    outcome: dict[str, int]
    date_precision: dict[str, int]
    latest_event_date: str
    duplicate_rows_skipped: int


def read_text_mixed(path: Path) -> str:
    """ATP_Database.csv is UTF-8 except for a couple of cp1252 lines."""
    out = []
    for line in path.read_bytes().split(b"\n"):
        try:
            out.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(line.decode("cp1252"))
    return "\n".join(out)


def read_player_db(path: Path) -> dict[str, dict]:
    rows = csv.DictReader(io.StringIO(read_text_mixed(path)))
    return {r["id"].strip(): r for r in rows if r.get("id", "").strip()}


def read_match_rows(mirror: Path) -> list[tuple[str, dict]]:
    files = sorted(p for p in mirror.glob("[12][0-9][0-9][0-9].csv"))
    ongoing = mirror / "ongoing_tourneys.csv"
    if ongoing.exists():
        files.append(ongoing)
    rows: list[tuple[str, dict]] = []
    for f in files:
        with open(f, newline="", encoding="utf-8") as fh:
            rows.extend((f.name, r) for r in csv.DictReader(fh))
    return rows


def side_identity(src_id: str, src_name: str, pdb: dict[str, dict]) -> tuple[str, str | None]:
    """Is this (source ID, display name) pair trustworthy enough to train on?"""
    if not src_id:
        return "missing_id", f"no ATP ID for {src_name!r}"
    rec = pdb.get(src_id)
    if norm_name(src_name) in PLACEHOLDER_NAMES or (rec and norm_name(rec["player"]) in PLACEHOLDER_NAMES):
        return "placeholder", f"placeholder player {src_id}"
    if rec is None:
        return "unknown_id", f"ATP ID {src_id} ({src_name!r}) not in player database"
    if not (names_compatible(src_name, rec["player"]) or names_compatible(src_name, rec.get("atpname", ""))):
        return "id_name_conflict", f"row says {src_name!r} but ATP ID {src_id} is {rec['player']!r}"
    return "ok", None


def ingest(conn: sqlite3.Connection, snap: Snapshot, registry_dir: Path | None = None) -> IngestReport:
    mirror = Path(snap.path)
    pdb = read_player_db(mirror / "ATP_Database.csv")
    raw_rows = read_match_rows(mirror)

    preg = registry.players(registry_dir)
    treg = registry.tournaments(registry_dir)
    ereg = registry.editions(registry_dir)
    mreg = registry.matches(registry_dir)
    vindex = venues.VenueIndex(venues.load())
    collected_at = snap.collected_at

    # ---- de-duplicate (ongoing file overlaps year files once a year is closed)
    seen_keys: set[str] = set()
    rows: list[tuple[str, dict, str]] = []
    dup_skipped = 0
    for fname, r in raw_rows:
        tid = r["tourney_id"].strip()
        mnum = r["match_num"].strip()
        key = f"{tid}#{mnum}" if mnum else f"{tid}#{r['round']}#{r['winner_id']}#{r['loser_id']}"
        if key in seen_keys:
            alt = f"{tid}#{mnum}#{r['round']}#{r['winner_id']}#{r['loser_id']}"
            if fname == "ongoing_tourneys.csv" or alt in seen_keys:
                dup_skipped += 1
                continue
            key = alt
        seen_keys.add(key)
        rows.append((fname, r, key))

    # ---- players: everyone in the player DB plus any ID that only appears in matches
    match_names: dict[str, Counter] = defaultdict(Counter)
    for _, r, _ in rows:
        for side in ("winner", "loser"):
            sid = r[f"{side}_id"].strip()
            if sid:
                match_names[sid][r[f"{side}_name"].strip()] += 1

    player_rows = []
    ext_rows = []
    for sid in sorted(set(pdb) | set(match_names)):
        pid = preg.get_or_mint("atp", sid, collected_at)
        rec = pdb.get(sid)
        name = rec["player"].strip() if rec else match_names[sid].most_common(1)[0][0]
        player_rows.append(
            (
                pid,
                name,
                norm_name(name),
                (rec or {}).get("hand") or None,
                (rec or {}).get("backhand") or None,
                (rec or {}).get("birthdate") or None,
                to_int((rec or {}).get("height")),
                (rec or {}).get("ioc") or None,
                int(norm_name(name) in PLACEHOLDER_NAMES),
            )
        )
        ext_rows.append(("atp", sid, pid))

    name_rows = []
    for sid, rec in pdb.items():
        pid = preg.lookup("atp", sid)
        for nm in {rec["player"].strip(), rec.get("atpname", "").strip()} - {""}:
            name_rows.append((pid, nm, norm_name(nm), "tml_player_db", 1))
    # Names from rows whose (ID, name) pair failed the identity check are kept
    # out: one TML row files Ivan Lendl under Jimmy Connors's ID, and letting
    # that name into the index would make "Ivan Lendl" ambiguous everywhere.
    for sid, names in match_names.items():
        pid = preg.lookup("atp", sid)
        for nm, n in names.items():
            if side_identity(sid, nm, pdb)[0] == "ok":
                name_rows.append((pid, nm, norm_name(nm), "tml_match_rows", n))

    # ---- editions
    by_edition: dict[str, list[tuple[str, dict, str]]] = defaultdict(list)
    for item in rows:
        by_edition[item[1]["tourney_id"].strip()].append(item)

    tourn_rows: dict[str, tuple] = {}
    edition_rows = []
    edition_meta: dict[str, dict] = {}
    for tid, items in by_edition.items():
        code = tid.split("-", 1)[1] if "-" in tid else tid
        tour_id = treg.get_or_mint("atp_tourney", code, collected_at)
        ed_id = ereg.get_or_mint("tml", tid, collected_at)
        dates = sorted({d for d in (parse_yyyymmdd(r["tourney_date"]) for _, r, _ in items) if d})

        def mode(col: str) -> str | None:
            c = Counter(r[col].strip() for _, r, _ in items if r[col].strip())
            return c.most_common(1)[0][0] if c else None

        name = mode("tourney_name") or tid
        level = mode("tourney_level")
        draw = to_int(mode("draw_size"))
        year = int(tid[:4]) if tid[:4].isdigit() else (dates[0].year if dates else 0)
        start = dates[0] if dates else None
        # TML started dating each match individually in 2026. Before that an
        # edition has one date (its week), occasionally with a stray second
        # date on a row or two, which must not be mistaken for match dates.
        precision = "day" if len(dates) >= 3 else "tourney_week"
        end_bound = dates[-1] if precision == "day" else (add_days(start, edition_span_days(level or "", draw)) if start else None)
        v = vindex.lookup(name, year)
        edition_rows.append(
            (
                ed_id, tour_id, tid, year, name, level, mode("surface"), mode("indoor"), draw,
                start.isoformat() if start else None,
                end_bound.isoformat() if end_bound else None,
                precision,
                v.city if v else None,
                v.latitude if v else None,
                v.longitude if v else None,
                v.elevation_m if v else None,
                venues.VENUE_SOURCE if v else None,
                snap.snapshot_id,
            )
        )
        edition_meta[tid] = {"edition_id": ed_id, "start": start, "end": end_bound, "precision": precision}
        prev = tourn_rows.get(tour_id)
        if prev is None or (start and prev[1] and start.isoformat() > prev[1]):
            tourn_rows[tour_id] = (code, start.isoformat() if start else None, name)

    # ---- matches, in replay order
    def sort_key(item):
        fname, r, key = item
        meta = edition_meta[r["tourney_id"].strip()]
        start = meta["start"].isoformat() if meta["start"] else "0000"
        row_date = r["tourney_date"].strip() if meta["precision"] == "day" else ""
        mnum = to_int(r["match_num"])
        return (start, r["tourney_id"].strip(), round_order(r["round"]), row_date, mnum if mnum is not None else 10**6)

    rows.sort(key=sort_key)

    match_rows = []
    mext_rows = []
    status_counts: Counter = Counter()
    outcome_counts: Counter = Counter()
    precision_counts: Counter = Counter()
    latest = ""
    for seq, (fname, r, key) in enumerate(rows, start=1):
        tid = r["tourney_id"].strip()
        meta = edition_meta[tid]
        mid = mreg.get_or_mint("tml", key, collected_at)
        w_sid, l_sid = r["winner_id"].strip(), r["loser_id"].strip()
        w_status, w_note = side_identity(w_sid, r["winner_name"].strip(), pdb)
        l_status, l_note = side_identity(l_sid, r["loser_name"].strip(), pdb)
        status = w_status if w_status != "ok" else l_status
        note = "; ".join(n for n in (w_note, l_note) if n) or None
        if meta["precision"] == "day":
            d = parse_yyyymmdd(r["tourney_date"])
            lo = hi = d.isoformat() if d else None
        else:
            lo = meta["start"].isoformat() if meta["start"] else None
            hi = meta["end"].isoformat() if meta["end"] else None
        latest = max(latest, hi or "")
        oc = outcome(r["score"])
        status_counts[status] += 1
        outcome_counts[oc] += 1
        precision_counts[meta["precision"]] += 1
        stats = [to_int(r.get(f"w_{c}")) for c in STAT_COLS] + [to_int(r.get(f"l_{c}")) for c in STAT_COLS]
        match_rows.append(
            (
                mid, meta["edition_id"], seq, r["round"].strip(), round_order(r["round"]), to_int(r["best_of"]),
                preg.lookup("atp", w_sid) if w_sid else None,
                preg.lookup("atp", l_sid) if l_sid else None,
                w_sid or None, l_sid or None, r["winner_name"].strip(), r["loser_name"].strip(),
                to_int(r["winner_rank"]), to_int(r["loser_rank"]),
                to_int(r["winner_rank_points"]), to_int(r["loser_rank_points"]),
                r["winner_entry"].strip() or None, r["loser_entry"].strip() or None,
                r["score"].strip(), oc, to_int(r["minutes"]),
                r["surface"].strip() or None, r["indoor"].strip() or None, r["tourney_level"].strip() or None,
                *stats,
                lo, hi, meta["precision"], None, collected_at,
                f"{hi}T23:59:59Z" if hi else None, "event_date_upper_bound",
                status, note, "tml", key, fname, snap.snapshot_id,
            )
        )
        mext_rows.append(("tml", key, mid))

    # ---- write
    with conn:
        for t in ("match_external_id", "charting_match", "prediction", "match", "tournament_edition", "tournament",
                  "player_name", "player_external_id", "player"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM source_snapshot WHERE source = 'tml'")
        conn.execute(
            "INSERT INTO source_snapshot VALUES (?,?,?,?,?,?,?)",
            (snap.snapshot_id, snap.source, snap.revision, snap.revision_date, snap.collected_at, snap.license, snap.to_json()),
        )
        conn.executemany("INSERT INTO player VALUES (?,?,?,?,?,?,?,?,?)", player_rows)
        conn.executemany("INSERT INTO player_external_id VALUES (?,?,?)", ext_rows)
        conn.executemany("INSERT OR IGNORE INTO player_name VALUES (?,?,?,?,?)", name_rows)
        conn.executemany(
            "INSERT INTO tournament VALUES (?,?,?)", [(tid, c, n) for tid, (c, _, n) in tourn_rows.items()]
        )
        conn.executemany(f"INSERT INTO tournament_edition VALUES ({','.join('?' * 18)})", edition_rows)
        conn.executemany(f"INSERT INTO match VALUES ({','.join('?' * len(match_rows[0]))})", match_rows)
        conn.executemany("INSERT INTO match_external_id VALUES (?,?,?)", mext_rows)
        db.set_meta(conn, "tml_snapshot", snap.snapshot_id)
        db.set_meta(conn, "latest_event_date", latest)
    for reg in (preg, treg, ereg, mreg):
        reg.save()

    report = IngestReport(
        players=len(player_rows),
        tournaments=len(tourn_rows),
        editions=len(edition_rows),
        matches=len(match_rows),
        identity_status=dict(status_counts),
        outcome=dict(outcome_counts),
        date_precision=dict(precision_counts),
        latest_event_date=latest,
        duplicate_rows_skipped=dup_skipped,
    )
    with conn:
        db.set_meta(conn, "tml_ingest_report", json.dumps(report.__dict__))
    return report
