"""Optional gradient-boosting layer (spec, "Prediction engine" step 3: an ML layer on the baseline's residual).

LightGBM learns a correction on top of the served stack's logit from the
model components, the context features and a few match flags. Each match is
used from both players' sides so the correction is antisymmetric. It is
chosen inside validation only: fit on 2014-16, compared with the logistic
stack on 2017-18, and adopted only if it wins there; then refit on 2014-18.

``lightgbm`` and ``numpy`` are optional (``pip install -e .[ml]``); without them
the logistic stack is served.
"""

from __future__ import annotations

import importlib.util
import itertools

from .backtest import PredRecord, paired_gain, safe_logit, score, window
from .features import NAMES

GRID = list(itertools.product((150, 300, 600), (4, 8, 16), (0.02, 0.04)))  # rounds, leaves, learning rate
N_ANTI = 4 + len(NAMES)  # logits and player-1-minus-player-2 features flip sign when the players swap


def available() -> bool:
    return all(importlib.util.find_spec(m) for m in ("lightgbm", "numpy"))


def row(stacked_p: float, elo_p: float, sr_p: float, blend_p: float, feats: list[float], best_of, level, surface) -> list[float]:
    s = (surface or "").capitalize()
    return [safe_logit(stacked_p), safe_logit(elo_p), safe_logit(sr_p), safe_logit(blend_p), *feats,
            float(best_of == 5), float(level == "G"), float(s == "Clay"), float(s == "Grass")]


def _swap(X):
    Y = X.copy()
    Y[:, :N_ANTI] *= -1
    return Y


def fit(X, y, rounds: int, leaves: int, lr: float):
    import lightgbm as lgb
    import numpy as np

    Xs = np.vstack([X, _swap(X)])
    data = lgb.Dataset(Xs, np.concatenate([y, 1 - y]), init_score=Xs[:, 0])
    params = {"objective": "binary", "learning_rate": lr, "num_leaves": leaves, "min_data_in_leaf": 200,
              "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 10.0,
              "verbose": -1, "seed": 7, "num_threads": 2}
    return lgb.train(params, data, num_boost_round=rounds)


def predict(booster, X):
    import numpy as np

    corr = booster.predict(X, raw_score=True) - booster.predict(_swap(X), raw_score=True)  # raw excludes init_score
    return 1 / (1 + np.exp(-(X[:, 0] + corr / 2)))


def run(stacked: list[PredRecord], rows: dict[str, list[float]], validation: tuple, windows: dict, log=print):
    """Return (result, predictions, booster) or (result, None, None) when the layer is not adopted."""
    import numpy as np

    def mat(preds):
        return np.array([rows[p.match_id] for p in preds]), np.array([p.y for p in preds])

    inner_fit = window(stacked, (validation[0], "2016-12-31"))
    inner_eval = window(stacked, ("2017-01-01", validation[1]))
    Xa, ya = mat(inner_fit)
    Xb, yb = mat(inner_eval)
    base = score(inner_eval)["log_loss"]
    trials = {}
    for params in GRID:
        p = predict(fit(Xa, ya, *params), Xb)
        trials[params] = -float(np.mean(yb * np.log(p) + (1 - yb) * np.log(1 - p)))
    best = min(trials, key=trials.get)
    log(f"  ml layer: inner 2017-18 log loss {trials[best]:.5f} with {best} vs stack {base:.5f}")
    result = {"inner_stack": base, "inner_gbm": trials[best], "params": best, "adopted": trials[best] < base - 1e-4}
    if not result["adopted"]:
        return result, None, None
    val = window(stacked, validation)
    booster = fit(*mat(val), *best)
    Xall, _ = mat(stacked)
    p_all = predict(booster, Xall)
    out = [PredRecord(**{**p.__dict__, "p1": float(min(max(q, 1e-9), 1 - 1e-9))}) for p, q in zip(stacked, p_all)]
    for name, rng in windows.items():
        b, g = window(stacked, rng), window(out, rng)
        if b:
            result[name] = {"stack": score(b), "gbm": score(g), "gain": paired_gain(b, g)}
    return result, out, booster


__all__ = ["available", "row", "run", "fit", "predict"]
