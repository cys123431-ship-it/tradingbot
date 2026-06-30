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


def test_entry_quality_gate_allows_moderate_short_signal():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "short",
        _cfg(),
        final_risk_multiplier=0.42,
        market_quality={"state": "reduced", "risk_multiplier": 0.75},
        ev_decision=_ev_decision(),
        ev_net=_ev_net(0.24),
        ev_exit=_ev_exit(True),
    )

    assert state == "reduced"
    assert "PASS final x0.420>=x0.40" in detail


def test_entry_quality_gate_blocks_weak_long_final_multiplier():
    state, detail = _engine()._evaluate_utbreakout_entry_quality_gate(
        "long",
        _cfg(),
        final_risk_multiplier=0.36,
        market_quality={"state": "reduced", "risk_multiplier": 0.80},
        ev_decision=_ev_decision(),
        ev_net=_ev_net(0.30),
        ev_exit=_ev_exit(True),
    )

    assert state is False
    assert "final risk multiplier x0.360<x0.45" in detail


@pytest.mark.parametrize(
    "decision,net,expected",
    [
        (_ev_decision(score=55.0), _ev_net(0.30), "EV score 55.0<60.0"),
        (_ev_decision(win_probability=0.51), _ev_net(0.30), "EV p 0.51<0.54"),
        (_ev_decision(), _ev_net(0.12), "EV net 0.120R<0.22R"),
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
    )

    assert state is False
    assert "market quality x0.20<x0.30" in detail
