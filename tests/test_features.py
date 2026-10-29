from datetime import date

from tennisscout.backtest import MatchRow
from tennisscout.features import FeatureState
from tennisscout.normalize import games_share


def row(mid, d, w, l):
    return MatchRow(mid, 0, int(d[:4]), d, w, l, "Hard", "O", 3, None, None, "250", "completed", "R32")


def test_features_use_only_the_past():
    info = {"a": (date(2000, 1, 1), "L", "USA"), "b": (date(1995, 1, 1), "R", "FRA")}
    st = FeatureState(info, {"m3": ("USA", -74.0)})
    first = st.features(row("m1", "2024-01-01", "a", "b"), "a", "b")
    assert first[2] == 0.0  # no head-to-head yet
    st.update(row("m1", "2024-01-01", "a", "b"))
    st.update(row("m2", "2024-01-08", "a", "b"))
    f = st.features(row("m3", "2024-01-10", "a", "b"), "a", "b")
    age, lefty, h2h, rest, load, home, exp, switch, travel = f
    assert round(age) == -5 and lefty == 1.0 and home == 1.0
    assert h2h == 2 / 6
    assert load == 0.0 and rest == 0.0 and exp == 0.0  # both played the same matches
    assert switch == 0.0 and travel == 0.0
    # Symmetry: swapping the players flips every feature.
    g = st.features(row("m3", "2024-01-10", "a", "b"), "b", "a")
    assert all(abs(x + y) < 1e-12 for x, y in zip(f, g))


def test_games_share():
    assert games_share("6-4 3-6 7-6(5)") == 16 / 32
    assert games_share("6-0 6-0") == 1.0
    assert games_share("W/O") is None


def test_context_is_symmetric_and_scales_the_logit():
    from tennisscout.features import ALL, INTERACTIONS, stack_row

    st = FeatureState({}, {})
    st.update(row("m1", "2024-01-01", "a", "c"))
    m = MatchRow("m2", 0, 2024, "2024-01-08", "a", "b", "Clay", "O", 5, None, None, "G", "completed", "SF")
    ctx = st.context(m, "a", "b")
    assert ctx == st.context(m, "b", "a") and len(ctx) == len(INTERACTIONS)
    conf, bo5, indoor, clay, grass, slam, masters, late = ctx
    assert conf == 0.0 and (bo5, indoor, clay, grass, slam, masters, late) == (1, 0, 1, 0, 1, 0, 1)  # b has no prior match
    vec = st.features(m, "a", "b") + ctx
    assert len(vec) == len(ALL)
    assert stack_row(2.0, vec, ["clay", "h2h"]) == [2.0, 2.0, vec[ALL.index("h2h")]]
