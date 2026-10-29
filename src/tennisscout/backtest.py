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
from dataclasses import asdict, dataclass, replace

from . import db, metrics
from .elo import OVERALL, EloConfig, EloModel, confidence
from .serve_return import ServeReturnModel, SRConfig
from .serve_return import confidence as sr_confidence

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
    tourney: str | None = None
    w_svpt: int | None = None  # serve points / serve points won; None when the stat line fails validation
    w_won: int | None = None
    l_svpt: int | None = None
    l_won: int | None = None


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
               COALESCE(m.level, e.level) AS level, m.outcome, m.round, e.name,
               {_valid_stats("w")}, {_valid_stats("l")}
        FROM match m JOIN tournament_edition e USING (edition_id)
        WHERE m.identity_status = 'ok'
          AND m.outcome NOT IN ({','.join('?' * len(EXCLUDED_OUTCOMES))})
          AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL
        ORDER BY m.seq
    """
    return [MatchRow(*r) for r in conn.execute(q, EXCLUDED_OUTCOMES)]


def _valid_stats(side: str) -> str:
    """SQL for (serve points, serve points won), NULL when the line is internally inconsistent."""
    ok = (f"m.{side}_svpt > 0 AND m.{side}_1stin <= m.{side}_svpt AND m.{side}_1stwon <= m.{side}_1stin "
          f"AND m.{side}_2ndwon <= m.{side}_svpt - m.{side}_1stin")
    return f"CASE WHEN {ok} THEN m.{side}_svpt END, CASE WHEN {ok} THEN m.{side}_1stwon + m.{side}_2ndwon END"


def player1_is_winner(match_id: str) -> bool:
    """Orientation that does not depend on the result, so calibration is meaningful."""
    return zlib.crc32(match_id.encode()) % 2 == 0


def replay(matches: Iterable[MatchRow], cfg: EloConfig, score_from_year: int, model: EloModel | None = None):
    """Walk history once; return predictions for years >= score_from_year and the final model."""
    model = model or EloModel(cfg)
    out: list[PredRecord] = []
    for m in matches:
        if m.year >= score_from_year:
            a, b, ra, rb, y = _orient(m)
            pred = model.predict(a, b, m.surface, m.indoor, m.best_of, ra, rb)
            out.append(
                PredRecord(m.match_id, m.seq, m.year, a, b, pred.p_a, y, confidence(pred), m.level,
                           pred.bucket, m.best_of, m.outcome, ra, rb)
            )
        model.update(m.winner_id, m.loser_id, m.surface, m.indoor, m.rank_w, m.rank_l, m.date)
    return out, model


def _orient(m: MatchRow):
    if player1_is_winner(m.match_id):
        return m.winner_id, m.loser_id, m.rank_w, m.rank_l, 1
    return m.loser_id, m.winner_id, m.rank_l, m.rank_w, 0


def replay_sr(matches: Iterable[MatchRow], cfg: SRConfig, score_from_year: int):
    """Same walk as ``replay`` for the serve/return + Markov model."""
    model = ServeReturnModel(cfg)
    out: list[PredRecord] = []
    for m in matches:
        if m.year >= score_from_year:
            a, b, ra, rb, y = _orient(m)
            p, _, _ = model.predict(a, b, m.surface, m.indoor, m.best_of, m.level, m.tourney, m.year)
            out.append(PredRecord(m.match_id, m.seq, m.year, a, b, p, y, sr_confidence(model, a, b), m.level,
                                  model.bucket(m.surface, m.indoor), m.best_of, m.outcome, ra, rb))
        model.update(m.winner_id, m.loser_id, m.surface, m.indoor, m.w_svpt, m.w_won, m.l_svpt, m.l_won)
    return out, model


def window(preds: list[PredRecord], years: tuple[int, int]) -> list[PredRecord]:
    return [p for p in preds if years[0] <= p.year <= years[1]]


def score(preds: list[PredRecord]) -> dict:
    return metrics.summarize([p.p1 for p in preds], [p.y for p in preds])


# -- ranking baseline ----------------------------------------------------------

def _rank_feature(p: PredRecord) -> float:
    return math.log(p.rank2 or UNRANKED) - math.log(p.rank1 or UNRANKED)


def fit_logistic(xs: list[list[float]], ys: list[int]) -> list[float]:
    """Logistic regression without intercept (player 1 is a coin flip, so none is needed), by Newton's method."""
    k = len(xs[0])
    w = [0.0] * k
    for _ in range(50):
        g = [0.0] * k
        h = [[0.0] * k for _ in range(k)]
        for x, y in zip(xs, ys):
            q = 1 / (1 + math.exp(-sum(wi * xi for wi, xi in zip(w, x))))
            for i in range(k):
                g[i] += (q - y) * x[i]
                for j in range(k):
                    h[i][j] += q * (1 - q) * x[i] * x[j]
        step = _solve(h, g)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(si) for si in step) < 1e-9:
            break
    return w


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination for the tiny Newton systems above."""
    n = len(b)
    m = [row[:] + [bi] for row, bi in zip(a, b)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(m[r][i]))
        m[i], m[piv] = m[piv], m[i]
        for r in range(i + 1, n):
            f = m[r][i] / m[i][i]
            m[r] = [x - f * y for x, y in zip(m[r], m[i])]
    out = [0.0] * n
    for i in reversed(range(n)):
        out[i] = (m[i][n] - sum(m[i][j] * out[j] for j in range(i + 1, n))) / m[i][i]
    return out


def fit_rank_logistic(preds: list[PredRecord]) -> float:
    """One-parameter logistic on the log-rank gap."""
    return fit_logistic([[_rank_feature(p)] for p in preds], [p.y for p in preds])[0]


def safe_logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def blend(elo: list[PredRecord], sr: list[PredRecord], coef: list[float]) -> list[PredRecord]:
    """Elo and serve/return probabilities combined on the logit scale."""
    out = []
    for e, r in zip(elo, sr):
        assert e.match_id == r.match_id
        z = coef[0] * safe_logit(e.p1) + coef[1] * safe_logit(r.p1)
        out.append(replace(e, p1=1 / (1 + math.exp(-z))))
    return out


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


def paired_gain(base: list[PredRecord], new: list[PredRecord]) -> dict:
    """Per-match Brier and log-loss improvement of ``new`` over ``base``, with standard errors."""
    out = {"n": len(base)}
    for name, loss in (("brier", lambda p, y: (p - y) ** 2), ("log_loss", lambda p, y: -math.log(p if y else 1 - p))):
        d = [loss(b.p1, b.y) - loss(n.p1, n.y) for b, n in zip(base, new)]
        mean = sum(d) / len(d)
        se = (sum((x - mean) ** 2 for x in d) / (len(d) - 1) / len(d)) ** 0.5
        out[name] = {"gain": mean, "se": se, "z": mean / se if se else None}
    return out


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

    # Serve/return + Markov is served through the blend with Elo, so its two variances are tuned on
    # the blend's validation log loss (standalone log loss rewards shrinkage that the blend undoes).
    val_e = window(preds_sel, VALIDATION)
    sr_grid = []
    for prior_var in (0.00125, 0.0025, 0.005, 0.01, 0.02, 0.04):
        for drift_var in (0.00005, 0.0001, 0.0002, 0.0005, 0.001):
            cfg = SRConfig(prior_var=prior_var, drift_var=drift_var)
            p, _ = replay_sr(matches, cfg, VALIDATION[0])
            val_r = window(p, VALIDATION)
            c = fit_logistic([[safe_logit(e.p1), safe_logit(r.p1)] for e, r in zip(val_e, val_r)], [e.y for e in val_e])
            sr_grid.append({"config": asdict(cfg), "coef": c, "validation": score(val_r), "test": score(window(p, TEST)),
                            "blend_validation": score(window(blend(preds_sel, p, c), VALIDATION))})
            log(f"  serve/return prior_var={prior_var} drift_var={drift_var}: val log loss {sr_grid[-1]['validation']['log_loss']:.5f}, "
                f"blend {sr_grid[-1]['blend_validation']['log_loss']:.5f}")
    best = min(sr_grid, key=lambda g: g["blend_validation"]["log_loss"])
    # Then per-player surface offsets, kept only if they improve the blend on validation.
    for surface_var in (0.0005, 0.001, 0.002, 0.004, 0.008, 0.016):
        cfg = SRConfig(**best["config"]).with_(surface_var=surface_var)
        p, _ = replay_sr(matches, cfg, VALIDATION[0])
        val_r = window(p, VALIDATION)
        c = fit_logistic([[safe_logit(e.p1), safe_logit(r.p1)] for e, r in zip(val_e, val_r)], [e.y for e in val_e])
        sr_grid.append({"config": asdict(cfg), "coef": c, "validation": score(val_r), "test": score(window(p, TEST)),
                        "blend_validation": score(window(blend(preds_sel, p, c), VALIDATION))})
        log(f"  serve/return surface_var={surface_var}: val log loss {sr_grid[-1]['validation']['log_loss']:.5f}, "
            f"blend {sr_grid[-1]['blend_validation']['log_loss']:.5f}")
    best = min(sr_grid, key=lambda g: g["blend_validation"]["log_loss"])
    sr_cfg, coef = SRConfig(**best["config"]), best["coef"]
    preds_sr, model_sr = replay_sr(matches, sr_cfg, VALIDATION[0])
    preds_blend = blend(preds_sel, preds_sr, coef)
    candidates = {"elo_selected": preds_sel, "serve_return": preds_sr, "elo_sr_blend": preds_blend}
    serving = min(candidates, key=lambda k: score(window(candidates[k], VALIDATION))["log_loss"])
    log(f"serving model (chosen on validation): {serving}")

    headline = {
        "coin_flip": metrics.summarize([0.5] * len(ys), ys),
        "ranking_logistic": {**metrics.summarize(test_rank_p, ys), "beta": beta},
        "elo_overall": score(window(preds_overall, TEST)),
        "elo_538_blend": score(window(preds_base, TEST)),
        "elo_selected": score(test_sel),
        "serve_return": score(window(preds_sr, TEST)),
        "elo_sr_blend": score(window(preds_blend, TEST)),
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
        "sr_config": asdict(sr_cfg),
        "sr_grid": sr_grid,
        "blend_coef": coef,
        "validation_by_model": {k: score(window(v, VALIDATION)) for k, v in candidates.items()},
        "serving_model": serving,
        "test_blend_vs_elo": paired_gain(test_sel, window(preds_blend, TEST)),
        "test_serving_by_confidence": breakdown(window(candidates[serving], TEST), lambda p: p.confidence),
        "test_serving_calibration": metrics.calibration([p.p1 for p in window(candidates[serving], TEST)], ys),
        "market_baseline": "not run yet: needs pre-match checkpoint prices joined at prediction time (next step)",
    }
    save(conn, selected, preds_sel, model_sel, result)
    save_sr(conn, sr_cfg, model_sr, {"serve_return": preds_sr, "elo_sr_blend": preds_blend}, coef, serving)
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
        db.set_meta(conn, "elo_model", cfg.name)
        db.set_meta(conn, f"model_config:{cfg.name}", json.dumps(asdict(cfg)))
        db.set_meta(conn, f"model_prior:{cfg.name}", json.dumps({"mean": model.prior.mean, "n": model.prior.n}))
        db.set_meta(conn, "backtest_result", json.dumps(result))


def save_sr(conn, cfg: SRConfig, model: ServeReturnModel, preds: dict[str, list[PredRecord]], coef, serving: str) -> None:
    with conn:
        for name, ps in preds.items():
            conn.execute("DELETE FROM prediction WHERE model = ?", (name,))
            conn.executemany("INSERT INTO prediction VALUES (?,?,?,?,?,?,?,?)",
                             [(name, p.match_id, p.player1_id, p.player2_id, p.p1, p.y, p.seq, p.confidence) for p in ps])
        conn.execute("DELETE FROM sr_rating WHERE model = ?", (cfg.name,))
        conn.executemany(
            "INSERT INTO sr_rating VALUES (?,?,?,?,?,?)",
            [(cfg.name, pid, kind, mean, model.var[(pid, kind)], model.n.get(pid, 0)) for (pid, kind), mean in model.mean.items()],
        )
        db.set_meta(conn, "sr_model", cfg.name)
        db.set_meta(conn, f"model_config:{cfg.name}", json.dumps(asdict(cfg)))
        db.set_meta(conn, f"sr_mu:{cfg.name}", json.dumps(model.mu))
        db.set_meta(conn, "blend_coef", json.dumps(coef))
        db.set_meta(conn, "serving_model", serving)


__all__ = ["run", "replay", "load_matches", "MatchRow", "PredRecord", "OVERALL"]
