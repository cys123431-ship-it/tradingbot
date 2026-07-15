from types import SimpleNamespace

import global_single_position_guard as guard


class DummyConfig:
    def __init__(self, config):
        self.config = config
        self.save_count = 0

    def save_config_sync(self):
        self.save_count += 1
        return True


def _engine(config):
    cfg = DummyConfig(config)
    return SimpleNamespace(ctrl=SimpleNamespace(cfg=cfg)), cfg


def test_opportunity_tuning_persists_changed_runtime_config():
    root = {
        "signal_engine": {
            "common_settings": {"scanner_enabled": False},
            "coin_selector": {
                "enabled": True,
                "analysis_limit": 20,
                "selection_max_rebound_pct": 18.0,
                "excluded_sectors": ["meme"],
            },
            "strategy_params": {
                "active_strategy": "UTBOT_ADAPTIVE",
                "UTBotFilteredBreakoutV1": {
                    "ev_min_entry_score": 72.0,
                    "ev_no_edge_relief_min_score": 80.0,
                    "ev_short_min_entry_score": 60.0,
                },
            },
        },
    }
    engine, cfg = _engine(root)

    assert guard.apply_opportunity_tuning(engine) is True

    signal = root["signal_engine"]
    selector = signal["coin_selector"]
    ut_cfg = signal["strategy_params"]["UTBotFilteredBreakoutV1"]
    assert cfg.save_count == 1
    assert signal["common_settings"]["scanner_enabled"] is True
    assert selector["analysis_limit"] == 80
    assert selector["selection_max_rebound_pct"] == 22.0
    assert "meme" not in selector["excluded_sectors"]
    assert ut_cfg["ev_min_entry_score"] == 62.0
    assert ut_cfg["entry_quality_gate_min_ev_score"] == 66.0
    assert ut_cfg["entry_quality_gate_min_profit_alpha_score"] == 68.0
    assert ut_cfg["profit_alpha_enabled"] is True
    assert ut_cfg["entry_edge_enabled"] is True
    assert ut_cfg["entry_edge_min_score"] == 68.0
    assert ut_cfg["entry_quality_gate_min_entry_edge_score"] == 68.0
    assert ut_cfg["market_regime_engine_enabled"] is True
    assert ut_cfg["data_quality_engine_enabled"] is True
    assert ut_cfg["execution_quality_engine_enabled"] is True
    assert ut_cfg["protection_health_execution_gate_enabled"] is True
    assert ut_cfg["overfit_governance_enabled"] is True
    assert ut_cfg["overfit_governance_hard_block_enabled"] is False
    assert ut_cfg["ev_no_edge_relief_min_score"] == 74.0
    assert ut_cfg["ev_short_min_entry_score"] == 62.0
    assert ut_cfg["ev_short_relaxed_signal_risk_cap"] == 0.25


def test_opportunity_tuning_does_not_rewrite_when_already_current():
    root = {
        "signal_engine": {
            "common_settings": {
                "scanner_enabled": True,
                "scanner_timeframe": "5m",
                "scanner_exit_timeframe": "15m",
                "scanner_min_rise_pct": 0.20,
                "scanner_max_rise_pct": 15.0,
            },
            "coin_selector": {
                "enabled": True,
                "analysis_limit": 80,
                "top_n": 20,
                "min_final_score": 45.0,
                "min_quote_volume_usdt": 25_000_000.0,
                "ideal_quote_volume_usdt": 250_000_000.0,
                "min_trade_count": 5_000,
                "ideal_trade_count": 120_000,
                "max_spread_pct": 0.12,
                "max_abs_price_change_pct": 24.0,
                "refresh_interval_seconds": 90,
                "candidate_cooldown_enabled": False,
                "custom_relax_discovery": True,
                "selection_quality_enabled": True,
                "selection_max_rebound_pct": 22.0,
                "excluded_sectors": [],
            },
            "strategy_params": {
                "active_strategy": "UTBOT_ADAPTIVE",
                "UTBotFilteredBreakoutV1": dict(guard.OPPORTUNITY_OVERRIDES),
            },
        },
    }
    engine, cfg = _engine(root)

    assert guard.apply_opportunity_tuning(engine) is False
    assert cfg.save_count == 0
