"""Everything one matchup card shows, computed from the database. card.py only lays it out.

Where each number comes from:

* Win chance: the served model (predict.py), exactly as /predict returns it.
* Style (radar, archetype, tags, gravity, wings, phases): profiles.py, from match stats and
  Match Charting Project lines in the last three years.
* Court speed: court_speed.py, per tournament edition; this week's edition once it has results,
  else the event's previous editions on the same surface.
* Records, holds, breaks, tiebreaks, form, head-to-head: the match table.
* "Expected" wins: the served model's stored pre-match predictions for those matches (tour level).
* Court tilt: a description, not a validated model. The chance each man wins a point on serve is
  split into short points (1-3 shots) and rally points (4+), using both men's charted rates; this
  court's share of short points (from its speed) replaces the average share for the surface, and
  the Markov chain turns the difference into match-win points. "Blend" is the served chance plus
  that tilt; the served chance itself is never changed.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from . import court_speed, db, markov, predict, profiles
from .ingest_api_tennis import TML_NAMES
from .normalize import norm_name
from .serve_return import logit, sigmoid

BAND = 10  # "courts like this": same surface and roof, speed within +-BAND
NOT_TOUR = ("C",)
TITLE_LEVELS = ("G", "M", "F", "A", "500", "250")  # Slams, Masters, Finals, other tour events
ROUND_NAMES = {"F": "Final", "SF": "Semis", "QF": "Quarters", "R16": "R16", "R32": "R32", "R64": "R64", "R128": "R128",
               "RR": "Round robin", "Q": "Qualifying", "BR": "Bronze"}
SLAM_ROUNDS = [("W", "Title"), ("F", "Final"), ("SF", "Semis"), ("QF", "Quarters"), ("R16", "4th round"),
               ("R32", "3rd round"), ("R64", "2nd round"), ("R128", "1st round")]
PARTICLES = {"de", "da", "di", "del", "della", "van", "von", "der", "den", "du", "dos", "la", "le", "ten", "ter", "mc"}


# --------------------------------------------------------------------------------------------- helpers

def event_display(name: str) -> str:
    """API-Tennis event names carry a country and sometimes a stray "- Qualification" on main-draw rounds."""
    name = re.sub(r"\s*\([^)]*\)", "", name or "")
    return re.sub(r"\s*-\s*Qualification$", "", name).strip()


def short_name(conn: sqlite3.Connection, pid: str, display: str) -> str:
    """The surname a scoreboard uses. API-Tennis writes "A. De Minaur", "T. M. Etcheverry": its most
    common form for this player, initials dropped, is the best source; otherwise the display name's last
    word plus any particle before it ("van de Zandschulp")."""
    names = Counter()
    for (n,) in conn.execute(
            """SELECT winner_src_name FROM match WHERE winner_id = ? AND source = 'api_tennis'
               UNION ALL SELECT loser_src_name FROM match WHERE loser_id = ? AND source = 'api_tennis'""", (pid, pid)):
        if n:
            names[n] += 1
    if names:
        toks = [t for t in names.most_common(1)[0][0].split() if not t.endswith(".")]
        if toks:
            short = " ".join(toks)
            tail = (display or "")[-len(short):]
            return tail if tail.lower() == short.lower() else short  # keep "van de Zandschulp" casing
    toks = (display or "?").split()
    out = [toks[-1]]
    for t in reversed(toks[1:-1]):
        if t.lower() in PARTICLES:
            out.insert(0, t)
        else:
            break
    return " ".join(out)


def score_sets(score: str | None) -> list[tuple[int, int, int | None]]:
    """'7-6(5) 3-6 6-4 RET' -> [(7, 6, 5), (3, 6, None), (6, 4, None)], winner's games first."""
    out = []
    for tok in (score or "").split():
        m = re.match(r"^\[?(\d+)-(\d+)\]?(?:\((\d+)(?:-\d+)?\))?$", tok)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None))
    return out


def is_tiebreak(s: tuple[int, int, int | None]) -> bool:
    return s[2] is not None or {s[0], s[1]} == {6, 7}


def fmt_set(s: tuple[int, int, int | None], flip: bool = False) -> str:
    a, b, t = s
    if flip:
        a, b = b, a
    return f"{a}-{b}" + (f"({t})" if t is not None else "")


def since_label(d: date) -> str:
    return d.strftime("%b %Y")


def where(event: str) -> str:
    """'in Paris' for the Paris Masters, 'at the US Open', 'at Wimbledon', 'at the Tour Finals'."""
    if event.endswith(" Masters"):
        return f"in {event[:-len(' Masters')]}"
    if event.endswith(("Open", "Finals", "Cup", "Games")):
        return f"at the {event}"
    if event in ("Wimbledon", "Roland Garros"):
        return f"at {event}"
    return f"in {event}"


def _rank_words(p: float) -> str:
    if p >= 90:
        return "ranks among the tour's best"
    if p >= 75:
        return "ranks well above the tour average"
    if p >= 60:
        return "ranks above the tour average"
    if p > 40:
        return "ranks around the tour average"
    if p > 25:
        return "ranks below the tour average"
    if p > 10:
        return "ranks near the bottom of the tour"
    return "ranks among the tour's weakest"


# --------------------------------------------------------------------------------------------- event

@dataclass
class Event:
    name: str
    date: str
    round: str | None = None
    surface: str = "Hard"
    indoor: bool = False
    best_of: int = 3
    level: str | None = None
    edition_id: str | None = None
    event_key: str | None = None
    speed: float | None = None
    speed_source: str = ""  # "this week" or "previous editions"
    history: list = field(default_factory=list)

    @property
    def speed_label(self) -> str:
        return court_speed.label(self.speed)

    @property
    def court(self) -> str:
        return ("indoor " if self.indoor else "") + self.surface.lower()

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(speed_label=self.speed_label, court=self.court)
        return d


def _name_variants(name: str) -> set[str]:
    base = re.sub(r"\s+(masters|open)$", "", name.strip(), flags=re.I)
    return {norm_name(x) for x in (name, TML_NAMES.get(name, name), f"{base} Masters", base, f"{base} Open")}


def find_event(conn: sqlite3.Connection, name: str, on: date, surface: str | None = None, indoor: bool | None = None,
               best_of: int | None = None, rnd: str | None = None) -> Event:
    """The edition being played on ``on`` (or the event's latest one) and its court speed."""
    keys_of = court_speed.event_keys(conn)
    tml_ids = {r[0] for r in conn.execute("SELECT edition_id FROM tournament_edition WHERE snapshot_id != 'api_tennis'")}
    variants = _name_variants(name)
    eds = conn.execute("SELECT edition_id, name, year, start_date, end_date_bound, surface, indoor, level FROM tournament_edition").fetchall()
    keys = {keys_of.get(r["edition_id"]) for r in eds if norm_name(r["name"]) in variants}
    keys.discard(None)
    family = sorted((r for r in eds if keys_of.get(r["edition_id"]) in keys), key=lambda r: (r["year"], r["start_date"] or ""))
    current = None
    for r in family:
        if r["year"] == on.year and r["start_date"] and r["start_date"] <= on.isoformat() <= \
                (date.fromisoformat(r["end_date_bound"] or r["start_date"]) + timedelta(days=10)).isoformat():
            current = r
    ref = current or (family[-1] if family else None)
    # Show the event's TML name ("Paris Masters") when API-Tennis calls it something shorter ("Paris").
    tml = [r for r in family if r["edition_id"] in tml_ids]
    ev = Event(name=event_display(tml[-1]["name"] if tml else name), date=on.isoformat(), round=rnd)
    if ref:
        ev.surface = surface or ref["surface"] or "Hard"
        ev.indoor = indoor if indoor is not None else (ref["indoor"] == "I")
        ev.level = ref["level"]
        ev.event_key = keys_of.get(ref["edition_id"])
    else:
        ev.surface, ev.indoor = surface or "Hard", bool(indoor)
    ev.best_of = best_of or (5 if ev.level == "G" else 3)
    ev.edition_id = current["edition_id"] if current else None
    io = "I" if ev.indoor else "O"
    same = [r for r in family if (r["surface"] == ev.surface and (r["indoor"] or "O") == io)]
    speeds = {r["edition_id"]: court_speed.speed_of(conn, r["edition_id"]) for r in same}
    ev.history = [{"year": r["year"], "speed": speeds[r["edition_id"]], "current": current is not None and r["edition_id"] == current["edition_id"]}
                  for r in same if speeds[r["edition_id"]] is not None][-6:]
    if current is not None and speeds.get(current["edition_id"]) is not None:
        ev.speed, ev.speed_source = speeds[current["edition_id"]], "this week"
    else:
        prev = [h["speed"] for h in ev.history][-3:]
        if prev:
            ev.speed, ev.speed_source = sum(prev) / len(prev), "previous editions"
    return ev


# --------------------------------------------------------------------------------------------- matches

def _serving_model(conn: sqlite3.Connection) -> list[str]:
    names = [db.get_meta(conn, "serving_model"), "elo_sr_blend", db.get_meta(conn, "elo_model")]
    return [n for n in names if n]


def predictions(conn: sqlite3.Connection, match_ids: list[str]) -> dict[str, tuple[str, float]]:
    """Stored pre-match predictions (player1, P(player1 wins)) from the served model, else the next best."""
    out: dict[str, tuple[str, float]] = {}
    ids = list(match_ids)
    for model in _serving_model(conn):
        todo = [m for m in ids if m not in out]
        for i in range(0, len(todo), 500):
            chunk = todo[i:i + 500]
            q = f"SELECT match_id, player1_id, p_player1 FROM prediction WHERE model = ? AND match_id IN ({','.join('?' * len(chunk))})"
            for mid, p1, p in conn.execute(q, (model, *chunk)):
                out[mid] = (p1, p)
    return out


def expected_for(pred: dict, mid: str, pid: str) -> float | None:
    if mid not in pred:
        return None
    p1, p = pred[mid]
    return p if p1 == pid else 1 - p


@dataclass
class M:
    match_id: str
    seq: int
    date: str
    event: str
    edition_id: str
    year: int
    round: str | None
    level: str | None
    surface: str | None
    indoor: str | None
    best_of: int | None
    won: bool
    opp_id: str | None
    opp_name: str
    score: str
    outcome: str
    speed: float | None
    my_sv: tuple | None  # (service games, break points saved, faced)
    opp_sv: tuple | None
    event_key: str | None = None

    @property
    def sets(self):
        return score_sets(self.score)

    @property
    def my_sets(self):
        """Sets from this player's side."""
        return [(b, a, t) if not self.won else (a, b, t) for a, b, t in self.sets]

    @property
    def tiebreaks(self) -> int:
        return sum(1 for s in self.sets if is_tiebreak(s))

    @property
    def games(self) -> int:
        return sum(a + b for a, b, _ in self.sets)

    @property
    def tour(self) -> bool:
        return (self.level or "") not in NOT_TOUR


def matches(conn: sqlite3.Connection, pid: str, since: date | None = None, until: date | None = None,
            keys_of: dict[str, str] | None = None) -> list[M]:
    """This player's completed and retired matches, newest first. ``keys_of`` (court_speed.event_keys) tags
    each match with its event across sources, so a tournament record spans TML and API-Tennis editions."""
    q = """SELECT m.match_id, m.seq, COALESCE(m.event_date_lo, e.start_date), e.name, m.edition_id, e.year, m.round,
                  COALESCE(m.level, e.level), COALESCE(m.surface, e.surface), COALESCE(m.indoor, e.indoor), m.best_of,
                  m.winner_id, m.loser_id, pw.display_name, pl.display_name, m.score, m.outcome, s.speed,
                  m.w_svgms, m.w_bpsaved, m.w_bpfaced, m.l_svgms, m.l_bpsaved, m.l_bpfaced, s.event_key
           FROM match m JOIN tournament_edition e USING (edition_id)
           LEFT JOIN edition_speed s ON s.edition_id = m.edition_id
           LEFT JOIN player pw ON pw.player_id = m.winner_id LEFT JOIN player pl ON pl.player_id = m.loser_id
           WHERE (m.winner_id = ? OR m.loser_id = ?) AND m.identity_status = 'ok'
             AND m.outcome IN ('completed', 'retired')
             AND COALESCE(m.event_date_lo, e.start_date) >= ? AND COALESCE(m.event_date_lo, e.start_date) < ?
           ORDER BY m.seq DESC"""
    lo = (since or date(1900, 1, 1)).isoformat()
    hi = (until or date(2100, 1, 1)).isoformat()
    out = []
    for r in conn.execute(q, (pid, pid, lo, hi)):
        won = r[11] == pid

        def sv(g, s, f):
            return (g, s, f) if (g and g > 0 and s is not None and f is not None and 0 <= s <= f and f - s <= g) else None

        w_sv, l_sv = sv(r[18], r[19], r[20]), sv(r[21], r[22], r[23])
        out.append(M(r[0], r[1], r[2], event_display(r[3]), r[4], r[5], r[6], r[7], r[8], r[9], r[10], won,
                     r[12] if won else r[11], (r[14] if won else r[13]) or "?", r[15] or "", r[16], r[17],
                     w_sv if won else l_sv, l_sv if won else w_sv, (keys_of or {}).get(r[4], r[24])))
    return out


def hold_break(ms: list[M]) -> dict:
    sg = held = rg = brk = 0
    for m in ms:
        if m.my_sv and m.opp_sv:
            g, s, f = m.my_sv
            sg, held = sg + g, held + g - (f - s)
            g2, s2, f2 = m.opp_sv
            rg, brk = rg + g2, brk + (f2 - s2)
    return {"service_games": sg, "return_games": rg, "hold": held / sg if sg else None, "brk": brk / rg if rg else None}


def record(ms: list[M]) -> tuple[int, int]:
    return sum(m.won for m in ms), sum(not m.won for m in ms)


# --------------------------------------------------------------------------------------------- player facts

def standings(conn: sqlite3.Connection) -> dict[str, dict]:
    """Latest stored ATP standings (API-Tennis), by internal player ID."""
    from . import api_tennis

    path = api_tennis.RAW / "standings.json.gz"
    if not path.exists():
        return {}
    raw = api_tennis.load(path)
    ext = {r[0]: r[1] for r in conn.execute("SELECT external_id, player_id FROM player_external_id WHERE source = 'api_tennis'")}
    out = {}
    for s in raw["result"]:
        pid = ext.get(str(s.get("player_key")))
        if pid:
            out[pid] = {"rank": s.get("place"), "points": s.get("points"), "as_of": raw["collected_at"][:10]}
    return out


def player_facts(conn: sqlite3.Connection, pid: str, live: dict[str, dict], all_ms: list[M]) -> dict:
    p = conn.execute("SELECT * FROM player WHERE player_id = ?", (pid,)).fetchone()
    ranks = conn.execute(
        """SELECT MIN(r) FROM (SELECT winner_rank AS r FROM match WHERE winner_id = ? AND winner_rank > 0
                             UNION ALL SELECT loser_rank FROM match WHERE loser_id = ? AND loser_rank > 0)""", (pid, pid)).fetchone()[0]
    latest = conn.execute(
        """SELECT r, d FROM (SELECT m.winner_rank AS r, COALESCE(m.event_date_lo, e.start_date) AS d, m.seq FROM match m
                               JOIN tournament_edition e USING (edition_id) WHERE m.winner_id = ? AND m.winner_rank > 0
                             UNION ALL SELECT m.loser_rank, COALESCE(m.event_date_lo, e.start_date), m.seq FROM match m
                               JOIN tournament_edition e USING (edition_id) WHERE m.loser_id = ? AND m.loser_rank > 0)
           ORDER BY d DESC LIMIT 1""", (pid, pid)).fetchone()
    titles = sum(1 for m in all_ms if m.won and m.round == "F" and (m.level or "") in TITLE_LEVELS)
    best = None
    for code, label in SLAM_ROUNDS:
        hit = [m for m in all_ms if m.level == "G" and m.round != "Q" and
               ((code == "W" and m.round == "F" and m.won) or (code != "W" and m.round == code))]
        if hit:
            best = label
            break
    rank = live.get(pid, {}).get("rank")
    high = min(x for x in (ranks, rank) if x) if (ranks or rank) else None
    age = None
    if p and p["birthdate"]:
        try:
            b = date(int(p["birthdate"][:4]), int(p["birthdate"][4:6]), int(p["birthdate"][6:8]))
            age = (date.today() - b).days // 365
        except ValueError:
            pass
    return {"player_id": pid, "name": p["display_name"] if p else pid, "short": short_name(conn, pid, p["display_name"] if p else pid),
            "ioc": p["ioc"] if p else None, "hand": p["hand"] if p else None, "height_cm": p["height_cm"] if p else None,
            "age": age, "rank": rank, "rank_live": rank is not None,
            "rank_last_known": latest[0] if latest else None, "rank_last_known_date": latest[1] if latest else None,
            "career_high": high, "titles": titles, "best_slam": best}


# --------------------------------------------------------------------------------------------- sections

def form(conn: sqlite3.Connection, pid: str, ms: list[M], n: int = 8, names=None) -> dict:
    tour = [m for m in ms if m.tour][:40]
    pred = predictions(conn, [m.match_id for m in tour])
    scored = [m for m in tour if m.match_id in pred][:n]
    w, l = record(scored)
    exp = sum(expected_for(pred, m.match_id, pid) for m in scored)
    last = ms[0] if ms else None
    retired = None
    if last and last.outcome == "retired" and not last.won:
        retired = {"event": last.event, "round": last.round, "opp": names(last.opp_id, last.opp_name) if names else last.opp_name,
                   "date": last.date}
    return {"w": w, "l": l, "n": len(scored), "expected": exp, "retired_last": retired}


def tournament_record(ms: list[M], event_key: str | None) -> tuple[int, int] | None:
    if not event_key:
        return None
    here = [m for m in ms if m.event_key == event_key and m.round != "Q"]
    return record(here) if here else (0, 0)


def court_records(ms: list[M], ev: Event, since: date) -> dict:
    """Record, holds and breaks on courts like this one vs. his other matches since ``since``."""
    io = "I" if ev.indoor else "O"
    window = [m for m in ms if m.date >= since.isoformat() and m.tour]
    if ev.speed is None:
        here, other = [], window
    else:
        here = [m for m in window if m.surface == ev.surface and (m.indoor or "O") == io and m.speed is not None
                and abs(m.speed - ev.speed) <= BAND]
        ids = {m.match_id for m in here}
        other = [m for m in window if m.match_id not in ids]
    hb_here, hb_other = hold_break(here), hold_break(other)
    w, l = record(here)
    out = {"w": w, "l": l, "n": len(here), "here": hb_here, "other": hb_other, "delta": None, "label": "TOO FEW TO CALL"}
    if hb_here["hold"] is not None and hb_other["hold"] is not None and hb_here["brk"] is not None and hb_other["brk"] is not None:
        delta = 100 * ((hb_here["hold"] + hb_here["brk"]) - (hb_other["hold"] + hb_other["brk"]))
        se = 100 * math.sqrt(sum(p * (1 - p) / n for p, n in (
            (hb_here["hold"], hb_here["service_games"]), (hb_here["brk"], hb_here["return_games"]),
            (hb_other["hold"], hb_other["service_games"]), (hb_other["brk"], hb_other["return_games"])) if n))
        out["delta"], out["se"] = delta, se
        if len(here) >= 6:
            out["label"] = ("BETTER HERE" if delta >= max(3.0, 2 * se) else
                            "WORSE HERE" if delta <= -max(3.0, 2 * se) else "NO CLEAR CHANGE")
    return out


def head_to_head(conn: sqlite3.Connection, a: str, a_ms: list[M], b: str) -> dict:
    meet = [m for m in a_ms if m.opp_id == b]
    out = {"a": sum(m.won for m in meet), "b": sum(not m.won for m in meet), "meetings": []}
    for m in meet:
        out["meetings"].append({"date": m.date, "year": m.year, "event": m.event, "round": m.round, "surface": m.surface,
                                "indoor": m.indoor, "speed": m.speed, "a_won": m.won, "score": m.score,
                                "retired": m.outcome == "retired",
                                "a_sets": [fmt_set(s) for s in m.my_sets], "best_of": m.best_of})
    return out


def first_strike_q(pa_: profiles.Profile, pb_: profiles.Profile, A: dict, B: dict, pool: profiles.Pool) -> dict:
    """Can B handle A's serve?"""
    srv, ret = pa_.axes["Serve"], pb_.axes["Return"]
    gap = srv - ret
    sa = pool.serve_stats.get(A["player_id"], {})
    tour_first_won = (sum(v["first_won"] for v in pool.serve_stats.values()) / len(pool.serve_stats)) if pool.serve_stats else None
    detail = (f"{A['short']} gets {pa_.measures['unret1']:.0%} of first serves unreturned (tour {pool.prior['unret1']:.0%})"
              + (f" and wins {sa['first_won']:.0%} behind it" if sa else "") + ". "
              f"{B['short']} puts {pb_.measures['inplay1']:.0%} of first serves back in play (tour {pool.prior['inplay1']:.0%}).")
    if not (pa_.n_charts and pb_.n_charts):
        detail = (f"{A['short']} wins {sa['first_won']:.0%} of first-serve points (tour {tour_first_won:.0%})." if sa and tour_first_won
                  else "") + " Charted serve and return detail is thin for this pair."
    return {"kind": "FIRST STRIKE", "question": f"Can {B['short']} handle {A['short']}'s serve?",
            "left": _side("b", B, "return", ret), "right": _side("a", A, "serve", srv),
            "edge": _edge(gap, "a", "b"), "detail": detail.strip(), "importance": abs(gap) + (10 if srv >= 85 or ret <= 15 else 0),
            "favours": "a" if gap > 0 else "b"}


def rally_power_q(pa_: profiles.Profile, pb_: profiles.Profile, A: dict, B: dict, pool: profiles.Pool) -> dict:
    """Can A take over the rallies (A's power v B's grind)?"""
    pw, gr = pa_.axes["Power"], pb_.axes["Grind"]
    gap = pw - gr
    detail = (f"{A['short']} ends {100 * pa_.measures['power']:.0f} of every 100 drives with a winner or forced error "
              f"(tour {100 * pool.prior['power']:.0f}). {B['short']} wins {pb_.measures['grind']:.0%} of rallies of 7+ shots "
              f"(tour {pool.prior['grind']:.0%}).")
    return {"kind": "RALLY", "question": f"Can {A['short']} take over the rallies?",
            "left": _side("b", B, "grind", gr), "right": _side("a", A, "power", pw),
            "edge": _edge(gap, "a", "b"), "detail": detail, "importance": abs(gap) + (10 if pw >= 85 or gr <= 15 else 0),
            "favours": "a" if gap > 0 else "b"}


def wing_qs(pa_: profiles.Profile, pb_: profiles.Profile, A: dict, B: dict, since: date) -> list[dict]:
    """Crosscourt diagonals: same-handed players meet forehand-to-forehand and backhand-to-backhand;
    a lefty's crosscourt forehand meets a righty's crosscourt backhand."""
    same = (A.get("hand") or "R") == (B.get("hand") or "R")
    pairs = [("bxc", "bxc"), ("fxc", "fxc")] if same else [("bxc", "fxc"), ("fxc", "bxc")]
    out = []
    for wa, wb in pairs:
        ra, rb = pa_.measure_pct[wa], pb_.measure_pct[wb]
        va, vb = 100 * pa_.measures[wa], 100 * pb_.measures[wb]
        weak_is_a = ra <= rb
        W, S = (A, B) if weak_is_a else (B, A)
        w_wing, s_wing = (wa, wb) if weak_is_a else (wb, wa)
        w_rank = min(ra, rb)
        wname = "backhand" if w_wing.startswith("b") else "forehand"
        sname = "backhand" if s_wing.startswith("b") else "forehand"
        if w_rank <= 25:
            q = f"Will {W['short']}'s {wname} hold up crosscourt?"
        elif w_wing == s_wing:
            q = f"Can {W['short']}'s crosscourt {wname} keep up with {S['short']}'s?"
        else:
            q = f"Can {W['short']}'s crosscourt {wname} keep up with {S['short']}'s {sname}?"
        gap = ra - rb
        out.append({"kind": "RALLY", "question": q,
                    "left": _side("a", A, f"crosscourt {'backhand' if wa.startswith('b') else 'forehand'}", ra),
                    "right": _side("b", B, f"crosscourt {'backhand' if wb.startswith('b') else 'forehand'}", rb),
                    "edge": _edge(gap, "a", "b"),
                    "detail": f"Winners and forced errors minus unforced errors, per 100 crosscourt shots: "
                              f"{A['short']} {va:+.1f}, {B['short']} {vb:+.1f} (charted matches since {since_label(since)}).",
                    "importance": abs(gap) + (10 if w_rank <= 15 else 0), "favours": "a" if gap > 0 else "b"})
    return out


def _side(side: str, P: dict, what: str, rank: float) -> dict:
    return {"side": side, "who": P["short"], "what": what, "label": f"{P['short']} {what}", "rank": rank}


def _edge(gap: float, a: str, b: str) -> dict:
    who = a if gap > 0 else b
    size = abs(gap)
    if size < 5:
        return {"label": "EVEN", "side": None}
    return {"label": ("EDGE" if size >= 15 else "SLIGHT EDGE"), "side": who}


def key_questions(pa_, pb_, A, B, pool, since) -> list[dict]:
    fs = [first_strike_q(pa_, pb_, A, B, pool), first_strike_q(pb_, pa_, B, A, pool)]
    for q in fs[1:]:  # second question was built with the roles swapped: put the sides back
        _swap_sides(q)
    charted = pa_.n_charts >= 3 and pb_.n_charts >= 3
    rally = []
    if charted:
        rally = [rally_power_q(pa_, pb_, A, B, pool), rally_power_q(pb_, pa_, B, A, pool)]
        _swap_sides(rally[1])
        rally += wing_qs(pa_, pb_, A, B, since)
    fs.sort(key=lambda q: -q["importance"])
    rally.sort(key=lambda q: -q["importance"])
    chosen = [fs[0]] + ([rally[0]] if rally else [fs[1]])
    chosen.sort(key=lambda q: -q["importance"])
    return chosen


def _swap_sides(q: dict) -> None:
    for k in ("left", "right"):
        q[k]["side"] = "a" if q[k]["side"] == "b" else "b"
    if q["edge"]["side"]:
        q["edge"]["side"] = "a" if q["edge"]["side"] == "b" else "b"
    q["favours"] = "a" if q["favours"] == "b" else "b"


def style_groups(pool: profiles.Pool, profs: dict[str, profiles.Profile], opp: profiles.Profile,
                 opp_facts: dict) -> list[dict]:
    """The opponent's style as groups of tour players: his ten closest style matches, and his archetype
    (or, for a plain baseliner, his weak spot)."""
    groups = []
    if opp.player_id in profs:
        groups.append({"label": "v the 10 most like him", "members": set(profiles.similar(pool, profs, opp.player_id))})
    else:  # few charts: closest on serve and return only
        me = opp.axes
        cand = []
        for q, pr in profs.items():
            cand.append((math.hypot(me["Serve"] - pr.axes["Serve"], me["Return"] - pr.axes["Return"]), q))
        cand.sort()
        groups.append({"label": "v the 10 closest on serve and return", "members": {q for _, q in cand[:10]}})
    if opp.archetype not in (profiles.FALLBACK[0], profiles.UNPROFILED[0]):
        mem = {q for q, pr in profs.items() if pr.archetype == opp.archetype or opp.archetype in pr.mixed}
        groups.append({"label": f"v {opp.archetype}s", "icon": opp.icon, "members": mem})
    elif opp.weaknesses:
        w = opp.weaknesses[0]
        mem = {q for q, pr in profs.items() if w in pr.weaknesses}
        groups.append({"label": f"v {w}", "weak": True, "members": mem})
    return groups


def against_style(conn, pid: str, ms: list[M], groups: list[dict], since: date, ev: Event) -> list[dict]:
    window = [m for m in ms if m.date >= since.isoformat() and m.tour]
    pred = predictions(conn, [m.match_id for m in window])
    out = []
    for g in groups:
        sel = [m for m in window if m.opp_id in g["members"] and m.match_id in pred and m.opp_id != pid]
        w, l = record(sel)
        exp = sum(expected_for(pred, m.match_id, pid) for m in sel)
        row = {"label": g["label"], "icon": g.get("icon"), "weak": g.get("weak", False), "w": w, "l": l,
               "expected": exp, "n": len(sel)}
        if ev.speed is not None:
            near = [m for m in sel if m.speed is not None and abs(m.speed - ev.speed) <= BAND]
            if len(near) >= 3:
                row["near"] = record(near)
        out.append(row)
    return out


def speed_tag(conn: sqlite3.Connection, pid: str, ms: list[M], since: date) -> dict | None:
    """'Fast-court riser' / 'Slow-court riser': wins above the model's expectation per match on fast courts
    (SPEED 60+) against slow ones (under 45), tour level, when the gap is at least 0.08 and z >= 1.64."""
    window = [m for m in ms if m.date >= since.isoformat() and m.tour and m.speed is not None]
    pred = predictions(conn, [m.match_id for m in window])
    groups = {"fast": [m for m in window if m.speed >= 60], "slow": [m for m in window if m.speed < 45]}
    stats = {}
    for k, g in groups.items():
        g = [m for m in g if m.match_id in pred]
        if len(g) < 10:
            return None
        ps = [expected_for(pred, m.match_id, pid) for m in g]
        stats[k] = (sum(m.won for m in g) - sum(ps)) / len(g), sum(p * (1 - p) for p in ps) / len(g) ** 2, len(g)
    diff = stats["fast"][0] - stats["slow"][0]
    z = diff / math.sqrt(stats["fast"][1] + stats["slow"][1])
    if abs(diff) < 0.08 or abs(z) < 1.64:
        return None
    return {"tag": "Fast-court riser" if diff > 0 else "Slow-court riser", "diff": diff, "z": z,
            "n_fast": stats["fast"][2], "n_slow": stats["slow"][2]}


def court_tilt(pool: profiles.Pool, pa_: profiles.Profile, pb_: profiles.Profile, ev: Event, pred: dict,
               conn: sqlite3.Connection) -> dict | None:
    """How this court's speed moves the served chance, through each man's short-point and rally rates."""
    c = pred["components"]
    if ev.speed is None or "anchored_point_win" not in c or not pool.short_fit[1]:
        return None
    io = "I" if ev.indoor else "O"
    row = conn.execute(
        """SELECT AVG(speed) FROM edition_speed WHERE surface = ? AND COALESCE(indoor, 'O') = ? AND year >= ?""",
        (ev.surface, io, court_speed.SCALE_FROM)).fetchone()
    bucket_speed = row[0] if row and row[0] is not None else 50.0
    slope = pool.short_fit[1]
    d_short = slope * (ev.speed - bucket_speed)

    def log5(srv, ret, mean):
        return sigmoid(logit(min(max(srv, 0.01), 0.99)) + logit(min(max(1 - ret, 0.01), 0.99)) - logit(mean))

    def on_serve(s: profiles.Profile, r: profiles.Profile):
        short = log5(s.measures["fs_sv"], r.measures["fs_rt"], pool.prior["fs_sv"])
        long = log5(s.measures["ra_sv"], r.measures["ra_rt"], pool.prior["ra_sv"])
        return short, long

    sa, la = on_serve(pa_, pb_)
    sb, lb = on_serve(pb_, pa_)
    qa, qb = c["anchored_point_win"]
    rule = markov.final_set_rule(ev.level, ev.name, int(ev.date[:4]), ev.best_of)
    p0 = markov.match_win(qa, qb, ev.best_of, rule)
    qa1 = min(max(qa + d_short * (sa - la), 0.01), 0.99)
    qb1 = min(max(qb + d_short * (sb - lb), 0.01), 0.99)
    p1 = markov.match_win(qa1, qb1, ev.best_of, rule)
    tilt = p1 - p0
    return {"bucket_speed": bucket_speed, "short_here": profiles.short_share_at(pool, ev.speed),
            "short_medium": profiles.short_share_at(pool, 50.0), "short_bucket": profiles.short_share_at(pool, bucket_speed),
            "d_short": d_short, "tilt_a": tilt, "short_win": [sa, sb], "long_win": [la, lb]}


def tiebreak_file(a_ms: list[M], b_ms: list[M], ev: Event, since: date, today: date) -> dict:
    year_ago = (today - timedelta(days=365)).isoformat()
    io = "I" if ev.indoor else "O"

    def rates(ms):
        last52 = [m for m in ms if m.date >= year_ago and m.tour]
        bucket = [m for m in last52 if m.surface == ev.surface and (m.indoor or "O") == io]
        bo3 = [m for m in last52 if (m.best_of or 3) == 3 and m.outcome == "completed"]
        fast = [m for m in ms if m.date >= since.isoformat() and m.tour and m.speed is not None and m.speed >= 57]
        band = [m for m in ms if m.date >= since.isoformat() and m.tour and m.surface == ev.surface and (m.indoor or "O") == io
                and ev.speed is not None and m.speed is not None and abs(m.speed - ev.speed) <= BAND and (m.best_of or 3) == 3]
        return {
            "tb_52": (sum(m.tiebreaks > 0 for m in last52), len(last52)),
            "tb_bucket_52": (sum(m.tiebreaks > 0 for m in bucket), len(bucket)),
            "bo3": len(bo3), "bo3_three": sum(len(m.sets) == 3 for m in bo3), "bo3_tb": sum(m.tiebreaks > 0 for m in bo3),
            "fast_hold": hold_break(fast)["hold"], "fast_n": len(fast),
            "band": len(band), "band_three": sum(len(m.sets) >= 3 for m in band), "band_tb": sum(m.tiebreaks > 0 for m in band),
        }

    last5 = [a_ms[:5], b_ms[:5]]
    ten = last5[0] + last5[1]
    return {"a": rates(a_ms), "b": rates(b_ms),
            "tiebreaks_last10": sum(m.tiebreaks for m in ten),
            "games_last10": (sum(m.games for m in ten) / len(ten)) if ten else None}


def tour_rates(conn: sqlite3.Connection, ev: Event, today: date) -> dict:
    """Tour-level baselines over the last year: share of matches with a tiebreak (all courts, this court type)
    and the hold rate on this court type."""
    since = (today - timedelta(days=365)).isoformat()
    io = "I" if ev.indoor else "O"
    q = """SELECT m.score, COALESCE(m.surface, e.surface), COALESCE(m.indoor, e.indoor),
                  m.w_svgms, m.w_bpsaved, m.w_bpfaced, m.l_svgms, m.l_bpsaved, m.l_bpfaced
           FROM match m JOIN tournament_edition e USING (edition_id)
           WHERE m.outcome = 'completed' AND COALESCE(m.level, e.level, '') != 'C' AND COALESCE(m.round, '') != 'Q'
             AND COALESCE(m.event_date_lo, e.start_date) >= ?"""
    n = tb = nb = tbb = sg = held = 0
    for score, surf, ind, wg, ws, wf, lg, ls, lf in conn.execute(q, (since,)):
        has = any(is_tiebreak(s) for s in score_sets(score))
        n, tb = n + 1, tb + has
        if surf == ev.surface and (ind or "O") == io:
            nb, tbb = nb + 1, tbb + has
            for g, s, f in ((wg, ws, wf), (lg, ls, lf)):
                if g and g > 0 and s is not None and f is not None and 0 <= s <= f and f - s <= g:
                    sg, held = sg + g, held + g - (f - s)
    return {"tb_share": tb / n if n else None, "tb_share_bucket": tbb / nb if nb else None, "hold_bucket": held / sg if sg else None}


def kalshi(conn: sqlite3.Connection) -> dict:
    """Open Kalshi ATP match quotes keyed by player pair; empty when the exchange cannot be reached."""
    try:
        from .upcoming import kalshi_quotes

        return kalshi_quotes(conn)
    except Exception:  # no network or the exchange refused: cards simply have no market line
        return {}


def market(quotes: dict, a: str, b: str) -> dict | None:
    """Kalshi's current price for the match, when it is listed (margin removed across both contracts)."""
    q = quotes.get(frozenset((a, b)))
    if not q:
        return None
    ma, mb = (q[a]["bid"] + q[a]["ask"]) / 2, (q[b]["bid"] + q[b]["ask"]) / 2
    return {"source": "Kalshi", "p_a": ma / (ma + mb), "bid_a": q[a]["bid"], "ask_a": q[a]["ask"],
            "bid_b": q[b]["bid"], "ask_b": q[b]["ask"]}


# --------------------------------------------------------------------------------------------- headline

def showdown(A: dict, B: dict, qs: list[dict], p_a: float, ev: Event, tilt: dict | None) -> str:
    fav, dog = (A, B) if p_a >= 0.5 else (B, A)
    fav_side = "a" if p_a >= 0.5 else "b"
    pf = max(p_a, 1 - p_a)
    q = qs[0]
    sides = {"a": A, "b": B}
    hi, lo = (q["right"], q["left"]) if q["right"]["rank"] >= q["left"]["rank"] else (q["left"], q["right"])
    H, L = sides[hi["side"]], sides[lo["side"]]
    hi_what, lo_what = hi["what"], lo["what"]
    if abs(hi["rank"] - lo["rank"]) < 10:
        kind = ("strength against strength" if lo["rank"] >= 60 else "weakness against weakness" if hi["rank"] < 40
                else "evenly matched")
        first = f"{H['short']}'s {hi_what} meets {L['short']}'s {lo_what}, {kind}: they rank close together."
    elif hi["rank"] >= 60 and lo["rank"] < 40:
        first = f"{H['short']}'s {hi_what} {_rank_words(hi['rank'])}, and {L['short']}'s {lo_what} does not."
    elif hi["rank"] >= 60:
        first = f"{H['short']}'s {hi_what} {_rank_words(hi['rank'])}, ahead of {L['short']}'s {lo_what}."
    else:
        first = f"{H['short']}'s {hi_what} ({hi['rank']:.0f}) outranks {L['short']}'s {lo_what} ({lo['rank']:.0f}), both below the tour middle."
    if pf < 0.53:
        second = "The model has it close to a coin flip, so that matchup is where it turns."
    elif q["favours"] == fav_side:
        second = f"It is a big part of why {fav['short']} is favored."
    else:
        tag = "slight underdog" if pf < 0.6 else "underdog"
        second = f"That is the {tag}'s cleanest path {where(ev.name)}."
    if tilt and abs(tilt["tilt_a"]) >= 0.01:
        helped = A if tilt["tilt_a"] > 0 else B
        second += f" The {ev.speed_label} court nudges it toward {helped['short']}."
    return f"{first} {second}"


# --------------------------------------------------------------------------------------------- build

def build(conn: sqlite3.Connection, a: str, b: str, ev: Event, as_of: date | None = None, quotes: dict | None = None,
          pool: profiles.Pool | None = None) -> dict:
    """The full card payload. ``a``/``b`` are names or internal IDs (resolved like /predict); ``quotes`` are
    Kalshi quotes from ``kalshi()`` (fetched once per slate), or None for no market line."""
    today = as_of or date.fromisoformat(ev.date)
    since = profiles.window_start(today)
    pred = predict.predict(conn, a, b, ev.surface, ev.indoor, ev.best_of).to_dict()
    ida, idb = pred["player_a"]["player_id"], pred["player_b"]["player_id"]
    pool = pool or profiles.build_pool(conn, today)
    profs = profiles.all_profiles(conn, pool)
    pa_, pb_ = profiles.profile(conn, pool, ida), profiles.profile(conn, pool, idb)
    live = standings(conn)
    keys_of = court_speed.event_keys(conn)
    # Only matches before the match date: a card for a match already played must not show its own result.
    a_ms = matches(conn, ida, until=today, keys_of=keys_of)
    b_ms = matches(conn, idb, until=today, keys_of=keys_of)
    A, B = player_facts(conn, ida, live, a_ms), player_facts(conn, idb, live, b_ms)
    for pr_, pid, ms in ((pa_, ida, a_ms), (pb_, idb, b_ms)):
        tag = speed_tag(conn, pid, ms, since)
        if tag:
            pr_.mixed = (pr_.mixed + [tag["tag"]])[-3:]
    qs = key_questions(pa_, pb_, A, B, pool, since)
    tilt = court_tilt(pool, pa_, pb_, ev, pred, conn)
    p_a = pred["p_a"]
    blend = min(max(p_a + (tilt["tilt_a"] if tilt else 0.0), 0.01), 0.99)
    groups_vs_b, groups_vs_a = style_groups(pool, profs, pb_, B), style_groups(pool, profs, pa_, A)
    shorts: dict[str, str] = {}

    def names(pid: str | None, display: str) -> str:
        if not pid:
            return display
        if pid not in shorts:
            shorts[pid] = short_name(conn, pid, display)
        return shorts[pid]
    out = {
        "event": ev.to_dict(),
        "as_of": today.isoformat(),
        "window_start": since.isoformat(),
        "players": [A, B],
        "profiles": [pa_.to_dict(), pb_.to_dict()],
        "prediction": pred,
        "model": {"p_a": p_a, "name": pred["model"], "confidence": pred["confidence"]},
        "blend": {"p_a": blend, "tilt_a": tilt["tilt_a"] if tilt else 0.0},
        "tilt": tilt,
        "questions": qs,
        "against": [against_style(conn, ida, a_ms, groups_vs_b, since, ev), against_style(conn, idb, b_ms, groups_vs_a, since, ev)],
        "form": [form(conn, ida, a_ms, names=names), form(conn, idb, b_ms, names=names)],
        "tournament_record": [tournament_record(a_ms, ev.event_key), tournament_record(b_ms, ev.event_key)],
        "court": [court_records(a_ms, ev, since), court_records(b_ms, ev, since)],
        "h2h": head_to_head(conn, ida, a_ms, idb),
        "tiebreaks": tiebreak_file(a_ms, b_ms, ev, since, today),
        "tour": tour_rates(conn, ev, today),
        "recent": [[_recent_row(m, names) for m in a_ms[:5]], [_recent_row(m, names) for m in b_ms[:5]]],
        "last_five": [record(a_ms[:5]), record(b_ms[:5])],
        "market": market(quotes, ida, idb) if quotes else None,
        "pool": {"charted_players": len(pool.charted), "min_charts": profiles.MIN_CHARTS},
    }
    out["headline"] = showdown(A, B, qs, p_a, ev, tilt)
    return out


def _recent_row(m: M, names=None) -> dict:
    return {"won": m.won, "opp": names(m.opp_id, m.opp_name) if names else m.opp_name, "event": m.event, "round": m.round, "date": m.date, "level": m.level,
            "sets": [fmt_set(s) for s in m.my_sets], "tb": [is_tiebreak(s) for s in m.my_sets], "tiebreaks": m.tiebreaks,
            "retired": m.outcome == "retired", "games": m.games}
