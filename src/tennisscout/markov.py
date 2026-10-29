"""Point -> game -> set -> match probabilities for i.i.d. serve points (spec, "Prediction engine" step 2).

``pa`` is the chance A wins a point on A's serve, ``pb`` the chance B wins a
point on B's serve. Who serves first does not change the chance of winning a
set or match under these assumptions (Newton & Keller 2005), so the server is
not tracked across sets; tests check that symmetry numerically.
"""

from __future__ import annotations

from functools import lru_cache

STANDARD = (6, 7)  # tiebreak at 6-6, first to 7


def hold(p: float) -> float:
    """Chance the server wins a standard (advantage) game."""
    q = 1 - p
    return p**4 * (1 + 4 * q + 10 * q * q) + 20 * p**3 * q**3 * p * p / (1 - 2 * p * q)


@lru_cache(maxsize=None)
def _tiebreak(pa: float, pb: float, n: int) -> float:
    """A serves the first point; then ABBA... First to n, win by 2."""
    a, b = pa * (1 - pb), (1 - pa) * pb  # one point on each serve, A takes both / B takes both
    from_tie = a / (a + b)
    prob = {(0, 0): 1.0}
    win = 0.0
    for total in range(2 * n - 2):
        for i in range(max(0, total - n + 1), min(total, n - 1) + 1):
            j = total - i
            m = prob.pop((i, j), 0.0)
            if not m:
                continue
            a_serves = total % 4 in (0, 3)
            p_point = pa if a_serves else 1 - pb
            for di, pm in ((1, p_point), (0, 1 - p_point)):
                ni, nj = i + di, j + 1 - di
                if ni == n:
                    win += m * pm
                elif nj < n:
                    prob[(ni, nj)] = prob.get((ni, nj), 0.0) + m * pm
    return win + prob.get((n - 1, n - 1), 0.0) * from_tie


def tiebreak(pa: float, pb: float, n: int = 7) -> float:
    return _tiebreak(pa, pb, n)


def _to_six_all(ha: float, hb: float) -> tuple[float, float]:
    """A serving first: (chance A wins the set before 6-6, chance the set reaches 6-6)."""
    prob = {(0, 0): 1.0}
    win = 0.0
    for total in range(10):
        for i in range(max(0, total - 6), min(total, 6) + 1):
            j = total - i
            m = prob.pop((i, j), 0.0)
            if not m:
                continue
            p_game = ha if total % 2 == 0 else 1 - hb
            for di, pg in ((1, p_game), (0, 1 - p_game)):
                ni, nj = i + di, j + 1 - di
                if ni >= 6 and ni - nj >= 2:
                    win += m * pg
                elif not (nj >= 6 and nj - ni >= 2):
                    prob[(ni, nj)] = prob.get((ni, nj), 0.0) + m * pg
    # From 5-5, two more games: 7-5 / 5-7, or 6-6.
    m55 = prob.pop((5, 5), 0.0)
    return win + m55 * ha * (1 - hb), m55 * (ha * hb + (1 - ha) * (1 - hb))


def reach_tiebreak(pa: float, pb: float) -> float:
    """Chance a standard set reaches 6-6."""
    return _to_six_all(hold(pa), hold(pb))[1]


def short_set_win(pa: float, pb: float) -> float:
    """Next Gen Finals: no-ad games, first to 4 games, tiebreak at 3-3."""
    ha, hb = (p**4 * (1 + 4 * (1 - p) + 10 * (1 - p) ** 2) + 20 * p**3 * (1 - p) ** 3 * p for p in (pa, pb))
    prob, win = {(0, 0): 1.0}, 0.0
    for total in range(6):
        for i in range(max(0, total - 3), min(total, 3) + 1):
            j = total - i
            m = prob.pop((i, j), 0.0)
            p_game = ha if total % 2 == 0 else 1 - hb
            for di, pg in ((1, p_game), (0, 1 - p_game)):
                ni, nj = i + di, j + 1 - di
                if ni == 4:
                    win += m * pg
                elif nj < 4:
                    prob[(ni, nj)] = prob.get((ni, nj), 0.0) + m * pg
    return win + prob.get((3, 3), 0.0) * tiebreak(pa, pb, 7)


def set_win(pa: float, pb: float, rule: tuple | None = STANDARD) -> float:
    """Chance A wins a set with A serving first.

    ``rule`` = (tiebreak at T-T, tiebreak first to n); ``None`` means an
    advantage set (no tiebreak); ``("match_tb", n)`` means the whole set is one
    tiebreak to n (Laver Cup deciders); ``("short",)`` is a Next Gen short set.
    """
    if rule and rule[0] == "match_tb":
        return tiebreak(pa, pb, rule[1])
    if rule and rule[0] == "short":
        return short_set_win(pa, pb)
    ha, hb = hold(pa), hold(pb)
    win, m66 = _to_six_all(ha, hb)
    # At 6-6 (A to serve): pairs of games; A takes both with a, B with b, split with c.
    a, b = ha * (1 - hb), (1 - ha) * hb
    c = 1 - a - b
    if rule is None:
        return win + m66 * a / (a + b)
    tb_at, n = rule
    reach_tb = c ** (tb_at - 6)
    return win + m66 * (a * (1 - reach_tb) / (1 - c) + reach_tb * tiebreak(pa, pb, n))


def match_win(pa: float, pb: float, best_of: int = 3, final_rule: tuple | None = STANDARD) -> float:
    s = set_win(pa, pb, final_rule if final_rule == ("short",) else STANDARD)
    f = set_win(pa, pb, final_rule)
    if best_of == 5:
        return s**3 + 3 * s**3 * (1 - s) + 6 * s * s * (1 - s) ** 2 * f
    return s * s + 2 * s * (1 - s) * f


def set_scores(pa: float, pb: float, best_of: int = 3, final_rule: tuple | None = STANDARD) -> dict[str, float]:
    """Distribution of final set scores from A's side, e.g. {"2-0": .., "2-1": .., "1-2": .., "0-2": ..}."""
    s = set_win(pa, pb, final_rule if final_rule == ("short",) else STANDARD)
    f = set_win(pa, pb, final_rule)
    q = 1 - s
    if best_of == 5:
        return {"3-0": s**3, "3-1": 3 * s**3 * q, "3-2": 6 * s * s * q * q * f,
                "2-3": 6 * s * s * q * q * (1 - f), "1-3": 3 * s * q**3, "0-3": q**3}
    return {"2-0": s * s, "2-1": 2 * s * q * f, "1-2": 2 * s * q * (1 - f), "0-2": q * q}


def final_set_rule(level: str | None, tourney: str | None, year: int, best_of: int | None, next_gen: bool = True):
    """Deciding-set format by event and year."""
    t = (tourney or "").lower()
    if level == "G":
        if year >= 2022:
            return (6, 10)
        if "wimbledon" in t:
            return None if year <= 2018 else (12, 7)
        if "australian" in t:
            return None if year <= 2018 else (6, 10)
        if "roland" in t or "french" in t:
            return None
        return STANDARD  # US Open
    if level == "D" and year <= 2015:
        return None
    if level == "O" and year <= 2012:
        return None
    if "laver cup" in t:
        return ("match_tb", 10)
    if next_gen and "next" in t and "gen" in t:
        return ("short",)  # every set is short, not just the decider
    return STANDARD
