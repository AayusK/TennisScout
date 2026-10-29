import pytest

from tennisscout.markov import STANDARD, final_set_rule, hold, match_win, reach_tiebreak, set_win, tiebreak

RULES = [STANDARD, None, (12, 7), (6, 10), ("match_tb", 10), ("short",)]


def test_hold_known_values():
    assert hold(0.5) == pytest.approx(0.5)
    assert hold(0.6) == pytest.approx(0.7357, abs=1e-4)
    assert hold(0.7) > 0.9


@pytest.mark.parametrize("pa,pb", [(0.62, 0.58), (0.7, 0.55), (0.5, 0.5)])
def test_who_serves_first_does_not_matter(pa, pb):
    # A serving first vs B serving first (roles swapped) must agree: P(A) = 1 - P(B).
    assert tiebreak(pa, pb) == pytest.approx(1 - tiebreak(pb, pa), abs=1e-12)
    for rule in RULES:
        assert set_win(pa, pb, rule) == pytest.approx(1 - set_win(pb, pa, rule), abs=1e-12)


def test_set_formats():
    # Equal servers: every format is a coin flip.
    for rule in RULES:
        assert set_win(0.64, 0.64, rule) == pytest.approx(0.5)
    # A 12-12 tiebreak mixes advantage play and a tiebreak, so it lies between the two.
    std, adv, mid = set_win(0.66, 0.6, STANDARD), set_win(0.66, 0.6, None), set_win(0.66, 0.6, (12, 7))
    assert min(std, adv) < mid < max(std, adv)


def test_best_of_five_helps_the_favourite():
    p3, p5 = match_win(0.66, 0.62, 3), match_win(0.66, 0.62, 5)
    assert 0.5 < p3 < p5


def test_final_set_rules():
    assert final_set_rule("G", "Wimbledon", 2019, 5) == (12, 7)
    assert final_set_rule("G", "Roland Garros", 2021, 5) is None
    assert final_set_rule("G", "Roland Garros", 2022, 5) == (6, 10)
    assert final_set_rule("G", "US Open", 2015, 5) == STANDARD
    assert final_set_rule("D", "Davis Cup WG", 2014, 5) is None
    assert final_set_rule("250", "Brisbane", 2024, 3) == STANDARD


def test_next_gen_and_tiebreak_reach():
    assert final_set_rule("A", "Next Gen ATP Finals", 2023, 5) == ("short",)
    assert final_set_rule("A", "Next Gen ATP Finals", 2023, 5, next_gen=False) == STANDARD
    # Short sets are more random: the favourite gains less.
    assert 0.5 < set_win(0.68, 0.6, ("short",)) < set_win(0.68, 0.6)
    assert reach_tiebreak(0.8, 0.8) > reach_tiebreak(0.6, 0.6)
