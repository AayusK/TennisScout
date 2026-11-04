"""Per-player stat lines from the Match Charting Project's per-match tables (men's charts).

One row per (chart, player): serve and return outcomes, points by rally length, groundstroke
outcomes, crosscourt / down-the-line wing outcomes, net play and shot mix. They feed the style
profiles (profiles.py). Only players whose chart name resolved to one internal ID at ingest
(ingest_charting) get a line: an unresolved or ambiguous name is never guessed here either.

Counts are stored as a JSON object of named integers so the profile code can add measures
without a schema change. Key names:

    serve_pts, return_pts, return_won, aces, dfs, first_in, first_won, second_in, second_won,
    winners, unforced                                    Overview (whole match)
    shots, gs_shots, gs_win, gs_forced, gs_ue,
    fside, bside, net_shots                              ShotTypes (gs = topspin/flat drives)
    {fxc,bxc,fdtl,bdtl,fio,bio,fdtm,bdtm}_{shots,win,forced,ue}   ShotDirOutcomes
    sv1_pts, sv1_won, sv1_aces, sv1_unret, sv1_forced    ServeBasics, first serves in
    sv2_pts, sv2_won                                     ServeBasics, second serves
    rt1_pts, rt1_inplay, rt1_won, rt2_pts, rt2_inplay    ReturnOutcomes
    net_pts, net_won                                     NetPoints
    {r13,r46,r79,r10}_{sv,rt}_{pts,won}                  Rally: points by length (shots incl. the serve),
                                                         on his serve (sv) and on his return (rt)
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from .sources import Snapshot

DIRS = {"F-XC": "fxc", "B-XC": "bxc", "F-DTL": "fdtl", "B-DTL": "bdtl", "F-IO": "fio", "B-IO": "bio",
        "F-DTM": "fdtm", "B-DTM": "bdtm"}
RALLY = {"1-3": "r13", "4-6": "r46", "7-9": "r79", "10": "r10"}


def _i(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _rows(path: Path, keep: set[str]):
    if not path.exists():  # older snapshots and test mirrors carry only the matches file
        return
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if r.get("match_id") in keep:
                yield r


def collect(mcp_dir: Path, charts: dict[str, dict]) -> dict[tuple[str, str], dict[str, int]]:
    """Counts per (chart id, chart player name) for the charts in ``charts`` (id -> {p1_name, p2_name})."""
    keep = set(charts)
    out: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def add(mid, name, **kv):
        c = out[(mid, name)]
        for k, v in kv.items():
            c[k] += _i(v)

    f = mcp_dir / "charting-m-stats-Overview.csv"
    for r in _rows(f, keep):
        if r["set"] == "Total":
            add(r["match_id"], r["player"], serve_pts=r["serve_pts"], aces=r["aces"], dfs=r["dfs"], first_in=r["first_in"],
                first_won=r["first_won"], second_in=r["second_in"], second_won=r["second_won"], return_pts=r["return_pts"],
                return_won=r["return_pts_won"], winners=r["winners"], unforced=r["unforced"])
    for r in _rows(mcp_dir / "charting-m-stats-ShotTypes.csv", keep):
        row, mid, p = r["row"], r["match_id"], r["player"]
        if row == "Total":
            add(mid, p, shots=r["shots"])
        elif row == "Gs":
            add(mid, p, gs_shots=r["shots"], gs_win=r["winners"], gs_forced=r["induced_forced"], gs_ue=r["unforced"])
        elif row == "Fside":
            add(mid, p, fside=r["shots"])
        elif row == "Bside":
            add(mid, p, bside=r["shots"])
        elif row == "Net":
            add(mid, p, net_shots=r["shots"])
    for r in _rows(mcp_dir / "charting-m-stats-ShotDirOutcomes.csv", keep):
        k = DIRS.get(r["row"])
        if k:
            add(r["match_id"], r["player"], **{f"{k}_shots": r["shots"], f"{k}_win": r["winners"],
                                                f"{k}_forced": r["induced_forced"], f"{k}_ue": r["unforced"]})
    for r in _rows(mcp_dir / "charting-m-stats-ServeBasics.csv", keep):
        if r["row"] == "1":
            add(r["match_id"], r["player"], sv1_pts=r["pts"], sv1_won=r["pts_won"], sv1_aces=r["aces"],
                sv1_unret=r["unret"], sv1_forced=r["forced_err"])
        elif r["row"] == "2":
            add(r["match_id"], r["player"], sv2_pts=r["pts"], sv2_won=r["pts_won"])
    for r in _rows(mcp_dir / "charting-m-stats-ReturnOutcomes.csv", keep):
        if r["row"] == "v1st":
            add(r["match_id"], r["player"], rt1_pts=r["pts"], rt1_inplay=r["in_play"], rt1_won=r["pts_won"])
        elif r["row"] == "v2nd":
            add(r["match_id"], r["player"], rt2_pts=r["pts"], rt2_inplay=r["in_play"])
    for r in _rows(mcp_dir / "charting-m-stats-NetPoints.csv", keep):
        if r["row"] == "NetPoints":
            add(r["match_id"], r["player"], net_pts=r["net_pts"], net_won=r["pts_won"])
    for r in _rows(mcp_dir / "charting-m-stats-Rally.csv", keep):
        length, _, side = r["row"].rpartition("-") if r["row"].count("-") == 2 or r["row"].startswith("10-") else ("", "", "")
        k = RALLY.get(length)
        if not k or side not in ("1", "2"):
            continue  # the unsuffixed rows add both servers together
        # Suffixed rows list the server first: pl1_* is the server's count, pl2_* the returner's.
        add(r["match_id"], r["server"], **{f"{k}_sv_pts": r["pts"], f"{k}_sv_won": r["pl1_won"]})
        add(r["match_id"], r["returner"], **{f"{k}_rt_pts": r["pts"], f"{k}_rt_won": r["pl2_won"]})
    return {k: dict(v) for k, v in out.items()}


def ingest(conn: sqlite3.Connection, snap: Snapshot) -> dict:
    charts = {r["mcp_match_id"]: dict(r) for r in conn.execute(
        """SELECT mcp_match_id, match_date, surface, p1_name, p2_name, p1_player_id, p2_player_id, match_id
           FROM charting_match WHERE p1_player_id IS NOT NULL OR p2_player_id IS NOT NULL""")}
    counts = collect(Path(snap.path), charts)
    rows = []
    for mid, c in charts.items():
        date = c["match_date"]
        for me, opp in (("p1", "p2"), ("p2", "p1")):
            pid = c[f"{me}_player_id"]
            line = counts.get((mid, c[f"{me}_name"]))
            if pid and line and line.get("serve_pts", 0) + line.get("return_pts", 0) > 0:
                rows.append((mid, pid, c[f"{opp}_player_id"], date, c["surface"], c["match_id"],
                             json.dumps(line, sort_keys=True, separators=(",", ":"))))
    with conn:
        conn.execute("DELETE FROM charting_line")
        conn.executemany("INSERT INTO charting_line VALUES (?,?,?,?,?,?,?)", rows)
    return {"charts": len(charts), "player_lines": len(rows)}
