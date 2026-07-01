"""Profit alpha layer for UTBreakout live entries.

The scanner and UT/EV layers find candidates.  This module is the execution
alpha layer: it scores whether that candidate has enough independent edge to
place an order, chooses a sub-strategy label, and defines early follow-through
management.  It is intentionally pure and never touches exchange state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Mapping


@dataclass(frozen=True)
class ProfitAlphaDecision:
    allowed: bool
    side: str
    engine: str
    score: float
    probability: float
    risk_multiplier: float
    exit_profile: str
    follow_through_bars: int
    follow_through_min_mfe_r: float
    early_exit_max_mae_r: float
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    components: Mapping[str, float] = field(default_factory=dict)
    meta_key: str = ""
    meta_sample_count: int = 0
    meta_expectancy_r: float | None = None
    exit_overrides: Mapping[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        state = "ALLOW" if self.allowed else "BLOCK"
        return (
            f"{state} {self.engine} score {self.score:.1f} "
            f"p={self.probability:.2f} risk x{self.risk_multiplier:.2f}"
        )


@dataclass(frozen=True)
class AlphaFollowThroughExit:
    should_exit: bool
    reason: str


def default_profit_alpha_config() -> dict[str, Any]:
    return {
        "profit_alpha_enabled": True,
        "profit_alpha_min_score": 68.0,
        "profit_alpha_long_min_score": 69.0,
        "profit_alpha_short_min_score": 68.0,
        "profit_alpha_min_probability": 0.555,
        "profit_alpha_long_min_probability": 0.560,
        "profit_alpha_short_min_probability": 0.555,
        "profit_alpha_opposite_regime_score_add": 5.0,
        "profit_alpha_opposite_regime_probability_add": 0.010,
        "profit_alpha_stale_signal_max_age_bars": 8.0,
        "profit_alpha_stale_signal_reaccel_min_range": 1.10,
        "profit_alpha_stale_signal_reaccel_min_volume": 1.00,
        "profit_alpha_derivatives_multi_adverse_block_count": 3,
        "profit_alpha_derivatives_multi_adverse_strong_count": 2,
        "profit_alpha_meta_min_samples": 8,
        "profit_alpha_meta_expectancy_block_below": -0.12,
        "profit_alpha_meta_probability_weight": 0.20,
        "profit_alpha_follow_through_enabled": True,
        "profit_alpha_default_follow_through_bars": 3,
        "profit_alpha_default_follow_through_min_mfe_r": 0.35,
        "profit_alpha_default_early_exit_max_mae_r": 0.75,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _first(values: Mapping[str, Any], *keys: str, default: float | None = None) -> float | None:
    for key in keys:
        parsed = _finite(values.get(key), None)
        if parsed is not None:
            return parsed
    return default


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _side_sign(side: str) -> int:
    return 1 if str(side or "").lower() == "long" else -1


def _is_aligned_direction(side: str, value: Any) -> bool:
    text = str(value or "").strip().lower()
    if str(side or "").lower() == "long":
        return text in {"long", "bull", "bullish", "up"}
    return text in {"short", "bear", "bearish", "down"}


def _is_opposite_direction(side: str, value: Any) -> bool:
    text = str(value or "").strip().lower()
    if str(side or "").lower() == "long":
        return text in {"short", "bear", "bearish", "down"}
    return text in {"long", "bull", "bullish", "up"}


def _market_regime_score(side: str, values: Mapping[str, Any]) -> tuple[float, int, int, bool, tuple[str, ...]]:
    context = values.get("market_regime_context")
    items = {}
    if isinstance(context, Mapping):
        raw_items = context.get("items")
        if isinstance(raw_items, Mapping):
            items = dict(raw_items)

    if not items:
        return 52.0, 0, 0, False, ("market regime neutral/unavailable",)

    aligned = 0
    opposite = 0
    strong_opposite = False
    reasons: list[str] = []
    for symbol, item in items.items():
        if not isinstance(item, Mapping):
            continue
        direction = item.get("direction")
        ret_pct = _finite(item.get("return_lookback_pct"), 0.0) or 0.0
        if _is_aligned_direction(side, direction):
            aligned += 1
            reasons.append(f"{symbol} aligned {str(direction).upper()} {ret_pct:+.2f}%")
        elif _is_opposite_direction(side, direction):
            opposite += 1
            if abs(ret_pct) >= 1.5:
                strong_opposite = True
            reasons.append(f"{symbol} opposite {str(direction).upper()} {ret_pct:+.2f}%")

    score = 52.0 + aligned * 14.0 - opposite * 12.0
    if strong_opposite:
        score -= 8.0
    return _clamp(score, 20.0, 90.0), aligned, opposite, strong_opposite, tuple(reasons)


def _trend_score(side: str, values: Mapping[str, Any]) -> tuple[float, tuple[str, ...]]:
    adx = _first(values, "adx", "ADX", default=18.0) or 18.0
    chop = _first(values, "chop", "choppiness", default=55.0) or 55.0
    plus_di = _first(values, "plus_di", "di_plus", default=None)
    minus_di = _first(values, "minus_di", "di_minus", default=None)
    close = _first(values, "close", "entry_price", default=None)
    ema50 = _first(values, "ema50", "ema_fast", default=None)
    ema200 = _first(values, "ema200", "ema_slow", default=None)
    htf_close = _first(values, "htf_close", default=None)
    htf_ema = _first(values, "htf_ema_slow", "htf_ema200", default=None)
    volume = _first(values, "volume_ratio", "relative_volume", default=1.0) or 1.0
    range_expansion = _first(values, "range_expansion", "range_expansion_ratio", default=1.0) or 1.0
    reaccel = bool(_attr(values, "reacceleration", values.get("ev_reacceleration", False)))

    score = 38.0
    score += _clamp((adx - 12.0) / 22.0, 0.0, 1.0) * 24.0
    score += _clamp((62.0 - chop) / 32.0, 0.0, 1.0) * 12.0
    score += _clamp((volume - 0.75) / 0.75, 0.0, 1.0) * 12.0
    score += _clamp((range_expansion - 0.95) / 0.45, 0.0, 1.0) * 8.0

    reasons = [
        f"ADX {adx:.1f}",
        f"chop {chop:.1f}",
        f"vol {volume:.2f}",
        f"range {range_expansion:.2f}",
    ]
    if plus_di is not None and minus_di is not None:
        di_gap = (plus_di - minus_di) * _side_sign(side)
        score += _clamp((di_gap + 2.0) / 12.0, 0.0, 1.0) * 10.0
        reasons.append(f"DI gap {di_gap:+.1f}")
    if close is not None and ema50 is not None:
        aligned = close >= ema50 if side == "long" else close <= ema50
        score += 6.0 if aligned else -6.0
        reasons.append("EMA50 aligned" if aligned else "EMA50 against")
    if close is not None and ema200 is not None:
        aligned = close >= ema200 if side == "long" else close <= ema200
        score += 6.0 if aligned else -6.0
        reasons.append("EMA200 aligned" if aligned else "EMA200 against")
    if htf_close is not None and htf_ema is not None:
        aligned = htf_close >= htf_ema if side == "long" else htf_close <= htf_ema
        score += 7.0 if aligned else -7.0
        reasons.append("HTF aligned" if aligned else "HTF against")
    if reaccel:
        score += 6.0
        reasons.append("reacceleration")

    return _clamp(score, 0.0, 100.0), tuple(reasons)


def _relative_strength_score(values: Mapping[str, Any], ev_decision: Any = None) -> tuple[float, tuple[str, ...]]:
    selector = _first(values, "coin_selector_score", "selector_quality_score", default=50.0) or 50.0
    trend_health = _first(values, "trend_health_score", default=50.0) or 50.0
    strategy_quality = _first(values, "strategy_quality_score", default=50.0) or 50.0
    q2 = _first(values, "quality_score_v2_score", default=50.0) or 50.0
    leadership = _finite(_attr(ev_decision, "leadership_score", None), None)
    if leadership is None:
        leadership = _first(values, "ev_leadership_score", "leadership_score", default=50.0) or 50.0

    score = (
        selector * 0.25
        + trend_health * 0.20
        + strategy_quality * 0.20
        + q2 * 0.20
        + leadership * 0.15
    )
    return _clamp(score, 0.0, 100.0), (
        f"selector {selector:.1f}",
        f"trend {trend_health:.1f}",
        f"strategy {strategy_quality:.1f}",
        f"q2 {q2:.1f}",
        f"leader {leadership:.1f}",
    )


def _squeeze_score(values: Mapping[str, Any]) -> tuple[float, bool, tuple[str, ...]]:
    bb_pct = _first(values, "bb_width_percentile", "bb_pct", "squeeze_bb_pct", default=50.0) or 50.0
    keltner = str(values.get("keltner_state", values.get("keltner_squeeze_state", "")) or "").lower()
    state = str(values.get("squeeze_state", "") or "").lower()
    range_expansion = _first(values, "range_expansion", "range_expansion_ratio", default=1.0) or 1.0
    volume = _first(values, "volume_ratio", "relative_volume", default=1.0) or 1.0

    score = 35.0
    score += _clamp((35.0 - bb_pct) / 35.0, 0.0, 1.0) * 22.0
    if "on" in keltner or "true" in keltner or values.get("keltner_squeeze") is True:
        score += 12.0
    if "release" in state or "break" in state:
        score += 16.0
    score += _clamp((range_expansion - 1.0) / 0.45, 0.0, 1.0) * 10.0
    score += _clamp((volume - 1.0) / 0.7, 0.0, 1.0) * 8.0
    active = score >= 68.0
    return _clamp(score, 0.0, 100.0), active, (
        f"BB pct {bb_pct:.1f}",
        f"keltner {keltner or 'n/a'}",
        f"state {state or 'n/a'}",
    )


def _derivatives_score(side: str, values: Mapping[str, Any]) -> tuple[float, float, int, int, tuple[str, ...], tuple[str, ...]]:
    taker = _first(values, "taker_buy_sell_ratio", "taker_ratio", default=None)
    ofi = _first(values, "rolling_ofi", "ofi", "order_flow_imbalance", default=None)
    orderbook = _first(values, "orderbook_imbalance_pct", "orderbook_imbalance", default=None)
    oi_1h = _first(values, "open_interest_change_1h", "open_interest_delta_pct", default=None)
    oi_accel = _first(values, "open_interest_acceleration", "open_interest_delta_acceleration", default=None)
    funding = _first(values, "funding_rate", default=None)
    basis = _first(values, "basis_pct", "futures_basis_pct", default=None)
    long_short = _first(values, "long_short_ratio", default=None)

    score = 55.0
    adverse = 0
    strong_adverse = 0
    reasons: list[str] = []
    blockers: list[str] = []
    sign = _side_sign(side)

    if taker is not None:
        aligned = taker >= 1.03 if side == "long" else taker <= 0.97
        against = taker <= 0.95 if side == "long" else taker >= 1.05
        if aligned:
            score += 10.0
            reasons.append(f"taker aligned {taker:.3f}")
        if against:
            score -= 12.0
            adverse += 1
            if taker <= 0.90 or taker >= 1.10:
                strong_adverse += 1
            blockers.append(f"taker against {taker:.3f}")

    for label, raw, scale in (
        ("OFI", ofi, 4.0),
        ("orderbook", orderbook, 8.0),
    ):
        if raw is None:
            continue
        aligned = raw * sign > 0
        if aligned:
            score += min(8.0, abs(raw) / scale)
            reasons.append(f"{label} aligned {raw:+.2f}")
        else:
            score -= min(10.0, 2.0 + abs(raw) / scale)
            adverse += 1
            if abs(raw) >= scale * 4.0:
                strong_adverse += 1
            blockers.append(f"{label} against {raw:+.2f}")

    if oi_1h is not None:
        if oi_1h >= 0.20:
            score += 8.0
            reasons.append(f"OI expanding {oi_1h:+.2f}%")
        elif abs(oi_1h) < 0.08:
            score -= 5.0
            adverse += 1
            blockers.append(f"OI stalled {oi_1h:+.2f}%")
        else:
            score -= 4.0
            adverse += 1
            blockers.append(f"OI contracting {oi_1h:+.2f}%")

    if oi_accel is not None:
        if oi_accel * sign >= -0.05:
            score += 4.0
        else:
            score -= 6.0
            adverse += 1
            blockers.append(f"OI acceleration against {oi_accel:+.3f}")

    if funding is not None:
        if side == "long" and funding >= 0.0009:
            score -= 8.0
            adverse += 1
            blockers.append(f"long funding crowded {funding:.6f}")
        elif side == "short" and funding <= -0.0009:
            score -= 8.0
            adverse += 1
            blockers.append(f"short funding crowded {funding:.6f}")
        elif side == "long" and funding <= -0.0002:
            score += 4.0
            reasons.append(f"long funding relief {funding:.6f}")
        elif side == "short" and funding >= 0.0002:
            score += 4.0
            reasons.append(f"short funding relief {funding:.6f}")

    if basis is not None:
        if side == "long" and basis > 0.25:
            score -= 6.0
            adverse += 1
            blockers.append(f"basis long crowded {basis:+.3f}%")
        elif side == "short" and basis < -0.25:
            score -= 6.0
            adverse += 1
            blockers.append(f"basis short crowded {basis:+.3f}%")

    if long_short is not None:
        if side == "long" and long_short >= 2.2:
            score -= 7.0
            adverse += 1
            blockers.append(f"L/S long crowded {long_short:.2f}")
        elif side == "short" and long_short <= 0.45:
            score -= 7.0
            adverse += 1
            blockers.append(f"L/S short crowded {long_short:.2f}")

    risk_multiplier = 1.0
    if adverse >= 2:
        risk_multiplier = 0.72
    if adverse >= 3:
        risk_multiplier = 0.55
    if strong_adverse >= 2:
        risk_multiplier = min(risk_multiplier, 0.45)

    return _clamp(score, 0.0, 100.0), risk_multiplier, adverse, strong_adverse, tuple(reasons), tuple(blockers)


def _choose_engine(
    side: str,
    trend_score: float,
    relative_score: float,
    regime_score: float,
    squeeze_score: float,
    squeeze_active: bool,
    ev_mode: str,
) -> str:
    ev_mode = str(ev_mode or "").upper()
    if squeeze_active and squeeze_score >= 72.0 and trend_score >= 54.0:
        return "SQUEEZE_BREAKOUT"
    if side == "short" and regime_score >= 64.0 and trend_score >= 66.0:
        return "STRONG_DOWNTREND_SHORT"
    if side == "long" and regime_score >= 64.0 and trend_score >= 68.0:
        return "STRONG_UPTREND_LONG"
    if "STRONG_TREND" in ev_mode and trend_score >= 64.0:
        return "TREND_CONTINUATION"
    if trend_score >= 60.0 and relative_score >= 55.0:
        return "RELATIVE_STRENGTH_TREND"
    return "NO_TRADE"


def _exit_overrides(engine: str, cfg: Mapping[str, Any]) -> dict[str, Any]:
    if engine in {"STRONG_UPTREND_LONG", "STRONG_DOWNTREND_SHORT", "TREND_CONTINUATION"}:
        return {
            "partial_take_profit_ratio": 0.20,
            "second_take_profit_r_multiple": 3.20,
            "second_take_profit_ratio": 0.30,
            "runner_pct": 0.50,
            "take_profit_r_multiple": 3.20,
            "atr_trailing_activation_r": 1.30,
            "atr_trailing_multiplier": 3.50,
            "runner_chandelier_multiplier": 3.50,
            "ev_time_stop_bars": 10,
            "ev_time_stop_min_mfe_r": 0.55,
            "profit_alpha_follow_through_bars": 4,
            "profit_alpha_follow_through_min_mfe_r": 0.40,
            "profit_alpha_early_exit_max_mae_r": 0.85,
        }
    if engine == "SQUEEZE_BREAKOUT":
        return {
            "partial_take_profit_ratio": 0.25,
            "second_take_profit_r_multiple": 2.80,
            "second_take_profit_ratio": 0.35,
            "runner_pct": 0.40,
            "take_profit_r_multiple": 2.80,
            "atr_trailing_activation_r": 1.15,
            "atr_trailing_multiplier": 3.20,
            "runner_chandelier_multiplier": 3.20,
            "ev_time_stop_bars": 7,
            "ev_time_stop_min_mfe_r": 0.50,
            "profit_alpha_follow_through_bars": 3,
            "profit_alpha_follow_through_min_mfe_r": 0.40,
            "profit_alpha_early_exit_max_mae_r": 0.75,
        }
    return {
        "partial_take_profit_ratio": 0.30,
        "second_take_profit_r_multiple": 2.20,
        "second_take_profit_ratio": 0.35,
        "runner_pct": 0.35,
        "take_profit_r_multiple": 2.20,
        "atr_trailing_activation_r": 1.00,
        "atr_trailing_multiplier": 2.70,
        "runner_chandelier_multiplier": 2.70,
        "ev_time_stop_bars": 6,
        "ev_time_stop_min_mfe_r": 0.35,
        "profit_alpha_follow_through_bars": int(
            cfg.get("profit_alpha_default_follow_through_bars", 3) or 3
        ),
        "profit_alpha_follow_through_min_mfe_r": float(
            cfg.get("profit_alpha_default_follow_through_min_mfe_r", 0.35) or 0.35
        ),
        "profit_alpha_early_exit_max_mae_r": float(
            cfg.get("profit_alpha_default_early_exit_max_mae_r", 0.75) or 0.75
        ),
    }


def _meta_stats(meta_stats: Any, engine: str, side: str) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(meta_stats, Mapping):
        return f"{side}:{engine}", {}
    keys = (
        f"{side}:{engine}",
        engine,
        side,
        "global",
    )
    for key in keys:
        item = meta_stats.get(key)
        if isinstance(item, Mapping):
            return key, item
    return f"{side}:{engine}", {}


def evaluate_profit_alpha(
    *,
    side: str,
    values: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    ev_decision: Any = None,
    ev_net: Any = None,
    meta_stats: Mapping[str, Any] | None = None,
) -> ProfitAlphaDecision:
    cfg = default_profit_alpha_config()
    if isinstance(config, Mapping):
        cfg.update(dict(config))
    side = str(side or "").lower()
    if side not in {"long", "short"}:
        return ProfitAlphaDecision(False, side, "NO_TRADE", 0.0, 0.0, 0.0, "NONE", 0, 0.0, 0.0, blockers=("invalid side",))

    if not bool(cfg.get("profit_alpha_enabled", True)):
        return ProfitAlphaDecision(
            True,
            side,
            "DISABLED",
            100.0,
            1.0,
            1.0,
            "BASELINE",
            int(cfg.get("profit_alpha_default_follow_through_bars", 3) or 3),
            float(cfg.get("profit_alpha_default_follow_through_min_mfe_r", 0.35) or 0.35),
            float(cfg.get("profit_alpha_default_early_exit_max_mae_r", 0.75) or 0.75),
            reasons=("profit alpha disabled",),
        )

    values = values if isinstance(values, Mapping) else {}
    ev_mode = str(_attr(ev_decision, "mode", values.get("ev_adaptive_mode", "")) or "")
    trend, trend_reasons = _trend_score(side, values)
    relative, relative_reasons = _relative_strength_score(values, ev_decision)
    regime, regime_aligned, regime_opposite, strong_opposite, regime_reasons = _market_regime_score(side, values)
    squeeze, squeeze_active, squeeze_reasons = _squeeze_score(values)
    derivatives, deriv_risk, adverse_count, strong_adverse, deriv_reasons, deriv_blockers = _derivatives_score(side, values)

    engine = _choose_engine(side, trend, relative, regime, squeeze, squeeze_active, ev_mode)
    if engine == "NO_TRADE":
        score = trend * 0.30 + relative * 0.25 + regime * 0.15 + derivatives * 0.20 + squeeze * 0.10
    elif engine == "SQUEEZE_BREAKOUT":
        score = trend * 0.25 + relative * 0.20 + regime * 0.15 + derivatives * 0.20 + squeeze * 0.20
    else:
        score = trend * 0.32 + relative * 0.26 + regime * 0.16 + derivatives * 0.18 + squeeze * 0.08
    score = _clamp(score, 0.0, 100.0)

    blockers: list[str] = []
    reasons: list[str] = []
    reasons.extend(trend_reasons[:4])
    reasons.extend(relative_reasons[:3])
    reasons.extend(regime_reasons[:3])
    reasons.extend(deriv_reasons[:3])
    reasons.extend(squeeze_reasons[:2])

    if engine == "NO_TRADE":
        blockers.append("no alpha sub-strategy selected")

    base_min_score = float(cfg.get("profit_alpha_min_score", 68.0) or 68.0)
    min_score = float(cfg.get(f"profit_alpha_{side}_min_score", base_min_score) or base_min_score)
    base_min_probability = float(cfg.get("profit_alpha_min_probability", 0.555) or 0.555)
    min_probability = float(
        cfg.get(f"profit_alpha_{side}_min_probability", base_min_probability)
        or base_min_probability
    )
    if regime_opposite > 0 and regime_aligned == 0:
        min_score += float(cfg.get("profit_alpha_opposite_regime_score_add", 5.0) or 5.0)
        min_probability += float(cfg.get("profit_alpha_opposite_regime_probability_add", 0.010) or 0.010)
        blockers.append("top market regime opposite; raised alpha hurdle")

    stale_age = _finite(_attr(ev_decision, "signal_age_candles", values.get("signal_age_candles")), None)
    if stale_age is None:
        stale_age = _first(values, "bias_continuation_signal_age_candles", "ut_signal_age_candles", default=None)
    range_expansion = _first(values, "range_expansion", "range_expansion_ratio", default=1.0) or 1.0
    volume = _first(values, "volume_ratio", "relative_volume", default=1.0) or 1.0
    reaccel = bool(_attr(ev_decision, "reacceleration", values.get("ev_reacceleration", False)))
    if stale_age is not None and stale_age > float(cfg.get("profit_alpha_stale_signal_max_age_bars", 8.0) or 8.0):
        range_ok = range_expansion >= float(cfg.get("profit_alpha_stale_signal_reaccel_min_range", 1.10) or 1.10)
        volume_ok = volume >= float(cfg.get("profit_alpha_stale_signal_reaccel_min_volume", 1.0) or 1.0)
        if not (reaccel and range_ok and volume_ok):
            blockers.append(f"stale signal {stale_age:.1f} bars without reacceleration")

    multi_adverse_block = int(cfg.get("profit_alpha_derivatives_multi_adverse_block_count", 3) or 3)
    strong_adverse_block = int(cfg.get("profit_alpha_derivatives_multi_adverse_strong_count", 2) or 2)
    if adverse_count >= multi_adverse_block or strong_adverse >= strong_adverse_block:
        blockers.append(
            f"derivatives adverse stack {adverse_count}/{multi_adverse_block}, strong {strong_adverse}"
        )
    elif adverse_count > 0:
        reasons.extend(deriv_blockers[:2])

    ev_probability = _finite(_attr(ev_decision, "win_probability", None), None)
    if ev_probability is None:
        ev_probability = _first(values, "ev_win_probability", default=0.54) or 0.54
    net_r = _finite(_attr(ev_net, "expected_net_r", None), None)
    meta_key, meta = _meta_stats(meta_stats, engine, side)
    meta_samples = int(_finite(meta.get("sample_count", meta.get("trades", 0)), 0) or 0) if isinstance(meta, Mapping) else 0
    meta_expectancy = _finite(meta.get("expectancy_r", meta.get("avg_pnl_r")), None) if isinstance(meta, Mapping) else None
    if meta_samples >= int(cfg.get("profit_alpha_meta_min_samples", 8) or 8) and meta_expectancy is not None:
        if meta_expectancy < float(cfg.get("profit_alpha_meta_expectancy_block_below", -0.12) or -0.12):
            blockers.append(f"meta expectancy {meta_expectancy:.3f}R after {meta_samples} samples")
        else:
            score += _clamp(meta_expectancy * 10.0, -6.0, 6.0)
            reasons.append(f"meta expectancy {meta_expectancy:.3f}R/{meta_samples}")
    elif meta_samples > 0:
        reasons.append(f"meta warmup {meta_samples} samples")

    probability = 0.46 + (score - 50.0) * 0.004
    probability += (ev_probability - 0.50) * 0.45
    if net_r is not None:
        probability += _clamp(net_r, -0.20, 0.80) * 0.035
    if meta_expectancy is not None and meta_samples >= int(cfg.get("profit_alpha_meta_min_samples", 8) or 8):
        probability += _clamp(meta_expectancy, -0.40, 0.60) * float(
            cfg.get("profit_alpha_meta_probability_weight", 0.20) or 0.20
        )
    probability = _clamp(probability, 0.40, 0.70)

    if score < min_score:
        blockers.append(f"profit alpha score {score:.1f}<{min_score:.1f}")
    if probability < min_probability:
        blockers.append(f"profit alpha p {probability:.3f}<{min_probability:.3f}")

    risk_multiplier = _clamp(0.35 + (score - 60.0) / 40.0 * 0.55, 0.25, 1.0)
    risk_multiplier = min(risk_multiplier, deriv_risk)
    if strong_opposite:
        risk_multiplier = min(risk_multiplier, 0.55)
    if meta_expectancy is not None and meta_samples >= int(cfg.get("profit_alpha_meta_min_samples", 8) or 8):
        if meta_expectancy < 0:
            risk_multiplier = min(risk_multiplier, 0.70)
        elif meta_expectancy > 0.18:
            risk_multiplier = min(1.0, risk_multiplier * 1.08)

    overrides = _exit_overrides(engine, cfg)
    follow_bars = int(overrides.get("profit_alpha_follow_through_bars", cfg.get("profit_alpha_default_follow_through_bars", 3)) or 3)
    follow_min_mfe = float(
        overrides.get(
            "profit_alpha_follow_through_min_mfe_r",
            cfg.get("profit_alpha_default_follow_through_min_mfe_r", 0.35),
        )
        or 0.35
    )
    early_mae = float(
        overrides.get(
            "profit_alpha_early_exit_max_mae_r",
            cfg.get("profit_alpha_default_early_exit_max_mae_r", 0.75),
        )
        or 0.75
    )

    components = {
        "trend": round(float(trend), 4),
        "relative": round(float(relative), 4),
        "regime": round(float(regime), 4),
        "derivatives": round(float(derivatives), 4),
        "squeeze": round(float(squeeze), 4),
        "adverse_count": float(adverse_count),
        "strong_adverse_count": float(strong_adverse),
    }
    return ProfitAlphaDecision(
        allowed=not blockers,
        side=side,
        engine=engine,
        score=_clamp(score, 0.0, 100.0),
        probability=probability,
        risk_multiplier=risk_multiplier,
        exit_profile=engine if engine != "NO_TRADE" else "NONE",
        follow_through_bars=follow_bars,
        follow_through_min_mfe_r=follow_min_mfe,
        early_exit_max_mae_r=early_mae,
        reasons=tuple(dict.fromkeys(str(item) for item in reasons if item)),
        blockers=tuple(dict.fromkeys(str(item) for item in blockers if item)),
        components=components,
        meta_key=meta_key,
        meta_sample_count=meta_samples,
        meta_expectancy_r=meta_expectancy,
        exit_overrides=overrides,
    )


def apply_profit_alpha_exit_overrides(cfg: Mapping[str, Any] | None, decision: ProfitAlphaDecision | None) -> dict[str, Any]:
    effective = dict(cfg or {})
    if not isinstance(decision, ProfitAlphaDecision) or not decision.allowed:
        return effective
    effective.update({
        "fixed_take_profit_enabled": True,
        "partial_take_profit_enabled": True,
        "second_take_profit_enabled": True,
        "atr_trailing_enabled": True,
        "shadow_runner_exit_enabled": True,
        "runner_exit_enabled": True,
        "runner_chandelier_enabled": True,
        "profit_alpha_follow_through_enabled": True,
    })
    for key, value in dict(decision.exit_overrides or {}).items():
        effective[key] = value
    if "second_take_profit_r_multiple" in effective:
        effective["take_profit_r_multiple"] = float(effective["second_take_profit_r_multiple"])
    return effective


def evaluate_alpha_follow_through_exit(
    *,
    enabled: bool,
    bars_held: int,
    mfe_r: float,
    mae_r: float,
    tp1_filled: bool,
    follow_through_bars: int,
    follow_through_min_mfe_r: float,
    early_exit_max_mae_r: float,
) -> AlphaFollowThroughExit:
    if not enabled:
        return AlphaFollowThroughExit(False, "disabled")
    if tp1_filled:
        return AlphaFollowThroughExit(False, "tp1 already filled")
    bars = max(0, int(bars_held or 0))
    mfe = max(0.0, float(mfe_r or 0.0))
    mae = max(0.0, float(mae_r or 0.0))
    follow_bars = max(1, int(follow_through_bars or 1))
    min_mfe = max(0.0, float(follow_through_min_mfe_r or 0.0))
    max_mae = max(0.0, float(early_exit_max_mae_r or 0.0))

    if max_mae > 0 and mae >= max_mae and mfe < min(0.20, min_mfe):
        return AlphaFollowThroughExit(
            True,
            f"early adverse move MAE {mae:.2f}R with MFE {mfe:.2f}R",
        )
    if bars >= follow_bars and mfe < min_mfe:
        return AlphaFollowThroughExit(
            True,
            f"no follow-through after {bars} bars: MFE {mfe:.2f}R<{min_mfe:.2f}R",
        )
    return AlphaFollowThroughExit(False, f"hold: bars {bars}/{follow_bars}, MFE {mfe:.2f}R")
