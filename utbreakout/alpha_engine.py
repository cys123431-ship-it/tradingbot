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

from .direction_engine import evaluate_direction_engine
from .intelligence import (
    build_signal_attribution,
    default_intelligence_config,
    evaluate_data_quality_engine,
    evaluate_execution_quality_engine,
    evaluate_market_regime_engine,
    evaluate_overfit_governance,
    evaluate_protection_health_engine,
)


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
    entry_type: str = "TREND_CONTINUATION"
    direction_score: float = 0.0
    exit_policy: str = ""
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    components: Mapping[str, Any] = field(default_factory=dict)
    meta_key: str = ""
    meta_sample_count: int = 0
    meta_expectancy_r: float | None = None
    exit_overrides: Mapping[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        state = "ALLOW" if self.allowed else "BLOCK"
        return (
            f"{state} {self.engine}/{self.entry_type} score {self.score:.1f} "
            f"dir={self.direction_score:.1f} p={self.probability:.2f} "
            f"risk x{self.risk_multiplier:.2f}"
        )


@dataclass(frozen=True)
class AlphaFollowThroughExit:
    should_exit: bool
    reason: str


@dataclass(frozen=True)
class EntryEdgeDecision:
    allowed: bool
    side: str
    engine: str
    score: float
    probability: float
    net_expectancy_r: float | None
    risk_multiplier: float
    exit_profile: str
    ev_mode: str = "NONE"
    ev_score: float | None = None
    alpha_score: float | None = None
    ev_probability: float | None = None
    alpha_probability: float | None = None
    mtf_alignment: str = "n/a"
    entry_type: str = "TREND_CONTINUATION"
    direction_score: float = 0.0
    exit_policy: str = ""
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    components: Mapping[str, float] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        state = "ALLOW" if self.allowed else "BLOCK"
        net = (
            f"net={self.net_expectancy_r:.3f}R"
            if self.net_expectancy_r is not None
            else "net=pending"
        )
        return (
            f"{state} {self.engine}/{self.entry_type} score {self.score:.1f} "
            f"dir={self.direction_score:.1f} p={self.probability:.3f} "
            f"{net} risk x{self.risk_multiplier:.2f}"
        )


def default_profit_alpha_config() -> dict[str, Any]:
    cfg = {
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
        "direction_engine_min_score": 62.0,
        "direction_engine_opposite_regime_min_score": 68.0,
        "entry_type_max_chase_extension_atr": 2.35,
        "entry_type_pullback_extension_atr": 1.35,
        "entry_type_breakout_min_range": 1.12,
        "entry_type_sweep_wick_ratio": 0.38,
        "exit_meta_min_samples": 8,
        "exit_meta_expectancy_block_below": -0.16,
        "exit_meta_expectancy_reduce_below": 0.0,
        "take_profit_front_run_atr": 0.14,
        "take_profit_front_run_pct": 0.055,
        "structure_stop_buffer_atr": 0.28,
        "soft_stop_enabled": True,
        "soft_stop_confirm_bars": 2,
        "near_miss_tp_enabled": True,
        "near_miss_tp_arm_ratio": 0.86,
        "near_miss_tp_lock_r": 0.28,
        "profit_alpha_follow_through_enabled": True,
        "profit_alpha_default_follow_through_bars": 3,
        "profit_alpha_default_follow_through_min_mfe_r": 0.35,
        "profit_alpha_default_early_exit_max_mae_r": 0.75,
        "entry_edge_enabled": True,
        "entry_edge_min_score": 68.0,
        "entry_edge_long_min_score": 69.0,
        "entry_edge_short_min_score": 68.0,
        "entry_edge_min_probability": 0.555,
        "entry_edge_long_min_probability": 0.560,
        "entry_edge_short_min_probability": 0.555,
        "entry_edge_min_net_expectancy_r": 0.14,
    }
    cfg.update(default_intelligence_config())
    return cfg


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


def _extension_atr(side: str, values: Mapping[str, Any]) -> float | None:
    direct = _first(values, "extension_atr", "bias_continuation_extension_atr", "ev_extension_atr", default=None)
    if direct is not None:
        return max(0.0, float(direct))
    close = _first(values, "close", "entry_price", default=None)
    atr_pct = _first(values, "atr_pct", default=None)
    if close is None or close <= 0 or atr_pct is None or atr_pct <= 0:
        return None
    candidates = []
    for key in ("ema50", "vwap", "bb_mid"):
        ref = _first(values, key, default=None)
        if ref is None or ref <= 0:
            continue
        if side == "long" and close < ref:
            continue
        if side == "short" and close > ref:
            continue
        dist_pct = abs(close - ref) / max(abs(close), 1e-9) * 100.0
        candidates.append(dist_pct / max(atr_pct, 1e-9))
    return min(candidates) if candidates else None


def _entry_type(
    side: str,
    values: Mapping[str, Any],
    *,
    engine: str,
    squeeze_active: bool,
    cfg: Mapping[str, Any],
) -> tuple[str, float, tuple[str, ...], tuple[str, ...]]:
    extension = _extension_atr(side, values)
    range_expansion = _first(values, "range_expansion", "range_expansion_ratio", default=1.0) or 1.0
    close = _first(values, "close", "entry_price", default=None)
    open_ = _first(values, "open", default=None)
    high = _first(values, "high", default=None)
    low = _first(values, "low", default=None)
    reaccel = bool(_attr(values, "reacceleration", values.get("ev_reacceleration", False)))
    reasons: list[str] = []
    blockers: list[str] = []

    if extension is not None:
        reasons.append(f"extension {extension:.2f}ATR")
    reasons.append(f"range {range_expansion:.2f}")

    wick_ratio = 0.0
    if close is not None and open_ is not None and high is not None and low is not None and high > low:
        body_high = max(close, open_)
        body_low = min(close, open_)
        if side == "long":
            wick_ratio = max(0.0, min(close, open_) - low) / max(high - low, 1e-9)
        else:
            wick_ratio = max(0.0, high - max(close, open_)) / max(high - low, 1e-9)
        reasons.append(f"sweep wick {wick_ratio:.2f}")

    max_chase = float(cfg.get("entry_type_max_chase_extension_atr", 2.35) or 2.35)
    pullback_extension = float(cfg.get("entry_type_pullback_extension_atr", 1.35) or 1.35)
    breakout_range = float(cfg.get("entry_type_breakout_min_range", 1.12) or 1.12)
    sweep_wick = float(cfg.get("entry_type_sweep_wick_ratio", 0.38) or 0.38)

    if wick_ratio >= sweep_wick and range_expansion >= 0.95:
        return "LIQUIDITY_SWEEP_REVERSAL", extension or 0.0, tuple(reasons), tuple(blockers)
    if squeeze_active or engine == "SQUEEZE_BREAKOUT":
        return "SQUEEZE_BREAKOUT", extension or 0.0, tuple(reasons), tuple(blockers)
    if extension is not None and extension >= max_chase and not reaccel:
        blockers.append(f"chase extension {extension:.2f}ATR without reacceleration")
        return "OVEREXTENDED_WAIT_PULLBACK", extension, tuple(reasons), tuple(blockers)
    if extension is not None and extension >= pullback_extension:
        return "PULLBACK_RETEST", extension, tuple(reasons), tuple(blockers)
    if range_expansion >= breakout_range:
        return "BREAKOUT", extension or 0.0, tuple(reasons), tuple(blockers)
    return "TREND_CONTINUATION", extension or 0.0, tuple(reasons), tuple(blockers)


def _exit_policy_name(engine: str, entry_type: str) -> str:
    if entry_type == "LIQUIDITY_SWEEP_REVERSAL":
        return "SWEEP_FAST_CAPTURE"
    if entry_type == "SQUEEZE_BREAKOUT" or engine == "SQUEEZE_BREAKOUT":
        return "SQUEEZE_RELEASE_LADDER"
    if entry_type == "PULLBACK_RETEST":
        return "PULLBACK_BALANCED_LADDER"
    if engine in {"STRONG_UPTREND_LONG", "STRONG_DOWNTREND_SHORT", "TREND_CONTINUATION"}:
        return "TREND_RUNNER"
    return "CHOP_DEFENSIVE_LADDER"


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


def _exit_overrides(
    engine: str,
    cfg: Mapping[str, Any],
    *,
    entry_type: str = "TREND_CONTINUATION",
    direction_score: float | None = None,
    exit_meta_expectancy: float | None = None,
) -> dict[str, Any]:
    front_run = {
        "take_profit_front_run_atr": float(cfg.get("take_profit_front_run_atr", 0.14) or 0.14),
        "take_profit_front_run_pct": float(cfg.get("take_profit_front_run_pct", 0.055) or 0.055),
        "structure_stop_buffer_atr": float(cfg.get("structure_stop_buffer_atr", 0.28) or 0.28),
        "soft_stop_enabled": bool(cfg.get("soft_stop_enabled", True)),
        "soft_stop_confirm_bars": int(cfg.get("soft_stop_confirm_bars", 2) or 2),
        "near_miss_tp_enabled": bool(cfg.get("near_miss_tp_enabled", True)),
        "near_miss_tp_arm_ratio": float(cfg.get("near_miss_tp_arm_ratio", 0.86) or 0.86),
        "near_miss_tp_lock_r": float(cfg.get("near_miss_tp_lock_r", 0.28) or 0.28),
    }

    def _profile_controls(
        *,
        front_atr: float,
        front_pct: float,
        structure_buffer: float,
        soft_confirm: int,
        arm_ratio: float,
        lock_r: float,
    ) -> dict[str, Any]:
        profile = dict(front_run)
        profile["take_profit_front_run_atr"] = max(float(profile["take_profit_front_run_atr"]), float(front_atr))
        profile["take_profit_front_run_pct"] = max(float(profile["take_profit_front_run_pct"]), float(front_pct))
        profile["structure_stop_buffer_atr"] = max(float(profile["structure_stop_buffer_atr"]), float(structure_buffer))
        profile["soft_stop_confirm_bars"] = max(1, int(soft_confirm or profile["soft_stop_confirm_bars"]))
        profile["near_miss_tp_arm_ratio"] = min(float(profile["near_miss_tp_arm_ratio"]), float(arm_ratio))
        profile["near_miss_tp_lock_r"] = max(float(profile["near_miss_tp_lock_r"]), float(lock_r))
        return profile
    if entry_type == "LIQUIDITY_SWEEP_REVERSAL":
        result = {
            "partial_take_profit_ratio": 0.35,
            "second_take_profit_r_multiple": 2.05,
            "second_take_profit_ratio": 0.30,
            "runner_pct": 0.25,
            "take_profit_r_multiple": 2.05,
            "atr_trailing_activation_r": 0.95,
            "atr_trailing_multiplier": 2.45,
            "runner_chandelier_multiplier": 2.45,
            "ev_time_stop_bars": 5,
            "ev_time_stop_min_mfe_r": 0.30,
            "profit_alpha_follow_through_bars": 2,
            "profit_alpha_follow_through_min_mfe_r": 0.28,
            "profit_alpha_early_exit_max_mae_r": 0.65,
        }
        result.update(_profile_controls(
            front_atr=0.20,
            front_pct=0.080,
            structure_buffer=0.24,
            soft_confirm=1,
            arm_ratio=0.82,
            lock_r=0.36,
        ))
        return result
    if entry_type == "PULLBACK_RETEST":
        result = {
            "partial_take_profit_ratio": 0.28,
            "second_take_profit_r_multiple": 2.45,
            "second_take_profit_ratio": 0.35,
            "runner_pct": 0.37,
            "take_profit_r_multiple": 2.45,
            "atr_trailing_activation_r": 1.05,
            "atr_trailing_multiplier": 2.85,
            "runner_chandelier_multiplier": 2.85,
            "ev_time_stop_bars": 7,
            "ev_time_stop_min_mfe_r": 0.38,
            "profit_alpha_follow_through_bars": 3,
            "profit_alpha_follow_through_min_mfe_r": 0.35,
            "profit_alpha_early_exit_max_mae_r": 0.70,
        }
        result.update(_profile_controls(
            front_atr=0.14,
            front_pct=0.055,
            structure_buffer=0.32,
            soft_confirm=2,
            arm_ratio=0.86,
            lock_r=0.28,
        ))
        return result
    if engine in {"STRONG_UPTREND_LONG", "STRONG_DOWNTREND_SHORT", "TREND_CONTINUATION"}:
        result = {
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
        if direction_score is not None and direction_score >= 78.0:
            result["runner_pct"] = 0.55
            result["second_take_profit_r_multiple"] = 3.40
            result["take_profit_r_multiple"] = 3.40
        result.update(_profile_controls(
            front_atr=0.10,
            front_pct=0.040,
            structure_buffer=0.34,
            soft_confirm=2,
            arm_ratio=0.88,
            lock_r=0.24,
        ))
        return result
    if engine == "SQUEEZE_BREAKOUT":
        result = {
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
        result.update(_profile_controls(
            front_atr=0.16,
            front_pct=0.065,
            structure_buffer=0.28,
            soft_confirm=2,
            arm_ratio=0.84,
            lock_r=0.32,
        ))
        return result
    result = {
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
    if exit_meta_expectancy is not None and exit_meta_expectancy < 0:
        result["partial_take_profit_ratio"] = max(0.35, float(result["partial_take_profit_ratio"]))
        result["second_take_profit_r_multiple"] = min(2.05, float(result["second_take_profit_r_multiple"]))
        result["take_profit_r_multiple"] = result["second_take_profit_r_multiple"]
        result["runner_pct"] = min(0.25, float(result["runner_pct"]))
        result["atr_trailing_multiplier"] = min(2.45, float(result["atr_trailing_multiplier"]))
        result["runner_chandelier_multiplier"] = min(2.45, float(result["runner_chandelier_multiplier"]))
    result.update(_profile_controls(
        front_atr=0.18,
        front_pct=0.080,
        structure_buffer=0.26,
        soft_confirm=1,
        arm_ratio=0.82,
        lock_r=0.36,
    ))
    return result


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


def _exit_meta_stats(meta_stats: Any, side: str, engine: str, exit_policy: str) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(meta_stats, Mapping) or not exit_policy:
        return f"{side}:{engine}:{exit_policy}", {}
    keys = (
        f"{side}:{engine}:{exit_policy}",
        f"{engine}:{exit_policy}",
        f"exit:{exit_policy}",
    )
    for key in keys:
        item = meta_stats.get(key)
        if isinstance(item, Mapping):
            return key, item
    return f"{side}:{engine}:{exit_policy}", {}


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
    entry_type, extension_atr, entry_type_reasons, entry_type_blockers = _entry_type(
        side,
        values,
        engine=engine,
        squeeze_active=squeeze_active,
        cfg=cfg,
    )
    exit_policy = _exit_policy_name(engine, entry_type)
    opposite_regime = regime_opposite > 0 and regime_aligned == 0
    direction_decision = evaluate_direction_engine(
        side=side,
        values=values,
        trend=trend,
        relative=relative,
        regime=regime,
        derivatives=derivatives,
        squeeze=squeeze,
        ev_decision=ev_decision,
        config=cfg,
        opposite_regime=opposite_regime,
    )
    direction_score = direction_decision.score
    market_regime_decision = evaluate_market_regime_engine(side=side, values=values, config=cfg)
    data_quality_decision = evaluate_data_quality_engine(values=values, config=cfg)
    execution_quality_decision = evaluate_execution_quality_engine(values=values, config=cfg)
    protection_health_decision = evaluate_protection_health_engine(values=values, config=cfg)
    attribution = build_signal_attribution(
        side=side,
        engine=engine,
        entry_type=entry_type,
        exit_policy=exit_policy,
        market_regime=market_regime_decision.regime,
        values=values,
    )
    overfit_decision = evaluate_overfit_governance(
        meta_stats=meta_stats,
        side=side,
        engine=engine,
        entry_type=entry_type,
        exit_policy=exit_policy,
        regime=market_regime_decision.regime,
        config=cfg,
    )
    if engine == "NO_TRADE":
        score = trend * 0.30 + relative * 0.25 + regime * 0.15 + derivatives * 0.20 + squeeze * 0.10
    elif engine == "SQUEEZE_BREAKOUT":
        score = trend * 0.25 + relative * 0.20 + regime * 0.15 + derivatives * 0.20 + squeeze * 0.20
    else:
        score = trend * 0.32 + relative * 0.26 + regime * 0.16 + derivatives * 0.18 + squeeze * 0.08
    score = score * 0.78 + direction_score * 0.22
    score += (market_regime_decision.score - 55.0) * 0.04
    score += (data_quality_decision.score - 80.0) * 0.02
    score += (execution_quality_decision.score - 80.0) * 0.02
    score += (overfit_decision.score - 72.0) * 0.03
    score = _clamp(score, 0.0, 100.0)

    blockers: list[str] = []
    reasons: list[str] = []
    reasons.extend(trend_reasons[:4])
    reasons.extend(relative_reasons[:3])
    reasons.extend(regime_reasons[:3])
    reasons.extend(deriv_reasons[:3])
    reasons.extend(squeeze_reasons[:2])
    reasons.extend(direction_decision.reasons[:3])
    reasons.extend(entry_type_reasons[:3])
    reasons.extend(market_regime_decision.reasons[:3])
    reasons.extend(data_quality_decision.reasons[:2])
    reasons.extend(execution_quality_decision.reasons[:2])
    reasons.extend(protection_health_decision.reasons[:2])
    reasons.extend(overfit_decision.reasons[:2])
    reasons.append(f"entry type {entry_type}")
    reasons.append(f"exit policy {exit_policy}")
    reasons.append(f"attribution {attribution.key}")

    if engine == "NO_TRADE":
        blockers.append("no alpha sub-strategy selected")
    blockers.extend(direction_decision.blockers[:3])
    blockers.extend(entry_type_blockers[:2])
    if not market_regime_decision.allowed:
        blockers.extend(f"Market Regime: {item}" for item in market_regime_decision.blockers[:2])
    if not data_quality_decision.allowed:
        blockers.extend(f"Data Quality: {item}" for item in data_quality_decision.blockers[:2])
    if not execution_quality_decision.allowed:
        blockers.extend(f"Execution Quality: {item}" for item in execution_quality_decision.blockers[:2])
    if not protection_health_decision.allowed:
        blockers.extend(f"Protection Health: {item}" for item in protection_health_decision.blockers[:2])
    if not overfit_decision.allowed:
        blockers.extend(f"Overfit Governance: {item}" for item in overfit_decision.blockers[:3])

    base_min_score = float(cfg.get("profit_alpha_min_score", 68.0) or 68.0)
    min_score = float(cfg.get(f"profit_alpha_{side}_min_score", base_min_score) or base_min_score)
    base_min_probability = float(cfg.get("profit_alpha_min_probability", 0.555) or 0.555)
    min_probability = float(
        cfg.get(f"profit_alpha_{side}_min_probability", base_min_probability)
        or base_min_probability
    )
    if opposite_regime:
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

    exit_meta_key, exit_meta = _exit_meta_stats(meta_stats, side, engine, exit_policy)
    exit_meta_samples = int(_finite(exit_meta.get("sample_count", exit_meta.get("trades", 0)), 0) or 0) if isinstance(exit_meta, Mapping) else 0
    exit_meta_expectancy = _finite(exit_meta.get("expectancy_r", exit_meta.get("avg_pnl_r")), None) if isinstance(exit_meta, Mapping) else None
    if exit_meta_samples >= int(cfg.get("exit_meta_min_samples", 8) or 8) and exit_meta_expectancy is not None:
        if exit_meta_expectancy < float(cfg.get("exit_meta_expectancy_block_below", -0.16) or -0.16):
            blockers.append(
                f"exit meta {exit_policy} {exit_meta_expectancy:.3f}R after {exit_meta_samples} samples"
            )
        else:
            score += _clamp(exit_meta_expectancy * 8.0, -5.0, 5.0)
            reasons.append(f"exit meta {exit_policy} {exit_meta_expectancy:.3f}R/{exit_meta_samples}")
    elif exit_meta_samples > 0:
        reasons.append(f"exit meta warmup {exit_policy} {exit_meta_samples}")

    probability = 0.46 + (score - 50.0) * 0.004
    probability += (ev_probability - 0.50) * 0.45
    if net_r is not None:
        probability += _clamp(net_r, -0.20, 0.80) * 0.035
    if meta_expectancy is not None and meta_samples >= int(cfg.get("profit_alpha_meta_min_samples", 8) or 8):
        probability += _clamp(meta_expectancy, -0.40, 0.60) * float(
            cfg.get("profit_alpha_meta_probability_weight", 0.20) or 0.20
        )
    if exit_meta_expectancy is not None and exit_meta_samples >= int(cfg.get("exit_meta_min_samples", 8) or 8):
        probability += _clamp(exit_meta_expectancy, -0.40, 0.60) * 0.12
    probability = _clamp(probability, 0.40, 0.70)

    if score < min_score:
        blockers.append(f"profit alpha score {score:.1f}<{min_score:.1f}")
    if probability < min_probability:
        blockers.append(f"profit alpha p {probability:.3f}<{min_probability:.3f}")

    risk_multiplier = _clamp(0.35 + (score - 60.0) / 40.0 * 0.55, 0.25, 1.0)
    risk_multiplier = min(risk_multiplier, deriv_risk)
    if strong_opposite:
        risk_multiplier = min(risk_multiplier, 0.55)
    if entry_type == "OVEREXTENDED_WAIT_PULLBACK":
        risk_multiplier = min(risk_multiplier, 0.35)
    elif entry_type == "PULLBACK_RETEST":
        risk_multiplier = min(risk_multiplier, 0.88)
    if meta_expectancy is not None and meta_samples >= int(cfg.get("profit_alpha_meta_min_samples", 8) or 8):
        if meta_expectancy < 0:
            risk_multiplier = min(risk_multiplier, 0.70)
        elif meta_expectancy > 0.18:
            risk_multiplier = min(1.0, risk_multiplier * 1.08)
    if exit_meta_expectancy is not None and exit_meta_samples >= int(cfg.get("exit_meta_min_samples", 8) or 8):
        if exit_meta_expectancy < float(cfg.get("exit_meta_expectancy_reduce_below", 0.0) or 0.0):
            risk_multiplier = min(risk_multiplier, 0.78)
    risk_multiplier = min(
        risk_multiplier,
        market_regime_decision.risk_multiplier,
        data_quality_decision.risk_multiplier,
        execution_quality_decision.risk_multiplier,
        protection_health_decision.risk_multiplier,
        overfit_decision.risk_multiplier,
    )

    overrides = _exit_overrides(
        engine,
        cfg,
        entry_type=entry_type,
        direction_score=direction_score,
        exit_meta_expectancy=exit_meta_expectancy,
    )
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
        "direction": round(float(direction_score), 4),
        "direction_min_score": round(float(direction_decision.min_score), 4),
        "direction_risk_multiplier": round(float(direction_decision.risk_multiplier), 4),
        "extension_atr": round(float(extension_atr or 0.0), 4),
        "adverse_count": float(adverse_count),
        "strong_adverse_count": float(strong_adverse),
        "exit_meta_samples": float(exit_meta_samples),
        "exit_meta_expectancy_r": float(exit_meta_expectancy or 0.0),
        "market_regime_engine_score": round(float(market_regime_decision.score), 4),
        "market_regime_engine_risk": round(float(market_regime_decision.risk_multiplier), 4),
        "data_quality_score": round(float(data_quality_decision.score), 4),
        "data_quality_risk": round(float(data_quality_decision.risk_multiplier), 4),
        "execution_quality_score": round(float(execution_quality_decision.score), 4),
        "execution_quality_risk": round(float(execution_quality_decision.risk_multiplier), 4),
        "protection_health_score": round(float(protection_health_decision.score), 4),
        "overfit_governance_score": round(float(overfit_decision.score), 4),
        "overfit_governance_risk": round(float(overfit_decision.risk_multiplier), 4),
        "overfit_sample_count": round(float(overfit_decision.components.get("sample_count", 0.0)), 4),
    }
    if isinstance(attribution.tags, Mapping):
        for key, value in attribution.tags.items():
            components[f"tag_{key}"] = str(value)
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
        entry_type=entry_type,
        direction_score=direction_score,
        exit_policy=exit_policy,
        reasons=tuple(dict.fromkeys(str(item) for item in reasons if item)),
        blockers=tuple(dict.fromkeys(str(item) for item in blockers if item)),
        components=components,
        meta_key=meta_key if not exit_meta else exit_meta_key,
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


def build_entry_edge_decision(
    *,
    side: str,
    ev_decision: Any = None,
    alpha_decision: ProfitAlphaDecision | None = None,
    ev_net: Any = None,
    ev_exit: Any = None,
    config: Mapping[str, Any] | None = None,
) -> EntryEdgeDecision:
    cfg = default_profit_alpha_config()
    if isinstance(config, Mapping):
        cfg.update(dict(config))
    side = str(side or "").lower()

    if not bool(cfg.get("entry_edge_enabled", True)):
        return EntryEdgeDecision(
            allowed=True,
            side=side,
            engine="DISABLED",
            score=100.0,
            probability=1.0,
            net_expectancy_r=None,
            risk_multiplier=1.0,
            exit_profile="BASELINE",
            reasons=("entry edge disabled",),
        )

    blockers: list[str] = []
    reasons: list[str] = []

    ev_allowed = bool(_attr(ev_decision, "allowed", False)) if ev_decision is not None else False
    ev_mode = str(_attr(ev_decision, "mode", "NONE") or "NONE")
    ev_score = _finite(_attr(ev_decision, "score", None), None)
    ev_probability = _finite(_attr(ev_decision, "win_probability", None), None)
    ev_risk = _finite(_attr(ev_decision, "risk_multiplier", None), 1.0) or 1.0
    mtf_alignment = str(_attr(ev_decision, "mtf_alignment", "n/a") or "n/a")
    ev_blockers = _attr(ev_decision, "blockers", ()) or ()
    ev_reasons = _attr(ev_decision, "reasons", ()) or ()
    if ev_decision is None:
        blockers.append("EV candidate unavailable")
    elif not ev_allowed:
        blockers.extend(f"EV: {item}" for item in list(ev_blockers)[:4])
    else:
        reasons.extend(f"EV: {item}" for item in list(ev_reasons)[:4])

    if not isinstance(alpha_decision, ProfitAlphaDecision):
        alpha_allowed = False
        alpha_score = None
        alpha_probability = None
        alpha_risk = 1.0
        alpha_engine = "NO_ALPHA"
        alpha_exit = "NONE"
        alpha_entry_type = "NONE"
        alpha_direction_score = 0.0
        alpha_exit_policy = "NONE"
        alpha_components = {}
        blockers.append("Profit Alpha unavailable")
    else:
        alpha_allowed = bool(alpha_decision.allowed)
        alpha_score = float(alpha_decision.score)
        alpha_probability = float(alpha_decision.probability)
        alpha_risk = float(alpha_decision.risk_multiplier)
        alpha_engine = str(alpha_decision.engine or "ALPHA")
        alpha_exit = str(alpha_decision.exit_profile or alpha_engine)
        alpha_entry_type = str(alpha_decision.entry_type or "TREND_CONTINUATION")
        alpha_direction_score = float(alpha_decision.direction_score or 0.0)
        alpha_exit_policy = str(alpha_decision.exit_policy or alpha_exit)
        alpha_components = dict(alpha_decision.components or {})
        if not alpha_allowed:
            blockers.extend(f"Alpha: {item}" for item in list(alpha_decision.blockers)[:4])
        else:
            reasons.extend(f"Alpha: {item}" for item in list(alpha_decision.reasons)[:4])

    net_expectancy_r = _finite(_attr(ev_net, "expected_net_r", None), None)
    if ev_exit is not None and not bool(_attr(ev_exit, "executable", True)):
        blockers.append(f"Exit ladder: {_attr(ev_exit, 'reason', 'not executable')}")
    if ev_net is not None and not bool(_attr(ev_net, "allowed", True)):
        blockers.append(f"Net edge: {_attr(ev_net, 'reason', 'not allowed')}")

    score_inputs = []
    if ev_score is not None:
        score_inputs.append((float(ev_score), 0.42))
    if alpha_score is not None:
        score_inputs.append((float(alpha_score), 0.58))
    if score_inputs:
        total_weight = sum(weight for _, weight in score_inputs)
        score = sum(value * weight for value, weight in score_inputs) / max(total_weight, 1e-9)
    else:
        score = 0.0

    probability_inputs = []
    if ev_probability is not None:
        probability_inputs.append((float(ev_probability), 0.45))
    if alpha_probability is not None:
        probability_inputs.append((float(alpha_probability), 0.55))
    if probability_inputs:
        total_weight = sum(weight for _, weight in probability_inputs)
        probability = sum(value * weight for value, weight in probability_inputs) / max(total_weight, 1e-9)
    else:
        probability = 0.0
    if net_expectancy_r is not None:
        probability += _clamp(net_expectancy_r, -0.20, 0.80) * 0.015
    probability = _clamp(probability, 0.0, 1.0)

    base_min_score = float(cfg.get("entry_edge_min_score", 68.0) or 68.0)
    min_score = float(cfg.get(f"entry_edge_{side}_min_score", base_min_score) or base_min_score)
    base_min_probability = float(cfg.get("entry_edge_min_probability", 0.555) or 0.555)
    min_probability = float(
        cfg.get(f"entry_edge_{side}_min_probability", base_min_probability)
        or base_min_probability
    )
    if score < min_score:
        blockers.append(f"Entry Edge score {score:.1f}<{min_score:.1f}")
    if probability < min_probability:
        blockers.append(f"Entry Edge p {probability:.3f}<{min_probability:.3f}")
    if net_expectancy_r is not None:
        min_net = float(cfg.get("entry_edge_min_net_expectancy_r", 0.14) or 0.14)
        if net_expectancy_r < min_net:
            blockers.append(f"Entry Edge net {net_expectancy_r:.3f}R<{min_net:.2f}R")

    risk_multiplier = _clamp(min(float(ev_risk), float(alpha_risk)), 0.0, 1.0)
    engine = alpha_engine if alpha_engine not in {"NO_ALPHA", "NO_TRADE"} else ev_mode
    components = {
        "ev_score": float(ev_score or 0.0),
        "alpha_score": float(alpha_score or 0.0),
        "ev_probability": float(ev_probability or 0.0),
        "alpha_probability": float(alpha_probability or 0.0),
        "ev_risk_multiplier": float(ev_risk),
        "alpha_risk_multiplier": float(alpha_risk),
        "alpha_direction_score": float(alpha_direction_score),
    }
    for key, value in alpha_components.items():
        parsed = _finite(value, None)
        if parsed is not None:
            components[f"alpha_{key}"] = float(parsed)

    return EntryEdgeDecision(
        allowed=not blockers,
        side=side,
        engine=engine,
        score=_clamp(score, 0.0, 100.0),
        probability=probability,
        net_expectancy_r=net_expectancy_r,
        risk_multiplier=risk_multiplier,
        exit_profile=alpha_exit,
        ev_mode=ev_mode,
        ev_score=ev_score,
        alpha_score=alpha_score,
        ev_probability=ev_probability,
        alpha_probability=alpha_probability,
        mtf_alignment=mtf_alignment,
        entry_type=alpha_entry_type,
        direction_score=alpha_direction_score,
        exit_policy=alpha_exit_policy,
        reasons=tuple(dict.fromkeys(str(item) for item in reasons if item)),
        blockers=tuple(dict.fromkeys(str(item) for item in blockers if item)),
        components=components,
    )


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
