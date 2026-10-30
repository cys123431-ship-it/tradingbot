"""SMA3/SMA120 /macross: A~E features, minimum-one rule, Telegram menu, backtest."""
import asyncio

import pytest

from btc_ma_cross_plus.backtest import all_feature_modes, cfg_for_mode, compare_modes, run_backtest
from btc_ma_cross_plus.config import (
    FEATURES,
    normalize_ma_cross_config,
    feature_mode,
)
from btc_ma_cross_plus.strategy import evaluate
from btc_ma_cross_plus.tradfi import market_in_tradfi_scope

BAR_MS = 4 * 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % BAR_MS)


def rows_from(closes):
    rows = []
    for i, close in enumerate(closes):
        rows.append([T0 + i * BAR_MS, close, close * 1.004, close * 0.996, close, 100.0])
    return rows


def trend_series(segments, start=100.0):
    """segments: [(bars, drift_per_bar)] -> closing prices."""
    closes, price = [], start
    for bars, drift in segments:
        for _ in range(bars):
            price = max(1.0, price + drift)
            closes.append(price)
    return closes


LONG_THEN_SHORT = trend_series([(200, 0.0), (80, 1.0), (80, -1.0), (80, 0.0)])


# ------------------------------------------------------------------- config
def test_config_defaults_and_normalization():
    cfg = normalize_ma_cross_config({})
    assert cfg["fast_period"] == 3 and cfg["slow_period"] == 120
    assert cfg["timeframe"] == "4h"
    assert cfg["scan_scope"] == "btc"
    assert cfg["enabled"] is False
    # C-only strategy: C is always on, A/B/D/E always off, no emergency stop.
    assert feature_mode(cfg) == "C"
    assert cfg["emergency_stop_roi_percent"] == 0
    assert cfg["locking"] is True
    for lev, expected in ((2, 2), (3, 3), (5, 5), (0, 5), (9, 5), (20, 5)):
        assert normalize_ma_cross_config({"leverage": lev})["leverage"] == expected


def test_c_only_is_pinned_against_old_configs():
    stale = normalize_ma_cross_config({
        "feature_a_trend_filter": 1, "feature_b_atr_stop": 1,
        "feature_d_chandelier": 1, "feature_e_donchian": 1,
        "emergency_stop_roi_percent": 30, "locking": False, "feature_c_vol_sizing": 0,
    })
    assert feature_mode(stale) == "C"
    assert stale["emergency_stop_roi_percent"] == 0
    assert stale["locking"] is True


def test_feature_mode_is_canonical():
    assert FEATURES == ("A", "B", "C", "D", "E")
    assert feature_mode(normalize_ma_cross_config({})) == "C"


# ------------------------------------------------------------------ strategy
def test_c_only_entry_produces_sma_cross_signals():
    rows = rows_from(LONG_THEN_SHORT)
    cfg = normalize_ma_cross_config({})
    index = None
    for i in range(1, len(rows)):
        if evaluate(rows, cfg, i)["side"] == "LONG":
            index = i
            break
    assert index is not None
    decision = evaluate(rows, cfg, index)
    assert decision["side"] == "LONG" and decision["trigger"] == "SMA_CROSS"
    assert decision["features"] == "C"
    # C only adjusts the size; it never adds a stop.
    assert decision["initial_stop"] is None
    assert 0.5 <= decision["size_multiplier"] <= 1.5


def test_stop_related_features_are_not_active():
    rows = rows_from(LONG_THEN_SHORT)
    cfg = normalize_ma_cross_config({})
    for i in range(1, len(rows)):
        decision = evaluate(rows, cfg, i)
        if decision["side"]:
            assert decision["initial_stop"] is None
            break


def test_feature_c_scales_size_with_volatility():
    cfg_calm = normalize_ma_cross_config({"feature_c_target_atr_pct": "0.10"})
    cfg_wild = normalize_ma_cross_config({"feature_c_target_atr_pct": "0.001"})
    rows = rows_from(LONG_THEN_SHORT)
    calm = wild = None
    for i in range(1, len(rows)):
        d = evaluate(rows, cfg_calm, i)
        if d["side"]:
            calm = d["size_multiplier"]
            break
    for i in range(1, len(rows)):
        d = evaluate(rows, cfg_wild, i)
        if d["side"]:
            wild = d["size_multiplier"]
            break
    assert calm is not None and wild is not None
    assert calm >= wild          # a higher target allows a bigger size
    assert 0.5 <= wild <= 1.5 and 0.5 <= calm <= 1.5


# ------------------------------------------------------------------ backtest
def test_backtest_returns_summary_for_all_31_combinations():
    modes = all_feature_modes()
    assert len(modes) == 31
    assert modes[0] == "A" and modes[-1] == "ABCDE"
    assert len(set(modes)) == 31
    rows = rows_from(LONG_THEN_SHORT)
    base = normalize_ma_cross_config({})
    results = compare_modes(rows, base, initial_equity=10_000.0)
    assert len(results) == 31
    for item in results:
        assert item["mode"] in modes
        assert item["trades"] >= 0
        assert "return_pct" in item and "max_drawdown_pct" in item
    returns = [item["return_pct"] for item in results]
    assert returns == sorted(returns, reverse=True)   # sorted best first


def test_backtest_is_deterministic_and_feature_isolated():
    rows = rows_from(LONG_THEN_SHORT)
    base = normalize_ma_cross_config({})
    cfg_a = cfg_for_mode("A", base)
    assert cfg_a["feature_a_trend_filter"] == 1
    assert cfg_a["feature_b_atr_stop"] == 0 and cfg_a["feature_d_chandelier"] == 0
    first = run_backtest(rows, cfg_a, initial_equity=10_000.0)
    second = run_backtest(rows, cfg_a, initial_equity=10_000.0)
    assert first["net_pnl"] == second["net_pnl"]
    assert first["trades"] == second["trades"]
    # feature D replaces the fixed profit lock
    assert cfg_for_mode("D", base)["locking"] is False
    assert cfg_for_mode("AB", base)["locking"] is True


# ------------------------------------------------------------------ Telegram
class _Cfg:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)

    async def update_value(self, path, value):
        node = self.data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value


def _controller():
    from bot_runtime.controller_btc_pullback import ControllerBtcPullbackMixin
    from bot_runtime.controller_ma_cross import ControllerMaCrossMixin

    class Controller(ControllerMaCrossMixin, ControllerBtcPullbackMixin):
        pass

    controller = Controller()
    controller.cfg = _Cfg({
        "ma_cross": {},
        "btc_ema_pullback": {"enabled": True},
        "btc_ma_cross": {"enabled": True},
    })
    controller.is_paused = False
    notes = []

    async def notify_plain(text, event_type=None):
        notes.append(text)

    controller.notify_plain = notify_plain
    controller.notes = notes
    return controller


def _press(controller, action):
    return asyncio.run(controller._handle_ma_cross_action(action))


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_menu_is_c_only():
    controller = _controller()
    _, markup = _press(controller, "status")
    callbacks = _callbacks(markup)
    # A~E toggle buttons are gone; only the fixed C row remains.
    for name in FEATURES:
        assert f"mc:feat:{name}" not in callbacks
    for lev in ("2", "3", "4", "5"):
        assert f"mc:lev:{lev}" in callbacks
    assert "mc:on" in callbacks and "mc:off" in callbacks


def test_feature_toggle_is_refused_because_c_is_fixed():
    controller = _controller()
    text, _ = _press(controller, "feat:A")
    assert "C(변동성 타겟 사이징) 전용" in text
    assert feature_mode(controller._ma_cross_config()) == "C"


def test_strategy_can_be_enabled_with_the_fixed_c_feature():
    controller = _controller()
    _press(controller, "confirm_on")
    assert controller.cfg.data["ma_cross"]["enabled"] is True


def test_enabling_turns_other_futures_strategies_off():
    controller = _controller()
    _press(controller, "confirm_on")
    assert controller.cfg.data["ma_cross"]["enabled"] is True
    assert controller.cfg.data["btc_ema_pullback"]["enabled"] is False
    assert controller.cfg.data["btc_ma_cross"]["enabled"] is False
    assert controller.is_paused is True


def test_status_and_rules_describe_the_c_only_strategy():
    controller = _controller()
    text, _ = _press(controller, "status")
    assert "C(변동성 타겟 사이징)" in text
    assert "반대 크로스" in text and "비상손절 OFF" in text
    rules = _press(controller, "rules")[0]
    assert "SMA3 / SMA120" in rules
    assert "반대 크로스 발생 시 포지션 종료" in rules
    assert "변동성 타겟 사이징" in rules


from trading_safety.sma_periods import (
    PERIOD_PRESETS,
    PeriodError,
    apply_periods,
    normalize_periods,
    parse_period_command,
    required_candle_history,
    validate_periods,
)


# ----------------------------------------------------------- periods (shared)
def test_period_validation_accepts_valid_pairs():
    assert validate_periods(1, 200) == (1, 200)
    assert validate_periods("5", "60") == (5, 60)
    assert validate_periods(3, "120") == (3, 120)


def test_period_validation_rejects_bad_pairs():
    for fast, slow in ((200, 3), (120, 120), (0, 200), (3, 1), (3, 501), (2.5, 120), ("a", 200)):
        with pytest.raises(PeriodError):
            validate_periods(fast, slow)


def test_parse_period_command_variants():
    assert parse_period_command(["1", "200"]) == (1, 200)
    assert parse_period_command(["1/200"]) == (1, 200)
    assert parse_period_command(["1,200"]) == (1, 200)
    assert parse_period_command(["5", "60"]) == (5, 60)
    for bad in ([], ["1"], ["1", "2", "3"], ["x", "200"]):
        with pytest.raises(PeriodError):
            parse_period_command(bad)


def test_normalize_periods_falls_back_to_defaults():
    assert normalize_periods({}, default_fast=3, default_slow=120) == (3, 120)
    assert normalize_periods({"fast_period": 5, "slow_period": 60}, default_slow=120) == (5, 60)
    # Corrupted values never raise; the defaults win.
    assert normalize_periods({"fast_period": 0, "slow_period": 0}, default_slow=200) == (3, 200)


def test_apply_periods_lifts_candle_history():
    cfg = {"candle_history": 220}
    assert apply_periods(cfg, default_slow=120) == (3, 120)
    assert cfg["candle_history"] == 220          # 120 needs only 130
    cfg = {"candle_history": 220}
    apply_periods(cfg, default_slow=200)
    assert cfg["candle_history"] == 220          # 200 needs 210
    cfg = {}
    apply_periods(cfg, default_slow=120)
    assert cfg["candle_history"] == required_candle_history(120)
    cfg = {"candle_history": 100}
    apply_periods(cfg, default_slow=400)
    assert cfg["candle_history"] >= 410


def test_every_preset_is_valid():
    for label, fast, slow in PERIOD_PRESETS:
        assert validate_periods(fast, slow) == (fast, slow)
        assert label == f"{fast}/{slow}"


def test_macross_periods_presets_and_text_command():
    controller = _controller()
    _, markup = _press(controller, "status")
    assert "mc:sma:3:120" in _callbacks(markup)
    assert "mc:sma:5:60" in _callbacks(markup) and "mc:sma:1:200" in _callbacks(markup)

    # The test stub has no exchange, so the holding lookup is skipped safely.
    text, _ = _press(controller, "sma:1:200")
    data = controller.cfg.data["ma_cross"]
    assert (data["fast_period"], data["slow_period"]) == (1, 200)
    assert "패스트 1 · 슬로우 200" in text
    # Non-default pairs warn that 3/120 is the backtested optimum.
    assert "3/120" in text
    # The help text advertises the text command.
    help_text, _ = _press(controller, "sma_help")
    assert "/map <패스트> <슬로우>" in help_text and "지금: 1/200" in help_text


def test_macross_invalid_period_is_refused():
    controller = _controller()
    text, _ = _press(controller, "sma:120:120")
    assert "기간 변경 실패" in text
    cfg = normalize_ma_cross_config(controller.cfg.data["ma_cross"])
    assert (cfg["fast_period"], cfg["slow_period"]) == (3, 120)


def test_macross_normalization_accepts_custom_periods():
    cfg = normalize_ma_cross_config({"fast_period": 1, "slow_period": 200})
    assert (cfg["fast_period"], cfg["slow_period"]) == (1, 200)
    assert cfg["candle_history"] >= required_candle_history(200)
    # Invalid -> defaults (3/120), never an exception.
    for bad in ({"fast_period": 200, "slow_period": 3}, {"slow_period": 0},
                {"fast_period": 3, "slow_period": 9000}):
        cfg = normalize_ma_cross_config(bad)
        assert (cfg["fast_period"], cfg["slow_period"]) == (3, 120)


# ------------------------------------------------------------------ TradFi
def test_menu_has_the_tradfi_button():
    controller = _controller()
    _, markup = _press(controller, "status")
    assert "mc:scope:tradfi" in _callbacks(markup)


def test_tradfi_button_is_refused_outside_mainnet():
    controller = _controller()
    controller.get_exchange_mode = lambda: "binance_testnet"
    text, _ = _press(controller, "scope:tradfi")
    assert "메인넷에서만" in text
    assert controller.cfg.data["ma_cross"].get("scan_scope") != "tradfi"


def test_tradfi_button_selects_the_scope_on_mainnet(monkeypatch):
    controller = _controller()
    controller.get_exchange_mode = lambda: "binance_mainnet"

    class _Service:
        @staticmethod
        def us_session_status():
            return {"open": True, "reason": "regular_session_open",
                    "regular_close": "2026-10-28T20:00:00+00:00"}

    controller.ma_cross_service = _Service()
    text, _ = _press(controller, "scope:tradfi")
    assert controller.cfg.data["ma_cross"]["scan_scope"] == "tradfi"
    assert "TradFi만 선택" in text and "미국 정규장" in text


def test_tradfi_classification_matches_the_3_200_strategy():
    from btc_ma_cross.service import market_in_tradfi_scope as btc_scope
    from tests.test_btc_ma_cross_tradfi import tradfi_market

    cases = [
        ("AAPL/USDT:USDT", tradfi_market("AAPLUSDT")),
        ("XAU/USDT:USDT", tradfi_market("XAUUSDT", underlying_type="COMMODITY", base="XAU")),
        ("XAG/USDT:USDT", tradfi_market("XAGUSDT", underlying_type="METAL", base="XAG")),
        ("WTI/USDT:USDT", tradfi_market("WTIUSDT", underlying_type="ENERGY", base="WTI")),
    ]
    for symbol, market in cases:
        assert market_in_tradfi_scope(symbol, market) is btc_scope(symbol, market)


def test_service_scope_wiring():
    from btc_ma_cross_plus import MaCrossService

    assert MaCrossService.tradfi_network_allowed("mainnet") is True
    assert MaCrossService.tradfi_network_allowed("testnet") is False
    assert normalize_ma_cross_config({"scan_scope": "tradfi"})["scan_scope"] == "tradfi"
    assert normalize_ma_cross_config({"scan_scope": "bogus"})["scan_scope"] == "btc"
