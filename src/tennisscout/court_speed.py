"""Court speed index per tournament edition, from the serve stats of the players who played there.

Every serve stat line (server s, returner r, edition E, season y) is an observation of two things:

    serve points won:  logit p    = c_E + a(s,y) - b(r,y)     (binomial in serve points)
    aces:              log rate   = d_E + u(s,y) + v(r,y)     (Poisson per serve point)

Player-season effects soak up who played, so c_E and d_E are what is left: how much easier it was
to win a point on serve, and to hit an ace, at that event than the same men managed elsewhere that
season. Both are standardised over tour-level editions since 2015, averaged, and put on a 0-100
scale: 50 is the average edition and 15 points is one standard deviation.

Each edition is shrunk toward a prior, so a six-match event cannot read as extreme: the mean of the
same event's previous three editions on the same surface when they exist, else its surface's
average. That is also what makes a speed for this week's edition usable after one round.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict

from . import db
from .ingest_api_tennis import TML_NAMES
from .normalize import norm_name

FIRST_YEAR = 2000
SCALE_FROM = 2015
SCALE_LEVELS = ("A", "M", "G", "F", "500", "250")  # standardise on tour-level events, not Davis Cup ties
RIDGE_PLAYER = {"spw": 25.0, "ace": 4.0}  # prior sd 0.2 (logit) and 0.5 (log rate) for player-season effects
RIDGE_EDITION = {"spw": 100.0, "ace": 16.0}  # prior sd 0.1 and 0.25 around the edition's prior
ITERATIONS = 12
BANDS = ((30, "slow"), (43, "medium-slow"), (57, "medium"), (70, "medium-fast"), (999, "fast"))


def label(speed: float | None) -> str:
    if speed is None:
        return "unknown"
    return next(name for top, name in BANDS if speed < top)


def event_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """edition_id -> event key. TML editions use their tournament ID; an API-Tennis edition joins the TML
    event whose name it matches (same rule as the API-Tennis ingest), else keeps its own tournament ID."""
    tml_by_name: dict[str, str] = {}
    out: dict[str, str] = {}
    rows = conn.execute("SELECT edition_id, tournament_id, name, snapshot_id, year FROM tournament_edition ORDER BY year").fetchall()
    for r in rows:
        if r["snapshot_id"] != "api_tennis":
            out[r["edition_id"]] = r["tournament_id"]
            tml_by_name[norm_name(r["name"])] = r["tournament_id"]
    for r in rows:
        if r["snapshot_id"] == "api_tennis":
            name = r["name"]
            keys = (norm_name(TML_NAMES.get(name, name)), norm_name(f"{name} Masters"), norm_name(name))
            out[r["edition_id"]] = next((tml_by_name[k] for k in keys if k in tml_by_name), r["tournament_id"])
    return out


def _lines(conn: sqlite3.Connection):
    ok = lambda s: (f"m.{s}_svpt > 0 AND m.{s}_1stin <= m.{s}_svpt AND m.{s}_1stwon <= m.{s}_1stin "
                    f"AND m.{s}_2ndwon <= m.{s}_svpt - m.{s}_1stin AND m.{s}_ace IS NOT NULL AND m.{s}_ace <= m.{s}_svpt")
    q = f"""SELECT m.edition_id, e.year, m.winner_id, m.loser_id,
                   CASE WHEN {ok('w')} THEN m.w_svpt END, m.w_1stwon + m.w_2ndwon, m.w_ace,
                   CASE WHEN {ok('l')} THEN m.l_svpt END, m.l_1stwon + m.l_2ndwon, m.l_ace
            FROM match m JOIN tournament_edition e USING (edition_id)
            WHERE m.identity_status = 'ok' AND m.outcome IN ('completed', 'retired')
              AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL AND e.year >= ?
              AND COALESCE(m.level, e.level, '') != 'C'"""
    for ed, year, w, l, wn, wk, wa, ln, lk, la in conn.execute(q, (FIRST_YEAR,)):
        if wn:
            yield ed, (w, year), (l, year), wn, wk, wa
        if ln:
            yield ed, (l, year), (w, year), ln, lk, la


def fit(obs: list[tuple], prior_factory, kind: str, iterations: int = ITERATIONS) -> dict[str, float]:
    """Edition effects for one outcome. obs: (edition, server key, returner key, n, k); kind 'spw' or 'ace'.

    Coordinate-wise Newton steps with ridge penalties. ``prior_factory(current effects)`` returns a function
    giving the value each edition is shrunk toward.
    """
    ace = kind == "ace"
    total_n, total_k = sum(o[3] for o in obs), sum(o[4] for o in obs)
    base = math.log(total_k / total_n) if ace else math.log(total_k / (total_n - total_k))
    ed = defaultdict(lambda: base)
    srv: dict = defaultdict(float)
    ret: dict = defaultdict(float)
    by_ed, by_srv, by_ret = defaultdict(list), defaultdict(list), defaultdict(list)
    for i, (e, s, r, _, _) in enumerate(obs):
        by_ed[e].append(i), by_srv[s].append(i), by_ret[r].append(i)
    sign = 1.0 if ace else -1.0  # aces: the returner's effect adds (aces allowed); points: it subtracts

    def eta(i):
        e, s, r, _, _ = obs[i]
        return ed[e] + srv[s] + sign * ret[r]

    def grad_info(idx):
        g = h = 0.0
        for i in idx:
            n, k = obs[i][3], obs[i][4]
            x = eta(i)
            if ace:
                mu = n * math.exp(x)
                g, h = g + k - mu, h + mu
            else:
                p = 1 / (1 + math.exp(-x))
                g, h = g + k - n * p, h + n * p * (1 - p)
        return g, h

    lam_p, lam_e = RIDGE_PLAYER[kind], RIDGE_EDITION[kind]
    for _ in range(iterations):
        for key, idx in by_srv.items():
            g, h = grad_info(idx)
            srv[key] += (g - lam_p * srv[key]) / (h + lam_p)
        for key, idx in by_ret.items():
            g, h = grad_info(idx)
            g *= sign
            ret[key] += (g - lam_p * ret[key]) / (h + lam_p)
        prior_of = prior_factory(dict(ed))
        for e, idx in by_ed.items():
            g, h = grad_info(idx)
            ed[e] += (g - lam_e * (ed[e] - prior_of(e))) / (h + lam_e)
    return dict(ed)


def compute(conn: sqlite3.Connection) -> dict:
    meta = {r["edition_id"]: r for r in conn.execute(
        "SELECT edition_id, year, surface, indoor, level, start_date FROM tournament_edition")}
    keys = event_keys(conn)
    spw_obs, ace_obs = [], []
    n_lines: dict[str, int] = defaultdict(int)
    for e, s, r, n, k, aces in _lines(conn):
        spw_obs.append((e, s, r, n, k))
        ace_obs.append((e, s, r, n, aces))
        n_lines[e] += 1
    if not spw_obs:
        return {"editions": 0}

    def group(e):
        m = meta[e]
        return (m["surface"] or "?", m["indoor"] or "O")

    # Previous editions of the same event on the same surface, newest first.
    history: dict[str, list[str]] = {}
    fam: dict[tuple, list[str]] = defaultdict(list)
    for e in n_lines:
        fam[(keys.get(e, e), *group(e))].append(e)
    for es in fam.values():
        es.sort(key=lambda e: (meta[e]["year"], meta[e]["start_date"] or ""))
        for i, e in enumerate(es):
            history[e] = es[max(0, i - 3):i][::-1]

    def prior_factory(effects):
        sums: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0])
        for x, v in effects.items():
            acc = sums[group(x)]
            acc[0] += v
            acc[1] += 1

        def prior_of(e):
            prev = [effects[p] for p in history.get(e, ()) if p in effects]
            if prev:
                return sum(prev) / len(prev)
            tot, n = sums.get(group(e), (0.0, 0))
            return tot / n if n else effects.get(e, 0.0)
        return prior_of

    spw = fit(spw_obs, prior_factory, "spw")
    ace = fit(ace_obs, prior_factory, "ace")

    pool = [e for e in n_lines if meta[e]["year"] >= SCALE_FROM and (meta[e]["level"] or "") in SCALE_LEVELS]
    pool = pool or list(n_lines)

    def stats(vals):
        m = sum(vals) / len(vals)
        return m, math.sqrt(sum((v - m) ** 2 for v in vals) / max(1, len(vals) - 1)) or 1.0

    ms, ss = stats([spw[e] for e in pool])
    ma, sa = stats([ace[e] for e in pool])
    comb = {e: ((spw[e] - ms) / ss + (ace[e] - ma) / sa) / 2 for e in n_lines}
    mc, sc = stats([comb[e] for e in pool])
    rows = []
    for e in n_lines:
        m = meta[e]
        speed = 50 + 15 * (comb[e] - mc) / sc
        rows.append((e, keys.get(e, e), m["year"], m["surface"], m["indoor"], round(speed, 2),
                     round(spw[e], 5), round(ace[e], 5), n_lines[e]))
    with conn:
        conn.execute("DELETE FROM edition_speed")
        conn.executemany("INSERT INTO edition_speed VALUES (?,?,?,?,?,?,?,?,?)", rows)
        db.set_meta(conn, "court_speed_scale", json.dumps(
            {"spw_mean": ms, "spw_sd": ss, "ace_mean": ma, "ace_sd": sa, "comb_mean": mc, "comb_sd": sc,
             "from_year": SCALE_FROM, "editions": len(rows)}))
    return {"editions": len(rows), "lines": len(spw_obs)}


def speed_of(conn: sqlite3.Connection, edition_id: str) -> float | None:
    r = conn.execute("SELECT speed FROM edition_speed WHERE edition_id = ?", (edition_id,)).fetchone()
    return r[0] if r else None


def history(conn: sqlite3.Connection, edition_id: str, n: int = 6) -> list[dict]:
    """Speeds of this event's editions on the same surface, oldest first, ending with this one."""
    me = conn.execute("SELECT * FROM edition_speed WHERE edition_id = ?", (edition_id,)).fetchone()
    if not me:
        return []
    rows = conn.execute(
        """SELECT s.edition_id, s.year, s.speed, s.n_lines FROM edition_speed s
           WHERE s.event_key = ? AND s.surface IS ? AND COALESCE(s.indoor, 'O') = COALESCE(?, 'O') AND s.year <= ?
           ORDER BY s.year""", (me["event_key"], me["surface"], me["indoor"], me["year"])).fetchall()
    return [dict(r) for r in rows][-n:]
