"""Pre-match context features (spec, "Features to compute per player"), built in replay order.

Everything for match m comes from state that only earlier matches have
touched, then m updates the state. Values are differences "player 1 minus
player 2" so a logistic layer without intercept stays symmetric.

* age: years (birthdates where known, 0 otherwise)
* lefty: +1 if only player 1 is left-handed, -1 if only player 2, else 0
* h2h: (wins - losses) / (meetings + 4), lightly shrunk (spec: "use a light weight")
* rest: log(1 + days since previous match), capped at a year
* load: matches in the previous 14 days (fatigue)
* home: +1 if player 1's country hosts the event, -1 if player 2's, else 0
* exp: log(1 + prior matches), all levels
* switch: +1 if only player 1's previous match was on another surface, -1 if only player 2's
* travel: hours of time-zone shift (longitude / 15) since the previous venue, when both venues are known

Interaction terms (symmetric match context; the stack multiplies each by the blend logit, so they
rescale confidence rather than favour a side):

* conf: log(1 + prior matches of the less experienced player); ratings of newcomers are noisier
* bo5, indoor, clay, grass, slam, masters, late (quarter-final or later): 0/1 flags
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict, deque
from datetime import date

NAMES = ("age", "lefty", "h2h", "rest", "load", "home", "exp", "switch", "travel")
INTERACTIONS = ("conf", "bo5", "indoor", "clay", "grass", "slam", "masters", "late")
ALL = NAMES + INTERACTIONS


def stack_row(logit: float, vec: list[float], chosen) -> list[float]:
    """Design row of the stacking layer: blend logit, chosen differences, and logit x context for interactions."""
    return [logit] + [vec[ALL.index(f)] * (logit if f in INTERACTIONS else 1.0) for f in chosen]


def player_info(conn: sqlite3.Connection) -> dict[str, tuple[date | None, str | None, str | None]]:
    out = {}
    for pid, bd, hand, ioc in conn.execute("SELECT player_id, birthdate, hand, ioc FROM player"):
        b = None
        if bd and len(bd) == 8 and bd.isdigit():
            try:
                b = date(int(bd[:4]), int(bd[4:6]), int(bd[6:]))
            except ValueError:
                b = None
        out[pid] = (b, hand, ioc)
    return out


def edition_country(conn: sqlite3.Connection) -> dict[str, tuple[str | None, float | None]]:
    """(host country, venue longitude) per match, from the venue metadata attached to its edition."""
    from . import venues

    vindex = venues.VenueIndex(venues.load())
    out = {}
    for mid, name, year in conn.execute("SELECT m.match_id, e.name, e.year FROM match m JOIN tournament_edition e USING (edition_id)"):
        v = vindex.lookup(name, year)
        out[mid] = (v.country, v.longitude) if v else (None, None)
    return out


class FeatureState:
    def __init__(self, info, hosts):
        self.info, self.hosts = info, hosts
        self.last: dict[str, date] = {}
        self.recent: dict[str, deque] = defaultdict(deque)
        self.h2h: dict[tuple[str, str], int] = defaultdict(int)
        self.n: dict[str, int] = defaultdict(int)
        self.last_surface: dict[str, str] = {}
        self.last_lon: dict[str, float] = {}

    def features(self, m, a: str, b: str) -> list[float]:
        d = date.fromisoformat(m.date)
        ia, ib = self.info.get(a, (None, None, None)), self.info.get(b, (None, None, None))
        age = ((d - ia[0]).days - (d - ib[0]).days) / 365.25 if ia[0] and ib[0] else 0.0
        lefty = (ia[1] == "L") - (ib[1] == "L")
        wins, losses = self.h2h[(a, b)], self.h2h[(b, a)]
        h2h = (wins - losses) / (wins + losses + 4)

        def rest(p):
            # Week-dated (TML) and day-dated (API-Tennis) rows can put "last" a few days after d: clamp at 0.
            return math.log1p(min(max((d - self.last[p]).days, 0), 365)) if p in self.last else math.log1p(365)

        def load(p):
            q = self.recent[p]
            return sum(1 for x in q if 0 <= (d - x).days <= 14)

        host, lon = self.hosts.get(m.match_id, (None, None))
        home = (bool(host) and ia[2] == host) - (bool(host) and ib[2] == host)
        exp = math.log1p(self.n[a]) - math.log1p(self.n[b])
        surf = getattr(m, "surface", None)

        def switched(p):
            return bool(surf) and p in self.last_surface and self.last_surface[p] != surf

        def travel(p):
            if lon is None or p not in self.last_lon:
                return 0.0
            dl = abs(lon - self.last_lon[p]) % 360
            return min(dl, 360 - dl) / 15

        return [age, float(lefty), h2h, rest(a) - rest(b), float(load(a) - load(b)), float(home),
                exp, float(switched(a)) - float(switched(b)), travel(a) - travel(b)]

    def context(self, m, a: str, b: str) -> list[float]:
        s, lvl = (getattr(m, "surface", None) or "").capitalize(), getattr(m, "level", None)
        return [math.log1p(min(self.n[a], self.n[b])), float(getattr(m, "best_of", None) == 5),
                float(getattr(m, "indoor", None) == "I"), float(s == "Clay"), float(s == "Grass"), float(lvl == "G"),
                float(lvl == "M"), float(getattr(m, "round", None) in ("QF", "SF", "F"))]

    def update(self, m) -> None:
        d = date.fromisoformat(m.date)
        _, lon = self.hosts.get(getattr(m, "match_id", None), (None, None))
        for p in (m.winner_id, m.loser_id):
            self.n[p] += 1
            if getattr(m, "surface", None):
                self.last_surface[p] = m.surface
            if lon is not None:
                self.last_lon[p] = lon
            self.last[p] = d
            q = self.recent[p]
            q.append(d)
            while q and (d - q[0]).days > 14:
                q.popleft()
        self.h2h[(m.winner_id, m.loser_id)] += 1


def replay_features(conn: sqlite3.Connection, matches, score_from_year: int) -> dict[str, list[float]]:
    """Feature vector in ``ALL`` order (differences are player 1 minus player 2, as in the backtest) per scored match."""
    from .backtest import _orient

    state = FeatureState(player_info(conn), edition_country(conn))
    out = {}
    for m in matches:
        if m.scored and m.year >= score_from_year:
            a, b, *_ = _orient(m)
            out[m.match_id] = state.features(m, a, b) + state.context(m, a, b)
        state.update(m)
    return out
