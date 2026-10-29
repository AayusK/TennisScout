import pytest

from tennisscout.elo import (
    OVERALL,
    EloConfig,
    EloModel,
    bo3_to_set_prob,
    expected,
    k_factor,
    set_prob_to_bo5,
)


def test_expected_is_symmetric_and_centred():
    assert expected(1500, 1500) == pytest.approx(0.5)
    assert expected(1700, 1500) + expected(1500, 1700) == pytest.approx(1.0)
    assert expected(1900, 1500) == pytest.approx(1 / (1 + 10 ** (-1)))


def test_k_shrinks_with_experience():
    cfg = EloConfig()
    assert k_factor(0, cfg) > k_factor(10, cfg) > k_factor(100, cfg)


def test_bo5_conversion():
    assert bo3_to_set_prob(0.5) == pytest.approx(0.5)
    s = bo3_to_set_prob(0.7)
    assert s * s * (3 - 2 * s) == pytest.approx(0.7, abs=1e-9)
    # A best-of-5 favourite is more of a favourite.
    assert set_prob_to_bo5(s) > 0.7
    assert set_prob_to_bo5(0.5) == pytest.approx(0.5)


def test_update_is_zero_sum_between_equally_experienced_players():
    m = EloModel(EloConfig())
    m.update("a", "b", "Hard", "O")
    assert m.rating[("a", OVERALL)] + m.rating[("b", OVERALL)] == pytest.approx(3000)
    assert m.rating[("a", "Hard")] > 1500 > m.rating[("b", "Hard")]
    assert ("a", "Clay") not in m.rating  # other surfaces untouched


def test_prediction_uses_only_prior_matches():
    m = EloModel(EloConfig())
    before = m.predict("a", "b", "Clay", "O").p_a
    assert before == pytest.approx(0.5)
    m.update("a", "b", "Clay", "O")
    assert m.predict("a", "b", "Clay", "O").p_a > before


def test_surface_weight_extremes():
    cfg = EloConfig(surface_weight=1.0)
    m = EloModel(cfg)
    for _ in range(5):
        m.update("a", "b", "Clay", "O")
    # On grass neither has a surface rating yet, so a surface-only model is indifferent.
    assert m.predict("a", "b", "Grass", "O").p_a == pytest.approx(0.5)
    assert m.predict("a", "b", "Clay", "O").p_a > 0.5


def test_rank_prior_does_not_inflate_the_pool():
    cfg = EloConfig(new_player_prior="rank", established_matches=3, prior_min_obs=5)
    m = EloModel(cfg)
    # Two established players keep beating a stream of debutants.
    for i in range(300):
        m.update("star", f"rookie{i}", "Hard", "O", rank_w=5, rank_l=150)
        m.update("vet", f"rookie_b{i}", "Hard", "O", rank_w=80, rank_l=80)
    debut = m.prior.value(80)
    assert debut != cfg.initial  # the prior is active for this bucket
    assert abs(debut - cfg.initial) < 400  # bounded offset, no runaway
    assert m.rating[("star", OVERALL)] < 3000


def test_layoff_boost_moves_returning_players_more():
    def after_break(boost):
        m = EloModel(EloConfig(layoff_boost=boost))
        for i in range(10):
            m.update("a", f"x{i}", "Hard", "O", date="2024-01-01")
        before = m.rating[("a", OVERALL)]
        m.update("y", "a", "Hard", "O", date="2025-03-01")  # back after 14 months, loses
        return before - m.rating[("a", OVERALL)]

    assert after_break(1.0) == pytest.approx(2 * after_break(0.0))
