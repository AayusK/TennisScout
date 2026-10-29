import math

import pytest

from tennisscout import metrics


def test_brier_and_log_loss_known_values():
    ps, ys = [0.8, 0.3], [1, 0]
    assert metrics.brier(ps, ys) == pytest.approx(((0.2) ** 2 + (0.3) ** 2) / 2)
    assert metrics.log_loss(ps, ys) == pytest.approx(-(math.log(0.8) + math.log(0.7)) / 2)


def test_accuracy_counts_coin_flips_as_half():
    assert metrics.accuracy([0.5, 0.9, 0.2], [1, 1, 1]) == pytest.approx((0.5 + 1 + 0) / 3)


def test_calibration_bins_and_ece():
    ps = [0.05, 0.15, 0.95, 1.0]
    ys = [0, 0, 1, 1]
    bins = metrics.calibration(ps, ys)
    assert len(bins) == 10
    assert bins[0]["n"] == 1 and bins[1]["n"] == 1 and bins[9]["n"] == 2
    assert metrics.ece(ps, ys) == pytest.approx((0.05 + 0.15 + 0.05 + 0.0) / 4)
