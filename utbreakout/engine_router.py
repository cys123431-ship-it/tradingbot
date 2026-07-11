"""Bi-directional alpha engine router for UT Breakout.

This module returns decisions only. It never places orders, changes leverage,
or mutates live runtime state.
"""

from dataclasses import dataclass, field
from math import isfinite

from .derivatives import (
    DerivativesDecision,
    derivatives_overheated_for_long,
    derivatives_overheated_for_short,
    evaluate_derivatives_filter,
    missing_derivatives_data,
)
from .exit_policy import DEFAULT_EXIT_POLICY
from .liquidity import lower_wick_ratio, upper_wick_ratio
from .macro_guard import apply_macro_guard
from .meta import meta_label_gate
from .regime import classify_regime, regime_action
from .sizing import calculate_adaptive_risk_pct


ALPHA_ENGINE_NAMES = (
    "TREND_CONTINUATION_LONG",
    "TREND_CONTINUATION_SHORT",
    "EXHAUSTION_REVERSAL_LONG",
    "EXHAUSTION_REVERSAL_SHORT",
    "LIQUIDITY_SWEEP_REVERSAL_LONG",
    "LIQUIDITY_SWEEP_REVERSAL_SHORT",
    "SQUEEZE_BREAKOUT_LONG",
    "SQUEEZE_BREAKOUT_SHORT",
)


@dataclass(frozen=True)
class AlphaSignal:
    valid: bool
    side: str | None = None
    engine: str = "NONE"
    confidence: float = 0.0
    expected_r: float = 0.0
    size_multiplier: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def invalid(cls, engine="NONE", reason="NO_ENGINE_SIGNAL", side=None, confidence=0.0, expected_r=0.0):
        return cls(False, side, engine, confidence, expected_r, 0.0, [reason])


@dataclass(frozen=True)
class LadderTP:
    tp1_r: float
    tp1_pct: float
    tp2_r: float
    tp2_pct: float
    tp3_r: float | None = None
    tp3_pct: float | None = None
    move_sl_to_be_after_tp1: bool = True
    move_sl_to_tp1_after_tp2: bool = False

    def targets(self):
        targets = [
            {"label": "TP1", "r": self.tp1_r, "pct": self.tp1_pct},
            {"label": "TP2", "r": self.tp2_r, "pct": self.tp2_pct},
        ]
        if self.tp3_r is not None and self.tp3_pct is not None and self.tp3_pct > 0:
            targets.append({"label": "TP3", "r": self.tp3_r, "pct": self.tp3_pct})
        return targets


@dataclass(frozen=True)
class EngineStats:
    trade_count: int = 0
    expectancy_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_losses: int = 0
    fee_burden: float = 0.0
    funding_burden: float = 0.0
    oos_expectancy: float | None = None


@dataclass(frozen=True)
class TradeDecision:
    valid: bool
    side: str | None = None
    engine: str = "NONE"
    risk_pct: float = 0.0
    exit_policy_name: str = DEFAULT_EXIT_POLICY
    ladder_tp: LadderTP | None = None
    runner_enabled: bool = False
    regime: str | None = None
    confidence: float = 0.0
    expected_r: float = 0.0
    reasons: list[str] = field(default_factory=list)
    macro_risk_multiplier: float = 1.0

    @classmethod
    def none(cls, reason="NO_TRADE"):
        reasons = reason if isinstance(reason, list) else [str(reason)]
        return cls(False, None, "NONE", 0.0, DEFAULT_EXIT_POLICY, None, False, None, 0.0, 0.0, reasons)


def _finite_float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clamp(value, low=0.0, high=1.0):
    return max(float(low), min(float(high), float(value)))


def _field(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _ctx_bar(context):
    return {
        "open": _field(context, "open", _field(context, "close", 0.0)),
        "high": _field(context, "high", 0.0),
        "low": _field(context, "low", 0.0),
        "close": _field(context, "close", 0.0),
    }


def _engine_stats(stats, engine):
    if isinstance(stats, EngineStats):
        return stats
    raw = {}
    if isinstance(stats, dict):
        raw = stats.get(engine, stats)
    if isinstance(raw, EngineStats):
        return raw
    if not isinstance(raw, dict):
        raw = {}
    return EngineStats(
        trade_count=int(_finite_float(raw.get("trade_count", raw.get("trades", 0)), 0) or 0),
        expectancy_r=_finite_float(raw.get("expectancy_r", raw.get("average_R", 0.0)), 0.0),
        profit_factor=_finite_float(raw.get("profit_factor"), 0.0),
        max_drawdown_pct=_finite_float(raw.get("max_drawdown_pct"), 0.0),
        max_consecutive_losses=int(_finite_float(raw.get("max_consecutive_losses"), 0) or 0),
        fee_burden=_finite_float(raw.get("fee_burden", raw.get("fee_burden_pct", 0.0)), 0.0),
        funding_burden=_finite_float(raw.get("funding_burden", raw.get("funding_burden_pct", 0.0)), 0.0),
        oos_expectancy=raw.get("oos_expectancy", raw.get("oos_expectancy_r")),
    )


def should_disable_engine(stats, config=None):
    config = config or {}
    stats = _engine_stats(stats, "")
    if stats.trade_count < int(config.get("min_engine_trades_before_judgment", 30) or 30):
        return False
    if stats.expectancy_r < _finite_float(config.get("engine_min_expectancy_r"), 0.0):
        return True
    if stats.profit_factor < _finite_float(config.get("engine_min_profit_factor"), 1.0):
        return True
    if stats.max_consecutive_losses >= int(config.get("engine_max_consecutive_losses", 5) or 5):
        return True
    if stats.max_drawdown_pct > _finite_float(config.get("engine_max_drawdown_pct"), 15.0):
        return True
    return False


def _expected_r(engine, stats=None, config=None):
    config = config or {}
    stat = _engine_stats(stats or {}, engine)
    minimum = int(config.get("min_engine_trades_before_judgment", 30) or 30)
    if stat.trade_count >= minimum:
        if stat.oos_expectancy is not None:
            return _finite_float(stat.oos_expectancy, stat.expectancy_r)
        return stat.expectancy_r
    return 0.0


def _score_liquidation(context):
    spike = _field(context, "liquidation_spike_score", None)
    if spike is None:
        return 0.0
    return _clamp(_finite_float(spike) / 5.0, 0.0, 0.12)


def _score_spread_penalty(context, config=None):
    spread = _field(context, "spread_bps", None)
    if spread is None:
        return 0.0
    max_spread = _finite_float((config or {}).get("max_spread_bps"), 10.0)
    return _clamp((_finite_float(spread) - max_spread * 0.5) / max(max_spread, 1e-9), 0.0, 0.15)


def _trend_confidence(context, config=None):
    adx = _finite_float(_field(context, "adx"), 0.0)
    volume = _finite_float(_field(context, "volume_ratio"), 1.0)
    close = _finite_float(_field(context, "close"), 0.0)
    high_prev = _finite_float(_field(context, "donchian_high_previous"), close)
    low_prev = _finite_float(_field(context, "donchian_low_previous"), close)
    breakout = max(abs(close - high_prev), abs(close - low_prev)) / max(close, 1e-9)
    confidence = 0.50
    confidence += _clamp((adx - 20.0) / 80.0, 0.0, 0.15)
    confidence += 0.10 if bool(_field(context, "htf_trend_aligned", False)) else 0.0
    confidence += _clamp((volume - 1.0) / 4.0, 0.0, 0.12)
    confidence += _clamp(breakout * 40.0, 0.0, 0.10)
    confidence -= _score_spread_penalty(context, config)
    return _clamp(confidence)


def _valid_signal(engine, side, confidence, expected_r, size_multiplier, reasons):
    if expected_r < 0:
        return AlphaSignal.invalid(engine, f"{engine}_EXPECTANCY_NEGATIVE", side, confidence, expected_r)
    evidence_reasons = list(reasons)
    if expected_r == 0:
        evidence_reasons.append("ENGINE_EXPECTANCY_UNKNOWN_OR_INSUFFICIENT_DATA")
    return AlphaSignal(
        True,
        side,
        engine,
        _clamp(confidence),
        expected_r,
        _clamp(size_multiplier, 0.0, 1.5),
        evidence_reasons,
    )


def evaluate_trend_continuation_long(context, config=None, stats=None):
    config = config or {}
    engine = "TREND_CONTINUATION_LONG"
    if should_disable_engine(_engine_stats(stats or {}, engine), config):
        return AlphaSignal.invalid(engine, "ENGINE_DISABLED_BY_STATS", "LONG")
    conditions = (
        str(_field(context, "utbot_direction", "")).upper() == "LONG"
        and _finite_float(_field(context, "close")) > _finite_float(_field(context, "donchian_high_previous"))
        and _finite_float(_field(context, "adx")) >= _finite_float(config.get("trend_long_adx_min"), 25.0)
        and _finite_float(_field(context, "plus_di")) > _finite_float(_field(context, "minus_di"))
        and str(_field(context, "htf_trend", "")).upper() == "UP"
        and str(_field(context, "supertrend_direction", "")).upper() == "UP"
        and _finite_float(_field(context, "volume_ratio"), 1.0) >= _finite_float(config.get("trend_volume_ratio_min"), 1.2)
        and 20.0 <= _finite_float(_field(context, "atr_percentile"), 50.0) <= 85.0
        and not derivatives_overheated_for_long(context, config)
        and not bool(_field(context, "macro_risk_flag", False))
    )
    if not conditions:
        return AlphaSignal.invalid(engine, "TREND_LONG_CONDITIONS_NOT_MET", "LONG")
    return _valid_signal(engine, "LONG", _trend_confidence(context, config), _expected_r(engine, stats, config), 1.0, ["UT_DONCHIAN_ADX_HTF_VOLUME_ALIGNED"])


def evaluate_trend_continuation_short(context, config=None, stats=None):
    config = config or {}
    engine = "TREND_CONTINUATION_SHORT"
    if should_disable_engine(_engine_stats(stats or {}, engine), config):
        return AlphaSignal.invalid(engine, "ENGINE_DISABLED_BY_STATS", "SHORT")
    conditions = (
        str(_field(context, "utbot_direction", "")).upper() == "SHORT"
        and _finite_float(_field(context, "close")) < _finite_float(_field(context, "donchian_low_previous"))
        and _finite_float(_field(context, "adx")) >= _finite_float(config.get("trend_short_adx_min"), 27.0)
        and _finite_float(_field(context, "minus_di")) > _finite_float(_field(context, "plus_di"))
        and str(_field(context, "htf_trend", "")).upper() == "DOWN"
        and str(_field(context, "supertrend_direction", "")).upper() == "DOWN"
        and _finite_float(_field(context, "volume_ratio"), 1.0) >= _finite_float(config.get("trend_volume_ratio_min"), 1.2)
        and 20.0 <= _finite_float(_field(context, "atr_percentile"), 50.0) <= 85.0
        and not derivatives_overheated_for_short(context, config)
        and not bool(_field(context, "macro_risk_flag", False))
    )
    if not conditions:
        return AlphaSignal.invalid(engine, "TREND_SHORT_CONDITIONS_NOT_MET", "SHORT")
    return _valid_signal(engine, "SHORT", _trend_confidence(context, config), _expected_r(engine, stats, config), 0.9, ["UT_DONCHIAN_ADX_HTF_VOLUME_ALIGNED"])


def evaluate_exhaustion_reversal_short(context, config=None, stats=None):
    config = config or {}
    engine = "EXHAUSTION_REVERSAL_SHORT"
    if should_disable_engine(_engine_stats(stats or {}, engine), config):
        return AlphaSignal.invalid(engine, "ENGINE_DISABLED_BY_STATS", "SHORT")
    if missing_derivatives_data(context):
        return AlphaSignal.invalid(engine, "MISSING_DERIVATIVES_DATA", "SHORT")
    conditions = (
        _finite_float(_field(context, "funding_rate")) > _finite_float(config.get("extreme_positive_funding"), 0.0010)
        and _finite_float(_field(context, "long_short_ratio")) > _finite_float(config.get("extreme_long_short_ratio"), 2.0)
        and _finite_float(_field(context, "oi_change_pct")) > _finite_float(config.get("extreme_oi_increase_pct"), 8.0)
        and _finite_float(_field(context, "close")) < _finite_float(_field(context, "donchian_mid"))
        and _finite_float(_field(context, "minus_di")) > _finite_float(_field(context, "plus_di"))
        and str(_field(context, "utbot_direction", "")).upper() == "SHORT"
    )
    if not conditions:
        return AlphaSignal.invalid(engine, "NO_LONG_EXHAUSTION", "SHORT")
    confidence = 0.50
    confidence += _clamp((_finite_float(_field(context, "funding_rate")) - 0.0010) * 80.0, 0.0, 0.10)
    confidence += _clamp((_finite_float(_field(context, "long_short_ratio")) - 2.0) / 8.0, 0.0, 0.10)
    confidence += _clamp((_finite_float(_field(context, "oi_change_pct")) - 8.0) / 80.0, 0.0, 0.10)
    confidence += _score_liquidation(context)
    confidence -= _score_spread_penalty(context, config)
    return _valid_signal(engine, "SHORT", confidence, _expected_r(engine, stats, config), _finite_float(config.get("reversal_risk_multiplier"), 0.5), ["CROWDING_EXHAUSTION_REVERSAL_SHORT"])


def evaluate_exhaustion_reversal_long(context, config=None, stats=None):
    config = config or {}
    engine = "EXHAUSTION_REVERSAL_LONG"
    if should_disable_engine(_engine_stats(stats or {}, engine), config):
        return AlphaSignal.invalid(engine, "ENGINE_DISABLED_BY_STATS", "LONG")
    if missing_derivatives_data(context):
        return AlphaSignal.invalid(engine, "MISSING_DERIVATIVES_DATA", "LONG")
    conditions = (
        _finite_float(_field(context, "funding_rate")) < _finite_float(config.get("extreme_negative_funding"), -0.0010)
        and _finite_float(_field(context, "long_short_ratio")) < _finite_float(config.get("extreme_short_ratio"), 0.50)
        and _finite_float(_field(context, "oi_change_pct")) > _finite_float(config.get("extreme_oi_increase_pct"), 8.0)
        and _finite_float(_field(context, "close")) > _finite_float(_field(context, "donchian_mid"))
        and _finite_float(_field(context, "plus_di")) > _finite_float(_field(context, "minus_di"))
        and str(_field(context, "utbot_direction", "")).upper() == "LONG"
    )
    if not conditions:
        return AlphaSignal.invalid(engine, "NO_SHORT_EXHAUSTION", "LONG")
    confidence = 0.50
    confidence += _clamp(abs(_finite_float(_field(context, "funding_rate")) + 0.0010) * 80.0, 0.0, 0.10)
    confidence += _clamp((0.50 - _finite_float(_field(context, "long_short_ratio"))) / 2.0, 0.0, 0.10)
    confidence += _clamp((_finite_float(_field(context, "oi_change_pct")) - 8.0) / 80.0, 0.0, 0.10)
    confidence += _score_liquidation(context)
    confidence -= _score_spread_penalty(context, config)
    return _valid_signal(engine, "LONG", confidence, _expected_r(engine, stats, config), _finite_float(config.get("reversal_risk_multiplier"), 0.5), ["CROWDING_EXHAUSTION_REVERSAL_LONG"])


def evaluate_liquidity_sweep_reversal_long(context, config=None, stats=None):
    config = config or {}
    engine = "LIQUIDITY_SWEEP_REVERSAL_LONG"
    if should_disable_engine(_engine_stats(stats or {}, engine), config):
        return AlphaSignal.invalid(engine, "ENGINE_DISABLED_BY_STATS", "LONG")
    previous_low = _finite_float(_field(context, "donchian_low_previous"))
    conditions = (
        _finite_float(_field(context, "low")) < previous_low
        and _finite_float(_field(context, "close")) > previous_low
        and _finite_float(_field(context, "volume_ratio"), 1.0) >= _finite_float(config.get("sweep_volume_ratio_min"), 1.5)
        and lower_wick_ratio(_ctx_bar(context)) >= _finite_float(config.get("sweep_wick_ratio_min"), 0.45)
        and _finite_float(_field(context, "plus_di")) > _finite_float(_field(context, "minus_di"))
    )
    if not conditions:
        return AlphaSignal.invalid(engine, "NO_LOW_SWEEP_REVERSAL", "LONG")
    if str(_field(context, "htf_trend", "")).upper() == "DOWN" and not bool(config.get("allow_countertrend_sweep", False)):
        return AlphaSignal.invalid(engine, "HTF_DOWN_COUNTERTREND_BLOCKED", "LONG")
    confidence = 0.50
    confidence += _clamp(lower_wick_ratio(_ctx_bar(context)) - 0.35, 0.0, 0.14)
    confidence += _clamp((_finite_float(_field(context, "volume_ratio")) - 1.2) / 5.0, 0.0, 0.12)
    confidence += _score_liquidation(context)
    confidence -= _score_spread_penalty(context, config)
    return _valid_signal(engine, "LONG", confidence, _expected_r(engine, stats, config), _finite_float(config.get("sweep_risk_multiplier"), 0.5), ["LOW_SWEEP_RECLAIM_VOLUME_WICK"])


def evaluate_liquidity_sweep_reversal_short(context, config=None, stats=None):
    config = config or {}
    engine = "LIQUIDITY_SWEEP_REVERSAL_SHORT"
    if should_disable_engine(_engine_stats(stats or {}, engine), config):
        return AlphaSignal.invalid(engine, "ENGINE_DISABLED_BY_STATS", "SHORT")
    previous_high = _finite_float(_field(context, "donchian_high_previous"))
    conditions = (
        _finite_float(_field(context, "high")) > previous_high
        and _finite_float(_field(context, "close")) < previous_high
        and _finite_float(_field(context, "volume_ratio"), 1.0) >= _finite_float(config.get("sweep_volume_ratio_min"), 1.5)
        and upper_wick_ratio(_ctx_bar(context)) >= _finite_float(config.get("sweep_wick_ratio_min"), 0.45)
        and _finite_float(_field(context, "minus_di")) > _finite_float(_field(context, "plus_di"))
    )
    if not conditions:
        return AlphaSignal.invalid(engine, "NO_HIGH_SWEEP_REVERSAL", "SHORT")
    if str(_field(context, "htf_trend", "")).upper() == "UP" and not bool(config.get("allow_countertrend_sweep", False)):
        return AlphaSignal.invalid(engine, "HTF_UP_COUNTERTREND_BLOCKED", "SHORT")
    confidence = 0.50
    confidence += _clamp(upper_wick_ratio(_ctx_bar(context)) - 0.35, 0.0, 0.14)
    confidence += _clamp((_finite_float(_field(context, "volume_ratio")) - 1.2) / 5.0, 0.0, 0.12)
    confidence += _score_liquidation(context)
    confidence -= _score_spread_penalty(context, config)
    return _valid_signal(engine, "SHORT", confidence, _expected_r(engine, stats, config), _finite_float(config.get("sweep_risk_multiplier"), 0.5), ["HIGH_SWEEP_REJECT_VOLUME_WICK"])


def evaluate_squeeze_breakout_long(context, config=None, stats=None):
    config = config or {}
    engine = "SQUEEZE_BREAKOUT_LONG"
    if should_disable_engine(_engine_stats(stats or {}, engine), config):
        return AlphaSignal.invalid(engine, "ENGINE_DISABLED_BY_STATS", "LONG")
    conditions = (
        _finite_float(_field(context, "squeeze_percentile"), 100.0) <= _finite_float(config.get("squeeze_percentile_max"), 25.0)
        and _finite_float(_field(context, "range_expansion"), 1.0) >= _finite_float(config.get("squeeze_range_expansion_min"), 1.05)
        and _finite_float(_field(context, "volume_ratio"), 1.0) >= _finite_float(config.get("squeeze_volume_ratio_min"), 1.2)
        and _finite_float(_field(context, "close")) > _finite_float(_field(context, "donchian_high_previous"))
        and str(_field(context, "utbot_direction", "")).upper() == "LONG"
        and str(_field(context, "htf_trend", "")).upper() == "UP"
    )
    if not conditions:
        return AlphaSignal.invalid(engine, "SQUEEZE_LONG_CONDITIONS_NOT_MET", "LONG")
    confidence = 0.56
    if _finite_float(_field(context, "squeeze_percentile"), 100.0) <= 10 and _finite_float(_field(context, "volume_ratio"), 1.0) >= 1.5:
        confidence += 0.10
    confidence += _clamp((_finite_float(_field(context, "range_expansion"), 1.0) - 1.0) / 2.0, 0.0, 0.08)
    confidence -= _score_spread_penalty(context, config)
    return _valid_signal(engine, "LONG", confidence, _expected_r(engine, stats, config), 0.8, ["SQUEEZE_RELEASE_BREAKOUT_LONG"])


def evaluate_squeeze_breakout_short(context, config=None, stats=None):
    config = config or {}
    engine = "SQUEEZE_BREAKOUT_SHORT"
    if should_disable_engine(_engine_stats(stats or {}, engine), config):
        return AlphaSignal.invalid(engine, "ENGINE_DISABLED_BY_STATS", "SHORT")
    conditions = (
        _finite_float(_field(context, "squeeze_percentile"), 100.0) <= _finite_float(config.get("squeeze_percentile_max"), 25.0)
        and _finite_float(_field(context, "range_expansion"), 1.0) >= _finite_float(config.get("squeeze_range_expansion_min"), 1.05)
        and _finite_float(_field(context, "volume_ratio"), 1.0) >= _finite_float(config.get("squeeze_volume_ratio_min"), 1.2)
        and _finite_float(_field(context, "close")) < _finite_float(_field(context, "donchian_low_previous"))
        and str(_field(context, "utbot_direction", "")).upper() == "SHORT"
        and str(_field(context, "htf_trend", "")).upper() == "DOWN"
    )
    if not conditions:
        return AlphaSignal.invalid(engine, "SQUEEZE_SHORT_CONDITIONS_NOT_MET", "SHORT")
    confidence = 0.56
    if _finite_float(_field(context, "squeeze_percentile"), 100.0) <= 10 and _finite_float(_field(context, "volume_ratio"), 1.0) >= 1.5:
        confidence += 0.10
    confidence += _clamp((_finite_float(_field(context, "range_expansion"), 1.0) - 1.0) / 2.0, 0.0, 0.08)
    confidence -= _score_spread_penalty(context, config)
    return _valid_signal(engine, "SHORT", confidence, _expected_r(engine, stats, config), 0.8, ["SQUEEZE_RELEASE_BREAKOUT_SHORT"])


ENGINE_EVALUATORS = {
    "TREND_CONTINUATION_LONG": evaluate_trend_continuation_long,
    "TREND_CONTINUATION_SHORT": evaluate_trend_continuation_short,
    "EXHAUSTION_REVERSAL_LONG": evaluate_exhaustion_reversal_long,
    "EXHAUSTION_REVERSAL_SHORT": evaluate_exhaustion_reversal_short,
    "LIQUIDITY_SWEEP_REVERSAL_LONG": evaluate_liquidity_sweep_reversal_long,
    "LIQUIDITY_SWEEP_REVERSAL_SHORT": evaluate_liquidity_sweep_reversal_short,
    "SQUEEZE_BREAKOUT_LONG": evaluate_squeeze_breakout_long,
    "SQUEEZE_BREAKOUT_SHORT": evaluate_squeeze_breakout_short,
}


def evaluate_alpha_engines(context, config=None, stats=None):
    config = config or {}
    only_engine = str(config.get("engine") or config.get("alpha_engine") or "ALL").upper()
    enabled = config.get("enabled_engines")
    if enabled:
        enabled = {str(item).upper() for item in enabled}
    candidates = []
    for engine in ALPHA_ENGINE_NAMES:
        if only_engine not in {"", "ALL", "NONE"} and engine != only_engine:
            continue
        if enabled and engine not in enabled:
            continue
        candidates.append(ENGINE_EVALUATORS[engine](context, config, stats))

    valid = [signal for signal in candidates if signal.valid]
    if not valid:
        return AlphaSignal.invalid("NONE", "NO_ENGINE_SIGNAL")
    sides = {signal.side for signal in valid}
    if len(sides) > 1:
        return AlphaSignal.invalid("CONFLICT", "LONG_SHORT_CONFLICT")

    if bool(config.get("block_trend_reversal_conflict", True)):
        families = {
            "TREND" if signal.engine.startswith("TREND_CONTINUATION") else
            "REVERSAL" if "REVERSAL" in signal.engine else
            "SQUEEZE"
            for signal in valid
        }
        if "TREND" in families and "REVERSAL" in families:
            return AlphaSignal.invalid("CONFLICT", "TREND_REVERSAL_CONFLICT", next(iter(sides)))

    valid.sort(key=lambda signal: signal.confidence * signal.expected_r * signal.size_multiplier, reverse=True)
    best = valid[0]
    min_confidence = _finite_float(config.get("min_alpha_confidence"), 0.55)
    if best.confidence < min_confidence:
        return AlphaSignal.invalid("LOW_CONFIDENCE", "LOW_ALPHA_CONFIDENCE", best.side, best.confidence, best.expected_r)
    return best


def build_adaptive_ladder_tp(context, alpha_signal, config=None):
    config = config or {}
    confidence = _finite_float(_field(alpha_signal, "confidence"), 0.0)
    adx = _finite_float(_field(context, "adx"), 0.0)
    aligned = bool(_field(context, "htf_trend_aligned", False))
    if confidence < 0.60:
        return LadderTP(1.0, 50.0, 1.6, 50.0, None, None, True, False)
    if confidence < 0.72:
        return LadderTP(1.2, 30.0, 1.8, 40.0, 2.4, 30.0, True, True)
    if adx >= _finite_float(config.get("ladder_strong_adx_min"), 30.0) and aligned:
        return LadderTP(1.3, 30.0, 2.0, 40.0, 3.0, 30.0, True, True)
    return LadderTP(1.2, 30.0, 1.8, 40.0, 2.4, 30.0, True, True)


def evaluate_final_trade_decision(candle=None, context=None, config=None, models=None, stats=None):
    config = config or {}
    if bool(config.get("global_trading_paused", False)):
        return TradeDecision.none("GLOBAL_PAUSED")
    symbol = str(_field(context, "symbol", "") or "")
    paused_symbols = {str(item) for item in config.get("paused_symbols", ())}
    if symbol and symbol in paused_symbols:
        return TradeDecision.none("SYMBOL_PAUSED")
    if not bool(config.get("advanced_alpha_engine_enabled", False)):
        return TradeDecision.none("ADVANCED_ALPHA_DISABLED")

    macro_config = dict(config)
    if bool(config.get("macro_guard_enabled", False)) and bool(_field(context, "macro_risk_flag", False)):
        macro_config["manual_macro_risk_flag"] = True
    macro = apply_macro_guard(_field(context, "now", None), _field(context, "macro_events", ()), macro_config)
    if macro.blocked:
        return TradeDecision.none(macro.reason)

    regime = classify_regime(context, config)
    if regime in {"BAD_LIQUIDITY", "NEWS_SPIKE_OR_ABNORMAL", "CROWDING_OVERHEATED"}:
        return TradeDecision.none(f"REGIME_BLOCK_{regime}")
    if regime == "CHOP" and not bool(config.get("allow_advanced_reversal_in_chop", False)):
        return TradeDecision.none("REGIME_BLOCK_CHOP")
    regime_act = regime_action(regime, None, config)

    alpha = evaluate_alpha_engines(context, config, stats)
    if not alpha.valid:
        return TradeDecision.none(alpha.reasons)
    if (alpha.side == "LONG" and not regime_act.allow_long) or (alpha.side == "SHORT" and not regime_act.allow_short):
        return TradeDecision.none(f"REGIME_SIDE_BLOCK_{regime}")
    derivatives = evaluate_derivatives_filter(alpha.side, context, config)
    if derivatives.veto:
        return TradeDecision.none(derivatives.reason)

    meta_model = None
    if isinstance(models, dict):
        meta_model = models.get("meta_model")
    else:
        meta_model = getattr(models, "meta_model", None)
    gate = meta_label_gate(alpha, context, model=meta_model, config=config)
    if not gate.allow:
        return TradeDecision.none(gate.reason)

    sizing_derivatives = DerivativesDecision(
        derivatives.veto,
        derivatives.reason,
        derivatives.size_multiplier * alpha.size_multiplier * macro.risk_multiplier,
    )
    risk_pct = calculate_adaptive_risk_pct(
        signal=alpha,
        context=context,
        regime_action=regime_act,
        derivatives=sizing_derivatives,
        gate=gate,
        config=config,
    )
    if risk_pct <= 0:
        return TradeDecision.none("RISK_BLOCKED")

    ladder = build_adaptive_ladder_tp(context, alpha, config) if bool(config.get("adaptive_ladder_tp_enabled", False)) else None
    return TradeDecision(
        True,
        alpha.side,
        alpha.engine,
        risk_pct,
        "ADAPTIVE_LADDER_TP" if ladder else DEFAULT_EXIT_POLICY,
        ladder,
        False,
        regime,
        alpha.confidence,
        alpha.expected_r,
        list(alpha.reasons) + [gate.reason, derivatives.reason],
        macro.risk_multiplier,
    )
