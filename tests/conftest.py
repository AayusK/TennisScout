"""Synthetic TML / MCP mirrors so tests run without the real (non-redistributable) data."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from tennisscout import db, ingest_charting, ingest_tml
from tennisscout.sources import Snapshot

TML_COLS = (
    "tourney_id,tourney_name,surface,draw_size,tourney_level,indoor,tourney_date,match_num,winner_id,winner_seed,"
    "winner_entry,winner_name,winner_hand,winner_ht,winner_ioc,winner_age,winner_rank,winner_rank_points,loser_id,"
    "loser_seed,loser_entry,loser_name,loser_hand,loser_ht,loser_ioc,loser_age,loser_rank,loser_rank_points,score,"
    "best_of,round,minutes,w_ace,w_df,w_svpt,w_1stIn,w_1stWon,w_2ndWon,w_SvGms,w_bpSaved,w_bpFaced,l_ace,l_df,l_svpt,"
    "l_1stIn,l_1stWon,l_2ndWon,l_SvGms,l_bpSaved,l_bpFaced"
).split(",")

PLAYERS = [
    # id, player, atpname, birthdate, hand, ioc
    ("AAA1", "Alex Alpha", "Alex Alpha", "19990101", "R", "USA"),
    ("BBB2", "Ben Beta", "Ben Beta", "19980202", "L", "GBR"),
    ("CCC3", "Carl Gamma", "Carl Gamma", "19970303", "R", "FRA"),
    ("DDD4", "Dan Delta", "Dan Delta", "19960404", "R", "ESP"),
    ("EEE5", "John Smith", "John Smith", "19900101", "R", "USA"),
    ("FFF6", "John Smith", "John Smith", "19920101", "R", "AUS"),
]


def m(tid, name, date, num, w, wn, l, ln, rnd, score="6-4 6-4", surface="Hard", indoor="O", level="250", wr=10, lr=20):
    row = dict.fromkeys(TML_COLS, "")
    row.update(
        tourney_id=tid, tourney_name=name, surface=surface, draw_size="32", tourney_level=level, indoor=indoor,
        tourney_date=date, match_num=str(num), winner_id=w, winner_name=wn, loser_id=l, loser_name=ln,
        score=score, best_of="3", round=rnd, winner_rank=str(wr), loser_rank=str(lr),
    )
    return row


TML_2024 = [
    # Week 1, Brisbane: SF then F (listed out of order on purpose; replay must follow rounds).
    m("2024-339", "Brisbane", "20240101", 3, "AAA1", "Alex Alpha", "BBB2", "Ben Beta", "F"),
    m("2024-339", "Brisbane", "20240101", 1, "AAA1", "Alex Alpha", "CCC3", "Carl Gamma", "SF"),
    m("2024-339", "Brisbane", "20240101", 2, "BBB2", "Ben Beta", "DDD4", "Dan Delta", "SF"),
    # Walkover: must not move ratings.
    m("2024-338", "Adelaide", "20240108", 1, "CCC3", "Carl Gamma", "DDD4", "Dan Delta", "SF", score="W/O"),
    # Row that files Dan Delta under Carl Gamma's ID: an ID/name conflict.
    m("2024-338", "Adelaide", "20240108", 2, "AAA1", "Alex Alpha", "CCC3", "Dan Delta", "F"),
    # The two John Smiths (homonyms) play each other on clay.
    m("2024-500", "Houston", "20240401", 1, "EEE5", "John Smith", "FFF6", "John Smith", "R32", surface="Clay"),
]

TML_2025 = [
    m("2025-339", "Brisbane", "20250101", 1, "BBB2", "Ben Beta", "AAA1", "Alex Alpha", "F"),
    m("2025-422", "Cincinnati Masters", "20250807", 1, "CCC3", "Carl Gamma", "BBB2", "Ben Beta", "QF", level="M"),
    # Overlaps the Cincinnati window on purpose.
    m("2025-421", "Canada Masters", "20250801", 1, "BBB2", "Ben Beta", "CCC3", "Carl Gamma", "QF", level="M"),
]

MCP_COLS = ["match_id", "Player 1", "Player 2", "Pl 1 hand", "Pl 2 hand", "Date", "Tournament", "Round", "Time",
            "Court", "Surface", "Umpire", "Best of", "Final TB?", "Charted by"]


def chart(mid, p1, p2, date, tourney, rnd, surface="Hard", time=""):
    return {"match_id": mid, "Player 1": p1, "Player 2": p2, "Pl 1 hand": "R", "Pl 2 hand": "R", "Date": date,
            "Tournament": tourney, "Round": rnd, "Time": time, "Court": "", "Surface": surface, "Umpire": "",
            "Best of": "3", "Final TB?": "", "Charted by": "t"}


MCP_ROWS = [
    chart("c1", "Alex Alpha", "Ben Beta", "20240107", "Brisbane", "F", time="2pm"),
    # Homonyms playing each other: the match link is unique, the player assignment is not.
    chart("c2", "John Smith", "John Smith", "20240403", "Houston", "R32", surface="Clay"),
    # Same pair, same round, overlapping windows (Canada and Cincinnati): tie broken by name.
    chart("c3", "Carl Gamma", "Ben Beta", "20250808", "Cincinnati Masters", "QF"),
    # A name variant that only a unique join can resolve.
    chart("c4", "Benjamin Beta", "Alex Alpha", "20250104", "Brisbane", "F"),
    # A Challenger: not in the tour-level source.
    chart("c5", "Alex Alpha", "Zed Unknown", "20240301", "Phoenix CH", "R32"),
    # After the source's coverage ends.
    chart("c6", "Alex Alpha", "Ben Beta", "20270101", "Brisbane", "F"),
]


def write_csv(path: Path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def mirrors(tmp_path: Path):
    tml = tmp_path / "tml"
    tml.mkdir()
    write_csv(tml / "2024.csv", TML_COLS, TML_2024)
    write_csv(tml / "2025.csv", TML_COLS, TML_2025)
    with open(tml / "ATP_Database.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(["id", "player", "atpname", "birthdate", "weight", "height", "turnedpro", "birthplace", "coaches", "hand", "backhand", "ioc"])
        for pid, name, atpname, bd, hand, ioc in PLAYERS:
            w.writerow([pid, name, atpname, bd, "", "", "", "", "", hand, "", ioc])
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    write_csv(mcp / "charting-m-matches.csv", MCP_COLS, MCP_ROWS)
    return tml, mcp


def snap(name: str, path: Path) -> Snapshot:
    return Snapshot(source=name, path=str(path), revision="0" * 40, revision_date="2026-01-01T00:00:00Z",
                    collected_at="2026-10-29T00:00:00Z", license="test", files={})


@pytest.fixture
def loaded(tmp_path: Path, mirrors):
    tml, mcp = mirrors
    conn = db.connect(tmp_path / "t.db")
    db.init(conn)
    reg = tmp_path / "registry"
    report = ingest_tml.ingest(conn, snap("tml", tml), registry_dir=reg)
    charting = ingest_charting.ingest(conn, snap("mcp", mcp))
    return conn, reg, report, charting
