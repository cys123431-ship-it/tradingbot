from emas import _rank_utbreakout_sets_stably


def test_set_stability_prefers_previous_set_when_score_gap_is_small():
    candidates = [
        {"set_id": 61, "score": 76.6, "name": "Set61 UT + Rolling OFI Confirmation"},
        {"set_id": 51, "score": 75.6, "name": "Set51 UT + Orderflow Imbalance"},
    ]

    ranked = _rank_utbreakout_sets_stably(
        candidates,
        previous_set_id=51,
        cfg={"set_selection_switch_margin": 3.0, "set_complexity_penalty_enabled": True},
    )

    assert ranked[0]["set_id"] == 51


def test_complex_set_loses_tie_break_when_score_gap_is_tiny():
    candidates = [
        {"set_id": 61, "score": 76.6, "name": "Set61 UT + Rolling OFI Confirmation"},
        {"set_id": 10, "score": 76.0, "name": "Set10 UT + Aroon"},
    ]

    ranked = _rank_utbreakout_sets_stably(
        candidates,
        previous_set_id=None,
        cfg={"set_selection_switch_margin": 3.0, "set_complexity_penalty_enabled": True},
    )

    assert ranked[0]["set_id"] in {10, 61}
    assert ranked[0]["adjusted_score"] <= ranked[0]["raw_score"]


def test_set_stability_switches_when_challenger_has_clear_edge():
    candidates = [
        {"set_id": 22, "score": 82.0, "name": "Set22 UT + Donchian breakout"},
        {"set_id": 51, "score": 70.0, "name": "Set51 UT + Orderflow Imbalance"},
    ]

    ranked = _rank_utbreakout_sets_stably(
        candidates,
        previous_set_id=51,
        cfg={"set_selection_switch_margin": 3.0, "set_complexity_penalty_enabled": True},
    )

    assert ranked[0]["set_id"] == 22
