"""Opponent-adjusted serve and return ratings feeding the Markov chain (spec, "Prediction engine" step 2).

Each player has a serve rating s and a return rating r on the logit scale. The
chance A wins a point on serve against B on surface bucket k is

    pa = sigmoid(mu_k + s_A - r_B)

where mu_k is the tour-wide serve logit for that surface. A match's serve
points won is a binomial observation of pa; ratings move by a one-step
Laplace (Kalman-style) update, so a player with few matches stays near the
pool and keeps a wide variance (spec, sparse-data rule).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from . import markov
from .normalize import surface_key


@dataclass(frozen=True)
class SRConfig:
    name: str = "serve_return"
    prior_var: float = 0.02  # spread of serve/return logits across the pool
    drift_var: float = 0.0005  # added to a rating's variance each match: form changes
    mu_alpha: float = 0.002  # EMA weight for the tour-wide serve logit per surface
    surface_var: float = 0.0  # prior variance of per-player surface offsets; 0 turns them off
    next_gen_format: bool = True  # score Next Gen Finals with short no-ad sets
    form_sd: float = 0.0  # match-level "form on the day" shift (logit), A up / B down; 0 = independent points

    def with_(self, **kw) -> "SRConfig":
        return replace(self, **kw)


# 7-point Gauss-Hermite rule for a standard normal: (node, weight).
_GH = [(0.0, 0.8102646176), (0.8162878829, 0.4256072526), (1.6735516288, 0.0545155828), (2.6519613568, 0.0009717812)]
GAUSS_NORMAL = [(sign * x * math.sqrt(2), w / math.sqrt(math.pi)) for x, w in _GH for sign in ((1,) if x == 0 else (1, -1))]


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def logit(p: float) -> float:
    return math.log(p / (1 - p))


class ServeReturnModel:
    def __init__(self, cfg: SRConfig | None = None):
        self.cfg = cfg or SRConfig()
        self.mean: dict[tuple[str, str], float] = {}
        self.var: dict[tuple[str, str], float] = {}
        self.n: dict[str, int] = {}  # matches with a stat line
        self.mu: dict[str, float] = {}

    def bucket(self, surface: str | None, indoor: str | None) -> str:
        return surface_key(surface or "", indoor or "", split_indoor_hard=True)

    def _get(self, pid: str, kind: str) -> tuple[float, float]:
        prior = self.cfg.surface_var if "@" in kind else self.cfg.prior_var
        return self.mean.get((pid, kind), 0.0), self.var.get((pid, kind), prior)

    def _terms(self, server: str, returner: str, k: str) -> list[tuple[tuple[str, str], int]]:
        """Rating keys in the serve-point logit and their signs: serve (+), return (-), plus surface offsets."""
        terms = [((server, "serve"), 1), ((returner, "return"), -1)]
        if self.cfg.surface_var:
            terms += [((server, f"serve@{k}"), 1), ((returner, f"return@{k}"), -1)]
        return terms

    def _eta(self, server: str, returner: str, k: str, mu: float) -> float:
        return mu + sum(sign * self._get(*key)[0] for key, sign in self._terms(server, returner, k))

    def point_probs(self, a: str, b: str, surface: str | None, indoor: str | None, shift: float = 0.0) -> tuple[float, float]:
        k = self.bucket(surface, indoor)
        mu = self.mu.get(k, self.mu.get("Hard", logit(0.64)))
        return sigmoid(self._eta(a, b, k, mu) + shift), sigmoid(self._eta(b, a, k, mu) - shift)

    def form_nodes(self):
        """(shift, weight) pairs to average over; a single unshifted point when form_sd is 0."""
        sd = self.cfg.form_sd
        return [(sd * z, w) for z, w in GAUSS_NORMAL] if sd else [(0.0, 1.0)]

    def predict(self, a, b, surface, indoor, best_of, level, tourney, year) -> tuple[float, float, float]:
        rule = markov.final_set_rule(level, tourney, year, best_of, self.cfg.next_gen_format)
        p = sum(w * markov.match_win(*self.point_probs(a, b, surface, indoor, d), best_of or 3, rule)
                for d, w in self.form_nodes())
        pa, pb = self.point_probs(a, b, surface, indoor)
        return p, pa, pb

    def _observe(self, server: str, returner: str, k: str, mu: float, points: int, won: int) -> None:
        terms = self._terms(server, returner, k)
        cur = {key: self._get(*key) for key, _ in terms}
        # Form drifts; surface offsets are treated as stable traits.
        var = {key: v + (0.0 if "@" in key[1] else self.cfg.drift_var) for key, (_, v) in cur.items()}
        p = sigmoid(mu + sum(sign * cur[key][0] for key, sign in terms))
        info = points * p * (1 - p)
        total_var = sum(var.values()) + self.cfg.form_sd ** 2  # day form is noise the ratings should not chase
        step = (won - points * p) / (1 + total_var * info)
        shrink = info / (1 + total_var * info)
        for key, sign in terms:
            self.mean[key] = cur[key][0] + sign * var[key] * step
            self.var[key] = var[key] - var[key] ** 2 * shrink

    def update(self, w: str, l: str, surface, indoor, w_svpt, w_won, l_svpt, l_won) -> None:
        """Absorb one match's serve stat lines (pre-validated; None means no usable stats)."""
        if not (w_svpt and l_svpt):
            return
        k = self.bucket(surface, indoor)
        mu = self.mu.get(k)
        observed = logit(min(max((w_won + l_won) / (w_svpt + l_svpt), 0.01), 0.99))  # guard degenerate stat lines
        if mu is None:
            mu = self.mu[k] = observed
        self._observe(w, l, k, mu, w_svpt, w_won)
        self._observe(l, w, k, mu, l_svpt, l_won)
        self.mu[k] = mu + self.cfg.mu_alpha * (observed - mu)
        self.n[w] = self.n.get(w, 0) + 1
        self.n[l] = self.n.get(l, 0) + 1


def confidence(model: ServeReturnModel, a: str, b: str) -> str:
    n = min(model.n.get(a, 0), model.n.get(b, 0))
    return "high" if n >= 50 else ("medium" if n >= 15 else "low")
