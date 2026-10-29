"""Chronological backtest of pre-match Elo predictions.

Rules (spec, "Measuring whether it works" and "Availability time"):

* Matches are replayed in ``seq`` order (edition start date, then round). A
  prediction for match *m* is made from ratings that have absorbed only
  matches with a smaller ``seq``, then *m* updates the ratings.
* The source has no publication timestamps, so availability is the event
  ordering itself; rankings used are the ones printed on the row, which TML
  records as of the tournament start, so they are pre-match facts.
* Rows whose player identity failed a check are excluded from both rating
  updates and scoring. Walkovers, defaults and unknown results are excluded too.
* Model choices are made on the validation window only. The test window is
  scored once per candidate and never used to choose.
"""

from __future__ import annotations

import json
import math
import sqlite3
import zlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from . import db, metrics
from .elo import OVERALL, EloConfig, EloModel, confidence

VALIDATION = (2014, 2018)
TEST = (2019, 2026)
EXCLUDED_OUTCOMES = ("walkover", "default", "unknown")
UNRANKED = 2000


@dataclass(frozen=True)
class MatchRow:
    match_id: str
    seq: int
    year: int
    date: str
    winner_id: str
    loser_id: str
    surface: str | None
    indoor: str | None
    best_of: int | None
    rank_w: int | None
    rank_l: int | None
    level: str | None
    outcome: str
    round: str | None


@dataclass
class PredRecord:
    match_id: str
    seq: int
    year: int
    player1_id: str
    player2_id: str
    p1: float
    y: int  # 1 if player1 won
    confidence: str
    level: str | None
    bucket: str
    best_of: int | None
    outcome: str
    rank1: int | None
    rank2: int | None


def load_matches(conn: sqlite3.Connection) -> list[MatchRow]:
    q = f"""
        SELECT m.match_id, m.seq, e.year, COALESCE(m.event_date_lo, e.start_date) AS d,
               m.winner_id, m.loser_id, COALESCE(m.surface, e.surface) AS surface,
               COALESCE(m.indoor, e.indoor) AS indoor, m.best_of, m.winner_rank, m.loser_rank,
               COALESCE(m.level, e.level) AS level, m.outcome, m.round
        FROM match m JOIN tournament_edition e USING (edition_id)
        WHERE m.identity_status = 'ok'
          AND m.outcome NOT IN ({','.join('?' * len(EXCLUDED_OUTCOMES))})
          AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL
        ORDER BY m.seq
    """
    return [MatchRow(*r) for r in conn.execute(q, EXCLUDED_OUTCOMES)]


def player1_is_winner(match_id: str) -> bool:
    """Orientation that does not depend on the result, so calibration is meaningful."""
    return zlib.crc32(match_id.encode()) % 2 == 0


def replay(matches: Iterable[MatchRow], cfg: EloConfig, score_from_year: int, model: EloModel | None = None):
    """Walk history once; return predictions for years >= score_from_year and the final model."""
    model = model or EloModel(cfg)
    out: list[PredRecord] = []
    for m in matches:
        if m.year >= score_from_year:
            if player1_is_winner(m.match_id):
                a, b, ra, rb, y = m.winner_id, m.loser_id, m.rank_w, m.rank_l, 1
            else:
                a, b, ra, rb, y = m.loser_id, m.winner_id, m.rank_l, m.rank_w, 0
            pred = model.predict(a, b, m.surface, m.indoor, m.best_of, ra, rb)
            out.append(
                PredRecord(m.match_id, m.seq, m.year, a, b, pred.p_a, y, confidence(pred), m.level,
                           pred.bucket, m.best_of, m.outcome, ra, rb)
            )
        model.update(m.winner_id, m.loser_id, m.surface, m.indoor, m.rank_w, m.rank_l, m.date)
    return out, model


def window(preds: list[PredRecord], years: tuple[int, int]) -> list[PredRecord]:
    return [p for p in preds if years[0] <= p.year <= years[1]]


def score(preds: list[PredRecord]) -> dict:
    return metrics.summarize([p.p1 for p in preds], [p.y for p in preds])


# -- ranking baseline ----------------------------------------------------------

def _rank_feature(p: PredRecord) -> float:
    return math.log(p.rank2 or UNRANKED) - math.log(p.rank1 or UNRANKED)


def fit_rank_logistic(preds: list[PredRecord]) -> float:
    """One-parameter logistic on the log-rank gap, fitted by Newton's method."""
    beta = 0.5
    for _ in range(50):
        g = h = 0.0
        for p in preds:
            x = _rank_feature(p)
            q = 1 / (1 + math.exp(-beta * x))
            g += (q - p.y) * x
            h += q * (1 - q) * x * x
        step = g / h if h else 0.0
        beta -= step
        if abs(step) < 1e-9:
            break
    return beta


def rank_predictions(preds: list[PredRecord], beta: float) -> list[float]:
    return [1 / (1 + math.exp(-beta * _rank_feature(p))) for p in preds]


# -- model selection -------------------------------------------------------------

BASELINE = EloConfig(name="elo_538_blend")  # w = 0.5, K = 250/(n+5)^0.4, indoor hard split

CANDIDATE_CHANGES: list[tuple[str, dict]] = [
    ("surface_weight=0.0 (overall only)", {"surface_weight": 0.0}),
    ("surface_weight=0.25", {"surface_weight": 0.25}),
    ("surface_weight=0.75", {"surface_weight": 0.75}),
    ("surface_weight=1.0 (surface only)", {"surface_weight": 1.0}),
    ("merge indoor hard into hard", {"split_indoor_hard": False}),
    ("ranking-range prior for debutants", {"new_player_prior": "rank"}),
    ("surface rating starts at overall", {"surface_init": "overall"}),
    ("best-of-5 adjustment", {"bo5_adjust": True}),
    ("k_shape=0.3", {"k_shape": 0.3}),
    ("k_shape=0.5", {"k_shape": 0.5}),
    ("k_num=200", {"k_num": 200.0}),
    ("k_num=300", {"k_num": 300.0}),
] + [(f"prediction scale x{s:.1f}", {"pred_scale": s}) for s in (1.1, 1.2, 1.3, 1.4, 1.5, 1.6)]


def forward_selection(matches: list[MatchRow], log=print):
    """Greedy: adopt the single change that most improves validation log loss; repeat."""
    current = BASELINE
    preds, _ = replay(matches, current, VALIDATION[0])
    best_val = score(window(preds, VALIDATION))["log_loss"]
    steps = [{"step": "baseline (FiveThirtyEight-style blend)", "config": asdict(current),
              "validation": score(window(preds, VALIDATION)), "test": score(window(preds, TEST))}]
    tried: list[dict] = []
    log(f"baseline val log loss {best_val:.5f}")
    remaining = list(CANDIDATE_CHANGES)
    while remaining:
        results = []
        for label, change in remaining:
            if all(getattr(current, k) == v for k, v in change.items()):
                continue
            cfg = current.with_(**change)
            p, _ = replay(matches, cfg, VALIDATION[0])
            val = score(window(p, VALIDATION))
            results.append((val["log_loss"], label, change, cfg, p, val))
            tried.append({"from": steps[-1]["step"], "change": label, "validation": val, "test": score(window(p, TEST))})
            log(f"  try {label:40s} val log loss {val['log_loss']:.5f}")
        if not results:
            break
        results.sort(key=lambda r: r[0])
        ll, label, change, cfg, p, val = results[0]
        if ll >= best_val - 1e-5:
            log("  no candidate improves validation; stop")
            break
        current, best_val = cfg, ll
        # Once one value of a knob is adopted, drop the other values of that knob.
        keys = set(change)
        remaining = [(lbl, ch) for lbl, ch in remaining if not (set(ch) & keys)]
        steps.append({"step": f"+ {label}", "config": asdict(current), "validation": val, "test": score(window(p, TEST))})
        log(f"adopt {label}: val log loss {ll:.5f}")
    return current.with_(name="elo_selected"), steps, tried


def breakdown(preds: list[PredRecord], key) -> dict:
    groups: dict[str, list[PredRecord]] = defaultdict(list)
    for p in preds:
        groups[str(key(p))].append(p)
    return {k: score(v) for k, v in sorted(groups.items())}


def run(conn: sqlite3.Connection, log=print) -> dict:
    matches = load_matches(conn)
    log(f"{len(matches)} scorable matches")
    selected, steps, tried = forward_selection(matches, log)

    preds_sel, model_sel = replay(matches, selected, VALIDATION[0])
    preds_base, _ = replay(matches, BASELINE, VALIDATION[0])
    preds_overall, _ = replay(matches, BASELINE.with_(name="elo_overall", surface_weight=0.0), VALIDATION[0])
    test_sel = window(preds_sel, TEST)

    beta = fit_rank_logistic(window(preds_sel, VALIDATION))
    test_rank_p = rank_predictions(test_sel, beta)
    ys = [p.y for p in test_sel]

    headline = {
        "coin_flip": metrics.summarize([0.5] * len(ys), ys),
        "ranking_logistic": {**metrics.summarize(test_rank_p, ys), "beta": beta},
        "elo_overall": score(window(preds_overall, TEST)),
        "elo_538_blend": score(window(preds_base, TEST)),
        "elo_selected": score(test_sel),
    }
    completed_only = [p for p in test_sel if p.outcome == "completed"]
    result = {
        "validation_years": VALIDATION,
        "test_years": TEST,
        "n_scorable_matches": len(matches),
        "selected_config": asdict(selected),
        "selection_steps": steps,
        "candidates_tried": tried,
        "test_headline": headline,
        "test_selected_completed_only": score(completed_only),
        "test_by_level": breakdown(test_sel, lambda p: p.level),
        "test_by_bucket": breakdown(test_sel, lambda p: p.bucket),
        "test_by_confidence": breakdown(test_sel, lambda p: p.confidence),
        "test_by_best_of": breakdown(test_sel, lambda p: p.best_of),
        "test_by_year": breakdown(test_sel, lambda p: p.year),
        "test_calibration": metrics.calibration([p.p1 for p in test_sel], ys),
        "market_baseline": "not run: Kalshi and Polymarket hosts are blocked by this environment's network policy",
    }
    save(conn, selected, preds_sel, model_sel, result)
    return result


def save(conn: sqlite3.Connection, cfg: EloConfig, preds: list[PredRecord], model: EloModel, result: dict) -> None:
    with conn:
        conn.execute("DELETE FROM prediction WHERE model = ?", (cfg.name,))
        conn.executemany(
            "INSERT INTO prediction VALUES (?,?,?,?,?,?,?,?)",
            [(cfg.name, p.match_id, p.player1_id, p.player2_id, p.p1, p.y, p.seq, p.confidence) for p in preds],
        )
        conn.execute("DELETE FROM elo_rating WHERE model = ?", (cfg.name,))
        conn.executemany(
            "INSERT INTO elo_rating VALUES (?,?,?,?,?,?)",
            [
                (cfg.name, pid, bucket, r, model.n[(pid, bucket)], model.last_date.get(pid))
                for (pid, bucket), r in model.rating.items()
            ],
        )
        db.set_meta(conn, "serving_model", cfg.name)
        db.set_meta(conn, f"model_config:{cfg.name}", json.dumps(asdict(cfg)))
        db.set_meta(conn, f"model_prior:{cfg.name}", json.dumps({"mean": model.prior.mean, "n": model.prior.n}))
        db.set_meta(conn, "backtest_result", json.dumps(result))


__all__ = ["run", "replay", "load_matches", "MatchRow", "PredRecord", "OVERALL"]
