"""Surface-weighted Elo (spec, "Prediction engine" step 1).

Two rating systems run side by side, as in FiveThirtyEight's tennis Elo:
an overall rating and a rating per surface bucket (hard, clay, grass,
indoor hard, carpet). The match prediction blends them:

    r = w * surface_rating + (1 - w) * overall_rating

Each system updates with its own K, which shrinks as a player accumulates
matches in that system: K = k_num / (n + k_offset) ** k_shape (Kovalchik 2016).

Sparse-data rule (spec): a debutant can start from a prior estimated by
ranking range instead of a flat 1500, and a player new to a surface can start
from their overall rating. Both are options so the backtest can decide
whether they earn their place.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .normalize import surface_key

OVERALL = "Overall"

# Ranking ranges for the debut prior. Upper bounds are inclusive.
RANK_BUCKETS = (10, 20, 50, 100, 200, 500, 1000, 10**9)
UNRANKED_BUCKET = len(RANK_BUCKETS)


@dataclass(frozen=True)
class EloConfig:
    name: str = "elo"
    initial: float = 1500.0
    k_num: float = 250.0
    k_offset: float = 5.0
    k_shape: float = 0.4
    surface_weight: float = 0.5  # 0 = overall only, 1 = surface only
    split_indoor_hard: bool = True
    new_player_prior: str = "flat"  # "flat" | "rank"
    surface_init: str = "flat"  # "flat" | "overall"
    bo5_adjust: bool = False
    pred_scale: float = 1.0  # >1 widens the logistic at prediction time only (calibration), updates unchanged
    established_matches: int = 30  # players whose ratings feed the rank prior
    prior_alpha: float = 0.02  # EMA weight for the rank prior
    prior_min_obs: int = 50

    def with_(self, **kw) -> "EloConfig":
        return replace(self, **kw)


def expected(r_a: float, r_b: float, scale: float = 400.0) -> float:
    return 1.0 / (1.0 + 10.0 ** ((r_b - r_a) / scale))


def k_factor(n_played: int, cfg: EloConfig) -> float:
    return cfg.k_num / (n_played + cfg.k_offset) ** cfg.k_shape


def bo3_to_set_prob(p_match: float) -> float:
    """Invert P(win best-of-3) = s^2 (3 - 2s) for the per-set probability s."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if mid * mid * (3 - 2 * mid) < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def set_prob_to_bo5(s: float) -> float:
    """P(win best-of-5) given independent sets won with probability s."""
    return s**3 * (10 - 15 * s + 6 * s * s)


def rank_bucket(rank: int | None) -> int:
    if not rank or rank <= 0:
        return UNRANKED_BUCKET
    for i, ub in enumerate(RANK_BUCKETS):
        if rank <= ub:
            return i
    return UNRANKED_BUCKET


@dataclass
class RankPrior:
    """Online estimate of how far each ranking range sits from the established pool.

    A debutant starts at ``initial + (bucket_mean - pool_mean)``. Using an
    offset rather than the bucket mean itself matters: Elo is zero-sum, so
    seeding newcomers at the (survivor-biased) established mean injects rating
    into the pool every time, and the pool inflates without bound.

    Fed only with ratings that existed before the match being predicted, so it
    never leaks future information into a debutant's starting point.
    """

    cfg: EloConfig
    mean: dict[int, float] = field(default_factory=dict)
    n: dict[int, int] = field(default_factory=dict)
    pool_mean: float | None = None

    def observe(self, rank: int | None, rating: float) -> None:
        a = self.cfg.prior_alpha
        self.pool_mean = rating if self.pool_mean is None else self.pool_mean + a / 10 * (rating - self.pool_mean)
        b = rank_bucket(rank)
        if b not in self.mean:
            self.mean[b], self.n[b] = rating, 1
            return
        self.mean[b] += a * (rating - self.mean[b])
        self.n[b] += 1

    def value(self, rank: int | None) -> float:
        b = rank_bucket(rank)
        if self.pool_mean is None or self.n.get(b, 0) < self.cfg.prior_min_obs:
            return self.cfg.initial
        return self.cfg.initial + (self.mean[b] - self.pool_mean)


@dataclass
class Prediction:
    p_a: float
    rating_a: float
    rating_b: float
    overall_a: float
    overall_b: float
    surface_a: float
    surface_b: float
    n_overall_a: int
    n_overall_b: int
    n_surface_a: int
    n_surface_b: int
    p_elo_bo3: float  # before any best-of-5 adjustment
    bucket: str


class EloModel:
    def __init__(self, cfg: EloConfig | None = None):
        self.cfg = cfg or EloConfig()
        self.rating: dict[tuple[str, str], float] = {}
        self.n: dict[tuple[str, str], int] = {}
        self.last_date: dict[str, str] = {}
        self.prior = RankPrior(self.cfg)

    # -- state ---------------------------------------------------------------
    def bucket(self, surface: str | None, indoor: str | None) -> str:
        return surface_key(surface or "", indoor or "", self.cfg.split_indoor_hard)

    def _ensure(self, pid: str, bucket: str, rank: int | None) -> None:
        cfg = self.cfg
        if (pid, OVERALL) not in self.rating:
            start = self.prior.value(rank) if cfg.new_player_prior == "rank" else cfg.initial
            self.rating[(pid, OVERALL)] = start
            self.n[(pid, OVERALL)] = 0
        if bucket != "Unknown" and (pid, bucket) not in self.rating:
            if cfg.surface_init == "overall":
                start = self.rating[(pid, OVERALL)]
            elif cfg.new_player_prior == "rank" and self.n[(pid, OVERALL)] == 0:
                start = self.rating[(pid, OVERALL)]
            else:
                start = cfg.initial
            self.rating[(pid, bucket)] = start
            self.n[(pid, bucket)] = 0

    def get(self, pid: str, bucket: str) -> tuple[float, int]:
        return self.rating.get((pid, bucket), self.cfg.initial), self.n.get((pid, bucket), 0)

    # -- predict / update ----------------------------------------------------
    def predict(
        self,
        a: str,
        b: str,
        surface: str | None,
        indoor: str | None,
        best_of: int | None = 3,
        rank_a: int | None = None,
        rank_b: int | None = None,
    ) -> Prediction:
        cfg = self.cfg
        bucket = self.bucket(surface, indoor)
        self._ensure(a, bucket, rank_a)
        self._ensure(b, bucket, rank_b)
        oa, na = self.get(a, OVERALL)
        ob, nb = self.get(b, OVERALL)
        if bucket == "Unknown":
            sa, sna, sb, snb = oa, 0, ob, 0
            w = 0.0
        else:
            sa, sna = self.get(a, bucket)
            sb, snb = self.get(b, bucket)
            w = cfg.surface_weight
        ra = w * sa + (1 - w) * oa
        rb = w * sb + (1 - w) * ob
        p3 = expected(ra, rb, 400.0 * cfg.pred_scale)
        p = p3
        if cfg.bo5_adjust and best_of == 5:
            p = set_prob_to_bo5(bo3_to_set_prob(p3))
        return Prediction(p, ra, rb, oa, ob, sa, sb, na, nb, sna, snb, p3, bucket)

    def update(
        self,
        winner: str,
        loser: str,
        surface: str | None,
        indoor: str | None,
        rank_w: int | None = None,
        rank_l: int | None = None,
        date: str | None = None,
    ) -> None:
        cfg = self.cfg
        bucket = self.bucket(surface, indoor)
        self._ensure(winner, bucket, rank_w)
        self._ensure(loser, bucket, rank_l)
        if cfg.new_player_prior == "rank":
            for pid, rk in ((winner, rank_w), (loser, rank_l)):
                if self.n[(pid, OVERALL)] >= cfg.established_matches:
                    self.prior.observe(rk, self.rating[(pid, OVERALL)])
        buckets = (OVERALL,) if bucket == "Unknown" else (OVERALL, bucket)
        for bk in buckets:
            rw, rl = self.rating[(winner, bk)], self.rating[(loser, bk)]
            e = expected(rw, rl)
            self.rating[(winner, bk)] = rw + k_factor(self.n[(winner, bk)], cfg) * (1 - e)
            self.rating[(loser, bk)] = rl - k_factor(self.n[(loser, bk)], cfg) * (1 - e)
            self.n[(winner, bk)] += 1
            self.n[(loser, bk)] += 1
        if date:
            self.last_date[winner] = date
            self.last_date[loser] = date


def confidence(pred: Prediction) -> str:
    """Coarse data-sufficiency grade for a prediction (spec, sparse-data rule)."""
    n_min = min(pred.n_overall_a, pred.n_overall_b)
    s_min = min(pred.n_surface_a, pred.n_surface_b)
    if n_min >= 50 and s_min >= 15:
        return "high"
    if n_min >= 15:
        return "medium"
    return "low"

