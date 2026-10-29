"""Load Match Charting Project (MCP) match records and link them to our IDs.

MCP identifies players by display name only, so this is where the spec's
entity-resolution rules bite:

* a name is resolved only if it maps to exactly one internal player;
* an ambiguous name is never guessed. It is kept as a candidate set and
  resolved only if exactly one candidate produces a tour match with the other
  player in the same round and date window (a join on both players, round and
  date, which the spec allows);
* a chart is linked to a TML match only when exactly one match fits both
  players, the round and the date window. Everything else stays unlinked,
  with a reason.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import config
from .normalize import names_compatible, norm_name, parse_yyyymmdd, to_int
from .sources import Snapshot

# Date windows around a TML edition for accepting an MCP chart. The strict
# window accepts on players + round alone; the relaxed one (TML edition dates
# are sometimes a week off, e.g. Shanghai 2025) also requires the tournament
# names to agree.
STRICT_BEFORE, STRICT_AFTER = 2, 1
RELAXED_BEFORE, RELAXED_AFTER = 4, 8
# Words that say nothing about which event a name refers to.
GENERIC_TOURNEY_WORDS = {"masters", "open", "atp", "tour", "cup", "finals", "final", "championships", "international"}


@dataclass
class NameIndex:
    by_norm: dict[str, set[str]]

    @classmethod
    def from_db(cls, conn: sqlite3.Connection, aliases: Path | None = None) -> "NameIndex":
        by_norm: dict[str, set[str]] = defaultdict(set)
        for r in conn.execute(
            "SELECT pn.name_norm, pn.player_id FROM player_name pn JOIN player p USING (player_id) WHERE p.is_placeholder = 0"
        ):
            by_norm[r["name_norm"]].add(r["player_id"])
        aliases = aliases or config.METADATA_DIR / "player_aliases.csv"
        if aliases.exists():
            with open(aliases, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    pid = conn.execute(
                        "SELECT player_id FROM player_external_id WHERE source = 'atp' AND external_id = ?",
                        (row["atp_id"].strip(),),
                    ).fetchone()
                    if pid:
                        by_norm[norm_name(row["name"])] = {pid["player_id"]}
        return cls(dict(by_norm))

    def candidates(self, name: str) -> set[str]:
        return set(self.by_norm.get(norm_name(name), ()))

    def fuzzy_candidates(self, name: str) -> set[str]:
        """Players whose known names are compatible variants ("Albert Ramos" ~ "Albert Ramos-Vinolas").

        Only ever used as a candidate set for a match-level join, never as a resolution on its own.
        """
        if not hasattr(self, "_by_token"):
            by_token: dict[str, set[str]] = defaultdict(set)
            for nm in self.by_norm:
                for tok in nm.split():
                    if len(tok) > 2:
                        by_token[tok].add(nm)
            self._by_token = by_token
        target = norm_name(name)
        pool: set[str] = set()
        for tok in target.split():
            pool |= self._by_token.get(tok, set())
        out: set[str] = set()
        for nm in pool:
            if names_compatible(nm, target) and len(set(nm.split()) & set(target.split())) >= 1:
                out |= self.by_norm[nm]
        return out


@dataclass
class TourMatch:
    match_id: str
    round: str
    start: date
    end: date
    surface: str | None
    tourney: str


def tour_match_index(conn: sqlite3.Connection) -> dict[frozenset, list[TourMatch]]:
    idx: dict[frozenset, list[TourMatch]] = defaultdict(list)
    q = """SELECT m.match_id, m.winner_id, m.loser_id, m.round, e.start_date, e.end_date_bound, e.surface, e.name,
                  m.event_date_lo, m.event_date_hi
           FROM match m JOIN tournament_edition e USING (edition_id)
           WHERE m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL AND e.start_date IS NOT NULL"""
    for r in conn.execute(q):
        lo = date.fromisoformat(r["event_date_lo"] or r["start_date"])
        hi = date.fromisoformat(r["event_date_hi"] or r["end_date_bound"] or r["start_date"])
        idx[frozenset((r["winner_id"], r["loser_id"]))].append(
            TourMatch(r["match_id"], r["round"], lo, hi, r["surface"], r["name"])
        )
    return idx


def find_links(cands1: set[str], cands2: set[str], d: date, rnd: str, idx, before: int, after: int):
    out = []
    for a in cands1:
        for b in cands2:
            if a == b:
                continue
            for tm in idx.get(frozenset((a, b)), ()):
                if tm.round != rnd:
                    continue
                if (tm.start - d).days > before or (d - tm.end).days > after:
                    continue
                out.append((a, b, tm))
    return out


def tourney_names_agree(mcp_name: str, tml_name: str) -> bool:
    a = set(norm_name(mcp_name).split()) - GENERIC_TOURNEY_WORDS
    b = set(norm_name(tml_name).split()) - GENERIC_TOURNEY_WORDS
    return bool(a & b) or (not a and not b)


def is_tour_level_guess(tournament: str, rnd: str) -> bool:
    """MCP also charts Challengers (' CH' suffix), ITFs and qualifying, which TML does not hold."""
    t = tournament.strip()
    if t.endswith(" CH") or " CH " in t or t.endswith(" ITF") or "Futures" in t or t.startswith("M15") or t.startswith("M25"):
        return False
    return not rnd.startswith("Q") and rnd not in {"PQ"}


def ingest(conn: sqlite3.Connection, snap: Snapshot) -> dict:
    path = Path(snap.path) / "charting-m-matches.csv"
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))

    names = NameIndex.from_db(conn)
    idx = tour_match_index(conn)
    coverage_end = (conn.execute("SELECT value FROM meta WHERE key = 'latest_event_date'").fetchone() or ["9999"])[0]
    out_rows = []
    link_counts: Counter = Counter()
    name_status: Counter = Counter()
    unresolved_names: Counter = Counter()
    ambiguous_names: Counter = Counter()

    for r in rows:
        mcp_id = r["match_id"].strip()
        d = parse_yyyymmdd(r["Date"])
        rnd = r["Round"].strip()
        surface = r["Surface"].strip()
        p1n, p2n = r["Player 1"].strip(), r["Player 2"].strip()
        base = dict(
            mcp_match_id=mcp_id, match_date=d.isoformat() if d else None, start_time=r["Time"].strip() or None,
            court=r["Court"].strip() or None, tournament=r["Tournament"].strip(), round=rnd, surface=surface or None,
            best_of=to_int(r["Best of"]), p1_name=p1n, p2_name=p2n,
        )
        if d is None or surface not in {"Hard", "Clay", "Grass", "Carpet"}:
            out_rows.append({**base, "p1_player_id": None, "p2_player_id": None, "p1_status": None, "p2_status": None,
                             "match_id": None, "link_status": "malformed_row", "link_note": "unparseable date or shifted columns"})
            link_counts["malformed_row"] += 1
            continue

        c1, c2 = names.candidates(p1n), names.candidates(p2n)
        st1 = "resolved" if len(c1) == 1 else ("ambiguous" if c1 else "unresolved")
        st2 = "resolved" if len(c2) == 1 else ("ambiguous" if c2 else "unresolved")
        for nm, st in ((p1n, st1), (p2n, st2)):
            name_status[st] += 1
            if st == "ambiguous":
                ambiguous_names[nm] += 1
        pid1 = next(iter(c1)) if st1 == "resolved" else None
        pid2 = next(iter(c2)) if st2 == "resolved" else None
        # Unresolved names get a fuzzy candidate set; it only counts if the match join is unique.
        f1 = c1 or names.fuzzy_candidates(p1n)
        f2 = c2 or names.fuzzy_candidates(p2n)

        tour_level = is_tour_level_guess(base["tournament"], rnd)
        note = None
        match_id = None
        if not f1 or not f2:
            status = "player_unresolved" if tour_level else "not_tour_level"
            note = "; ".join(f"{n!r} unresolved" for n, c in ((p1n, f1), (p2n, f2)) if not c)
        else:
            links = find_links(f1, f2, d, rnd, idx, STRICT_BEFORE, STRICT_AFTER)
            if not links:
                links = [(a, b, tm) for a, b, tm in find_links(f1, f2, d, rnd, idx, RELAXED_BEFORE, RELAXED_AFTER)
                         if tourney_names_agree(base["tournament"], tm.tourney)]
                if links:
                    note = "MCP date outside TML edition window; accepted because tournament names agree"
            by_match = {tm.match_id: (a, b, tm) for a, b, tm in links}
            assignments: dict[str, set] = defaultdict(set)
            for a, b, tm in links:
                assignments[tm.match_id].add((a, b))
            if len(by_match) > 1:
                agreeing = {k: v for k, v in by_match.items() if tourney_names_agree(base["tournament"], v[2].tourney)}
                if len(agreeing) == 1:
                    by_match, note = agreeing, "tie between candidate matches broken by tournament name"
            if len(by_match) == 1:
                a, b, tm = next(iter(by_match.values()))
                status, match_id = "linked", tm.match_id
                if len(assignments[tm.match_id]) > 1:
                    # Homonyms playing each other: the match is certain, who is "Player 1" is not.
                    pid1 = pid2 = None
                    st1 = st2 = "ambiguous_assignment"
                else:
                    if st1 != "resolved":
                        pid1, st1 = a, ("resolved_via_match" if st1 == "ambiguous" else "resolved_via_match_fuzzy")
                    if st2 != "resolved":
                        pid2, st2 = b, ("resolved_via_match" if st2 == "ambiguous" else "resolved_via_match_fuzzy")
                if tm.surface and tm.surface != surface:
                    note = (note + "; " if note else "") + f"surface disagrees: MCP {surface}, TML {tm.surface}"
                if not tourney_names_agree(base["tournament"], tm.tourney):
                    note = (note + "; " if note else "") + f"tournament name differs: MCP {base['tournament']!r}, TML {tm.tourney!r}"
            elif len(by_match) > 1:
                status, note = "ambiguous", f"{len(by_match)} tour matches fit"
            elif d.isoformat() > coverage_end:
                status, note = "beyond_source_coverage", f"TML mirror ends {coverage_end}"
            else:
                status = "no_candidate" if tour_level else "not_tour_level"
        for nm, st in ((p1n, st1), (p2n, st2)):
            if st == "unresolved":
                unresolved_names[nm] += 1
        link_counts[status] += 1
        out_rows.append({**base, "p1_player_id": pid1, "p2_player_id": pid2, "p1_status": st1, "p2_status": st2,
                         "match_id": match_id, "link_status": status, "link_note": note})

    cols = ["mcp_match_id", "match_date", "start_time", "court", "tournament", "round", "surface", "best_of",
            "p1_name", "p2_name", "p1_player_id", "p2_player_id", "p1_status", "p2_status", "match_id",
            "link_status", "link_note"]
    with conn:
        conn.execute("DELETE FROM charting_match")
        conn.execute("DELETE FROM source_snapshot WHERE source = 'mcp'")
        conn.execute(
            "INSERT INTO source_snapshot VALUES (?,?,?,?,?,?,?)",
            (snap.snapshot_id, snap.source, snap.revision, snap.revision_date, snap.collected_at, snap.license, snap.to_json()),
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO charting_match VALUES ({','.join('?' * (len(cols) + 2))})",
            [tuple(r[c] for c in cols) + (snap.collected_at, snap.snapshot_id) for r in out_rows],
        )
    return {
        "charts": len(rows),
        "link_status": dict(link_counts),
        "name_status": dict(name_status),
        "final_player_status": dict(Counter(r[k] for r in out_rows for k in ("p1_status", "p2_status") if r[k])),
        "top_unresolved_names": unresolved_names.most_common(25),
        "ambiguous_names": ambiguous_names.most_common(25),
    }
