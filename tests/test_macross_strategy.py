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
    assert feature_mode(cfg) == ""          # every feature OFF by default
    for lev, expected in ((2, 2), (3, 3), (5, 5), (0, 5), (9, 5), (20, 5)):
        assert normalize_ma_cross_config({"leverage": lev})["leverage"] == expected


def test_feature_mode_is_canonical():
    cfg = normalize_ma_cross_config({
        "feature_e_donchian": 1, "feature_a_trend_filter": 1, "feature_c_vol_sizing": 1,
    })
    assert feature_mode(cfg) == "ACE"
    assert FEATURES == ("A", "B", "C", "D", "E")


# ------------------------------------------------------------------ strategy
def test_no_feature_selected_makes_no_signals():
    cfg = normalize_ma_cross_config({})
    decision = evaluate(rows_from(LONG_THEN_SHORT), cfg)
    assert decision["side"] is None
    assert decision["skip_reason"] == "NO_FEATURE_SELECTED"


def test_sma_cross_signal_and_features_are_reported():
    rows = rows_from(LONG_THEN_SHORT)
    cfg = normalize_ma_cross_config({"feature_b_atr_stop": 1})
    # find the candle index where the fast SMA crosses up
    index = None
    for i in range(1, len(rows)):
        decision = evaluate(rows, cfg, i)
        if decision["side"] == "LONG":
            index = i
            break
    assert index is not None
    decision = evaluate(rows, cfg, index)
    assert decision["side"] == "LONG" and decision["trigger"] == "SMA_CROSS"
    assert decision["initial_stop"] is not None and decision["initial_stop"] < rows[index][4]
    assert decision["atr_distance"] > 0


def test_feature_a_blocks_when_trend_disagrees():
    rows = rows_from(LONG_THEN_SHORT)
    base = normalize_ma_cross_config({"feature_b_atr_stop": 1})
    strict = normalize_ma_cross_config({
        "feature_a_trend_filter": 1, "feature_b_atr_stop": 1,
        "feature_a_adx_min": "0", "feature_a_efficiency_min": "0",
    })
    plain_long = strict_long = 0
    for i in range(1, len(rows)):
        plain = evaluate(rows, base, i)
        gated = evaluate(rows, strict, i)
        if plain["side"]:
            plain_long += 1
            if gated["side"]:
                strict_long += 1
            else:
                assert gated["skip_reason"].startswith("REJECTED_TREND_FILTER")
    # with ADX/ER floors at 0 the only extra rejection is the SMA side check,
    # so the gated signal set can never be larger than the plain one
    assert strict_long <= plain_long


def test_feature_c_scales_size_with_volatility():
    cfg_calm = normalize_ma_cross_config({"feature_c_vol_sizing": 1,
                                          "feature_c_target_atr_pct": "0.10"})
    cfg_wild = normalize_ma_cross_config({"feature_c_vol_sizing": 1,
                                          "feature_c_target_atr_pct": "0.001"})
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


def test_feature_e_donchian_adds_breakout_trigger():
    rows = rows_from(trend_series([(200, 0.0), (60, 0.2), (40, 2.0)]))
    cfg = normalize_ma_cross_config({"feature_e_donchian": 1, "feature_e_period": 20})
    triggers = {evaluate(rows, cfg, i)["trigger"] for i in range(150, len(rows))}
    assert "DONCHIAN" in triggers


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


def test_menu_has_every_feature_button():
    controller = _controller()
    _, markup = _press(controller, "status")
    callbacks = _callbacks(markup)
    for name in FEATURES:
        assert f"mc:feat:{name}" in callbacks
    for lev in ("2", "3", "4", "5"):
        assert f"mc:lev:{lev}" in callbacks
    assert "mc:on" in callbacks and "mc:off" in callbacks


def test_feature_toggles_are_independent_and_combinable():
    controller = _controller()
    cfg = controller.cfg.data["ma_cross"]
    _press(controller, "feat:A")
    assert feature_mode(controller._ma_cross_config()) == "A"
    _press(controller, "feat:C")
    assert feature_mode(controller._ma_cross_config()) == "AC"
    _press(controller, "feat:E")
    assert feature_mode(controller._ma_cross_config()) == "ACE"
    _press(controller, "feat:A")                      # toggle back off
    assert feature_mode(controller._ma_cross_config()) == "CE"
    assert cfg["feature_a_trend_filter"] == 0


def test_last_feature_cannot_be_turned_off():
    controller = _controller()
    _press(controller, "feat:B")
    assert feature_mode(controller._ma_cross_config()) == "B"
    text, _ = _press(controller, "feat:B")
    assert "최소 1개" in text
    assert feature_mode(controller._ma_cross_config()) == "B"


def test_strategy_cannot_be_enabled_without_a_feature():
    controller = _controller()
    text, markup = _press(controller, "on")
    assert "최소 1개" in text
    _press(controller, "confirm_on")
    assert controller.cfg.data["ma_cross"].get("enabled") is not True


def test_enabling_turns_other_futures_strategies_off():
    controller = _controller()
    _press(controller, "feat:B")
    _press(controller, "confirm_on")
    assert controller.cfg.data["ma_cross"]["enabled"] is True
    assert controller.cfg.data["btc_ema_pullback"]["enabled"] is False
    assert controller.cfg.data["btc_ma_cross"]["enabled"] is False
    assert controller.is_paused is True


def test_status_shows_the_selected_combination():
    controller = _controller()
    _press(controller, "feat:A")
    _press(controller, "feat:D")
    text, _ = _press(controller, "status")
    assert "A(추세필터" in text and "D(ATR 샹들리에" in text
    rules = _press(controller, "rules")[0]
    assert "SMA3 / SMA120" in rules and "Donchian" in rules
