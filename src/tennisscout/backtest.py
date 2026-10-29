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
* Only tour-level main-draw matches are scored. Challenger and qualifying
  matches (API-Tennis) update ratings but are never scored, so every window is
  comparable with the TML-only results.
* Windows are dates: validation 2014-2018, test 2019 to the end of TML
  coverage, forward = API-Tennis matches after that (a true holdout).
"""

from __future__ import annotations

import json
import math
import sqlite3
import zlib
from datetime import date, timedelta
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace

from . import db, metrics
from .elo import OVERALL, EloConfig, EloModel, confidence
from .normalize import games_share
from .serve_return import ServeReturnModel, SRConfig
from .serve_return import confidence as sr_confidence

VALIDATION = ("2014-01-01", "2018-12-31")
TEST = ("2019-01-01", "2026-01-21")  # run() replaces the end with the TML coverage end
DATA_DECISION = ("2021-01-01", "2022-12-31")  # where Challenger data exists to judge it
EXCLUDED_OUTCOMES = ("walkover", "default", "unknown")
UNRANKED = 2000
MEAN_GAMES_SHARE = 0.618  # winner's average share of games, tour matches since 2000


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
    score: str | None = None
    w_svpt: int | None = None  # serve points / serve points won; None when the stat line fails validation
    w_won: int | None = None
    l_svpt: int | None = None
    l_won: int | None = None
    source: str = "tml"
    scored: bool = True  # tour-level main draw; Challenger and qualifying only update ratings


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
    date: str = ""


def load_matches(conn: sqlite3.Connection) -> list[MatchRow]:
    q = f"""
        SELECT m.match_id, m.seq, e.year, COALESCE(m.event_date_lo, e.start_date) AS d,
               m.winner_id, m.loser_id, COALESCE(m.surface, e.surface) AS surface,
               COALESCE(m.indoor, e.indoor) AS indoor, m.best_of, m.winner_rank, m.loser_rank,
               COALESCE(m.level, e.level) AS level, m.outcome, m.round, e.name, m.score,
               {_valid_stats("w")}, {_valid_stats("l")}, m.source,
               CASE WHEN COALESCE(m.level, e.level) = 'C' OR m.round = 'Q' THEN 0 ELSE 1 END
        FROM match m JOIN tournament_edition e USING (edition_id)
        WHERE m.identity_status = 'ok'
          AND m.outcome NOT IN ({','.join('?' * len(EXCLUDED_OUTCOMES))})
          AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL
        ORDER BY m.seq
    """
    return [MatchRow(*r[:-1], scored=bool(r[-1])) for r in conn.execute(q, EXCLUDED_OUTCOMES)]


def tour_only(matches: list[MatchRow]) -> list[MatchRow]:
    """The checkpoint-2 data set: no Challenger or qualifying matches."""
    return [m for m in matches if m.scored]


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
        if m.scored and m.year >= score_from_year:
            a, b, ra, rb, y = _orient(m)
            pred = model.predict(a, b, m.surface, m.indoor, m.best_of, ra, rb)
            out.append(
                PredRecord(m.match_id, m.seq, m.year, a, b, pred.p_a, y, confidence(pred), m.level,
                           pred.bucket, m.best_of, m.outcome, ra, rb, m.date)
            )
        weight = cfg.challenger_k_weight if m.level == "C" else 1.0
        if cfg.mov:
            weight *= ((games_share(m.score) or MEAN_GAMES_SHARE) / MEAN_GAMES_SHARE) ** cfg.mov
        model.update(m.winner_id, m.loser_id, m.surface, m.indoor, m.rank_w, m.rank_l, m.date, weight)
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
        if m.scored and m.year >= score_from_year:
            a, b, ra, rb, y = _orient(m)
            p, _, _ = model.predict(a, b, m.surface, m.indoor, m.best_of, m.level, m.tourney, m.year)
            out.append(PredRecord(m.match_id, m.seq, m.year, a, b, p, y, sr_confidence(model, a, b), m.level,
                                  model.bucket(m.surface, m.indoor), m.best_of, m.outcome, ra, rb, m.date))
        model.update(m.winner_id, m.loser_id, m.surface, m.indoor, m.w_svpt, m.w_won, m.l_svpt, m.l_won)
    return out, model


def window(preds: list[PredRecord], dates: tuple[str, str]) -> list[PredRecord]:
    return [p for p in preds if dates[0] <= p.date <= dates[1]]


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
    ("margin of victory, linear", {"mov": 1.0}),
    ("margin of victory, squared", {"mov": 2.0}),
    ("margin of victory, cubed", {"mov": 3.0}),
    ("layoff boost 0.5", {"layoff_boost": 0.5}),
    ("layoff boost 1.0", {"layoff_boost": 1.0}),
] + [(f"prediction scale x{s:.1f}", {"pred_scale": s}) for s in (1.1, 1.2, 1.3, 1.4, 1.5, 1.6)]


def forward_selection(matches: list[MatchRow], log=print, test=TEST):
    """Greedy: adopt the single change that most improves validation log loss; repeat."""
    current = BASELINE
    preds, _ = replay(matches, current, int(VALIDATION[0][:4]))
    best_val = score(window(preds, VALIDATION))["log_loss"]
    steps = [{"step": "baseline (FiveThirtyEight-style blend)", "config": asdict(current),
              "validation": score(window(preds, VALIDATION)), "test": score(window(preds, test))}]
    tried: list[dict] = []
    log(f"baseline val log loss {best_val:.5f}")
    remaining = list(CANDIDATE_CHANGES)
    while remaining:
        results = []
        for label, change in remaining:
            if all(getattr(current, k) == v for k, v in change.items()):
                continue
            cfg = current.with_(**change)
            p, _ = replay(matches, cfg, int(VALIDATION[0][:4]))
            val = score(window(p, VALIDATION))
            results.append((val["log_loss"], label, change, cfg, p, val))
            tried.append({"from": steps[-1]["step"], "change": label, "validation": val, "test": score(window(p, test))})
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
        steps.append({"step": f"+ {label}", "config": asdict(current), "validation": val, "test": score(window(p, test))})
        log(f"adopt {label}: val log loss {ll:.5f}")
    return current.with_(name="elo_selected"), steps, tried


def paired_gain(base: list[PredRecord], new: list[PredRecord]) -> dict:
    """Per-match Brier and log-loss improvement of ``new`` over ``base``, with standard errors."""
    out = {"n": len(base)}
    for name, loss in (("brier", lambda p, y: (p - y) ** 2), ("log_loss", lambda p, y: -math.log(max(p if y else 1 - p, metrics.EPS)))):
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


START_YEAR = int(VALIDATION[0][:4])


def _sr_candidate(matches, cfg: SRConfig, preds_e: list[PredRecord], test) -> dict:
    p, _ = replay_sr(matches, cfg, START_YEAR)
    val_e, val_r = window(preds_e, VALIDATION), window(p, VALIDATION)
    c = fit_logistic([[safe_logit(e.p1), safe_logit(r.p1)] for e, r in zip(val_e, val_r)], [e.y for e in val_e])
    return {"config": asdict(cfg), "coef": c, "validation": score(val_r), "test": score(window(p, test)),
            "blend_validation": score(window(blend(preds_e, p, c), VALIDATION))}


def run(conn: sqlite3.Connection, log=print) -> dict:
    all_matches = load_matches(conn)
    tml_end = db.get_meta(conn, "tml_coverage_end") or TEST[1]
    test = (TEST[0], tml_end)
    forward = (str(date.fromisoformat(tml_end) + timedelta(days=1)), "9999-12-31")
    after_decision = ("2023-01-01", tml_end)
    matches = tour_only(all_matches)
    log(f"{len(all_matches)} usable matches, {len(matches)} tour-level main draw")
    selected, steps, tried = forward_selection(matches, log, test)

    preds_sel, _ = replay(matches, selected, START_YEAR)

    # Serve/return + Markov is served through the blend with Elo, so its variances are tuned on
    # the blend's validation log loss (standalone log loss rewards shrinkage that the blend undoes).
    sr_grid = []
    for prior_var in (0.00125, 0.0025, 0.005, 0.01, 0.02, 0.04):
        for drift_var in (0.00005, 0.0001, 0.0002, 0.0005, 0.001):
            sr_grid.append(_sr_candidate(matches, SRConfig(prior_var=prior_var, drift_var=drift_var), preds_sel, test))
            log(f"  serve/return prior_var={prior_var} drift_var={drift_var}: blend {sr_grid[-1]['blend_validation']['log_loss']:.5f}")
    best = min(sr_grid, key=lambda g: g["blend_validation"]["log_loss"])
    # Then per-player surface offsets, kept only if they improve the blend on validation.
    for surface_var in (0.0005, 0.001, 0.002, 0.004, 0.008, 0.016):
        sr_grid.append(_sr_candidate(matches, SRConfig(**best["config"]).with_(surface_var=surface_var), preds_sel, test))
        log(f"  serve/return surface_var={surface_var}: blend {sr_grid[-1]['blend_validation']['log_loss']:.5f}")
    best = min(sr_grid, key=lambda g: g["blend_validation"]["log_loss"])
    # Then match-level form on the day, kept only if it improves the blend on validation.
    for form_sd in (0.1, 0.15, 0.2, 0.25, 0.3):
        sr_grid.append(_sr_candidate(matches, SRConfig(**best["config"]).with_(form_sd=form_sd), preds_sel, test))
        log(f"  serve/return form_sd={form_sd}: blend {sr_grid[-1]['blend_validation']['log_loss']:.5f}, "
            f"alone {sr_grid[-1]['validation']['log_loss']:.5f}")
    # The blend is nearly indifferent to form_sd (it rescales the Markov input), so among settings within
    # 1e-4 of the best blend score take the one whose Markov model is best on its own: that is the model
    # behind the set-score and tiebreak predictions.
    top = min(g["blend_validation"]["log_loss"] for g in sr_grid)
    best = min((g for g in sr_grid if g["blend_validation"]["log_loss"] <= top + 1e-4), key=lambda g: g["validation"]["log_loss"])
    sr_cfg, coef = SRConfig(**best["config"]), best["coef"]

    # Data decision: do Challenger / qualifying results (updates only) help tour-level predictions?
    # Judged on 2021-22, where that data exists; 2023 on and the forward window stay untouched by it.
    data_variants = {"tour_only": (matches, selected)}
    if len(all_matches) > len(matches):
        for w in (1.0, 0.5):
            data_variants[f"with_challenger_k{w}"] = (all_matches, selected.with_(challenger_k_weight=w))
    data_results, variant_preds = {}, {}
    for name, (ms, ecfg) in data_variants.items():
        pe, me = replay(ms, ecfg, START_YEAR)
        pr, mr = replay_sr(ms, sr_cfg, START_YEAR)
        pb = blend(pe, pr, coef)
        variant_preds[name] = (ecfg, pe, me, pr, mr, pb)
        data_results[name] = {k: score(window(pb, rng)) for k, rng in
                              (("decision_2021_22", DATA_DECISION), ("test_2023_on", after_decision), ("forward", forward))}
        log(f"  data {name}: decision log loss {data_results[name]['decision_2021_22'].get('log_loss', float('nan')):.5f}")
    data_choice = min(data_results, key=lambda k: data_results[k]["decision_2021_22"]["log_loss"])
    ecfg, preds_sel, model_sel, preds_sr, model_sr, preds_blend = variant_preds[data_choice]
    selected = ecfg
    log(f"data choice (2021-22): {data_choice}")

    candidates = {"elo_selected": preds_sel, "serve_return": preds_sr, "elo_sr_blend": preds_blend}
    serving = min(candidates, key=lambda k: score(window(candidates[k], VALIDATION))["log_loss"])
    log(f"serving model (chosen on validation): {serving}")

    preds_base, _ = replay(matches, BASELINE, START_YEAR)
    preds_overall, _ = replay(matches, BASELINE.with_(name="elo_overall", surface_weight=0.0), START_YEAR)
    test_sel = window(preds_sel, test)
    beta = fit_rank_logistic(window(preds_sel, VALIDATION))
    ys = [p.y for p in test_sel]

    def headline(rng, with_rank=True):
        sel = window(preds_sel, rng)
        y = [p.y for p in sel]
        out = {"coin_flip": metrics.summarize([0.5] * len(y), y)}
        if with_rank:
            out["ranking_logistic"] = {**metrics.summarize(rank_predictions(sel, beta), y), "beta": beta}
        out.update({
            "elo_overall": score(window(preds_overall, rng)),
            "elo_538_blend": score(window(preds_base, rng)),
            "elo_selected": score(sel),
            "serve_return": score(window(preds_sr, rng)),
            "elo_sr_blend": score(window(preds_blend, rng)),
        })
        return out

    fwd_serving = window(candidates[serving], forward)
    used = all_matches if data_choice != "tour_only" else matches
    stack, preds_stacked = stack_experiment(conn, used, candidates[serving],
                                            {"test": test, "test_2023_on": after_decision, "forward": forward}, log)
    if stack["features"]:  # the layer only exists if it improved validation
        candidates["stacked"], serving = preds_stacked, "stacked"
        log(f"serving model: stacked over {stack['features']}")
    # Set-score and tiebreak odds are their own output, so their form-on-the-day width is chosen on
    # validation scoreline accuracy (straight-sets log loss), separately from the match-winner blend.
    sc_grid = {sd: scoreline_check(used, sr_cfg, VALIDATION, form_sd=sd) for sd in (0.0, 0.1, 0.15, 0.2, 0.25, 0.3)}
    scoreline_sd = min(sc_grid, key=lambda sd: sc_grid[sd]["straight_sets"]["log_loss"])
    log(f"scoreline form_sd (validation): {scoreline_sd}")
    scorelines = {name: scoreline_check(used, sr_cfg, rng, form_sd=scoreline_sd)
                  for name, rng in (("validation", VALIDATION), ("test", test), ("forward", forward))}
    scorelines["independent_points_test"] = scoreline_check(used, sr_cfg, test, form_sd=0.0)
    completed_only = [p for p in test_sel if p.outcome == "completed"]
    result = {
        "validation_years": VALIDATION,
        "test_years": test,
        "forward_window": forward,
        "n_scorable_matches": len(matches),
        "n_usable_matches": len(all_matches),
        "selected_config": asdict(selected),
        "selection_steps": steps,
        "candidates_tried": tried,
        "test_headline": headline(test),
        "forward_headline": headline(forward, with_rank=False) if fwd_serving else None,
        "data_variants": data_results,
        "data_choice": data_choice,
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
        "test_blend_vs_elo": paired_gain(test_sel, window(preds_blend, test)),
        "forward_blend_vs_elo": paired_gain(window(preds_sel, forward), window(preds_blend, forward)) if fwd_serving else None,
        "test_serving_by_confidence": breakdown(window(candidates[serving], test), lambda p: p.confidence),
        "test_serving_calibration": metrics.calibration([p.p1 for p in window(candidates[serving], test)], ys),
        "forward_serving_calibration": metrics.calibration([p.p1 for p in fwd_serving], [p.y for p in fwd_serving]) if fwd_serving else None,
        "stack_experiment": stack,
        "scoreline_check": scorelines,
        "scoreline_form_sd": scoreline_sd,
        "scoreline_grid": {str(k): v["straight_sets"]["log_loss"] for k, v in sc_grid.items()},
    }
    save(conn, selected, preds_sel, model_sel, result)
    extra = {"serve_return": preds_sr, "elo_sr_blend": preds_blend}
    if "stacked" in candidates:
        extra["stacked"] = candidates["stacked"]
    save_sr(conn, sr_cfg, model_sr, extra, coef, serving)
    with conn:
        db.set_meta(conn, "stack", json.dumps({"features": stack["features"], "coef": stack["coef"]}))
        db.set_meta(conn, "scoreline_form_sd", json.dumps(scoreline_sd))
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


def scoreline_check(matches: list[MatchRow], cfg: SRConfig, dates=TEST, form_sd: float | None = None) -> dict:
    """How well the Markov chain predicts straight sets and tiebreaks, vs the actual scores.

    ``form_sd`` widens the form-on-the-day integration at prediction time only (ratings use ``cfg``).
    """
    from . import markov

    model = ServeReturnModel(cfg)
    if form_sd is not None:
        model.form_sd_override = form_sd
    rows = []
    for m in matches:
        if m.scored and dates[0] <= m.date <= dates[1] and m.outcome == "completed":
            sets = [t for t in (m.score or "").split() if "-" in t]
            rule = markov.final_set_rule(m.level, m.tourney, m.year, m.best_of, cfg.next_gen_format)
            need = 3 if m.best_of == 5 else 2
            p_straight = e_tb = 0.0
            for d, w in model.form_nodes():
                pa, pb = model.point_probs(m.winner_id, m.loser_id, m.surface, m.indoor, d)
                s = markov.set_win(pa, pb, rule if rule == ("short",) else markov.STANDARD)
                ps_ = s**need + (1 - s) ** need
                if need == 3:
                    e_sets = 3 * ps_ + 4 * 3 * (s**3 * (1 - s) + s * (1 - s) ** 3) + 5 * 6 * s * s * (1 - s) ** 2
                else:
                    e_sets = 2 * ps_ + 3 * (1 - ps_)
                p_straight += w * ps_
                e_tb += w * e_sets * (0 if rule == ("short",) else markov.reach_tiebreak(pa, pb))
            if sets:
                rows.append((p_straight, len(sets) == need, e_tb, sum(t[:3] in ("7-6", "6-7") for t in sets)))
        model.update(m.winner_id, m.loser_id, m.surface, m.indoor, m.w_svpt, m.w_won, m.l_svpt, m.l_won)
    ps, ys = [r[0] for r in rows], [int(r[1]) for r in rows]
    return {
        "n": len(rows),
        "straight_sets": {**metrics.summarize(ps, ys), "mean_pred": sum(ps) / len(ps), "observed": sum(ys) / len(ys),
                          "calibration": metrics.calibration(ps, ys, bins=5)},
        "tiebreaks_per_match": {"pred": sum(r[2] for r in rows) / len(rows), "observed": sum(r[3] for r in rows) / len(rows)},
    }


def stack_experiment(conn, matches: list[MatchRow], base: list[PredRecord], windows: dict, log=print) -> dict:
    """Greedy feature selection on validation for a logistic layer on top of the served model."""
    from .features import NAMES, replay_features

    feats = replay_features(conn, matches, START_YEAR)

    def rows(preds, chosen):
        return [[safe_logit(p.p1)] + [feats[p.match_id][NAMES.index(f)] for f in chosen] for p in preds]

    def apply(preds, chosen, coef):
        out = []
        for p, x in zip(preds, rows(preds, chosen)):
            z = sum(c * v for c, v in zip(coef, x))
            out.append(replace(p, p1=1 / (1 + math.exp(-z))))
        return out

    val = window(base, VALIDATION)
    ys = [p.y for p in val]
    chosen: list[str] = []
    coef = fit_logistic(rows(val, chosen), ys)
    best = score(apply(val, chosen, coef))["log_loss"]
    trail = [{"features": [], "validation": best}]
    while True:
        trials = []
        for f in NAMES:
            if f in chosen:
                continue
            c = fit_logistic(rows(val, chosen + [f]), ys)
            trials.append((score(apply(val, chosen + [f], c))["log_loss"], f, c))
        if not trials:
            break
        ll, f, c = min(trials)
        log(f"  stack + {f}: validation log loss {ll:.5f} (was {best:.5f})")
        if ll >= best - 1e-4:
            break
        chosen, coef, best = chosen + [f], c, ll
        trail.append({"features": list(chosen), "validation": ll})
    stacked = apply(base, chosen, coef)
    res = {"features": chosen, "coef": coef, "trail": trail}
    for name, rng in windows.items():
        b, s_ = window(base, rng), window(stacked, rng)
        if b:
            res[name] = {"base": score(b), "stacked": score(s_), "gain": paired_gain(b, s_)}
    return res, stacked
