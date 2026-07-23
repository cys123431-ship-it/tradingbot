import json
from datetime import datetime, timezone

import emas


def test_utbreak_status_contract_keeps_todays_extended_limit():
    cfg = emas.apply_profit_opportunity_effective_overrides({})
    cfg["max_daily_trades"] = 10

    lines = emas.build_utbreakout_effective_status_contract(cfg, daily_entries=6)
    assert "일일 리스크: trades 6/10" in lines

    rendered = emas.enforce_utbreakout_effective_status_contract(
        "일일 리스크: trades 6/5",
        cfg,
        daily_entries=6,
    )
    assert "일일 리스크: trades 6/10" in rendered
    assert "trades 6/5" not in rendered


def test_legacy_persisted_ten_is_migrated_to_today_extension(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "signal_engine": {
                    "strategy_params": {
                        "UTBotFilteredBreakoutV1": {"max_daily_trades": 10}
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = emas.TradingConfig(str(path))
    signal_cfg = cfg.get("signal_engine", {})
    controls = signal_cfg["automatic_trading_controls"]
    raw_ut = signal_cfg["strategy_params"]["UTBotFilteredBreakoutV1"]

    assert controls["daily_trade_limit_extension_utc_date"] == datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")
    assert raw_ut["max_daily_trades"] == 5


def test_effective_strategy_limit_and_status_contract_agree_on_ten():
    engine = emas.SignalEngine.__new__(emas.SignalEngine)
    engine.ctrl = type(
        "ControllerStub",
        (),
        {"get_effective_automatic_daily_trade_limit": lambda self: 10},
    )()

    effective = engine._get_utbot_filtered_breakout_config(
        {
            "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
            "UTBotFilteredBreakoutV1": {"max_daily_trades": 5},
        }
    )
    assert effective["max_daily_trades"] == 10
    assert "일일 리스크: trades 5/10" in emas.build_utbreakout_effective_status_contract(
        effective,
        daily_entries=5,
    )
