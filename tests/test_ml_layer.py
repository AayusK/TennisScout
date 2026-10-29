import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("lightgbm")

from tennisscout import ml_layer  # noqa: E402


def test_correction_is_antisymmetric():
    rng = np.random.default_rng(1)
    n = 2000
    X = rng.normal(size=(n, ml_layer.N_ANTI + 4))
    X[:, ml_layer.N_ANTI:] = rng.integers(0, 2, size=(n, 4))  # symmetric context flags
    y = (rng.random(n) < 1 / (1 + np.exp(-(X[:, 0] + 0.3 * X[:, 5])))).astype(float)
    booster = ml_layer.fit(X, y, rounds=50, leaves=4, lr=0.1)
    p = ml_layer.predict(booster, X)
    q = ml_layer.predict(booster, ml_layer._swap(X))
    assert np.allclose(p + q, 1.0)
