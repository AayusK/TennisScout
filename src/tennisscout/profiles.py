"""Style profiles: what kind of player each man is, ranked against the tour.

Two sources, each used for what it can support:

* Match stats (every tour-level match with a stat line) give the serve and return axes, through the
  opponent-adjusted serve/return ratings, plus first-serve and hold/break rates.
* Match Charting Project lines (a sample of matches, charted shot by shot) give everything about
  how points are played: groundstroke outcomes, wings, rally length, net play, shot mix.

Every charting rate is shrunk toward the pool average by a pseudo-count, so a man with three
charted matches sits near the middle instead of at an extreme, and the profile says how many
matches it rests on. Ranks are percentiles (0-100) among the pool: men with at least
``MIN_CHARTS`` charted matches in the window for charting measures, men with a tour-level match
in the last year for serve and return.

Archetypes and tags are rules on those ranks, written out below; nothing is learned or hidden.
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db

WINDOW_YEARS = 3
MIN_CHARTS = 5
MIN_TOUR_MATCHES = 10  # tour-level matches in the last year to count as a tour regular (serve/return ranks)
AXES = ("Serve", "Power", "Net", "Variety", "Grind", "Consistency", "Return")
AXIS_HELP = {
    "Serve": "opponent-adjusted serve rating",
    "Return": "opponent-adjusted return rating",
    "Power": "winners + forced errors per 100 drives",
    "Consistency": "unforced errors per 100 drives, reversed",
    "Grind": "points won in rallies of 7+ shots",
    "Variety": "share of slices, drops, lobs and volleys",
    "Net": "points finished at the net per 100",
}
WINGS = {"fxc": "crosscourt forehand", "bxc": "crosscourt backhand", "fdtl": "down-the-line forehand",
         "bdtl": "down-the-line backhand", "fio": "inside-out forehand"}

# name -> (icon, rule over percentiles p and the profile, anchor axis for ordering). When several fit, the
# highest anchor rank plus PRIORITY wins: serve, return and power define a game more than shot mix does.
ARCHETYPES = {
    "Big Server": ("🚀", lambda p, x: p["Serve"] >= 80, "Serve"),
    "Returner": ("🛡️", lambda p, x: p["Return"] >= 80, "Return"),
    "Gunslinger": ("🔫", lambda p, x: p["Power"] >= 70 and p["Consistency"] <= 35, "Power"),
    "Shotmaker": ("🪄", lambda p, x: p["Power"] >= 75 and p["Consistency"] > 35, "Power"),
    "Forehand Bully": ("💪", lambda p, x: x.get("fh_share_pct", 0) >= 70 and x.get("fh_pct", 0) >= 65, "fh_pct"),
    "Grinder": ("🧱", lambda p, x: p["Grind"] >= 75 and p["Consistency"] >= 60, "Grind"),
    "Counterpuncher": ("🔄", lambda p, x: p["Return"] >= 65 and p["Grind"] >= 65 and p["Power"] <= 50, "Return"),
    "Net Rusher": ("🏃", lambda p, x: p["Net"] >= 85, "Net"),
    "Artist": ("🎨", lambda p, x: p["Variety"] >= 80, "Variety"),
    "All-Courter": ("⚖️", lambda p, x: min(p.values()) >= 40 and sum(v >= 55 for v in p.values()) >= 4, None),
}
PRIORITY = {"Big Server": 8, "Returner": 6, "Gunslinger": 4, "Shotmaker": 4, "Forehand Bully": 4, "Grinder": 2,
            "Counterpuncher": 2, "Net Rusher": 0, "Artist": -4, "All-Courter": -8}
FALLBACK = ("Baseliner", "🎾")
UNPROFILED = ("Unprofiled", "❔")
WEAKNESSES = {
    "Weak backhand": lambda p, x: x.get("bh_pct", 50) <= 20,
    "Weak forehand": lambda p, x: x.get("fh_pct", 50) <= 20,
    "Error-prone": lambda p, x: p["Consistency"] <= 20,
    "Soft return": lambda p, x: p["Return"] <= 20,
    "Low first-serve %": lambda p, x: x.get("first_in_pct", 50) <= 20,
    "Shaky second serve": lambda p, x: x.get("second_won_pct", 50) <= 20,
}


def window_start(as_of: date) -> date:
    return as_of.replace(year=as_of.year - WINDOW_YEARS)


def pct_of(value: float, pool: list[float], higher_is_better: bool = True) -> float:
    """Percentile of a value that may not be in the pool."""
    order = sorted(pool)
    if not order:
        return 50.0
    lo, hi = bisect.bisect_left(order, value), bisect.bisect_right(order, value)
    r = 100 * (lo + (hi - lo) / 2) / len(order)
    return r if higher_is_better else 100 - r


@dataclass
class Rate:
    num: float = 0.0
    den: float = 0.0

    def add(self, num, den):
        self.num += num or 0
        self.den += den or 0

    def shrunk(self, prior: float, k: float) -> float:
        return (self.num + k * prior) / (self.den + k)

    def raw(self) -> float | None:
        return self.num / self.den if self.den else None


# measure -> (numerator fn, denominator fn, pseudo-count). c is one charting line's counts.
CHART_MEASURES = {
    "power": (lambda c: c.get("gs_win", 0) + c.get("gs_forced", 0), lambda c: c.get("gs_shots", 0), 400),
    "ue_rate": (lambda c: c.get("gs_ue", 0), lambda c: c.get("gs_shots", 0), 400),
    "grind": (lambda c: sum(c.get(f"{k}_{s}_won", 0) for k in ("r79", "r10") for s in ("sv", "rt")),
              lambda c: sum(c.get(f"{k}_{s}_pts", 0) for k in ("r79", "r10") for s in ("sv", "rt")), 60),
    "variety": (lambda c: c.get("shots", 0) - c.get("gs_shots", 0), lambda c: c.get("shots", 0), 300),
    "net": (lambda c: c.get("net_pts", 0), lambda c: c.get("serve_pts", 0) + c.get("return_pts", 0), 150),
    "fh_share": (lambda c: c.get("fside", 0), lambda c: c.get("fside", 0) + c.get("bside", 0), 300),
    "unret1": (lambda c: c.get("sv1_aces", 0) + c.get("sv1_unret", 0) + c.get("sv1_forced", 0), lambda c: c.get("sv1_pts", 0), 80),
    "inplay1": (lambda c: c.get("rt1_inplay", 0), lambda c: c.get("rt1_pts", 0), 80),
    "fs_sv": (lambda c: c.get("r13_sv_won", 0), lambda c: c.get("r13_sv_pts", 0), 60),
    "fs_rt": (lambda c: c.get("r13_rt_won", 0), lambda c: c.get("r13_rt_pts", 0), 60),
    "ra_sv": (lambda c: sum(c.get(f"{k}_sv_won", 0) for k in ("r46", "r79", "r10")),
              lambda c: sum(c.get(f"{k}_sv_pts", 0) for k in ("r46", "r79", "r10")), 60),
    "ra_rt": (lambda c: sum(c.get(f"{k}_rt_won", 0) for k in ("r46", "r79", "r10")),
              lambda c: sum(c.get(f"{k}_rt_pts", 0) for k in ("r46", "r79", "r10")), 60),
    "rally": (lambda c: sum(c.get(f"{k}_{s}_won", 0) for k in ("r46", "r79", "r10") for s in ("sv", "rt")),
              lambda c: sum(c.get(f"{k}_{s}_pts", 0) for k in ("r46", "r79", "r10") for s in ("sv", "rt")), 80),
    "short_sv": (lambda c: c.get("r13_sv_pts", 0), lambda c: sum(c.get(f"{k}_sv_pts", 0) for k in ("r13", "r46", "r79", "r10")), 80),
}
for _w in WINGS:
    CHART_MEASURES[_w] = (lambda c, w=_w: c.get(f"{w}_win", 0) + c.get(f"{w}_forced", 0) - c.get(f"{w}_ue", 0),
                          lambda c, w=_w: c.get(f"{w}_shots", 0), 150)


def _short_share(c: dict) -> float | None:
    pts = sum(c.get(f"{k}_{s}_pts", 0) for k in ("r13", "r46", "r79", "r10") for s in ("sv",))
    if not pts:
        return None
    return c.get("r13_sv_pts", 0) / pts


@dataclass
class Pool:
    as_of: date
    lines: dict[str, list[dict]]                      # player -> charting lines in window
    rates: dict[str, dict[str, Rate]]                 # player -> measure -> Rate
    prior: dict[str, float]                           # measure -> pool rate
    charted: set[str]                                 # players with >= MIN_CHARTS charts
    sr: dict[str, dict[str, float]]                   # player -> {"serve": mean, "return": mean}
    active: set[str]                                  # MIN_TOUR_MATCHES+ tour-level matches in the last year
    serve_stats: dict[str, dict[str, float]]          # player -> first_in, first_won, second_won (last year)
    gravity: dict[str, float] = field(default_factory=dict)
    gravity_n: dict[str, int] = field(default_factory=dict)
    short_fit: tuple[float, float] = (0.0, 0.0)      # match short-point share = a + b * speed
    speed_by_match: dict[str, float] = field(default_factory=dict)
    _cache: dict = field(default_factory=dict, repr=False)

    def value(self, pid: str, measure: str) -> float:
        k = CHART_MEASURES[measure][2]
        r = self.rates.get(pid, {}).get(measure)
        return r.shrunk(self.prior[measure], k) if r else self.prior[measure]

    def pool_values(self, measure: str) -> list[float]:
        key = ("pool", measure)
        if key not in self._cache:
            self._cache[key] = sorted(self.value(p, measure) for p in self.charted)
        return self._cache[key]


def _sr_ratings(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    model = db.get_meta(conn, "sr_model") or "serve_return"
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for r in conn.execute("SELECT player_id, kind, mean FROM sr_rating WHERE model = ? AND kind IN ('serve', 'return')", (model,)):
        out[r[0]][r[1]] = r[2]
    return out


def _active_and_serve(conn: sqlite3.Connection, as_of: date) -> tuple[set[str], dict[str, dict[str, float]]]:
    since = (as_of - timedelta(days=365)).isoformat()
    tot: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0])  # svpt, 1stin, 1stwon, 2nd pts, 2ndwon, matches
    played: dict[str, int] = defaultdict(int)
    q = """SELECT m.winner_id, m.loser_id, m.w_svpt, m.w_1stin, m.w_1stwon, m.w_2ndwon,
                  m.l_svpt, m.l_1stin, m.l_1stwon, m.l_2ndwon
           FROM match m JOIN tournament_edition e USING (edition_id)
           WHERE m.identity_status = 'ok' AND m.outcome IN ('completed', 'retired')
             AND COALESCE(m.event_date_lo, e.start_date) >= ? AND COALESCE(m.event_date_lo, e.start_date) < ?
             AND COALESCE(m.level, e.level, '') != 'C' AND COALESCE(m.round, '') != 'Q'"""
    for r in conn.execute(q, (since, as_of.isoformat())):
        for pid, sv, fi, fw, sw in ((r[0], *r[2:6]), (r[1], *r[6:10])):
            if not pid:
                continue
            played[pid] += 1
            if sv and fi is not None and fw is not None and sw is not None and 0 < fi <= sv and fw <= fi and sw <= sv - fi:
                t = tot[pid]
                t[0] += sv
                t[1] += fi
                t[2] += fw
                t[3] += sv - fi
                t[4] += sw
                t[5] += 1
    active = {p for p, n in played.items() if n >= MIN_TOUR_MATCHES}
    stats = {}
    for pid, (sv, fi, fw, sp, sw, n) in tot.items():
        if n >= MIN_TOUR_MATCHES:
            stats[pid] = {"first_in": fi / sv, "first_won": fw / fi, "second_won": sw / sp if sp else 0.0, "matches": n}
    return active, stats


def build_pool(conn: sqlite3.Connection, as_of: date | None = None) -> Pool:
    as_of = as_of or date.today()
    start = window_start(as_of).isoformat()
    lines: dict[str, list[dict]] = defaultdict(list)
    speed_by_match = {}
    try:
        q = """SELECT c.mcp_match_id, c.player_id, c.opponent_id, c.match_date, c.surface, c.match_id, c.counts_json, s.speed
               FROM charting_line c LEFT JOIN match m ON m.match_id = c.match_id
               LEFT JOIN edition_speed s ON s.edition_id = m.edition_id
               WHERE c.match_date >= ? AND c.match_date < ?"""
        rows = conn.execute(q, (start, as_of.isoformat())).fetchall()
    except sqlite3.OperationalError:  # tables not built yet (no charting or speed step run)
        rows = []
    for r in rows:
        line = {"chart": r[0], "pid": r[1], "opp": r[2], "date": r[3], "surface": r[4], "match_id": r[5],
                "c": json.loads(r[6]), "speed": r[7]}
        lines[r[1]].append(line)
        if r[7] is not None:
            speed_by_match[r[0]] = r[7]
    rates: dict[str, dict[str, Rate]] = {}
    prior_tot = {m: Rate() for m in CHART_MEASURES}
    for pid, ls in lines.items():
        rs = {m: Rate() for m in CHART_MEASURES}
        for ln in ls:
            for m, (num, den, _) in CHART_MEASURES.items():
                rs[m].add(num(ln["c"]), den(ln["c"]))
        rates[pid] = rs
    charted = {p for p, ls in lines.items() if len(ls) >= MIN_CHARTS}
    for p in charted:  # the pool average is over the men who are ranked, each counted by his own volume
        for m in CHART_MEASURES:
            prior_tot[m].add(rates[p][m].num, rates[p][m].den)
    prior = {m: (r.raw() if r.den else 0.0) for m, r in prior_tot.items()}
    active, serve_stats = _active_and_serve(conn, as_of)
    pool = Pool(as_of, dict(lines), rates, prior, charted, _sr_ratings(conn), active, serve_stats,
                speed_by_match=speed_by_match)
    _fit_gravity(pool)
    return pool


def _fit_gravity(pool: Pool, ridge: float = 4.0, iterations: int = 15) -> None:
    """Points per 100 that end within three shots in a man's matches, above (+) or below (-) what the court
    speed and his opponents' own pull predict. Two-way fit (player + opponent) on charted matches."""
    charts: dict[str, dict] = {}
    for pid, ls in pool.lines.items():
        for ln in ls:
            share = _short_share(ln["c"])
            pts = sum(ln["c"].get(f"{k}_sv_pts", 0) for k in ("r13", "r46", "r79", "r10"))
            if share is None or pts < 20:
                continue
            ch = charts.setdefault(ln["chart"], {"players": {}, "speed": ln["speed"], "surface": ln["surface"]})
            ch["players"][pid] = (share, pts)
    obs = []  # (chart, players, match-level short share, speed, surface)
    for cid, ch in charts.items():
        tot = sum(p for _, p in ch["players"].values())
        sh = sum(s * p for s, p in ch["players"].values()) / tot
        obs.append((cid, list(ch["players"]), sh, ch["speed"], ch["surface"]))
    if len(obs) < 30:
        return
    with_speed = [(o[3], o[2]) for o in obs if o[3] is not None]
    if len(with_speed) >= 30:
        mx = sum(x for x, _ in with_speed) / len(with_speed)
        my = sum(y for _, y in with_speed) / len(with_speed)
        sxx = sum((x - mx) ** 2 for x, _ in with_speed)
        b = sum((x - mx) * (y - my) for x, y in with_speed) / sxx if sxx else 0.0
        pool.short_fit = (my - b * mx, b)
    by_surface: dict[str, list[float]] = defaultdict(list)
    for o in obs:
        by_surface[o[4] or "?"].append(o[2])
    surf_mean = {s: sum(v) / len(v) for s, v in by_surface.items()}
    a, b = pool.short_fit

    def base(o):
        return a + b * o[3] if (o[3] is not None and b) else surf_mean[o[4] or "?"]

    g: dict[str, float] = defaultdict(float)
    n: dict[str, int] = defaultdict(int)
    for o in obs:
        for p in o[1]:
            n[p] += 1
    for _ in range(iterations):
        acc: dict[str, float] = defaultdict(float)
        for o in obs:
            resid = o[2] - base(o)
            for p in o[1]:
                acc[p] += resid - sum(g[q] for q in o[1] if q != p)
        g = defaultdict(float, {p: acc[p] / (n[p] + ridge) for p in n})
    pool.gravity = {p: 100 * v for p, v in g.items()}
    pool.gravity_n = dict(n)


def short_share_at(pool: Pool, speed: float) -> float:
    a, b = pool.short_fit
    return a + b * speed


@dataclass
class Profile:
    player_id: str
    axes: dict[str, float]                 # axis -> percentile
    axis_n: dict[str, int]                 # axis -> matches behind it
    measures: dict[str, float]             # raw (shrunk) values
    measure_pct: dict[str, float]
    archetype: str
    icon: str
    mixed: list[str]
    weaknesses: list[str]
    spike: str
    n_charts: int
    gravity: float | None
    sr_matches: int

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def profile(conn: sqlite3.Connection, pool: Pool, pid: str) -> Profile:
    sr_vals_serve = {p: v["serve"] for p, v in pool.sr.items() if p in pool.active and "serve" in v}
    sr_vals_ret = {p: v["return"] for p, v in pool.sr.items() if p in pool.active and "return" in v}
    me = pool.sr.get(pid, {})
    p_serve = pct_of(me.get("serve", 0.0), list(sr_vals_serve.values()))
    p_ret = pct_of(me.get("return", 0.0), list(sr_vals_ret.values()))

    measures = {m: pool.value(pid, m) for m in CHART_MEASURES}
    mpct = {}
    for m in CHART_MEASURES:
        mpct[m] = pct_of(measures[m], pool.pool_values(m), higher_is_better=(m != "ue_rate"))
    axes = {
        "Serve": p_serve, "Return": p_ret, "Power": mpct["power"], "Consistency": mpct["ue_rate"],
        "Grind": mpct["grind"], "Variety": mpct["variety"], "Net": mpct["net"],
    }
    n_charts = len(pool.lines.get(pid, ()))
    sr_n = conn.execute(
        """SELECT COUNT(*) FROM match m JOIN tournament_edition e USING (edition_id)
           WHERE (m.winner_id = ? OR m.loser_id = ?) AND m.w_svpt > 0 AND m.l_svpt > 0
             AND COALESCE(m.event_date_lo, e.start_date) >= ?""",
        (pid, pid, window_start(pool.as_of).isoformat())).fetchone()[0]
    axis_n = {a: (sr_n if a in ("Serve", "Return") else n_charts) for a in AXES}

    extra = {
        "fh_share_pct": mpct["fh_share"],
        "fh_pct": (mpct["fxc"] + mpct["fio"]) / 2,
        "bh_pct": (mpct["bxc"] + mpct["bdtl"]) / 2,
    }
    ss = pool.serve_stats.get(pid)
    if ss:
        extra["first_in_pct"] = pct_of(ss["first_in"], [v["first_in"] for v in pool.serve_stats.values()])
        extra["second_won_pct"] = pct_of(ss["second_won"], [v["second_won"] for v in pool.serve_stats.values()])
    charted = n_charts >= 2  # below this the charting axes are the pool average, not a reading
    chart_axes = ("Power", "Consistency", "Grind", "Variety", "Net")
    rule_axes = dict(axes) if charted else {a: (v if a in ("Serve", "Return") else 50.0) for a, v in axes.items()}
    rule_extra = extra if charted else {k: v for k, v in extra.items() if k in ("first_in_pct", "second_won_pct")}

    matches = []
    for name, (icon, rule, anchor) in ARCHETYPES.items():
        if not charted and name not in ("Big Server", "Returner"):
            continue
        if rule(rule_axes, rule_extra):
            score = rule_axes.get(anchor, rule_extra.get(anchor, 0)) if anchor else sum(rule_axes.values()) / len(rule_axes)
            matches.append((score + PRIORITY.get(name, 0), name, icon))
    matches.sort(reverse=True)
    if matches:
        _, archetype, icon = matches[0]
    elif charted:
        archetype, icon = FALLBACK
    else:  # no charted matches and no serve/return spike: say so rather than guess a style
        archetype, icon = UNPROFILED
    mixed = [n for _, n, _ in matches[1:3]]
    weaknesses = [w for w, rule in WEAKNESSES.items()
                  if (charted or w in ("Soft return", "Low first-serve %", "Shaky second serve")) and rule(rule_axes, rule_extra)]
    shown = {a: v for a, v in axes.items() if charted or a not in chart_axes}
    spike = max(shown, key=shown.get)
    return Profile(pid, axes, axis_n, measures, {**mpct, **extra}, archetype, icon, mixed, weaknesses[:2], spike, n_charts,
                   pool.gravity.get(pid), sr_n)


def similar(pool: Pool, profiles: dict[str, Profile], pid: str, n: int = 10, exclude: set[str] = frozenset()) -> list[str]:
    """The n pool players whose seven style ranks are closest (Euclidean) to ``pid``'s."""
    me = profiles[pid].axes
    dist = []
    for q, pr in profiles.items():
        if q == pid or q in exclude or pr.n_charts < MIN_CHARTS:
            continue
        dist.append((math.sqrt(sum((me[a] - pr.axes[a]) ** 2 for a in AXES)), q))
    dist.sort()
    return [q for _, q in dist[:n]]


def all_profiles(conn: sqlite3.Connection, pool: Pool) -> dict[str, Profile]:
    """Profiles for every charted pool player (used for style groups and similarity)."""
    return {p: profile(conn, pool, p) for p in pool.charted}
