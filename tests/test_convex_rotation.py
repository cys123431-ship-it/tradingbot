from utbreakout.convex_rotation import evaluate_convex_rotation_exit


def _decision(**overrides):
    values = {
        "enabled": True,
        "bars_held": 36,
        "min_bars": 32,
        "max_bars": 48,
        "mfe_r": 0.20,
        "max_mfe_r": 0.35,
        "current_r": 0.05,
        "max_current_r": 0.25,
        "entry_percentile": 92.0,
        "current_percentile": 25.0,
        "percentile_floor": 35.0,
        "reaccelerating": False,
        "prior_bad_count": 0,
        "required_confirmations": 2,
    }
    values.update(overrides)
    return evaluate_convex_rotation_exit(**values)


def test_convex_rotation_requires_persistent_rank_decay():
    first = _decision()
    second = _decision(prior_bad_count=first.bad_count, bars_held=40)

    assert first.bad_observation is True
    assert first.should_exit is False
    assert second.bad_count == 2
    assert second.should_exit is True


def test_convex_rotation_keeps_winners_and_reaccelerating_trends():
    winner = _decision(mfe_r=0.80, prior_bad_count=1)
    reaccelerating = _decision(reaccelerating=True, prior_bad_count=1)

    assert winner.should_exit is False
    assert winner.bad_count == 0
    assert reaccelerating.should_exit is False
    assert reaccelerating.bad_count == 0


def test_convex_rotation_fails_open_when_relative_rank_is_missing():
    missing = _decision(current_percentile=None, prior_bad_count=1)

    assert missing.should_exit is False
    assert missing.bad_count == 0
    assert "unavailable" in missing.reason
