"""Probability scoring: Brier score, log loss, calibration (spec, "Measuring whether it works")."""

from __future__ import annotations

import math
from collections.abc import Sequence

EPS = 1e-12


def brier(ps: Sequence[float], ys: Sequence[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def log_loss(ps: Sequence[float], ys: Sequence[int]) -> float:
    total = 0.0
    for p, y in zip(ps, ys):
        p = min(max(p, EPS), 1 - EPS)
        total -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return total / len(ps)


def accuracy(ps: Sequence[float], ys: Sequence[int]) -> float:
    """Share of matches where the favourite won; an exact 50/50 call scores half."""
    hits = 0.0
    for p, y in zip(ps, ys):
        hits += 0.5 if p == 0.5 else float((p > 0.5) == bool(y))
    return hits / len(ps)


def calibration(ps: Sequence[float], ys: Sequence[int], bins: int = 10) -> list[dict]:
    rows = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        idx = [j for j, p in enumerate(ps) if (lo <= p < hi) or (i == bins - 1 and p == 1.0)]
        if not idx:
            rows.append({"lo": lo, "hi": hi, "n": 0, "mean_p": None, "observed": None})
            continue
        rows.append(
            {
                "lo": lo,
                "hi": hi,
                "n": len(idx),
                "mean_p": sum(ps[j] for j in idx) / len(idx),
                "observed": sum(ys[j] for j in idx) / len(idx),
            }
        )
    return rows


def ece(ps: Sequence[float], ys: Sequence[int], bins: int = 10) -> float:
    """Expected calibration error: bin-size-weighted |mean prediction - observed rate|."""
    n = len(ps)
    return sum(r["n"] / n * abs(r["mean_p"] - r["observed"]) for r in calibration(ps, ys, bins) if r["n"])


def summarize(ps: Sequence[float], ys: Sequence[int]) -> dict:
    if not ps:
        return {"n": 0}
    return {
        "n": len(ps),
        "accuracy": accuracy(ps, ys),
        "brier": brier(ps, ys),
        "log_loss": log_loss(ps, ys),
        "ece": ece(ps, ys),
    }
