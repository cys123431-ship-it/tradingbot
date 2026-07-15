from types import SimpleNamespace

import pytest

import emas


def _engine():
    return object.__new__(emas.SignalEngine)


def _cfg(**overrides):
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg = emas.apply_stable_utbreak_final_overrides(cfg)
    cfg = emas.apply_profit_opportunity_effective_overrides(cfg)
    cfg.update(overrides)
    return cfg


def _ev_decision(**overrides):
    values = {
        "allowed": True,
        "mode": "TREND",
        "score": 76.0,
        "win_probability": 0.56,
        "mtf_alignment": "2/3",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ev_net(expected_net_r=0.28):
    return SimpleNamespace(expected_net_r=expected_net_r)


def _ev_exit(executable=True):
    return SimpleNamespace(executable=executable)


def _alpha_decision(**overrides):
    values = {
        "allowed": True,
        "engine": "TREND_CONTINUATION",
        "score": 75.0,
        "probability": 0.58,
        "risk_multiplier": 0.78,
        "blockers": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _entry_edge_decision(**overrides):
    values = {
        "allowed": True,
        "engine": "TREND_CONTINUATION",
        "score": 72.0,
        "probability": 0.58,
        "risk_multiplier": 0.74,
        "net_expectancy_r": 0.24,
        "blockers": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_entry_quality_gate_allows_moderate_short_signal():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "short",
        _cfg(),
        final_risk_multiplier=0.46,
        market_quality={"state": "reduced", "risk_multiplier": 0.75},
        ev_decision=_ev_decision(),
        ev_net=_ev_net(0.32),
        ev_exit=_ev_exit(True),
        alpha_decision=_alpha_decision(),
    )

    assert state == "reduced"
    assert "PASS final x0.460>=x0.45" in detail


def test_entry_quality_gate_blocks_weak_long_final_multiplier():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "long",
        _cfg(),
        final_risk_multiplier=0.36,
        market_quality={"state": "reduced", "risk_multiplier": 0.80},
        ev_decision=_ev_decision(),
        ev_net=_ev_net(0.30),
        ev_exit=_ev_exit(True),
        alpha_decision=_alpha_decision(),
    )

    assert state is False
    assert "final risk multiplier x0.360<x0.50" in detail


@pytest.mark.parametrize(
    "decision,net,expected",
    [
        (_ev_decision(score=55.0), _ev_net(0.30), "EV score 55.0<66.0"),
        (_ev_decision(win_probability=0.51), _ev_net(0.30), "EV p 0.51<0.54"),
        (_ev_decision(), _ev_net(0.12), "EV net 0.120R<0.30R"),
        (_ev_decision(mtf_alignment="1/3"), _ev_net(0.30), "MTF 1/3<2/3"),
    ],
)
def test_entry_quality_gate_blocks_weak_ev_inputs(decision, net, expected):
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "short",
        _cfg(),
        final_risk_multiplier=0.50,
        market_quality={"state": True, "risk_multiplier": 1.0},
        ev_decision=decision,
        ev_net=net,
        ev_exit=_ev_exit(True),
        alpha_decision=_alpha_decision(),
    )

    assert state is False
    assert expected in detail


def test_entry_quality_gate_blocks_extreme_market_reduction():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "short",
        _cfg(),
        final_risk_multiplier=0.50,
        market_quality={"state": "reduced", "risk_multiplier": 0.20},
        ev_decision=_ev_decision(),
        ev_net=_ev_net(0.30),
        ev_exit=_ev_exit(True),
        alpha_decision=_alpha_decision(),
    )

    assert state is False
    assert "market quality x0.20<x0.30" in detail


def test_entry_quality_gate_blocks_profit_alpha_rejection():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "long",
        _cfg(),
        final_risk_multiplier=0.60,
        market_quality={"state": True, "risk_multiplier": 1.0},
        ev_decision=_ev_decision(),
        ev_net=_ev_net(0.32),
        ev_exit=_ev_exit(True),
        alpha_decision=_alpha_decision(
            allowed=False,
            score=61.0,
            probability=0.52,
            blockers=("derivatives adverse stack",),
        ),
    )

    assert state is False
    assert "Profit Alpha" in detail
    assert "derivatives adverse stack" in detail


def test_entry_quality_gate_uses_entry_edge_as_single_integrated_gate():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "long",
        _cfg(),
        final_risk_multiplier=0.60,
        market_quality={"state": True, "risk_multiplier": 1.0},
        ev_decision=_ev_decision(score=52.0, win_probability=0.50),
        ev_net=_ev_net(0.12),
        ev_exit=_ev_exit(True),
        alpha_decision=_alpha_decision(allowed=False, blockers=("legacy alpha fail",)),
        entry_edge_decision=_entry_edge_decision(),
    )

    assert state == "reduced"
    assert "Entry Edge TREND_CONTINUATION score 72.0" in detail
    assert "EV score" not in detail
    assert "Profit Alpha" not in detail


def test_entry_quality_gate_does_not_reapply_integrated_edge_thresholds():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "long",
        _cfg(),
        final_risk_multiplier=0.60,
        market_quality={"state": True, "risk_multiplier": 1.0},
        entry_edge_decision=_entry_edge_decision(
            allowed=True,
            score=67.0,
            probability=0.552,
            risk_multiplier=0.60,
        ),
    )

    assert state == "reduced"
    assert "Entry Edge TREND_CONTINUATION score 67.0" in detail
    assert "BLOCK" not in detail


def test_entry_quality_gate_blocks_entry_edge_rejection():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "short",
        _cfg(),
        final_risk_multiplier=0.62,
        market_quality={"state": True, "risk_multiplier": 1.0},
        ev_decision=_ev_decision(),
        ev_net=_ev_net(0.30),
        ev_exit=_ev_exit(True),
        alpha_decision=_alpha_decision(),
        entry_edge_decision=_entry_edge_decision(
            allowed=False,
            score=61.0,
            probability=0.52,
            blockers=("Entry Edge score 61.0<68.0",),
        ),
    )

    assert state is False
    assert "Entry Edge TREND_CONTINUATION not allowed" in detail
    assert "Entry Edge score 61.0<68.0" in detail
