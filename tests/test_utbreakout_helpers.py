from datetime import datetime, timezone
import asyncio
import re

import math

import pandas as pd
import pytest

from utbreakout.indicators import (
    bollinger_width_percentile,
    keltner_squeeze_state,
    previous_donchian,
)
from utbreakout.research import summarize_diagnostic_events
from utbreakout.risk import calculate_risk_plan, normalize_risk_percent


def _signal_engine_cls():
    return pytest.importorskip("emas", reason="emas runtime dependencies are optional in CI").SignalEngine


def _emas_module():
    return pytest.importorskip("emas", reason="emas runtime dependencies are optional in CI")


def test_previous_donchian_excludes_current_candle():
    highs = [10, 11, 12, 13, 14, 999]
    lows = [9, 8, 7, 6, 5, -999]

    result = previous_donchian(highs, lows, 5)

    assert result["ready"] is True
    assert result["high"] == 14
    assert result["low"] == 5


def test_bollinger_width_percentile_handles_short_and_ready_data():
    short = bollinger_width_percentile([100.0] * 30, length=20, lookback=80)
    assert short["ready"] is False
    assert short["reason"] == "insufficient_data"

    closes = [100.0 + (idx * 0.08) + math.sin(idx / 5.0) for idx in range(140)]
    ready = bollinger_width_percentile(closes, length=20, lookback=80)

    assert ready["ready"] is True
    assert 0.0 <= ready["percentile"] <= 100.0
    assert ready["width_pct"] > 0


def test_keltner_squeeze_state_returns_bool_for_valid_series():
    closes = [100.0 + math.sin(idx / 8.0) * 0.25 for idx in range(60)]
    highs = [close + 0.35 for close in closes]
    lows = [close - 0.35 for close in closes]

    result = keltner_squeeze_state(highs, lows, closes)

    assert result["ready"] is True
    assert isinstance(result["squeeze_on"], bool)
    assert result["bb_width_pct"] >= 0
    assert result["kc_width_pct"] > 0


def test_rolling_orderflow_snapshot_ignores_non_finite_values():
    engine_cls = _signal_engine_cls()
    engine = engine_cls.__new__(engine_cls)
    engine.utbreakout_orderflow_snapshots = {}

    bad = {
        "timestamp": 1,
        "orderbook_imbalance_pct": float("nan"),
        "bid_depth_usdt": 10000,
        "ask_depth_usdt": 10000,
        "futures_spread_pct": 0.01,
        "best_bid": 99,
        "best_ask": 101,
    }
    engine._update_utbreakout_orderflow_snapshots("BTC/USDT", bad, window=5)
    for idx, imbalance in enumerate([2.0, 4.0, 7.0], start=2):
        engine._update_utbreakout_orderflow_snapshots(
            "BTC/USDT",
            {
                "timestamp": idx,
                "orderbook_imbalance_pct": imbalance,
                "bid_depth_usdt": 10000 + idx,
                "ask_depth_usdt": 9000,
                "futures_spread_pct": 0.01,
                "best_bid": 99,
                "best_ask": 101,
            },
            window=5,
        )

    context = engine._update_utbreakout_orderflow_snapshots("BTC/USDT", None, window=5)

    assert context["rolling_ofi_samples"] == 3
    assert context["rolling_orderbook_imbalance_pct"] == pytest.approx((2.0 + 4.0 + 7.0) / 3.0)
    assert context["rolling_orderbook_imbalance_delta"] == pytest.approx(5.0)
    assert context["rolling_ofi_score"] > 0


def test_open_interest_stats_are_safe_for_short_and_flat_history():
    engine_cls = _signal_engine_cls()
    engine = engine_cls.__new__(engine_cls)

    short = engine._calculate_utbreakout_open_interest_stats([{"sumOpenInterestValue": "100"}])
    flat = engine._calculate_utbreakout_open_interest_stats([
        {"timestamp": 1, "sumOpenInterestValue": "100"},
        {"timestamp": 2, "sumOpenInterestValue": "100"},
        {"timestamp": 3, "sumOpenInterestValue": "100"},
        {"timestamp": 4, "sumOpenInterestValue": "100"},
    ])
    varied = engine._calculate_utbreakout_open_interest_stats([
        {"timestamp": 1, "sumOpenInterestValue": "100"},
        {"timestamp": 2, "sumOpenInterestValue": "101"},
        {"timestamp": 3, "sumOpenInterestValue": "103"},
        {"timestamp": 4, "sumOpenInterestValue": "106"},
        {"timestamp": 5, "sumOpenInterestValue": "110"},
    ])

    assert short["open_interest_delta_pct"] is None
    assert flat["open_interest_delta_pct"] == pytest.approx(0.0)
    assert flat["open_interest_delta_z"] is None
    assert varied["open_interest_delta_pct"] is not None
    assert varied["open_interest_delta_z"] is not None
    assert varied["open_interest_acceleration"] is not None


def test_controller_futures_context_delegates_to_signal_engine():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    controller.is_upbit_mode = lambda: False

    class FakeSignal:
        async def _fetch_utbreakout_futures_context(self, symbol):
            assert symbol == "BTC/USDT"
            return {
                "rolling_orderbook_imbalance_pct": 4.5,
                "rolling_orderbook_imbalance_delta": 1.2,
                "rolling_ofi_score": 5.1,
                "open_interest_delta_z": 0.8,
                "open_interest_acceleration": 0.3,
            }

    controller.engines = {"signal": FakeSignal()}

    result = asyncio.run(controller._fetch_utbreakout_futures_context("BTC/USDT"))

    assert result["rolling_orderbook_imbalance_pct"] == 4.5
    assert result["open_interest_delta_z"] == 0.8


def test_controller_futures_context_fallback_returns_new_fields_without_network():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    controller.engines = {}
    controller.utbreakout_futures_context_cache = {}
    controller.utbreakout_orderflow_snapshots = {}
    controller.is_upbit_mode = lambda: False
    controller._build_binance_futures_rest_symbol = lambda symbol: "BTCUSDT"
    seen_requests = []

    async def fake_fetch(path, params):
        seen_requests.append((path, dict(params)))
        if path == "/fapi/v1/premiumIndex":
            return {
                "lastFundingRate": "0.0001",
                "nextFundingTime": "1800000000000",
                "markPrice": "101",
                "indexPrice": "100",
            }
        if path == "/fapi/v1/openInterest":
            return {"openInterest": "2500", "time": "1700000000000"}
        if path == "/futures/data/openInterestHist":
            return [
                {"timestamp": 1, "sumOpenInterestValue": "100"},
                {"timestamp": 2, "sumOpenInterestValue": "101"},
                {"timestamp": 3, "sumOpenInterestValue": "103"},
                {"timestamp": 4, "sumOpenInterestValue": "106"},
                {"timestamp": 5, "sumOpenInterestValue": "110"},
            ]
        if path == "/futures/data/globalLongShortAccountRatio":
            return [{"longShortRatio": "1.1", "longAccount": "0.52", "shortAccount": "0.48"}]
        if path == "/fapi/v1/depth":
            return {
                "bids": [["100", "10"], ["99", "5"]],
                "asks": [["101", "4"], ["102", "3"]],
            }
        if path == "/futures/data/takerlongshortRatio":
            return [{"buySellRatio": "1.04", "buyVol": "120", "sellVol": "100"}]
        raise AssertionError(path)

    controller._fetch_binance_public_json = fake_fetch

    result = asyncio.run(controller._fetch_utbreakout_futures_context("BTC/USDT"))

    assert result["open_interest_delta_pct"] is not None
    assert result["open_interest_delta_z"] is not None
    assert result["open_interest_acceleration"] is not None
    assert result["open_interest_hist_samples"] == 5
    assert result["rolling_ofi_samples"] == 1
    assert result["rolling_orderbook_imbalance_pct"] > 0
    assert result["rolling_ofi_score"] > 0
    assert result["taker_buy_sell_ratio"] == pytest.approx(1.04)
    assert any(path == "/fapi/v1/depth" for path, _ in seen_requests)
    assert any(
        path == "/futures/data/openInterestHist" and params["limit"] == 20
        for path, params in seen_requests
    )


def test_risk_plan_uses_loss_budget_not_fixed_margin():
    plan = calculate_risk_plan(
        side="long",
        entry_price=100.0,
        atr_value=2.0,
        stop_atr_multiplier=1.5,
        ut_stop=96.0,
        take_profit_r_multiple=2.0,
        min_risk_reward=2.0,
        balance_usdt=4000.0,
        risk_per_trade_percent=1.0,
        max_risk_per_trade_usdt=50.0,
        leverage=10.0,
    )

    assert plan["risk_distance"] == 4.0
    assert plan["risk_usdt"] == 40.0
    assert plan["qty"] == 10.0
    assert plan["planned_notional"] == 1000.0
    assert plan["planned_margin"] == 100.0
    assert plan["take_profit"] == 108.0


def test_risk_percent_normalization_clamps_legacy_live_defaults():
    assert normalize_risk_percent({"risk_per_trade_pct": 10.0, "max_risk_per_trade_pct": 100.0}) == 1.0
    assert normalize_risk_percent({"risk_per_trade_percent": 0.01}) == 0.05
    assert normalize_risk_percent({"risk_per_trade_percent": 0.5}) == 0.5


def test_trading_config_clamps_signal_futures_risk_defaults(tmp_path):
    emas = _emas_module()
    config_path = tmp_path / "config.json"
    cfg = emas.TradingConfig(str(config_path))
    common = cfg.config["signal_engine"]["common_settings"]
    assert common["risk_per_trade_pct"] == 0.5
    assert common["min_risk_per_trade_pct"] == 0.05
    assert common["max_risk_per_trade_pct"] == 1.0
    upbit_common = cfg.config["upbit"]["common_settings"]
    assert upbit_common["risk_per_trade_pct"] == 0.5
    assert upbit_common["min_risk_per_trade_pct"] == 0.05
    assert upbit_common["max_risk_per_trade_pct"] == 1.0
    dual_thrust = cfg.config["dual_thrust_engine"]
    dual_mode = cfg.config["dual_mode_engine"]
    assert dual_thrust["risk_per_trade_pct"] == 0.5
    assert dual_thrust["max_risk_per_trade_pct"] == 1.0
    assert dual_mode["risk_per_trade_pct"] == 0.5
    assert dual_mode["max_risk_per_trade_pct"] == 1.0

    config_path.write_text(
        (
            '{"api":{"exchange_mode":"binance_testnet"},'
            '"signal_engine":{"common_settings":{"risk_per_trade_pct":10,"max_risk_per_trade_pct":100}},'
            '"upbit":{"common_settings":{"risk_per_trade_pct":10,"max_risk_per_trade_pct":100}},'
            '"dual_thrust_engine":{"risk_per_trade_pct":50,"max_risk_per_trade_pct":100},'
            '"dual_mode_engine":{"risk_per_trade_pct":10,"max_risk_per_trade_pct":100}}'
        ),
        encoding="utf-8",
    )
    legacy_cfg = emas.TradingConfig(str(config_path))
    legacy_common = legacy_cfg.config["signal_engine"]["common_settings"]
    assert legacy_common["risk_per_trade_pct"] == 1.0
    assert legacy_common["max_risk_per_trade_pct"] == 1.0
    assert legacy_cfg.config["upbit"]["common_settings"]["risk_per_trade_pct"] == 1.0
    assert legacy_cfg.config["upbit"]["common_settings"]["max_risk_per_trade_pct"] == 1.0
    assert legacy_cfg.config["dual_thrust_engine"]["risk_per_trade_pct"] == 1.0
    assert legacy_cfg.config["dual_thrust_engine"]["max_risk_per_trade_pct"] == 1.0
    assert legacy_cfg.config["dual_mode_engine"]["risk_per_trade_pct"] == 1.0
    assert legacy_cfg.config["dual_mode_engine"]["max_risk_per_trade_pct"] == 1.0


def test_utbreakout_runtime_blocks_unsafe_live_and_emergency_sets():
    emas = _emas_module()
    engine_cls = _signal_engine_cls()

    class LiveController:
        def get_exchange_mode(self):
            return emas.BINANCE_MAINNET

    class TestnetController:
        def get_exchange_mode(self):
            return emas.BINANCE_TESTNET

    engine = engine_cls.__new__(engine_cls)
    engine.ctrl = LiveController()
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg["active_set_id"] = 1

    selected, _, reason = asyncio.run(engine._resolve_utbreakout_selected_set("BTC/USDT", pd.DataFrame(), cfg))

    assert selected["id"] == 22
    assert "Set1" in reason

    cfg["allow_ut_only_live_override"] = True
    selected, _, _ = asyncio.run(engine._resolve_utbreakout_selected_set("BTC/USDT", pd.DataFrame(), cfg))
    assert selected["id"] == 1

    engine.ctrl = TestnetController()
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg["active_set_id"] = 50
    selected, _, reason = asyncio.run(engine._resolve_utbreakout_selected_set("BTC/USDT", pd.DataFrame(), cfg))

    assert selected["id"] == 22
    assert "Set50" in reason

    cfg["emergency_mode"] = True
    selected, _, _ = asyncio.run(engine._resolve_utbreakout_selected_set("BTC/USDT", pd.DataFrame(), cfg))
    assert selected["id"] == 50


def test_utbreakout_set63_requires_volume_on_squeeze_release():
    emas = _emas_module()
    engine_cls = _signal_engine_cls()
    engine = engine_cls.__new__(engine_cls)
    cfg = emas.build_default_utbot_filtered_breakout_config()
    set_info = emas.get_utbreakout_set_definition(63)
    cfg.update(set_info["params"])
    values = {
        "bb_width_percentile": 20.0,
        "keltner_squeeze_on": False,
        "range_expansion_ratio": 1.10,
        "volume_ratio": 1.0,
        "entry_price": 105.0,
        "donchian_high_prev": 100.0,
        "donchian_low_prev": 90.0,
    }

    low_volume = engine._evaluate_utbreakout_set_filter_items("long", set_info, cfg, values)
    item = next(item for item in low_volume if item["code"] == "REJECTED_SQUEEZE_RELEASE")

    assert item["state"] is False
    assert "vol>=1.20" in item["detail"]

    values["volume_ratio"] = 1.25
    high_volume = engine._evaluate_utbreakout_set_filter_items("long", set_info, cfg, values)
    item = next(item for item in high_volume if item["code"] == "REJECTED_SQUEEZE_RELEASE")
    assert item["state"] is True


def test_research_summary_detects_set_concentration_and_protection_gaps():
    events = []
    for idx in range(6):
        events.append({
            "ts": datetime(2026, 1, 1, 0, idx, tzinfo=timezone.utc).isoformat(),
            "event": "rejected",
            "symbol": "BTC/USDT",
            "side": "long",
            "code": "REJECTED_ADX_LOW",
            "auto_selected_set_id": 2,
            "candidate_type": "bias_state",
            "decision_candle_ts": idx,
            "risk_usdt": 1.0,
            "risk_distance": 10.0,
            "entry_price": 1000.0,
            "planned_margin": 5.0,
            "planned_notional": 50.0,
        })
    events.append({
        "ts": datetime(2026, 1, 1, 0, 7, tzinfo=timezone.utc).isoformat(),
        "event": "accepted",
        "symbol": "BTC/USDT",
        "side": "short",
        "code": "ACCEPTED_ENTRY",
        "auto_selected_set_id": 7,
        "candidate_type": "fresh_signal",
        "decision_candle_ts": 7,
    })

    summary = summarize_diagnostic_events(
        events,
        protection_status={"BTC/USDT": {"missing_sl": True, "missing_tp": False}},
    )

    assert summary["top_set"] == "Set2"
    assert summary["top_set_share_pct"] > 50.0
    assert summary["top_rejects"][0] == ("REJECTED_ADX_LOW", 6)
    assert summary["protection_missing_sl"] == ["BTC/USDT"]


def _market(**overrides):
    base = {
        "symbol": "BTC/USDT:USDT",
        "quote": "USDT",
        "settle": "USDT",
        "swap": True,
        "active": True,
        "type": "swap",
        "info": {"contractType": "PERPETUAL", "status": "TRADING"},
    }
    base.update(overrides)
    return base


def test_coin_selector_market_lookup_prefers_futures_over_spot():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    spot = _market(
        symbol="BTC/USDT",
        settle=None,
        swap=False,
        type="spot",
        info={"status": "TRADING"},
    )
    futures = _market(symbol="BTC/USDT:USDT")
    markets = {
        "BTC/USDT": spot,
        "BTC/USDT:USDT": futures,
    }

    assert engine._coin_selector_market_for_symbol("BTC/USDT", markets) is futures


def test_custom_coin_symbol_resolution_uses_futures_canonical_symbol():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    markets = {
        "BTC/USDT": _market(
            symbol="BTC/USDT",
            settle=None,
            swap=False,
            type="spot",
            info={"status": "TRADING"},
        ),
        "BTC/USDT:USDT": _market(symbol="BTC/USDT:USDT"),
    }

    for raw in ["BTC", "BTCUSDT", "BTC/USDT", "BTC/USDT:USDT"]:
        assert engine._coin_selector_exchange_symbol_for_custom(raw, markets) == "BTC/USDT:USDT"

    assert engine._coin_selector_market_for_symbol("BTC/USDC", markets) is None


def test_controller_resolves_tradifi_watch_symbol_on_binance_mainnet():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    controller.cfg = {"api": {"exchange_mode": emas.BINANCE_MAINNET, "use_testnet": False}}
    markets = {
        "AAPL/USDT:USDT": _market(
            symbol="AAPL/USDT:USDT",
            info={"contractType": "TRADIFI_PERPETUAL", "status": "TRADING"},
        ),
    }

    assert controller._resolve_futures_watch_symbol_from_markets("AAPL", markets) == "AAPL/USDT:USDT"


def test_controller_blocks_tradifi_watch_symbol_on_testnet():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    controller.cfg = {"api": {"exchange_mode": emas.BINANCE_TESTNET, "use_testnet": True}}
    markets = {
        "AAPL/USDT:USDT": _market(
            symbol="AAPL/USDT:USDT",
            info={"contractType": "TRADIFI_PERPETUAL", "status": "TRADING"},
        ),
    }

    with pytest.raises(ValueError, match="메인넷"):
        controller._resolve_futures_watch_symbol_from_markets("AAPL", markets)


def test_coin_selector_tradifi_universe_only_auto_on_binance_mainnet():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.is_upbit_mode = lambda: False
    cfg = {"include_tradifi_universe": True}

    class MainnetCtrl:
        def get_exchange_mode(self):
            return emas.BINANCE_MAINNET

    class TestnetCtrl:
        def get_exchange_mode(self):
            return emas.BINANCE_TESTNET

    engine.ctrl = MainnetCtrl()
    assert engine._coin_selector_should_include_tradifi_universe(cfg, custom_enabled=False) is True
    assert engine._coin_selector_should_include_tradifi_universe(cfg, custom_enabled=True) is False

    engine.ctrl = TestnetCtrl()
    assert engine._coin_selector_should_include_tradifi_universe(cfg, custom_enabled=False) is False


def test_coin_selector_candidate_cooldown_counts_unique_decision_keys():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.coin_selector_candidate_cooldowns = {}
    cfg = {
        "candidate_cooldown_enabled": True,
        "candidate_cooldown_misses": 2,
        "candidate_cooldown_seconds": 60,
    }

    first = engine._record_coin_selector_candidate_outcome(
        "BTC/USDT:USDT",
        reason="no signal",
        cfg=cfg,
        now=100,
        decision_key="utbot:15m:1",
    )
    duplicate = engine._record_coin_selector_candidate_outcome(
        "BTC/USDT:USDT",
        reason="no signal",
        cfg=cfg,
        now=110,
        decision_key="utbot:15m:1",
    )
    remaining, _ = engine._coin_selector_cooldown_remaining("BTCUSDT", cfg, now=111)

    assert first["miss_count"] == 1
    assert duplicate["miss_count"] == 1
    assert remaining == 0

    cooled = engine._record_coin_selector_candidate_outcome(
        "BTC/USDT:USDT",
        reason="filter blocked",
        cfg=cfg,
        now=120,
        decision_key="utbot:15m:2",
    )
    remaining, state = engine._coin_selector_cooldown_remaining("BTC/USDT", cfg, now=130)

    assert cooled["cooldown_until"] == 180
    assert remaining == pytest.approx(50)
    assert state["last_reason"] == "filter blocked"


def test_coin_selector_candidate_cooldown_success_clears_state():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.coin_selector_candidate_cooldowns = {}
    cfg = {
        "candidate_cooldown_enabled": True,
        "candidate_cooldown_misses": 1,
        "candidate_cooldown_seconds": 60,
    }

    engine._record_coin_selector_candidate_outcome(
        "ETH/USDT:USDT",
        reason="entry call did not open position",
        cfg=cfg,
        now=100,
        decision_key="utbot:15m:1",
    )
    assert engine._coin_selector_cooldown_remaining("ETH/USDT", cfg, now=101)[0] > 0

    engine._record_coin_selector_candidate_outcome("ETH/USDT:USDT", accepted=True, cfg=cfg, now=102)

    assert engine._coin_selector_cooldown_remaining("ETH/USDT", cfg, now=103) == (0.0, None)
    assert engine.coin_selector_candidate_cooldowns == {}


def test_utbreakout_status_symbol_prefers_live_position_over_watchlist():
    emas = _emas_module()

    class _PositionEngine:
        scanner_active_symbol = "LAB/USDT"

        async def get_active_position_symbols(self, use_cache=True):
            return {"XRP/USDT"}

    controller = emas.MainController.__new__(emas.MainController)
    controller.engines = {emas.CORE_ENGINE: _PositionEngine()}
    controller.status_data = {}
    controller.is_upbit_mode = lambda: False
    controller.get_active_watchlist = lambda: ["LAB/USDT"]
    controller._get_current_symbol = lambda: "LAB/USDT"

    symbol = asyncio.run(controller._resolve_utbreakout_status_symbol())

    assert symbol == "XRP/USDT"


def test_utbreakout_status_symbol_uses_scanner_when_no_position():
    emas = _emas_module()

    class _ScannerEngine:
        scanner_active_symbol = "SOL/USDT"

        async def get_active_position_symbols(self, use_cache=True):
            return set()

    controller = emas.MainController.__new__(emas.MainController)
    controller.engines = {emas.CORE_ENGINE: _ScannerEngine()}
    controller.status_data = {"LAB/USDT": {"symbol": "LAB/USDT", "pos_side": "NONE"}}
    controller.is_upbit_mode = lambda: False
    controller.get_active_watchlist = lambda: ["LAB/USDT"]
    controller._get_current_symbol = lambda: "LAB/USDT"

    symbol = asyncio.run(controller._resolve_utbreakout_status_symbol())

    assert symbol == "SOL/USDT"


def test_main_keyboard_removes_utbot_button():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)

    keyboard = controller._build_main_keyboard()
    labels = [
        button.text
        for row in keyboard.keyboard
        for button in row
    ]

    assert "/utbreak" in labels
    assert "/setup" in labels
    assert "/utbot" not in labels


def test_legacy_utbot_command_routes_to_integrated_menu_handler():
    emas = _emas_module()

    assert re.match(emas.TELEGRAM_MENU_COMMAND_PATTERN, "/utbot")
    assert re.match(emas.TELEGRAM_MENU_COMMAND_PATTERN, "/utbot on")
    assert "/utbot" in emas.TELEGRAM_UTBREAK_INTEGRATED_COMMANDS


def test_utbreakout_visible_callback_actions_include_watchlist_button():
    emas = _emas_module()

    assert emas.UTBREAKOUT_VISIBLE_CALLBACK_ACTIONS == {"on", "off", "condition_status", "watchlist"}
    assert {"fixed", "auto_scan", "sets", "why", "entry_analyze"}.issubset(emas.UTBREAKOUT_CALLBACK_ACTIONS)


def test_setup_keyboard_keeps_exchange_button_choices():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)

    setup_keyboard = controller._build_setup_keyboard()
    network_keyboard = controller._build_setup_network_keyboard()
    setup_labels = [button.text for row in setup_keyboard.keyboard for button in row]
    network_labels = [button.text for row in network_keyboard.keyboard for button in row]

    assert "거래소/네트워크 전환" in setup_labels
    assert "나가기" in setup_labels
    assert "1. 바이낸스 테스트넷" in network_labels
    assert "2. 바이낸스 메인넷" in network_labels
    assert "3. 업비트 KRW 현물" in network_labels


def test_setup_button_labels_normalize_to_existing_number_flow():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)

    assert controller._normalize_setup_choice_text("거래소/네트워크 전환") == "22"
    assert controller._normalize_setup_choice_text("나가기") == "0"
    assert controller._normalize_setup_network_choice("1. 바이낸스 테스트넷") == "1"
    assert controller._normalize_setup_network_choice("2. 바이낸스 메인넷") == "2"
    assert controller._normalize_setup_network_choice("3. 업비트 KRW 현물") == "3"


def test_utbreakout_position_scan_context_shows_position_and_next_candidate():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.ctrl = type("Ctrl", (), {"status_data": {}})()
    engine.coin_selector_candidate_cooldowns = {}
    engine.scanner_active_symbol = None
    engine.last_utbot_filtered_breakout_status = {
        "XRP/USDT:USDT": {
            "auto_selected_set_id": 7,
            "auto_selected_set_name": "Conservative",
            "auto_selection_reason": "trend and liquidity aligned",
            "reason": "ACCEPTED_ENTRY: LONG Set7 confirmed",
        }
    }
    engine.coin_selector_last_result = {
        "selected": [
            {
                "normalized_symbol": "XRP/USDT",
                "exchange_symbol": "XRP/USDT:USDT",
                "selection_state": "SELECTED",
                "score": 80,
            },
            {
                "normalized_symbol": "ETH/USDT",
                "exchange_symbol": "ETH/USDT:USDT",
                "selection_state": "SELECTED",
                "score": 74.5,
                "auto_set_id": 12,
                "auto_set_name": "Momentum",
                "adaptive_tf": "15m",
                "auto_selection_reason": "breakout quality improved",
            },
        ]
    }
    engine._get_coin_selector_config = lambda: {
        "enabled": True,
        "candidate_cooldown_enabled": True,
        "candidate_cooldown_misses": 3,
        "candidate_cooldown_seconds": 1800,
    }
    engine._micro_auto_enabled = lambda: False

    async def _active_symbols(use_cache=True):
        return {"XRP/USDT"}

    async def _server_position(symbol, use_cache=True):
        return {
            "symbol": symbol,
            "side": "long",
            "contracts": 25,
            "entryPrice": 0.55,
            "unrealizedPnl": 12.5,
        }

    engine.get_active_position_symbols = _active_symbols
    engine.get_server_position = _server_position

    lines = asyncio.run(engine._build_utbreakout_position_scan_context_lines("XRP/USDT"))
    text = "\n".join(lines)

    assert "현재 포지션: XRP/USDT" in text
    assert "trend and liquidity aligned" in text
    assert "ACCEPTED_ENTRY: LONG Set7 confirmed" in text
    assert "다음 스캔 후보: ETH/USDT:USDT" in text
    assert "breakout quality improved" in text


class _MemoryConfig:
    def __init__(self):
        self.values = {}

    async def update_value(self, path, value):
        node = self.values
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value


class _TelegramConfig:
    def __init__(self, chat_id):
        self.chat_id = chat_id

    def get_chat_id(self):
        return self.chat_id


class _FakeTelegramChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeTelegramMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.documents = []

    async def reply_text(self, *args, **kwargs):
        self.replies.append((args, kwargs))

    async def reply_document(self, *args, **kwargs):
        self.documents.append((args, kwargs))


class _FakeTelegramMessageRejectsLongText(_FakeTelegramMessage):
    async def reply_text(self, *args, **kwargs):
        text = str(args[0]) if args else ""
        if len(text) > 4096:
            raise _emas_module().BadRequest("Message is too long")
        await super().reply_text(*args, **kwargs)


class _FakeTelegramUpdate:
    def __init__(self, chat_id, text):
        self.effective_chat = _FakeTelegramChat(chat_id)
        self.message = _FakeTelegramMessage(text)
        self.effective_message = self.message
        self.callback_query = None


def _telegram_controller(chat_id=12345):
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    controller.cfg = _TelegramConfig(chat_id)
    controller.is_paused = False
    controller.active_engine = None
    return controller


def test_telegram_update_requires_configured_chat_id():
    controller = _telegram_controller(chat_id=12345)

    assert controller._is_authorized_telegram_update(_FakeTelegramUpdate(12345, "/status")) is True
    assert controller._is_authorized_telegram_update(_FakeTelegramUpdate(99999, "/status")) is False


def test_telegram_global_handler_rejects_unauthorized_stop_without_emergency_call():
    controller = _telegram_controller(chat_id=12345)
    called = False

    async def emergency_stop():
        nonlocal called
        called = True

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(99999, "STOP")

    result = asyncio.run(controller.global_handler(update, None))

    emas = _emas_module()
    assert result == emas.ConversationHandler.END
    assert called is False
    assert len(update.message.replies) == 1


def test_telegram_long_text_reply_sends_preview_and_document():
    controller = _telegram_controller(chat_id=12345)
    message = _FakeTelegramMessage("/utbreak status")
    long_text = "\n".join(f"line {idx} " + ("x" * 90) for idx in range(80))

    asyncio.run(controller._reply_long_text_with_document(
        message,
        long_text,
        filename="utbreakout_condition_status.txt",
        caption="condition status",
        preview_suffix="상세 조건 스테이터스는 파일로 보냈습니다.",
    ))

    assert len(message.replies) == 1
    assert "상세 조건 스테이터스는 파일로 보냈습니다." in message.replies[0][0][0]
    assert len(message.documents) == 1
    assert message.documents[0][1]["filename"] == "utbreakout_condition_status.txt"
    assert message.documents[0][1]["document"].getvalue().decode("utf-8") == long_text


def test_telegram_markdown_safe_long_reply_falls_back_to_document():
    controller = _telegram_controller(chat_id=12345)
    message = _FakeTelegramMessageRejectsLongText("/status")
    long_text = "x" * 5000

    asyncio.run(controller._reply_markdown_safe(message, long_text))

    assert len(message.replies) == 1
    assert "상세 내용은 파일로 보냈습니다." in message.replies[0][0][0]
    assert len(message.documents) == 1
    assert message.documents[0][1]["filename"] == "telegram_message.txt"


def test_telegram_global_handler_requires_exact_emergency_text():
    controller = _telegram_controller(chat_id=12345)
    called = False

    async def emergency_stop():
        nonlocal called
        called = True

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(12345, "PLEASE STOP")

    result = asyncio.run(controller.global_handler(update, None))

    assert result is None
    assert called is False
    assert update.message.replies == []


def test_telegram_global_handler_accepts_authorized_exact_stop():
    controller = _telegram_controller(chat_id=12345)
    called = False

    async def emergency_stop():
        nonlocal called
        called = True

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(12345, "STOP")

    result = asyncio.run(controller.global_handler(update, None))

    emas = _emas_module()
    assert result == emas.ConversationHandler.END
    assert called is True
    assert len(update.message.replies) == 1


def test_telegram_global_handler_accepts_main_keyboard_stop_button():
    controller = _telegram_controller(chat_id=12345)
    called = False

    async def emergency_stop():
        nonlocal called
        called = True

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(12345, "🚨 STOP")

    result = asyncio.run(controller.global_handler(update, None))

    emas = _emas_module()
    assert result == emas.ConversationHandler.END
    assert called is True
    assert len(update.message.replies) == 1
    assert re.match(emas.TELEGRAM_EMERGENCY_PATTERN, "🚨 STOP")


def test_telegram_global_handler_reports_no_position_stop_result():
    controller = _telegram_controller(chat_id=12345)

    async def emergency_stop():
        return {"status": "no_position", "closed": 0, "failed": 0, "cancelled_orders": 0}

    controller.emergency_stop = emergency_stop
    update = _FakeTelegramUpdate(12345, "/stop")

    result = asyncio.run(controller.global_handler(update, None))

    emas = _emas_module()
    assert result == emas.ConversationHandler.END
    assert "청산할 오픈 포지션 없음" in update.message.replies[0][0][0]


class _EmergencyExchange:
    def __init__(self, positions):
        self.positions = list(positions)
        self.created = []
        self.cancel_all_requests = []

    def fetch_positions(self):
        return list(self.positions)

    def amount_to_precision(self, symbol, amount):
        return str(round(float(amount), 8)).rstrip("0").rstrip(".")

    def cancel_all_orders(self, symbol):
        self.cancel_all_requests.append(symbol)
        return []

    def _signed_position_amount(self, position):
        info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
        value = info.get("positionAmt")
        if value not in (None, ""):
            return float(value)
        contracts = float(position.get("contracts", 0) or 0)
        if str(position.get("side", "")).lower() == "short":
            return -abs(contracts)
        return abs(contracts)

    def _symbol_key(self, value):
        return str(value or "").upper().replace(":USDT", "").replace("/", "")

    def _apply_market_close(self, symbol, side, amount):
        close_amount = abs(float(amount or 0))
        if close_amount <= 0:
            return
        target_key = self._symbol_key(symbol)
        for position in self.positions:
            info = position.setdefault("info", {})
            pos_symbol = position.get("symbol") or info.get("symbol")
            if self._symbol_key(pos_symbol) != target_key:
                continue
            signed = self._signed_position_amount(position)
            if side == "buy":
                signed = min(0.0, signed + close_amount)
            elif side == "sell":
                signed = max(0.0, signed - close_amount)
            info["positionAmt"] = str(signed)
            position["contracts"] = abs(signed)
            position["side"] = "long" if signed > 0 else ("short" if signed < 0 else None)
            return

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = {
            "id": f"created-{len(self.created) + 1}",
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "params": dict(params or {}),
        }
        self.created.append(order)
        if str(order_type).lower() == "market" and params and params.get("reduceOnly"):
            self._apply_market_close(symbol, side, amount)
        return order


def _emergency_controller(positions):
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    exchange = _EmergencyExchange(positions)
    notices = []
    controller.exchange = exchange
    controller.engines = {}
    controller.active_engine = None
    controller.is_paused = False
    controller.is_upbit_mode = lambda: False
    controller.get_active_watchlist = lambda: ["BTC/USDT"]
    controller._get_current_symbol = lambda: "BTC/USDT"

    async def notify(message):
        notices.append(message)

    controller.notify = notify
    return controller, exchange, notices


def test_emergency_stop_closes_binance_position_amt_when_contracts_missing():
    controller, exchange, notices = _emergency_controller([
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": None,
            "side": None,
            "unrealizedPnl": "1.25",
            "info": {
                "symbol": "BTCUSDT",
                "positionAmt": "-0.25",
                "positionSide": "BOTH",
            },
        }
    ])

    result = asyncio.run(controller.emergency_stop())

    assert result["status"] == "closed"
    assert result["closed"] == 1
    assert result["failed"] == 0
    assert controller.is_paused is True
    assert exchange.created[0]["symbol"] == "BTC/USDT"
    assert exchange.created[0]["side"] == "buy"
    assert exchange.created[0]["amount"] == "0.25"
    assert exchange.created[0]["params"]["reduceOnly"] is True
    assert any("청산 완료" in notice for notice in notices)


def test_emergency_stop_retries_until_position_is_flat():
    class _PartialEmergencyExchange(_EmergencyExchange):
        def _apply_market_close(self, symbol, side, amount):
            fill_amount = float(amount) / 2 if len(self.created) == 1 else amount
            super()._apply_market_close(symbol, side, fill_amount)

    controller, exchange, notices = _emergency_controller([
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": "0.4",
            "side": "long",
            "unrealizedPnl": "0",
            "info": {"symbol": "BTCUSDT", "positionAmt": "0.4", "positionSide": "BOTH"},
        }
    ])
    exchange = _PartialEmergencyExchange(exchange.positions)
    controller.exchange = exchange

    result = asyncio.run(controller.emergency_stop())

    assert result["status"] == "closed"
    assert result["closed"] == 1
    assert len(exchange.created) == 2
    assert float(exchange.created[0]["amount"]) == 0.4
    assert float(exchange.created[1]["amount"]) == 0.2
    assert any("잔여 포지션" in notice for notice in notices)


def test_emergency_stop_fails_when_accepted_order_does_not_flatten_position():
    class _StickyEmergencyExchange(_EmergencyExchange):
        def _apply_market_close(self, symbol, side, amount):
            return

    controller, exchange, notices = _emergency_controller([
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": "0.3",
            "side": "long",
            "unrealizedPnl": "0",
            "info": {"symbol": "BTCUSDT", "positionAmt": "0.3", "positionSide": "BOTH"},
        }
    ])
    exchange = _StickyEmergencyExchange(exchange.positions)
    controller.exchange = exchange

    result = asyncio.run(controller.emergency_stop())

    assert result["status"] == "failed"
    assert result["closed"] == 0
    assert result["failed"] == 1
    assert len(exchange.created) == 5
    assert "still open" in result["failed_positions"][0]["error"]
    assert any("청산 실패" in notice for notice in notices)


class _ResettableSignalEngine:
    def __init__(self):
        self.scanner_active_symbol = "ETH/USDT"
        self.reset_kwargs = None

    def reset_signal_runtime_state(self, **kwargs):
        self.reset_kwargs = kwargs


def test_return_signal_engine_to_utbot_turns_off_utbreakout_customcoins_and_scanner():
    emas = _emas_module()
    controller = emas.MainController.__new__(emas.MainController)
    controller.cfg = _MemoryConfig()
    signal_engine = _ResettableSignalEngine()
    controller.engines = {"signal": signal_engine}

    asyncio.run(controller._return_signal_engine_to_utbot())

    signal_cfg = controller.cfg.values["signal_engine"]
    strategy = signal_cfg["strategy_params"]
    breakout = strategy["UTBotFilteredBreakoutV1"]
    assert strategy["active_strategy"] == "utbot"
    assert breakout["adaptive_timeframe_enabled"] is False
    assert breakout["auto_select_enabled"] is False
    assert breakout["selection_mode"] == "manual"
    assert signal_cfg["coin_selector"]["enabled"] is False
    assert signal_cfg["coin_selector"]["custom_universe_enabled"] is False
    assert signal_cfg["common_settings"]["scanner_enabled"] is False
    assert signal_engine.scanner_active_symbol is None
    assert signal_engine.reset_kwargs == {
        "reset_entry_cache": True,
        "reset_exit_cache": True,
        "reset_stateful_strategy": True,
    }


def test_rsibb_is_selectable_without_becoming_default(tmp_path):
    emas = _emas_module()

    cfg = emas.TradingConfig(str(tmp_path / "config.json"))
    assert cfg.config["signal_engine"]["strategy_params"]["active_strategy"] == "utbot"
    rsibb_cfg = cfg.config["signal_engine"]["strategy_params"]["RSIBB"]
    assert rsibb_cfg["rsibb_enabled"] is False
    assert rsibb_cfg["rsibb_paper_only"] is True
    assert "rsibb" in emas.CORE_STRATEGIES

    controller = emas.MainController.__new__(emas.MainController)
    controller.cfg = {
        "signal_engine": {
            "strategy_params": {
                "active_strategy": "rsibb",
                "RSIBB": dict(rsibb_cfg),
            }
        }
    }
    controller.is_upbit_mode = lambda: False

    assert controller.get_active_strategy_params()["active_strategy"] == "rsibb"


def test_rsibb_guard_blocks_default_and_paper_only_mainnet():
    emas = _emas_module()
    engine_cls = _signal_engine_cls()
    engine = engine_cls.__new__(engine_cls)

    class _Ctrl:
        def __init__(self, mode):
            self.mode = mode

        def get_exchange_mode(self):
            return self.mode

    engine.ctrl = _Ctrl(emas.BINANCE_TESTNET)
    allowed, reason = engine._rsibb_runtime_guard({"RSIBB": {}})
    assert allowed is False
    assert "rsibb_enabled=False" in reason

    engine.ctrl = _Ctrl(emas.BINANCE_MAINNET)
    allowed, reason = engine._rsibb_runtime_guard({
        "RSIBB": {
            "rsibb_enabled": True,
            "rsibb_paper_only": True,
            "rsibb_regime_guard_enabled": True,
        }
    })
    assert allowed is False
    assert "paper_only=True" in reason

    engine.ctrl = _Ctrl(emas.BINANCE_TESTNET)
    allowed, _ = engine._rsibb_runtime_guard({
        "RSIBB": {
            "rsibb_enabled": True,
            "rsibb_paper_only": True,
            "rsibb_regime_guard_enabled": True,
        }
    })
    assert allowed is True


def test_protection_order_classifies_binance_stop_market_from_orig_type():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    order = {
        "type": "market",
        "side": "sell",
        "info": {
            "type": "STOP_MARKET",
            "origType": "STOP_MARKET",
            "stopPrice": "78000",
            "reduceOnly": "true",
            "symbol": "BTCUSDT",
        },
    }

    assert signal_engine._classify_protection_order(engine, order) == "sl"
    assert signal_engine._protection_order_matches_symbol(engine, order, "BTC/USDT") is True


def test_protection_order_keeps_take_profit_separate_from_stop_loss():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    take_profit_market = {
        "type": "market",
        "side": "sell",
        "info": {
            "type": "TAKE_PROFIT_MARKET",
            "origType": "TAKE_PROFIT_MARKET",
            "stopPrice": "82000",
            "reduceOnly": "true",
        },
    }
    take_profit_limit = {"type": "limit", "side": "sell", "reduceOnly": True}

    assert signal_engine._classify_protection_order(engine, take_profit_market) == "tp"
    assert signal_engine._classify_protection_order(engine, take_profit_limit) == "tp"


def test_protection_order_classifies_bot_client_ids_even_without_reduce_only():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    take_profit = {"id": "tp", "type": "limit", "side": "buy", "clientOrderId": "utbtpBTCUSDTabc"}
    stop_loss = {"id": "sl", "type": "market", "side": "buy", "clientOrderId": "utbslBTCUSDTabc"}

    assert signal_engine._classify_protection_order(engine, take_profit) == "tp"
    assert signal_engine._classify_protection_order(engine, stop_loss) == "sl"


def test_protection_order_classifies_binance_stop_market_close_position_as_sl():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    order = {
        "id": "sl-stop-market",
        "side": "sell",
        "type": "STOP_MARKET",
        "closePosition": True,
        "stopPrice": "90",
        "info": {"symbol": "BTCUSDT", "origType": "STOP_MARKET", "closePosition": "true"},
    }

    assert signal_engine._classify_protection_order(engine, order) == "sl"


def test_protection_order_classifies_reduce_only_trigger_price_as_sl():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    order = {
        "id": "sl-trigger",
        "side": "sell",
        "type": "market",
        "reduceOnly": True,
        "triggerPrice": "90",
        "info": {"symbol": "BTCUSDT", "reduceOnly": "true"},
    }

    assert signal_engine._classify_protection_order(engine, order) == "sl"


def test_protection_tp_labels_from_client_ids():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    tp1 = {"id": "tp1", "type": "limit", "side": "sell", "reduceOnly": True, "clientOrderId": "utbtp1BTC"}
    tp2 = {"id": "tp2", "type": "limit", "side": "sell", "reduceOnly": True, "clientOrderId": "tp2BTC"}

    assert signal_engine._classify_protection_order(engine, tp1) == "tp"
    assert signal_engine._protection_tp_label(engine, tp1) == "TP1"
    assert signal_engine._classify_protection_order(engine, tp2) == "tp"
    assert signal_engine._protection_tp_label(engine, tp2) == "TP2"


class _DummyCtrl:
    def format_symbol_for_display(self, symbol):
        return symbol

    async def notify(self, message):
        self.messages = getattr(self, "messages", [])
        self.messages.append(message)
        self.last_message = message


class _FakeExchange:
    def __init__(self, orders, symbol_scope_returns=True, positions=None):
        self.orders = list(orders)
        self.positions = list(positions or [])
        self.cancelled = []
        self.cancel_all_requests = []
        self.created = []
        self.symbol_scope_returns = symbol_scope_returns

    def amount_to_precision(self, symbol, amount):
        return str(round(float(amount), 6))

    def price_to_precision(self, symbol, price):
        return str(round(float(price), 2))

    def fetch_open_orders(self, symbol=None):
        if symbol and not self.symbol_scope_returns:
            return []
        return list(self.orders)

    def fetch_positions(self, symbols=None):
        return list(self.positions)

    def cancel_all_orders(self, symbol):
        self.cancel_all_requests.append(symbol)
        self.orders = []
        return []

    def cancel_order(self, order_id, symbol):
        self.cancelled.append((str(order_id), symbol))
        self.orders = [
            order for order in self.orders
            if str(order.get("id") or order.get("info", {}).get("orderId")) != str(order_id)
        ]
        return {"id": order_id}

    def _symbol_key(self, value):
        return str(value or "").upper().replace(":USDT", "").replace("/", "")

    def _signed_position_amount(self, position):
        info = position.get("info", {}) if isinstance(position.get("info"), dict) else {}
        value = info.get("positionAmt")
        if value not in (None, ""):
            return float(value)
        contracts = float(position.get("contracts", 0) or 0)
        if str(position.get("side", "")).lower() == "short":
            return -abs(contracts)
        return abs(contracts)

    def _apply_market_close(self, symbol, side, amount):
        close_amount = abs(float(amount or 0))
        if close_amount <= 0:
            return
        target_key = self._symbol_key(symbol)
        for position in self.positions:
            info = position.setdefault("info", {})
            pos_symbol = position.get("symbol") or info.get("symbol")
            if self._symbol_key(pos_symbol) != target_key:
                continue
            signed = self._signed_position_amount(position)
            if side == "buy":
                signed = min(0.0, signed + close_amount)
            elif side == "sell":
                signed = max(0.0, signed - close_amount)
            info["positionAmt"] = str(signed)
            position["contracts"] = abs(signed)
            position["side"] = "long" if signed > 0 else ("short" if signed < 0 else None)
            return

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        params = dict(params or {})
        order_id = f"created-{len(self.created) + 1}"
        info = {
            "symbol": symbol.replace("/", "").replace(":USDT", ""),
            "reduceOnly": str(bool(params.get("reduceOnly", False))).lower(),
        }
        if str(order_type).lower() == "stop_market":
            info.update({
                "type": "STOP_MARKET",
                "origType": "STOP_MARKET",
                "stopPrice": params.get("stopPrice"),
            })
        order = {
            "id": order_id,
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "reduceOnly": bool(params.get("reduceOnly", False)),
            "clientOrderId": params.get("newClientOrderId"),
            "info": info,
            "params": params,
        }
        self.created.append(order)
        self.orders.append(order)
        if str(order_type).lower() == "market" and params.get("reduceOnly"):
            self._apply_market_close(symbol, side, amount)
        return order


def _protection_engine(orders, symbol_scope_returns=True, positions=None):
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.exchange = _FakeExchange(orders, symbol_scope_returns=symbol_scope_returns, positions=positions)
    engine.ctrl = _DummyCtrl()
    engine.last_protection_alert_ts = {}
    engine.last_protection_order_status = {}
    engine.protection_missing_candidates = {}
    engine.last_orphan_protection_sweep_ts = 0.0
    engine.orphan_protection_candidates = {}
    engine.ORPHAN_PROTECTION_SWEEP_INTERVAL = 10.0
    engine.position_cache = {}
    engine.POSITION_CACHE_TTL = 0.0
    engine.is_upbit_mode = lambda: False
    return engine


def test_protection_audit_fetch_failure_does_not_alert_missing_sl():
    class FailingOpenOrdersExchange(_FakeExchange):
        def fetch_open_orders(self, symbol=None):
            raise RuntimeError("temporary exchange outage")

    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])
    engine.exchange = FailingOpenOrdersExchange([], positions=[pos])

    status = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=pos, expected_tp=False, expected_sl=True, alert=True)
    )

    assert status["status"] == "OPEN_ORDERS_FETCH_FAILED"
    assert status["fetch_ok"] is False
    assert status["missing_sl"] is False
    assert not getattr(engine.ctrl, "messages", [])


def test_protection_audit_missing_sl_requires_two_confirmed_reads():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])
    engine.PROTECTION_MISSING_MIN_AGE_SEC = 0.0

    first = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=pos, expected_tp=False, expected_sl=True, alert=True)
    )
    assert first["status"] == "MISSING_SL"
    assert first["missing_confirmed"] is False
    assert not getattr(engine.ctrl, "messages", [])

    second = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=pos, expected_tp=False, expected_sl=True, alert=True)
    )
    assert second["status"] == "MISSING_SL"
    assert second["missing_confirmed"] is True
    assert "SL 없음" in engine.ctrl.messages[-1]
    assert "2회 연속" in engine.ctrl.messages[-1]


def test_get_server_position_matches_futures_symbol_and_position_amt():
    position = {
        "symbol": "BTC/USDT:USDT",
        "contracts": None,
        "side": None,
        "entryPrice": "100",
        "info": {"symbol": "BTCUSDT", "positionAmt": "-0.25", "positionSide": "BOTH"},
    }
    engine = _protection_engine([], positions=[position])

    pos_plain = asyncio.run(engine.get_server_position("BTC/USDT", use_cache=False))
    pos_swap = asyncio.run(engine.get_server_position("BTC/USDT:USDT", use_cache=False))

    assert pos_plain["symbol"] == "BTC/USDT"
    assert pos_plain["side"] == "short"
    assert pos_plain["contracts"] == 0.25
    assert pos_swap["side"] == "short"
    assert pos_swap["contracts"] == 0.25


def test_protection_audit_cancels_orphan_orders_even_when_symbol_fetch_misses_them():
    engine = _protection_engine(
        [
            {
                "id": "sl-old",
                "side": "buy",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "105",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        symbol_scope_returns=False,
    )

    status = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=None, expected_tp=False, expected_sl=False, alert=False)
    )

    assert status["status"] == "ORPHAN_CANCELLED"
    assert status["orphan_cancelled"] == 1
    assert engine.exchange.orders == []


def test_reconcile_closed_position_cancels_leftover_tp_and_sl_orders():
    engine = _protection_engine(
        [
            {
                "id": "tp-left",
                "side": "sell",
                "type": "limit",
                "clientOrderId": "utbtpBTCUSDTleft",
                "info": {"symbol": "BTCUSDT"},
            },
            {
                "id": "sl-left",
                "side": "sell",
                "type": "market",
                "clientOrderId": "utbslBTCUSDTleft",
                "info": {"origType": "STOP_MARKET", "stopPrice": "95", "symbol": "BTCUSDT"},
            },
        ],
        symbol_scope_returns=False,
    )

    async def _no_position(symbol, use_cache=False):
        return None

    engine.get_server_position = _no_position

    status = asyncio.run(
        engine._reconcile_closed_position_protection(
            "BTC/USDT",
            reason="tp/sl filled",
            alert=False,
            attempts=1,
        )
    )

    assert status["status"] == "ORPHAN_CANCELLED"
    assert status["orphan_cancelled"] == 2
    assert engine.exchange.orders == []


def test_global_orphan_sweep_cancels_leftover_stop_loss_without_tracked_symbol():
    engine = _protection_engine(
        [
            {
                "id": "sl-orphan",
                "side": "sell",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "95",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        positions=[],
    )

    status = asyncio.run(
        engine._cleanup_orphan_protection_orders(
            reason="test orphan sweep",
            alert=False,
            min_interval=0,
            confirm_delay_sec=0,
        )
    )

    assert status["status"] == "ORPHAN_CANCELLED"
    assert status["cancelled"] == 1
    assert status["symbols"]["BTC/USDT"]["cancelled"] == 1
    assert engine.exchange.orders == []


def test_global_orphan_sweep_keeps_orders_when_position_is_active():
    engine = _protection_engine(
        [
            {
                "id": "sl-active",
                "side": "sell",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "95",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        positions=[{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "0.1", "entryPrice": "100"}],
    )

    status = asyncio.run(
        engine._cleanup_orphan_protection_orders(
            reason="test active position sweep",
            alert=False,
            min_interval=0,
            confirm_delay_sec=0,
        )
    )

    assert status["status"] == "OK"
    assert status["cancelled"] == 0
    assert [order["id"] for order in engine.exchange.orders] == ["sl-active"]


def test_global_orphan_sweep_requires_confirmation_before_cancelling():
    engine = _protection_engine(
        [
            {
                "id": "sl-pending",
                "side": "sell",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "95",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ],
        positions=[],
    )

    status = asyncio.run(
        engine._cleanup_orphan_protection_orders(
            reason="test pending sweep",
            alert=False,
            min_interval=0,
            confirm_delay_sec=60,
        )
    )

    assert status["status"] == "PENDING_CONFIRMATION"
    assert status["pending"] == 1
    assert [order["id"] for order in engine.exchange.orders] == ["sl-pending"]


def test_cancel_protection_order_tries_raw_binance_symbol_variant():
    class _RawOnlyExchange(_FakeExchange):
        def cancel_order(self, order_id, symbol):
            if symbol != "BTCUSDT":
                raise ValueError(f"wrong symbol {symbol}")
            return super().cancel_order(order_id, symbol)

    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.exchange = _RawOnlyExchange(
        [
            {
                "id": "sl-raw",
                "side": "buy",
                "type": "market",
                "info": {
                    "origType": "STOP_MARKET",
                    "stopPrice": "105",
                    "reduceOnly": "true",
                    "symbol": "BTCUSDT",
                },
            }
        ]
    )
    engine.ctrl = _DummyCtrl()
    engine.last_protection_alert_ts = {}
    engine.last_protection_order_status = {}
    engine.is_upbit_mode = lambda: False

    cancelled = asyncio.run(engine._cancel_protection_orders("BTC/USDT", reason="raw symbol fallback"))

    assert cancelled == 1
    assert engine.exchange.cancelled == [("sl-raw", "BTCUSDT")]
    assert engine.exchange.orders == []


def test_protection_audit_deduplicates_short_stop_loss_orders():
    orders = [
        {
            "id": "sl-old",
            "side": "buy",
            "type": "market",
            "timestamp": 1000,
            "info": {"origType": "STOP_MARKET", "stopPrice": "105", "reduceOnly": "true", "symbol": "BTCUSDT"},
        },
        {
            "id": "sl-new",
            "side": "buy",
            "type": "market",
            "timestamp": 2000,
            "info": {"origType": "STOP_MARKET", "stopPrice": "106", "reduceOnly": "true", "symbol": "BTCUSDT"},
        },
        {
            "id": "tp",
            "side": "buy",
            "type": "limit",
            "price": "90",
            "reduceOnly": True,
            "info": {"symbol": "BTCUSDT"},
        },
    ]
    engine = _protection_engine(orders)
    pos = {"side": "short", "contracts": 1, "entryPrice": 100}

    status = asyncio.run(
        engine._audit_protection_orders("BTC/USDT", pos=pos, expected_tp=True, expected_sl=True, alert=False)
    )

    assert status["status"] == "DUPLICATE_CANCELLED"
    assert status["duplicate_cancelled"] == 1
    remaining_ids = {order["id"] for order in engine.exchange.orders}
    assert remaining_ids == {"sl-new", "tp"}


def test_utbreakout_defaults_enable_fixed_tp_ladder_and_disable_runner():
    emas = _emas_module()

    cfg = emas.build_default_utbot_filtered_breakout_config()

    assert cfg["fixed_take_profit_enabled"] is True
    assert cfg["partial_take_profit_enabled"] is True
    assert cfg["partial_take_profit_r_multiple"] == 1.5
    assert cfg["partial_take_profit_ratio"] == 0.5
    assert cfg["second_take_profit_enabled"] is True
    assert cfg["second_take_profit_r_multiple"] == 2.0
    assert cfg["second_take_profit_ratio"] == 0.5
    assert cfg["atr_trailing_enabled"] is False
    assert cfg["atr_trailing_multiplier"] == 2.0
    assert cfg["atr_trailing_activation_r"] == 1.5
    assert cfg["short_conservative_enabled"] is True
    assert cfg["short_risk_multiplier"] == 0.5
    assert cfg["short_adx_threshold"] == 25.0
    assert cfg["short_dmi_min_gap"] == 4.0
    assert cfg["short_require_htf_supertrend"] is True
    assert cfg["short_require_entry_ema_downtrend"] is True
    assert cfg["short_require_momentum_downtrend"] is True
    assert cfg["bias_continuation_enabled"] is True
    assert cfg["bias_continuation_risk_multiplier"] == 0.65
    assert cfg["bias_continuation_15m_risk_multiplier"] == 0.5
    assert cfg["bias_continuation_15m_max_signal_age_candles"] == 3
    assert cfg["bias_continuation_min_adx"] == 18.0
    assert cfg["bias_continuation_15m_min_adx"] == 20.0
    assert cfg["bias_continuation_min_volume_ratio"] == 0.75
    assert cfg["bias_continuation_15m_min_volume_ratio"] == 0.80
    assert cfg["bias_continuation_max_extension_atr"] == 1.60
    assert cfg["bias_continuation_15m_max_extension_atr"] == 1.50
    assert cfg["bias_continuation_min_adaptive_tf_score"] == 42.0
    assert cfg["bias_continuation_15m_min_adaptive_tf_score"] == 50.0
    assert cfg["quality_score_v2_enabled"] is True
    assert cfg["quality_score_v2_block_below"] == 60.0
    assert cfg["quality_score_v2_reduce_below"] == 70.0
    assert cfg["quality_score_v2_min_risk_multiplier"] == 0.5
    assert cfg["quality_score_v2_long_block_below"] == 50.0
    assert cfg["quality_score_v2_long_reduce_below"] == 60.0
    assert cfg["quality_score_v2_long_15m_block_below"] == 50.0
    assert cfg["quality_score_v2_long_15m_reduce_below"] == 60.0
    assert cfg["quality_score_v2_short_15m_block_below"] == 70.0
    assert cfg["dynamic_take_profit_enabled"] is True
    assert cfg["dynamic_tp2_base_r_multiple"] == 2.0
    assert cfg["dynamic_tp2_strong_r_multiple"] == 2.5
    assert cfg["dynamic_tp2_elite_r_multiple"] == 3.0
    assert cfg["tp1_breakeven_enabled"] is True
    assert cfg["tp1_breakeven_wait_for_partial"] is True
    assert cfg["market_quality_enabled"] is True
    assert cfg["market_quality_data_required"] is False
    assert cfg["market_quality_min_risk_multiplier"] == 0.25
    assert cfg["shadow_triple_barrier_enabled"] is True
    assert cfg["adaptive_exit_enabled"] is True
    assert cfg["volatility_targeting_enabled"] is True
    assert cfg["volatility_target_atr_pct"] == 1.0
    assert cfg["meta_labeling_enabled"] is True
    assert cfg["short_asymmetry_enabled"] is True
    assert cfg["shadow_runner_exit_enabled"] is False
    assert cfg["runner_exit_enabled"] is False
    assert cfg["runner_chandelier_enabled"] is False
    assert cfg["runner_chandelier_multiplier"] == 2.4
    assert cfg["trend_health_enabled"] is True
    assert cfg["aggressive_growth_enabled"] is False
    assert cfg["aggressive_growth_balance_sleeve_pct"] == 0.20
    assert cfg["aggressive_growth_max_trade_risk_pct"] == 0.015


def test_utbreakout_short_guard_requires_htf_and_dmi_alignment():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = {
        "short_conservative_enabled": True,
        "short_adx_threshold": 25.0,
        "short_dmi_min_gap": 4.0,
        "short_require_htf_supertrend": True,
        "short_require_entry_ema_downtrend": True,
        "short_require_momentum_downtrend": True,
    }

    ok, reason = engine._utbreakout_short_guard_passes(
        cfg,
        {
            "htf_close": 95,
            "htf_ema_fast": 90,
            "htf_ema_slow": 100,
            "adx": 25,
            "plus_di": 12,
            "minus_di": 28,
            "htf_supertrend_direction": "short",
            "entry_price": 88,
            "ema50": 90,
            "ema50_prev": 91,
            "momentum_6_pct": -1.2,
            "momentum_12_pct": -2.4,
        },
    )
    assert ok is True
    assert reason == "short guard passed"

    ok, reason = engine._utbreakout_short_guard_passes(
        cfg,
        {
            "htf_close": 105,
            "htf_ema_fast": 110,
            "htf_ema_slow": 100,
            "adx": 18,
            "plus_di": 30,
            "minus_di": 20,
            "htf_supertrend_direction": "long",
            "entry_price": 108,
            "ema50": 105,
            "ema50_prev": 104,
            "momentum_6_pct": 1.2,
            "momentum_12_pct": 2.4,
        },
    )
    assert ok is False
    assert "ADX" in reason
    assert "-DI > +DI" in reason


def test_utbreakout_short_guard_status_item_matches_real_gate():
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = {
        "short_conservative_enabled": True,
        "short_risk_multiplier": 0.5,
        "short_adx_threshold": 25.0,
        "short_dmi_min_gap": 4.0,
    }

    label, state, detail = engine._build_utbreakout_short_guard_status_item(
        cfg,
        {
            "htf_close": 105,
            "htf_ema_fast": 110,
            "htf_ema_slow": 100,
            "adx": 18,
            "plus_di": 30,
            "minus_di": 20,
        },
    )

    assert label == "보수적 숏 가드"
    assert state is False
    assert "ADX" in detail
    assert "숏 리스크 x0.50" in detail


def _bias_continuation_engine_and_cfg():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg["entry_timeframe"] = "15m"
    cfg["adaptive_timeframe_enabled"] = True
    return engine, cfg


def _passing_bias_continuation_values():
    return {
        "entry_price": 100.0,
        "open": 99.6,
        "ema50": 99.7,
        "ema50_prev": 99.4,
        "ema200": 98.0,
        "vwap": 99.8,
        "bb_mid": 99.6,
        "adx": 28.0,
        "plus_di": 31.0,
        "minus_di": 16.0,
        "atr_pct": 0.8,
        "volume_ratio": 1.2,
        "range_expansion_ratio": 1.1,
        "donchian_high_prev": 103.0,
        "keltner_upper": 101.0,
        "bb_upper": 102.0,
        "htf_ready": True,
        "htf_close": 100.0,
        "htf_ema_fast": 99.0,
        "htf_ema_slow": 98.0,
    }


def test_utbreakout_bias_continuation_passes_recent_aligned_15m_state():
    engine, cfg = _bias_continuation_engine_and_cfg()

    result = engine._evaluate_utbreakout_bias_continuation(
        "long",
        cfg,
        {
            "candidate_type": "bias_state",
            "decision_candle_ts": 3 * 900_000,
            "ut_signal_ts": 1 * 900_000,
            "adaptive_timeframe_decision": {"selected_score": 70.0},
        },
        _passing_bias_continuation_values(),
        {"id": 7},
    )

    assert result["state"] is True
    assert result["risk_multiplier"] == 0.5
    assert result["signal_age_candles"] == 2.0
    assert result["extension_atr"] < 1.0


def test_utbreakout_bias_continuation_rejects_stale_15m_state():
    engine, cfg = _bias_continuation_engine_and_cfg()

    result = engine._evaluate_utbreakout_bias_continuation(
        "long",
        cfg,
        {
            "candidate_type": "bias_state",
            "decision_candle_ts": 6 * 900_000,
            "ut_signal_ts": 1 * 900_000,
            "adaptive_timeframe_decision": {"selected_score": 70.0},
        },
        _passing_bias_continuation_values(),
        {"id": 7},
    )

    assert result["state"] is False
    assert "stale" in result["summary"]


def test_utbreakout_bias_continuation_treats_overextension_as_long_soft_filter():
    engine, cfg = _bias_continuation_engine_and_cfg()
    values = _passing_bias_continuation_values()
    values.update({
        "entry_price": 105.0,
        "open": 104.8,
        "ema50": 100.0,
        "ema50_prev": 99.7,
        "vwap": 99.8,
        "bb_mid": 100.5,
        "atr_pct": 0.5,
    })

    result = engine._evaluate_utbreakout_bias_continuation(
        "long",
        cfg,
        {
            "candidate_type": "bias_state",
            "decision_candle_ts": 3 * 900_000,
            "ut_signal_ts": 1 * 900_000,
            "adaptive_timeframe_decision": {"selected_score": 70.0},
        },
        values,
        {"id": 7},
    )

    assert result["state"] is True
    assert "extension" in "; ".join(result["reasons"])
    assert result["soft_pass_count"] >= 3


def test_utbreakout_bias_continuation_long_soft_failures_reduce_risk_without_blocking():
    engine, cfg = _bias_continuation_engine_and_cfg()
    values = _passing_bias_continuation_values()
    values.update({
        "entry_price": 105.0,
        "open": 104.8,
        "ema50": 100.0,
        "ema50_prev": 99.7,
        "vwap": 99.8,
        "bb_mid": 100.5,
        "adx": 15.0,
        "volume_ratio": 0.60,
        "atr_pct": 0.5,
    })

    result = engine._evaluate_utbreakout_bias_continuation(
        "long",
        cfg,
        {
            "candidate_type": "bias_state",
            "decision_candle_ts": 3 * 900_000,
            "ut_signal_ts": 1 * 900_000,
            "adaptive_timeframe_decision": {"selected_score": 45.0},
        },
        values,
        {"id": 7},
    )

    assert result["state"] == "reduced"
    assert result["soft_pass_count"] == 2
    assert result["risk_multiplier"] == pytest.approx(0.325)
    assert "soft misses" in result["summary"]


def test_utbreakout_quality_score_v2_blocks_weak_confluence_and_reduces_mixed():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg["entry_timeframe"] = "15m"

    blocked = engine._build_utbreakout_quality_score_v2(
        "long",
        cfg,
        {"candidate_type": "fresh_signal", "entry_timeframe": "15m", "adaptive_timeframe_decision": {"selected_score": 50}},
        {},
        trend_health={"score": 0, "risk_multiplier": 0.35},
        strategy_quality={"score": 0, "risk_multiplier": 0.35},
        market_quality={"state": True, "risk_multiplier": 1.0},
        selector_quality={"score": 70},
    )
    assert blocked["state"] is False
    assert blocked["risk_multiplier"] == 0

    reduced = engine._build_utbreakout_quality_score_v2(
        "long",
        cfg,
        {"candidate_type": "fresh_signal", "entry_timeframe": "15m", "adaptive_timeframe_decision": {"selected_score": 70}},
        {},
        trend_health={"score": 64, "risk_multiplier": 0.7},
        strategy_quality={"score": 66, "risk_multiplier": 0.7},
        market_quality={"state": True, "risk_multiplier": 1.0},
        selector_quality={"score": 76},
    )
    assert reduced["state"] == "reduced"
    assert 0 < reduced["risk_multiplier"] < 1


def test_utbreakout_quality_score_v2_uses_stricter_short_thresholds():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg["entry_timeframe"] = "15m"
    status = {
        "candidate_type": "fresh_signal",
        "entry_timeframe": "15m",
        "adaptive_timeframe_decision": {"selected_score": 65},
    }

    common_kwargs = {
        "trend_health": {"score": 60, "risk_multiplier": 0.7},
        "strategy_quality": {"score": 60, "risk_multiplier": 0.7},
        "market_quality": {"state": "reduced", "risk_multiplier": 0.5},
        "selector_quality": {"score": 70},
    }

    long_result = engine._build_utbreakout_quality_score_v2("long", cfg, status, {}, **common_kwargs)
    short_result = engine._build_utbreakout_quality_score_v2("short", cfg, status, {}, **common_kwargs)

    assert long_result["state"] == "reduced"
    assert short_result["state"] is False
    assert short_result["block_below"] > long_result["block_below"]


def test_utbreakout_dynamic_tp2_expands_only_on_strong_confluence():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()

    base = engine._build_utbreakout_dynamic_tp2(
        "long",
        cfg,
        {"score": 69},
        trend_health={"score": 80},
        strategy_quality={"score": 80},
    )
    assert base["second_take_profit_r_multiple"] == 2.0
    assert base["tier"] == "base"

    strong = engine._build_utbreakout_dynamic_tp2(
        "long",
        cfg,
        {"score": 76},
        trend_health={"score": 72},
        strategy_quality={"score": 71},
    )
    assert strong["second_take_profit_r_multiple"] == 2.5
    assert strong["tier"] == "strong"

    elite = engine._build_utbreakout_dynamic_tp2(
        "long",
        cfg,
        {"score": 86},
        trend_health={"score": 80},
        strategy_quality={"score": 79},
    )
    assert elite["second_take_profit_r_multiple"] == 3.0
    assert elite["tier"] == "elite"


def test_utbreakout_market_quality_reduces_risk_without_blocking_on_mild_funding():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()

    result = engine._evaluate_utbreakout_market_quality(
        "long",
        cfg,
        {
            "atr_pct": 0.5,
            "funding_rate": 0.0007,
            "open_interest_delta_pct": 0.3,
            "taker_buy_sell_ratio": 1.04,
            "futures_spread_pct": 0.02,
        },
    )

    assert result["state"] == "reduced"
    assert 0 < result["risk_multiplier"] < 1
    assert result["hard_block"] is False
    assert "funding" in result["summary"]


def test_utbreakout_market_quality_reduces_extreme_long_risk_without_blocking():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()

    result = engine._evaluate_utbreakout_market_quality(
        "long",
        cfg,
        {
            "atr_pct": 11.0,
            "funding_rate": 0.002,
            "futures_spread_pct": 0.02,
            "market_regime_context": {
                "items": {
                    "BTC/USDT": {
                        "direction": "short",
                        "return_lookback_pct": -2.0,
                    },
                },
            },
        },
    )

    assert result["state"] == "reduced"
    assert result["risk_multiplier"] > 0
    assert result["hard_block"] is False
    assert result["summary"].startswith("REDUCE")


def test_utbreakout_market_quality_blocks_extreme_short_adverse_funding():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()

    result = engine._evaluate_utbreakout_market_quality(
        "short",
        cfg,
        {
            "atr_pct": 0.5,
            "funding_rate": -0.002,
            "futures_spread_pct": 0.02,
        },
    )

    assert result["state"] is False
    assert result["risk_multiplier"] == 0
    assert result["hard_block"] is True
    assert result["summary"].startswith("BLOCK")


def test_utbreakout_market_quality_status_item_shows_reduced_state():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    cfg = emas.build_default_utbot_filtered_breakout_config()

    label, state, detail = engine._build_utbreakout_market_quality_status_item(
        "short",
        cfg,
        {
            "atr_pct": 0.6,
            "taker_buy_sell_ratio": 1.10,
            "futures_spread_pct": 0.02,
        },
    )

    assert label == "시장 품질 게이트"
    assert state == "reduced"
    assert "REDUCE" in detail


def test_utbreakout_shadow_candidate_resolves_to_diagnostic_event():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.utbreakout_shadow_pending = {}
    engine.utbreakout_shadow_resolved_keys = set()
    captured = []

    def _capture(symbol, status, event=None, extra=None):
        captured.append((symbol, status, event, extra))

    engine._record_utbreakout_diagnostic_event = _capture
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg["shadow_runner_exit_enabled"] = False
    plan = {
        "entry_price": 100,
        "stop_loss": 95,
        "take_profit": 110,
        "risk_distance": 5,
        "rr_multiple": 2.0,
        "decision_candle_ts": 1000,
        "entry_timeframe": "15m",
        "htf_timeframe": "1h",
    }

    pending = engine._register_utbreakout_shadow_candidate(
        "BTC/USDT",
        "long",
        {"decision_candle_ts": 1000},
        plan,
        cfg,
        {"id": 2, "name": "UT + ATR guard"},
    )
    assert pending is not None

    closed = pd.DataFrame(
        [
            {"timestamp": 1000, "open": 100, "high": 101, "low": 99, "close": 100},
            {"timestamp": 2000, "open": 100, "high": 111, "low": 100, "close": 110},
        ]
    )
    resolved = engine._update_utbreakout_shadow_triple_barrier("BTC/USDT", closed, cfg)

    assert resolved[0]["shadow_outcome"] == "tp"
    assert captured[0][2] == "shadow_outcome"
    assert captured[0][3]["code"] == "SHADOW_TP"
    assert engine.utbreakout_shadow_pending == {}


def test_utbreakout_shadow_candidate_logs_runner_diagnostic_event():
    emas = _emas_module()
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.utbreakout_shadow_pending = {}
    engine.utbreakout_shadow_resolved_keys = set()
    engine.utbreakout_runner_stats_cache = {}
    captured = []

    def _capture(symbol, status, event=None, extra=None):
        captured.append((symbol, status, event, extra))

    engine._record_utbreakout_diagnostic_event = _capture
    cfg = emas.build_default_utbot_filtered_breakout_config()
    cfg.update({
        "shadow_runner_exit_enabled": True,
        "shadow_runner_max_bars": 8,
        "shadow_triple_barrier_max_bars": 8,
        "atr_length": 2,
        "partial_take_profit_r_multiple": 1.0,
        "partial_take_profit_ratio": 0.35,
        "atr_trailing_activation_r": 1.0,
        "runner_chandelier_lookback": 3,
        "runner_structure_lookback": 2,
        "runner_dynamic_multiplier_enabled": False,
    })
    plan = {
        "entry_price": 100,
        "stop_loss": 95,
        "take_profit": 110,
        "risk_distance": 5,
        "rr_multiple": 2.0,
        "decision_candle_ts": 1000,
        "entry_timeframe": "15m",
        "htf_timeframe": "1h",
    }

    pending = engine._register_utbreakout_shadow_candidate(
        "BTC/USDT",
        "long",
        {"decision_candle_ts": 1000},
        plan,
        cfg,
        {"id": 2, "name": "UT + ATR guard"},
    )
    assert pending is not None

    rows = [{"timestamp": 1000, "open": 100, "high": 101, "low": 99, "close": 100}]
    for idx in range(1, 9):
        close = 100 + idx * 1.2
        rows.append({
            "timestamp": 1000 + idx * 1000,
            "open": close - 0.4,
            "high": close + 1.5,
            "low": close - 1.0,
            "close": close,
        })
    closed = pd.DataFrame(rows)

    resolved = engine._update_utbreakout_shadow_triple_barrier("BTC/USDT", closed, cfg)

    event_names = [item[2] for item in captured]
    assert "shadow_outcome" in event_names
    assert "runner_shadow_outcome" in event_names
    assert any(str(extra["code"]).startswith("SHADOW_RUNNER_") for extra in resolved)
    assert engine.utbreakout_shadow_pending == {}


def test_place_tp_sl_orders_uses_partial_tp_quantity_and_full_sl_quantity():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "2", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])

    async def _get_position(symbol, use_cache=False):
        return pos

    engine.get_server_position = _get_position

    asyncio.run(
        engine._place_tp_sl_orders(
            "BTC/USDT",
            "long",
            100,
            "2",
            tp_distance=15,
            sl_distance=10,
            tp_qty_ratio=0.5,
        )
    )

    stop_order = next(order for order in engine.exchange.created if order["type"] == "stop_market")
    tp_order = next(order for order in engine.exchange.created if order["type"] == "limit")
    assert float(stop_order["amount"]) == 2.0
    assert float(stop_order["params"]["stopPrice"]) == 90.0
    assert float(tp_order["amount"]) == 1.0
    assert float(tp_order["price"]) == 115.0


def test_place_tp_sl_orders_can_place_utbreakout_split_tp_ladder():
    emas = _emas_module()
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "2", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY
    }

    async def _get_position(symbol, use_cache=False):
        return pos

    engine.get_server_position = _get_position

    asyncio.run(
        engine._place_tp_sl_orders(
            "BTC/USDT",
            "long",
            100,
            "2",
            sl_distance=10,
            tp_targets=[
                {"label": "TP1", "kind": "tp1", "distance": 15, "qty_ratio": 0.5},
                {"label": "TP2", "kind": "tp2", "distance": 20, "qty_ratio": 0.5},
            ],
        )
    )

    tp_orders = [order for order in engine.exchange.created if order["type"] == "limit"]
    stop_order = next(order for order in engine.exchange.created if order["type"] == "stop_market")
    assert [float(order["amount"]) for order in tp_orders] == [1.0, 1.0]
    assert [float(order["price"]) for order in tp_orders] == [115.0, 120.0]
    assert float(stop_order["amount"]) == 2.0
    assert float(stop_order["params"]["stopPrice"]) == 90.0
    assert engine.last_protection_order_status["BTC/USDT"]["tp_count"] == 2
    assert engine.last_protection_order_status["BTC/USDT"]["tp1_present"] is True
    assert engine.last_protection_order_status["BTC/USDT"]["tp2_present"] is True
    assert engine.last_protection_order_status["BTC/USDT"]["sl_count"] == 1


def test_place_tp_sl_orders_makes_final_tp_residual_after_precision():
    class FloorPrecisionExchange(_FakeExchange):
        def amount_to_precision(self, symbol, amount):
            return str(math.floor(float(amount) * 1000) / 1000)

    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])
    engine.exchange = FloorPrecisionExchange([], positions=[pos])
    engine.get_runtime_strategy_params = lambda: {"active_strategy": _emas_module().UTBOT_FILTERED_BREAKOUT_STRATEGY}

    asyncio.run(
        engine._place_tp_sl_orders(
            "BTC/USDT",
            "long",
            100,
            "1",
            sl_distance=10,
            tp_targets=[
                {"label": "TP1", "kind": "tp1", "distance": 15, "qty_ratio": 0.3333},
                {"label": "TP2", "kind": "tp2", "distance": 20, "qty_ratio": 0.6667},
            ],
        )
    )

    tp_orders = [order for order in engine.exchange.created if order["type"] == "limit"]
    assert [float(order["amount"]) for order in tp_orders] == [0.333, 0.667]
    assert sum(float(order["amount"]) for order in tp_orders) == pytest.approx(1.0)


def test_protection_audit_marks_tp1_only_as_missing_tp2():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "2", "entryPrice": "100"}
    engine = _protection_engine(
        [
            {
                "id": "tp1-existing",
                "side": "sell",
                "type": "limit",
                "price": "115",
                "amount": "1",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT", "reduceOnly": "true"},
            },
            {
                "id": "sl-existing",
                "side": "sell",
                "type": "stop_market",
                "amount": "2",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT", "origType": "STOP_MARKET", "stopPrice": "90", "reduceOnly": "true"},
            },
        ],
        positions=[pos],
    )
    state = {
        "side": "long",
        "entry_price": 100.0,
        "initial_qty": 2.0,
        "planned_tp_orders": [
            {"tp_label": "TP1", "tp_name": "TP1", "side": "sell", "price": 115.0, "qty": 1.0},
            {"tp_label": "TP2", "tp_name": "TP2", "side": "sell", "price": 120.0, "qty": 1.0},
        ],
    }
    engine.utbreakout_trailing_states = {"BTC/USDT": state}

    status = asyncio.run(
        engine._audit_protection_orders(
            "BTC/USDT",
            pos=pos,
            expected_tp=True,
            expected_sl=True,
            planned_tp_orders=state["planned_tp_orders"],
            alert=False,
        )
    )

    assert status["status"] == "MISSING_TP2"
    assert status["tp1_present"] is True
    assert status["tp2_present"] is False
    assert status["missing_tp2"] is True


def test_missing_tp2_repair_recreates_planned_residual_order():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "2", "entryPrice": "100"}
    engine = _protection_engine(
        [
            {
                "id": "tp1-existing",
                "side": "sell",
                "type": "limit",
                "price": "115",
                "amount": "1",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT", "reduceOnly": "true"},
            },
            {
                "id": "sl-existing",
                "side": "sell",
                "type": "stop_market",
                "amount": "2",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT", "origType": "STOP_MARKET", "stopPrice": "90", "reduceOnly": "true"},
            },
        ],
        positions=[pos],
    )
    state = {
        "side": "long",
        "entry_price": 100.0,
        "initial_qty": 2.0,
        "last_stop_price": 90.0,
        "planned_tp_orders": [
            {"tp_label": "TP1", "tp_name": "TP1", "side": "sell", "price": 115.0, "qty": 1.0},
            {"tp_label": "TP2", "tp_name": "TP2", "side": "sell", "price": 120.0, "qty": 1.0},
        ],
    }
    engine.utbreakout_trailing_states = {"BTC/USDT": state}

    result = asyncio.run(
        engine._audit_and_repair_live_ladder_protection(
            "BTC/USDT",
            pos,
            state,
            {"min_notional_usdt": 0.0},
            reason="test missing TP2",
        )
    )

    assert result["status"] == "MISSING_TP2"
    assert result["tp2_repair"]["status"] == "TP2_REPAIRED"
    created_tp2 = [order for order in engine.exchange.created if order["type"] == "limit" and order["side"] == "sell"][-1]
    assert float(created_tp2["amount"]) == 1.0
    assert float(created_tp2["price"]) == 120.0
    assert "tp2" in created_tp2["clientOrderId"].lower()


def test_place_tp_sl_orders_uses_position_amt_for_short_futures_symbol():
    pos = {
        "symbol": "BTC/USDT:USDT",
        "contracts": None,
        "side": None,
        "entryPrice": "100",
        "info": {"symbol": "BTCUSDT", "positionAmt": "-2", "positionSide": "BOTH"},
    }
    engine = _protection_engine([], positions=[pos])

    asyncio.run(
        engine._place_tp_sl_orders(
            "BTC/USDT:USDT",
            "short",
            101,
            "1",
            tp_distance=10,
            sl_distance=5,
        )
    )

    stop_order = next(order for order in engine.exchange.created if order["type"] == "stop_market")
    tp_order = next(order for order in engine.exchange.created if order["type"] == "limit")
    assert stop_order["side"] == "buy"
    assert float(stop_order["amount"]) == 2.0
    assert float(stop_order["params"]["stopPrice"]) == 105.0
    assert tp_order["side"] == "buy"
    assert float(tp_order["amount"]) == 2.0
    assert float(tp_order["price"]) == 90.0


def test_utbreakout_split_tp_short_side_prices_and_labels_audit_ok():
    pos = {"symbol": "BTC/USDT:USDT", "side": "short", "contracts": "2", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])
    engine.get_runtime_strategy_params = lambda: {"active_strategy": _emas_module().UTBOT_FILTERED_BREAKOUT_STRATEGY}

    asyncio.run(
        engine._place_tp_sl_orders(
            "BTC/USDT",
            "short",
            100,
            "2",
            sl_distance=10,
            tp_targets=[
                {"label": "TP1", "kind": "tp1", "distance": 15, "qty_ratio": 0.5},
                {"label": "TP2", "kind": "tp2", "distance": 20, "qty_ratio": 0.5},
            ],
        )
    )

    tp_orders = [order for order in engine.exchange.created if order["type"] == "limit"]
    assert [order["side"] for order in tp_orders] == ["buy", "buy"]
    assert [float(order["price"]) for order in tp_orders] == [85.0, 80.0]
    status = engine.last_protection_order_status["BTC/USDT"]
    assert status["status"] == "OK"
    assert status["tp1_present"] is True
    assert status["tp2_present"] is True


def test_place_tp_sl_orders_emergency_closes_when_stop_loss_creation_fails():
    emas = _emas_module()

    class StopFailingExchange(_FakeExchange):
        def __init__(self, orders, symbol_scope_returns=True, positions=None):
            super().__init__(orders, symbol_scope_returns=symbol_scope_returns, positions=positions)
            self.stop_attempts = 0

        def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            if str(order_type).lower() == "stop_market":
                self.stop_attempts += 1
                raise RuntimeError("stop rejected")
            return super().create_order(symbol, order_type, side, amount, price, params)

    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "2", "entryPrice": "100"}
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.exchange = StopFailingExchange([], positions=[pos])
    engine.ctrl = _DummyCtrl()
    engine.last_protection_alert_ts = {}
    engine.last_protection_order_status = {}
    engine.last_orphan_protection_sweep_ts = 0.0
    engine.orphan_protection_candidates = {}
    engine.ORPHAN_PROTECTION_SWEEP_INTERVAL = 10.0
    engine.position_cache = {}
    engine.POSITION_CACHE_TTL = 0.0
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": emas.UTBOT_FILTERED_BREAKOUT_STRATEGY,
        "UTBotFilteredBreakoutV1": {
            "sl_place_max_retries": 2,
            "sl_retry_delay_sec": 0.0,
            "emergency_close_on_sl_fail": True,
        },
    }

    asyncio.run(
        engine._place_tp_sl_orders(
            "BTC/USDT",
            "long",
            100,
            "2",
            tp_distance=15,
            sl_distance=10,
            tp_qty_ratio=0.5,
        )
    )

    market_orders = [order for order in engine.exchange.created if order["type"] == "market"]
    assert engine.exchange.stop_attempts == 2
    assert len(market_orders) == 1
    assert market_orders[0]["side"] == "sell"
    assert market_orders[0]["params"]["reduceOnly"] is True
    assert float(market_orders[0]["amount"]) == 2.0
    assert float(engine.exchange.positions[0]["contracts"]) == 0.0
    assert engine.last_protection_order_status["BTC/USDT"]["emergency_close_status"] == "EMERGENCY_CLOSED"


def test_emergency_close_failure_sets_critical_paused_state():
    class MarketFailingExchange(_FakeExchange):
        def create_order(self, symbol, order_type, side, amount, price=None, params=None):
            if str(order_type).lower() == "market":
                raise RuntimeError("market rejected")
            return super().create_order(symbol, order_type, side, amount, price, params)

    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "2", "entryPrice": "100"}
    signal_engine = _signal_engine_cls()
    engine = signal_engine.__new__(signal_engine)
    engine.exchange = MarketFailingExchange([], positions=[pos])
    engine.ctrl = _DummyCtrl()
    engine.ctrl.is_paused = False
    engine.last_protection_alert_ts = {}
    engine.last_protection_order_status = {}
    engine.last_orphan_protection_sweep_ts = 0.0
    engine.orphan_protection_candidates = {}
    engine.ORPHAN_PROTECTION_SWEEP_INTERVAL = 10.0
    engine.position_cache = {}
    engine.POSITION_CACHE_TTL = 0.0
    engine.is_upbit_mode = lambda: False
    engine.get_runtime_strategy_params = lambda: {
        "active_strategy": _emas_module().UTBOT_FILTERED_BREAKOUT_STRATEGY,
        "UTBotFilteredBreakoutV1": {},
    }

    status = asyncio.run(
        engine._emergency_close_position_without_stop_loss(
            "BTC/USDT",
            reason="test emergency failure",
            max_attempts=1,
        )
    )

    assert status["status"] == "CRITICAL_PAUSED"
    assert status["emergency_close_status"] == "EMERGENCY_CLOSE_FAILED"
    assert engine.ctrl.is_paused is True
    assert "Emergency close failed" in engine.critical_pause_reason


def test_utbreakout_trailing_replaces_sl_and_keeps_partial_tp_order():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine(
        [
            {
                "id": "tp-existing",
                "side": "sell",
                "type": "limit",
                "price": "115",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT"},
            },
            {
                "id": "sl-old",
                "side": "sell",
                "type": "market",
                "clientOrderId": "utbslBTCUSDTold",
                "info": {"origType": "STOP_MARKET", "stopPrice": "90", "reduceOnly": "true", "symbol": "BTCUSDT"},
            },
        ],
        positions=[pos],
    )
    engine.utbreakout_trailing_states = {
        "BTC/USDT": {
            "side": "long",
            "entry_price": 100.0,
            "initial_qty": 2.0,
            "remaining_ratio": 0.5,
            "risk_distance": 10.0,
            "activation_r": 1.5,
            "trailing_atr_multiplier": 1.0,
            "breakeven_enabled": True,
            "last_stop_price": 90.0,
            "active": False,
        }
    }
    rows = []
    for idx in range(25):
        close = 100 + idx * 1.5
        rows.append({
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        })
    df = pd.DataFrame(rows)

    state = asyncio.run(
        engine._manage_utbreakout_partial_trailing(
            "BTC/USDT",
            pos,
            df,
            {
                "atr_length": 14,
                "atr_trailing_enabled": True,
                "atr_trailing_multiplier": 1.0,
                "atr_trailing_breakeven_enabled": True,
            },
        )
    )

    assert state["active"] is True
    assert ("sl-old", "BTC/USDT") in engine.exchange.cancelled
    remaining_ids = {order["id"] for order in engine.exchange.orders}
    assert "tp-existing" in remaining_ids
    assert "sl-old" not in remaining_ids
    assert any(order["type"] == "stop_market" for order in engine.exchange.created)
    assert engine.last_protection_order_status["BTC/USDT"]["tp_expected"] is True
    assert engine.last_protection_order_status["BTC/USDT"]["sl_count"] == 1


def test_utbreakout_tp1_breakeven_replaces_sl_even_when_runner_is_off():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine(
        [
            {
                "id": "tp2-existing",
                "side": "sell",
                "type": "limit",
                "price": "125",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT"},
            },
            {
                "id": "sl-old",
                "side": "sell",
                "type": "market",
                "clientOrderId": "utbslBTCUSDTold",
                "info": {"origType": "STOP_MARKET", "stopPrice": "90", "reduceOnly": "true", "symbol": "BTCUSDT"},
            },
        ],
        positions=[pos],
    )
    engine.utbreakout_trailing_states = {
        "BTC/USDT": {
            "side": "long",
            "entry_price": 100.0,
            "initial_qty": 2.0,
            "remaining_ratio": 0.5,
            "risk_distance": 10.0,
            "activation_r": 1.5,
            "atr_trailing_enabled": False,
            "tp1_breakeven_enabled": True,
            "tp1_breakeven_trigger_r": 1.5,
            "tp1_breakeven_offset_r": 0.03,
            "tp1_breakeven_wait_for_partial": True,
            "tp1_breakeven_qty_tolerance": 0.08,
            "last_stop_price": 90.0,
            "active": False,
            "breakeven_armed": False,
        }
    }
    rows = []
    for idx in range(25):
        close = 100 + idx * 0.9
        rows.append({
            "open": close - 0.4,
            "high": close + 0.8,
            "low": close - 0.8,
            "close": close,
        })
    df = pd.DataFrame(rows)

    state = asyncio.run(
        engine._manage_utbreakout_partial_trailing(
            "BTC/USDT",
            pos,
            df,
            {
                "atr_length": 14,
                "atr_trailing_enabled": False,
                "tp1_breakeven_enabled": True,
                "tp1_breakeven_offset_r": 0.03,
                "tp1_breakeven_wait_for_partial": True,
            },
        )
    )

    assert state["active"] is True
    assert state["breakeven_armed"] is True
    assert state["runner_mode"] == "tp1_breakeven"
    assert ("sl-old", "BTC/USDT") in engine.exchange.cancelled
    stop_order = next(order for order in engine.exchange.created if order["type"] == "stop_market")
    assert float(stop_order["params"]["stopPrice"]) == 100.3
    remaining_ids = {order["id"] for order in engine.exchange.orders}
    assert "tp2-existing" in remaining_ids
    assert engine.last_protection_order_status["BTC/USDT"]["sl_count"] == 1


def test_utbreakout_trailing_does_not_create_duplicate_sl_when_cancel_does_not_clear():
    class _StickyStopExchange(_FakeExchange):
        def cancel_order(self, order_id, symbol):
            self.cancelled.append((str(order_id), symbol))
            return {"id": order_id}

    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine(
        [
            {
                "id": "tp-existing",
                "side": "sell",
                "type": "limit",
                "price": "115",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT"},
            },
            {
                "id": "sl-old",
                "side": "sell",
                "type": "market",
                "clientOrderId": "utbslBTCUSDTold",
                "info": {"origType": "STOP_MARKET", "stopPrice": "90", "reduceOnly": "true", "symbol": "BTCUSDT"},
            },
        ],
        positions=[pos],
    )
    engine.exchange = _StickyStopExchange(engine.exchange.orders, positions=[pos])
    engine.PROTECTION_REPLACE_CONFIRM_ATTEMPTS = 1
    engine.PROTECTION_REPLACE_CONFIRM_DELAY = 0
    engine.utbreakout_trailing_states = {
        "BTC/USDT": {
            "side": "long",
            "entry_price": 100.0,
            "initial_qty": 2.0,
            "remaining_ratio": 0.5,
            "risk_distance": 10.0,
            "activation_r": 1.5,
            "trailing_atr_multiplier": 1.0,
            "breakeven_enabled": True,
            "last_stop_price": 90.0,
            "active": False,
        }
    }
    rows = []
    for idx in range(25):
        close = 100 + idx * 1.5
        rows.append({
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        })
    df = pd.DataFrame(rows)

    state = asyncio.run(
        engine._manage_utbreakout_partial_trailing(
            "BTC/USDT",
            pos,
            df,
            {
                "atr_length": 14,
                "atr_trailing_enabled": True,
                "atr_trailing_multiplier": 1.0,
                "atr_trailing_breakeven_enabled": True,
            },
        )
    )

    assert state is None
    assert ("sl-old", "BTC/USDT") in engine.exchange.cancelled
    assert engine.exchange.created == []
    assert engine.utbreakout_trailing_states["BTC/USDT"]["last_stop_price"] == 90.0
    remaining_sl = [
        order for order in engine.exchange.orders
        if engine._classify_protection_order(order) == "sl"
    ]
    assert [order["id"] for order in remaining_sl] == ["sl-old"]


def test_ladder_fill_state_repairs_sl_and_tp2_to_current_residual_qty():
    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine(
        [
            {
                "id": "tp2-old",
                "side": "sell",
                "type": "limit",
                "price": "120",
                "amount": "0.5",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT", "reduceOnly": "true"},
            },
            {
                "id": "sl-old",
                "side": "sell",
                "type": "stop_market",
                "amount": "2",
                "reduceOnly": True,
                "info": {"symbol": "BTCUSDT", "origType": "STOP_MARKET", "stopPrice": "90", "reduceOnly": "true"},
            },
        ],
        positions=[pos],
    )
    state = {
        "advanced_live_ladder_state": True,
        "side": "long",
        "entry_price": 100.0,
        "initial_qty": 2.0,
        "risk_distance": 10.0,
        "initial_stop_price": 90.0,
        "last_stop_price": 90.0,
        "planned_tp_orders": [
            {"tp_label": "TP1", "tp_name": "TP1", "side": "sell", "price": 115.0, "qty": 1.0, "filled": False},
            {"tp_label": "TP2", "tp_name": "TP2", "side": "sell", "price": 120.0, "qty": 1.0, "filled": False},
        ],
        "tp1_filled": False,
        "tp2_filled": False,
        "sl_moved_to_be": False,
        "sl_moved_to_tp1_area": False,
    }
    engine.utbreakout_trailing_states = {"BTC/USDT": state}

    updated = asyncio.run(engine._refresh_ladder_fill_state("BTC/USDT", pos, state, {"min_notional_usdt": 0.0}))

    assert updated["tp1_filled"] is True
    assert ("sl-old", "BTC/USDT") in engine.exchange.cancelled
    assert ("tp2-old", "BTC/USDT") in engine.exchange.cancelled
    new_sl = [order for order in engine.exchange.created if order["type"] == "stop_market"][-1]
    new_tp2 = [order for order in engine.exchange.created if order["type"] == "limit" and "tp2" in order["clientOrderId"].lower()][-1]
    assert float(new_sl["amount"]) == 1.0
    assert float(new_tp2["amount"]) == 1.0
    assert float(new_tp2["price"]) == 120.0


def test_tp2_fallback_close_requires_enabled_option():
    class TickerExchange(_FakeExchange):
        def fetch_ticker(self, symbol):
            return {"last": 111.0, "bid": 110.5, "ask": 111.5}

    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])
    engine.exchange = TickerExchange([], positions=[pos])
    state = {
        "side": "long",
        "entry_price": 100.0,
        "initial_qty": 1.0,
        "planned_tp_orders": [
            {"tp_label": "TP2", "tp_name": "TP2", "side": "sell", "price": 110.0, "qty": 1.0},
        ],
        "tp2_filled": False,
        "tp2_fallback_reached_loops": 0,
    }
    engine.utbreakout_trailing_states = {"BTC/USDT": state}

    result = asyncio.run(engine._maybe_tp2_fallback_close("BTC/USDT", pos, state, {}, audit_status={"status": "MISSING_TP2"}))

    assert result["status"] == "DISABLED"
    assert engine.exchange.created == []


def test_tp2_fallback_close_executes_after_confirmed_reached_loops():
    class TickerExchange(_FakeExchange):
        def fetch_ticker(self, symbol):
            return {"last": 111.0, "bid": 110.5, "ask": 111.5}

    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine([], positions=[pos])
    engine.exchange = TickerExchange([], positions=[pos])
    state = {
        "side": "long",
        "entry_price": 100.0,
        "initial_qty": 1.0,
        "planned_tp_orders": [
            {"tp_label": "TP2", "tp_name": "TP2", "side": "sell", "price": 110.0, "qty": 1.0},
        ],
        "tp2_filled": False,
        "tp2_fallback_reached_loops": 0,
    }
    engine.utbreakout_trailing_states = {"BTC/USDT": state}
    cfg = {"enable_tp2_fallback_close": True, "tp2_fallback_confirm_loops": 2, "tp2_fallback_use_market": True}

    first = asyncio.run(engine._maybe_tp2_fallback_close("BTC/USDT", pos, state, cfg, audit_status={"status": "MISSING_TP2"}))
    second = asyncio.run(engine._maybe_tp2_fallback_close("BTC/USDT", pos, state, cfg, audit_status={"status": "MISSING_TP2"}))

    assert first["status"] == "CONFIRMING"
    assert second["status"] == "TP2_FALLBACK_CLOSED"
    market_order = [order for order in engine.exchange.created if order["type"] == "market"][-1]
    assert market_order["side"] == "sell"
    assert market_order["params"]["reduceOnly"] is True
    assert float(engine.exchange.positions[0]["contracts"]) == 0.0


def test_replace_stop_loss_aborts_when_open_order_fetch_fails():
    class _FetchFailExchange(_FakeExchange):
        def fetch_open_orders(self, symbol=None):
            raise RuntimeError("open orders unavailable")

    pos = {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "1", "entryPrice": "100"}
    engine = _protection_engine(
        [
            {
                "id": "sl-old",
                "side": "sell",
                "type": "market",
                "clientOrderId": "utbslBTCUSDTold",
                "info": {"origType": "STOP_MARKET", "stopPrice": "90", "reduceOnly": "true", "symbol": "BTCUSDT"},
            }
        ],
        positions=[pos],
    )
    engine.exchange = _FetchFailExchange(engine.exchange.orders, positions=[pos])
    engine.PROTECTION_REPLACE_CONFIRM_ATTEMPTS = 1
    engine.PROTECTION_REPLACE_CONFIRM_DELAY = 0

    order = asyncio.run(
        engine._replace_stop_loss_order(
            "BTC/USDT",
            pos,
            stop_price=95,
            reason="test fetch failure",
        )
    )

    assert order is None
    assert engine.exchange.created == []
    assert engine.last_protection_order_status["BTC/USDT"]["status"] == "SL_REPLACE_FETCH_FAILED"
