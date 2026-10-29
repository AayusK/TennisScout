import random

from tennisscout import backtest
from tennisscout.backtest import MatchRow, player1_is_winner, replay
from tennisscout.elo import EloConfig


def synthetic_history(n=400, seed=1):
    rng = random.Random(seed)
    strength = {f"p{i}": rng.gauss(0, 1) for i in range(30)}
    rows = []
    for seq in range(1, n + 1):
        a, b = rng.sample(sorted(strength), 2)
        pa = 1 / (1 + 10 ** (-(strength[a] - strength[b])))
        w, l = (a, b) if rng.random() < pa else (b, a)
        rows.append(MatchRow(f"M{seq:07d}", seq, 2000 + seq // 100, "2000-01-01", w, l, "Hard", "O", 3, None, None, "250", "completed", "R32"))
    return rows


def test_no_lookahead():
    """Changing the future must not change any earlier prediction."""
    hist = synthetic_history()
    cut = 250
    preds_a, _ = replay(hist, EloConfig(), 2000)
    rng = random.Random(7)
    future = [
        MatchRow(m.match_id, m.seq, m.year, m.date, m.loser_id, m.winner_id, "Clay", "O", 5, 1, 2, "G", "completed", "F")
        if rng.random() < 0.5 else m
        for m in hist[cut:]
    ]
    preds_b, _ = replay(hist[:cut] + future, EloConfig(), 2000)
    assert [p.p1 for p in preds_a[:cut]] == [p.p1 for p in preds_b[:cut]]


def test_orientation_does_not_depend_on_result():
    flips = [player1_is_winner(f"M{i:07d}") for i in range(2000)]
    assert 0.45 < sum(flips) / len(flips) < 0.55


def test_elo_beats_coin_on_signal():
    hist = synthetic_history(n=3000)
    preds, _ = replay(hist, EloConfig(), 2010)
    s = backtest.score(preds)
    assert s["brier"] < 0.25 and s["accuracy"] > 0.55


def test_rank_logistic_fit_recovers_sign():
    hist = synthetic_history(n=50)
    preds, _ = replay(hist, EloConfig(), 2000)
    for p in preds:  # make the better-ranked player win
        p.rank1, p.rank2 = (5, 50) if p.y else (50, 5)
    assert backtest.fit_rank_logistic(preds) > 0


def test_end_to_end_on_fixture(loaded):
    conn, *_ = loaded
    rows = backtest.load_matches(conn)
    # 9 rows minus one walkover and one identity conflict.
    assert len(rows) == 7
    assert [r.seq for r in rows] == sorted(r.seq for r in rows)
