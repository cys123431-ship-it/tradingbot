from emas import _classify_set_filter_result


def test_orderflow_low_samples_soft_fail_not_block():
    state, mult, detail = _classify_set_filter_result(
        "long",
        False,
        "Rolling OFI failed",
        metrics={"imb": -20, "taker": 0.9, "samples": 2},
        cfg={},
    )
    assert state == "reduced"
    assert mult > 0
    assert "low samples" in detail


def test_orderflow_strong_opposite_with_enough_samples_reduces():
    state, mult, detail = _classify_set_filter_result(
        "long",
        False,
        "Rolling OFI failed",
        metrics={"imb": -20, "taker": 0.9, "samples": 8},
        cfg={},
    )
    assert state == "reduced"
    assert 0.0 < mult < 0.50
    assert "hard opposite" in detail


def test_excessive_spread_still_blocks():
    state, mult, detail = _classify_set_filter_result(
        "long",
        False,
        "Spread/depth failed",
        metrics={
            "imb": 0,
            "taker": 1.0,
            "samples": 2,
            "futures_spread_pct": 0.08,
        },
        cfg={"rolling_ofi_spread_max_pct": 0.05},
    )
    assert state is False
    assert mult == 0.0
    assert "spread" in detail
