import random
from dataclasses import replace

import pytest

from tennisscout import backtest
from tennisscout.backtest import MatchRow, replay_sr
from tennisscout.serve_return import ServeReturnModel, SRConfig


def test_update_moves_the_right_ratings():
    m = ServeReturnModel(SRConfig())
    m.update("a", "b", "Hard", "O", 80, 60, 80, 44)  # a won 75% on serve, b 55%
    assert m.mean[("a", "serve")] > 0 > m.mean[("b", "serve")]
    assert m.mean[("a", "return")] > 0 > m.mean[("b", "return")]
    assert m.var[("a", "serve")] < SRConfig().prior_var
    pa, pb = m.point_probs("a", "b", "Hard", "O")
    assert pa > pb


def test_missing_stats_leave_ratings_at_the_prior():
    m = ServeReturnModel(SRConfig())
    m.update("a", "b", "Clay", "O", None, None, None, None)
    assert m.mean == {} and m.n == {}
    m.update("a", "b", "Clay", "O", 70, 45, 70, 40)
    pc, pd = m.point_probs("c", "d", "Clay", "O")  # strangers: both at the surface average
    assert pc == pytest.approx(pd)


def test_sparse_player_moves_less_than_a_settled_one_would():
    big = ServeReturnModel(SRConfig(prior_var=0.04))
    small = ServeReturnModel(SRConfig(prior_var=0.005))
    for mdl in (big, small):
        mdl.update("a", "b", "Hard", "O", 80, 64, 80, 48)
    assert abs(small.mean[("a", "serve")]) < abs(big.mean[("a", "serve")])


def test_no_lookahead():
    rng = random.Random(3)
    rows = [MatchRow(f"M{i:07d}", i, 2000 + i // 200, "2000-01-01", f"p{rng.randrange(20)}", f"q{rng.randrange(20)}",
                     "Hard", "O", 3, None, None, "250", "completed", "R32", "X", None, 80, rng.randrange(40, 70), 80, rng.randrange(40, 70))
            for i in range(1, 801)]
    a, _ = replay_sr(rows, SRConfig(), 2000)
    changed = rows[:400] + [replace(r, w_svpt=None, w_won=None, l_svpt=None, l_won=None) for r in rows[400:]]
    b, _ = replay_sr(changed, SRConfig(), 2000)
    assert [p.p1 for p in a[:400]] == [p.p1 for p in b[:400]]


def test_fit_logistic_recovers_coefficients():
    rng = random.Random(5)
    xs, ys = [], []
    for _ in range(4000):
        x = [rng.gauss(0, 1), rng.gauss(0, 1)]
        z = 1.5 * x[0] - 0.5 * x[1]
        xs.append(x)
        ys.append(int(rng.random() < 1 / (1 + 2.718281828 ** -z)))
    w = backtest.fit_logistic(xs, ys)
    assert w[0] == pytest.approx(1.5, abs=0.15) and w[1] == pytest.approx(-0.5, abs=0.15)


def test_surface_offsets_capture_a_clay_specialist():
    m = ServeReturnModel(SRConfig(surface_var=0.002))
    for _ in range(30):
        m.update("clay", "x", "Clay", "O", 80, 60, 80, 44)  # dominant on clay
        m.update("x", "clay", "Hard", "O", 80, 52, 80, 52)  # ordinary on hard
    assert m.mean[("clay", "serve@Clay")] > 0
    on_clay = m.point_probs("clay", "x", "Clay", "O")
    on_hard = m.point_probs("clay", "x", "Hard", "O")
    assert on_clay[0] - on_clay[1] > on_hard[0] - on_hard[1]
