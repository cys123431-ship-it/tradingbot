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
