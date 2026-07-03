"""Decision intelligence helpers for UTBreakout execution gates.

These helpers are pure: they do not select scanner candidates, mutate runtime
state, size orders, or place/cancel exchange orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Mapping, Sequence

from .adaptive import evaluate_shadow_triple_barrier
from .performance import (
    apply_multiple_testing_penalty,
    calculate_performance_metrics,
    deflated_sharpe_proxy,
    pbo_proxy,
    walk_forward_splits,
)


@dataclass(frozen=True)
class IntelligenceDecision:
    allowed: bool
    name: str
    score: float
    risk_multiplier: float = 1.0
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    components: Mapping[str, float] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        state = "ALLOW" if self.allowed else "BLOCK"
        return f"{state} {self.name} score {self.score:.1f} risk x{self.risk_multiplier:.2f}"


@dataclass(frozen=True)
class MarketRegimeDecision(IntelligenceDecision):
    regime: str = "MIXED"
    preferred_side: str = "both"


@dataclass(frozen=True)
class SignalAttribution:
    tags: Mapping[str, str] = field(default_factory=dict)
    key: str = ""
    summary: str = ""


@dataclass(frozen=True)
class OverfitBacktestReport:
    passed: bool
    pbo: float
    adjusted_expectancy_r: float
    adjusted_profit_factor: float
    deflated_sharpe_pass: bool
    train_trade_count: int
    test_trade_count: int
    reasons: tuple[str, ...] = ()


def default_intelligence_config() -> dict[str, Any]:
    return {
        "market_regime_engine_enabled": True,
        "market_regime_opposite_risk_multiplier": 0.62,
        "market_regime_chop_risk_multiplier": 0.72,
        "market_regime_high_vol_risk_multiplier": 0.58,
        "market_regime_block_extreme_chaos": True,
        "market_regime_extreme_chaos_score": 82.0,
        "data_quality_engine_enabled": True,
        "data_quality_min_derivative_sources": 2,
        "data_quality_block_missing_price": True,
        "data_quality_block_on_stale_feed": True,
        "data_quality_max_feed_age_sec": 240.0,
        "data_quality_missing_derivatives_risk_multiplier": 0.78,
        "execution_quality_engine_enabled": True,
        "execution_quality_max_spread_pct": 0.12,
        "execution_quality_soft_spread_pct": 0.07,
        "execution_quality_min_depth_usdt": 25_000.0,
        "execution_quality_low_depth_risk_multiplier": 0.72,
        "protection_health_engine_enabled": True,
        "protection_health_require_plan_fields": False,
        "protection_health_execution_gate_enabled": True,
        "protection_health_block_missing_stop": True,
        "protection_health_block_missing_take_profit": True,
        "signal_attribution_engine_enabled": True,
        "strategy_replay_engine_enabled": True,
        "overfit_governance_enabled": True,
        "overfit_min_samples": 12,
        "overfit_warmup_risk_multiplier": 0.92,
        "overfit_expectancy_block_below": -0.12,
        "overfit_oos_expectancy_block_below": -0.05,
        "overfit_min_profit_factor": 0.92,
        "overfit_max_pbo": 0.65,
        "overfit_multiple_testing_trials": 24,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _cfg(config: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = default_intelligence_config()
    if isinstance(config, Mapping):
        merged.update(dict(config))
    return merged


def _first(values: Mapping[str, Any], *keys: str, default: float | None = None) -> float | None:
    for key in keys:
        parsed = _finite(values.get(key), None)
        if parsed is not None:
            return parsed
    return default


def _direction_text(side: str) -> str:
    side = str(side or "").lower()
    if side == "long":
        return "long"
    if side == "short":
        return "short"
    return "both"


def _is_aligned(side: str, direction: Any) -> bool:
    text = str(direction or "").strip().lower()
    return (side == "long" and text in {"long", "up", "bull", "bullish"}) or (
        side == "short" and text in {"short", "down", "bear", "bearish"}
    )


def _is_opposite(side: str, direction: Any) -> bool:
    text = str(direction or "").strip().lower()
    return (side == "long" and text in {"short", "down", "bear", "bearish"}) or (
        side == "short" and text in {"long", "up", "bull", "bullish"}
    )


def evaluate_market_regime_engine(
    *, side: str, values: Mapping[str, Any] | None = None, config: Mapping[str, Any] | None = None
) -> MarketRegimeDecision:
    cfg = _cfg(config)
    if not bool(cfg.get("market_regime_engine_enabled", True)):
        return MarketRegimeDecision(True, "MARKET_REGIME", 100.0, 1.0, ("disabled",), regime="DISABLED")

    values = values if isinstance(values, Mapping) else {}
    side = _direction_text(side)
    adx = _first(values, "adx", "ADX", default=18.0) or 18.0
    chop = _first(values, "chop", "choppiness", default=50.0) or 50.0
    volume = _first(values, "volume_ratio", "relative_volume", default=1.0) or 1.0
    range_expansion = _first(values, "range_expansion", "range_expansion_ratio", default=1.0) or 1.0
    atr_pct = _first(values, "atr_pct", "atr_percent", default=0.0) or 0.0
    squeeze_pct = _first(values, "bb_width_percentile", "squeeze_bb_pct", default=50.0) or 50.0
    oi = _first(values, "open_interest_change_1h", "open_interest_delta_pct", default=0.0) or 0.0

    aligned = 0
    opposite = 0
    context = values.get("market_regime_context")
    items = context.get("items") if isinstance(context, Mapping) else None
    if isinstance(items, Mapping):
        for item in items.values():
            if not isinstance(item, Mapping):
                continue
            if _is_aligned(side, item.get("direction")):
                aligned += 1
            elif _is_opposite(side, item.get("direction")):
                opposite += 1

    trend_score = _clamp((adx - 12.0) / 22.0, 0.0, 1.0) * 45.0
    trend_score += _clamp((58.0 - chop) / 28.0, 0.0, 1.0) * 25.0
    trend_score += _clamp((volume - 0.9) / 0.7, 0.0, 1.0) * 15.0
    trend_score += _clamp((range_expansion - 0.95) / 0.45, 0.0, 1.0) * 15.0

    high_vol_score = _clamp((atr_pct - 1.5) / 2.0, 0.0, 1.0) * 55.0
    high_vol_score += _clamp((range_expansion - 1.3) / 0.8, 0.0, 1.0) * 30.0
    high_vol_score += _clamp((chop - 55.0) / 20.0, 0.0, 1.0) * 15.0

    compression_score = _clamp((30.0 - squeeze_pct) / 30.0, 0.0, 1.0) * 55.0
    compression_score += _clamp((0.9 - range_expansion) / 0.35, 0.0, 1.0) * 20.0
    compression_score += _clamp((1.0 - volume) / 0.5, 0.0, 1.0) * 15.0

    if high_vol_score >= 72.0:
        regime = "HIGH_VOL_CHAOS"
        score = high_vol_score
        preferred_side = "both"
        risk = float(cfg.get("market_regime_high_vol_risk_multiplier", 0.58) or 0.58)
    elif compression_score >= 62.0:
        regime = "SQUEEZE_BUILDUP"
        score = compression_score
        preferred_side = "both"
        risk = 0.82
    elif trend_score >= 72.0 and aligned >= max(1, opposite):
        regime = "STRONG_BULL_TREND" if side == "long" else "STRONG_BEAR_TREND"
        score = trend_score
        preferred_side = side
        risk = 1.0
    elif trend_score >= 58.0:
        regime = "BULL_TREND" if side == "long" else "BEAR_TREND"
        score = trend_score
        preferred_side = side if aligned >= opposite else ("short" if side == "long" else "long")
        risk = 1.0 if aligned >= opposite else float(cfg.get("market_regime_opposite_risk_multiplier", 0.62) or 0.62)
    elif chop >= 58.0:
        regime = "CHOP"
        score = 100.0 - _clamp(chop, 0.0, 100.0)
        preferred_side = "both"
        risk = float(cfg.get("market_regime_chop_risk_multiplier", 0.72) or 0.72)
    else:
        regime = "MIXED"
        score = max(45.0, trend_score)
        preferred_side = "both"
        risk = 0.86

    if opposite > aligned and side in {"long", "short"}:
        risk = min(risk, float(cfg.get("market_regime_opposite_risk_multiplier", 0.62) or 0.62))

    reasons = [
        f"regime {regime}",
        f"ADX {adx:.1f}",
        f"chop {chop:.1f}",
        f"top aligned/opposite {aligned}/{opposite}",
    ]
    blockers: list[str] = []
    if (
        bool(cfg.get("market_regime_block_extreme_chaos", True))
        and regime == "HIGH_VOL_CHAOS"
        and score >= float(cfg.get("market_regime_extreme_chaos_score", 82.0) or 82.0)
        and oi < 0
    ):
        blockers.append(f"extreme volatile chaos with contracting OI {oi:+.2f}%")

    return MarketRegimeDecision(
        allowed=not blockers,
        name="MARKET_REGIME",
        score=_clamp(score, 0.0, 100.0),
        risk_multiplier=_clamp(risk, 0.0, 1.0),
        reasons=tuple(reasons),
        blockers=tuple(blockers),
        components={
            "trend_score": round(trend_score, 4),
            "high_vol_score": round(high_vol_score, 4),
            "compression_score": round(compression_score, 4),
            "aligned_top": float(aligned),
            "opposite_top": float(opposite),
        },
        regime=regime,
        preferred_side=preferred_side,
    )


def evaluate_data_quality_engine(
    *, values: Mapping[str, Any] | None = None, config: Mapping[str, Any] | None = None
) -> IntelligenceDecision:
    cfg = _cfg(config)
    if not bool(cfg.get("data_quality_engine_enabled", True)):
        return IntelligenceDecision(True, "DATA_QUALITY", 100.0, 1.0, ("disabled",))

    values = values if isinstance(values, Mapping) else {}
    close = _first(values, "close", "entry_price", "mark_price", default=None)
    blockers: list[str] = []
    reasons: list[str] = []
    score = 100.0
    risk = 1.0
    if bool(cfg.get("data_quality_block_missing_price", True)) and (close is None or close <= 0):
        blockers.append("missing valid market price")
        score -= 45.0

    now_ts = _first(values, "now_ts", "current_ts", default=None)
    feed_ts = _first(values, "feed_last_ts", "last_candle_ts", "timestamp", default=None)
    if now_ts is not None and feed_ts is not None and now_ts > 10_000 and feed_ts > 10_000:
        age = now_ts - feed_ts
        if age > float(cfg.get("data_quality_max_feed_age_sec", 240.0) or 240.0):
            score -= 35.0
            if bool(cfg.get("data_quality_block_on_stale_feed", True)):
                blockers.append(f"stale market feed {age:.0f}s")
            else:
                risk = min(risk, 0.70)
        else:
            reasons.append(f"feed age {age:.0f}s")

    derivative_keys = (
        "funding_rate",
        "open_interest_change_1h",
        "open_interest_delta_pct",
        "taker_buy_sell_ratio",
        "rolling_ofi",
        "orderbook_imbalance_pct",
        "long_short_ratio",
    )
    sources = sum(1 for key in derivative_keys if values.get(key) is not None)
    min_sources = int(cfg.get("data_quality_min_derivative_sources", 2) or 2)
    if sources < min_sources:
        score -= (min_sources - sources) * 10.0
        risk = min(risk, float(cfg.get("data_quality_missing_derivatives_risk_multiplier", 0.78) or 0.78))
        reasons.append(f"derivative coverage {sources}/{min_sources}")
    else:
        reasons.append(f"derivative coverage {sources}/{min_sources}")

    return IntelligenceDecision(
        allowed=not blockers,
        name="DATA_QUALITY",
        score=_clamp(score, 0.0, 100.0),
        risk_multiplier=_clamp(risk, 0.0, 1.0),
        reasons=tuple(reasons),
        blockers=tuple(blockers),
        components={"derivative_sources": float(sources)},
    )


def evaluate_execution_quality_engine(
    *, values: Mapping[str, Any] | None = None, config: Mapping[str, Any] | None = None
) -> IntelligenceDecision:
    cfg = _cfg(config)
    if not bool(cfg.get("execution_quality_engine_enabled", True)):
        return IntelligenceDecision(True, "EXECUTION_QUALITY", 100.0, 1.0, ("disabled",))

    values = values if isinstance(values, Mapping) else {}
    spread = _first(values, "futures_spread_pct", "spread_pct", default=None)
    bid_depth = _first(values, "bid_depth_usdt", default=None)
    ask_depth = _first(values, "ask_depth_usdt", default=None)
    blockers: list[str] = []
    reasons: list[str] = []
    score = 100.0
    risk = 1.0
    max_spread = float(cfg.get("execution_quality_max_spread_pct", 0.12) or 0.12)
    soft_spread = float(cfg.get("execution_quality_soft_spread_pct", 0.07) or 0.07)
    if spread is not None:
        if spread > max_spread:
            blockers.append(f"spread {spread:.3f}%>{max_spread:.3f}%")
            score -= 45.0
        elif spread > soft_spread:
            risk = min(risk, 0.80)
            score -= 15.0
            reasons.append(f"wide spread {spread:.3f}%")
        else:
            reasons.append(f"spread {spread:.3f}%")

    depths = [item for item in (bid_depth, ask_depth) if item is not None and item > 0]
    min_depth = float(cfg.get("execution_quality_min_depth_usdt", 25_000.0) or 25_000.0)
    if depths and min(depths) < min_depth:
        risk = min(risk, float(cfg.get("execution_quality_low_depth_risk_multiplier", 0.72) or 0.72))
        score -= 15.0
        reasons.append(f"thin depth {min(depths):.0f}<{min_depth:.0f}")
    elif depths:
        reasons.append(f"depth {min(depths):.0f}")
    else:
        reasons.append("depth unavailable")

    return IntelligenceDecision(
        allowed=not blockers,
        name="EXECUTION_QUALITY",
        score=_clamp(score, 0.0, 100.0),
        risk_multiplier=_clamp(risk, 0.0, 1.0),
        reasons=tuple(reasons),
        blockers=tuple(blockers),
    )


def evaluate_protection_health_engine(
    *, values: Mapping[str, Any] | None = None, config: Mapping[str, Any] | None = None
) -> IntelligenceDecision:
    cfg = _cfg(config)
    if not bool(cfg.get("protection_health_engine_enabled", True)):
        return IntelligenceDecision(True, "PROTECTION_HEALTH", 100.0, 1.0, ("disabled",))

    values = values if isinstance(values, Mapping) else {}
    require_plan = bool(cfg.get("protection_health_require_plan_fields", False))
    blockers: list[str] = []
    reasons: list[str] = []
    score = 100.0
    if require_plan or values.get("entry_plan") or values.get("stop_loss") or values.get("take_profit"):
        stop = _first(values, "stop_loss", "hard_stop_loss", default=None)
        tp = _first(values, "take_profit", "tp1_price", "take_profit_1", default=None)
        if bool(cfg.get("protection_health_block_missing_stop", True)) and (stop is None or stop <= 0):
            blockers.append("missing protective stop in entry plan")
            score -= 50.0
        if bool(cfg.get("protection_health_block_missing_take_profit", True)) and (tp is None or tp <= 0):
            blockers.append("missing take-profit in entry plan")
            score -= 35.0
        if not blockers:
            reasons.append("SL/TP plan present")
    else:
        reasons.append("pre-plan observation")

    return IntelligenceDecision(
        allowed=not blockers,
        name="PROTECTION_HEALTH",
        score=_clamp(score, 0.0, 100.0),
        risk_multiplier=1.0 if not blockers else 0.0,
        reasons=tuple(reasons),
        blockers=tuple(blockers),
    )


def build_signal_attribution(
    *,
    side: str,
    engine: str,
    entry_type: str,
    exit_policy: str,
    market_regime: str,
    values: Mapping[str, Any] | None = None,
) -> SignalAttribution:
    values = values if isinstance(values, Mapping) else {}
    side = _direction_text(side)
    tags = {
        "side": side,
        "engine": str(engine or "UNKNOWN").upper(),
        "entry_type": str(entry_type or "UNKNOWN").upper(),
        "exit_policy": str(exit_policy or "UNKNOWN").upper(),
        "market_regime": str(market_regime or "MIXED").upper(),
        "timeframe": str(values.get("timeframe") or values.get("entry_timeframe") or "15m"),
    }
    sector = values.get("sector") or values.get("sector_tag")
    if sector:
        tags["sector"] = str(sector).upper()
    key = "|".join(tags.get(item, "") for item in ("side", "engine", "entry_type", "exit_policy", "market_regime"))
    return SignalAttribution(tags=tags, key=key, summary=key)


def _stats_for_key(meta_stats: Any, keys: Sequence[str]) -> Mapping[str, Any]:
    if not isinstance(meta_stats, Mapping):
        return {}
    for key in keys:
        row = meta_stats.get(key)
        if isinstance(row, Mapping):
            return row
    return {}


def evaluate_overfit_governance(
    *,
    meta_stats: Mapping[str, Any] | None,
    side: str,
    engine: str,
    entry_type: str,
    exit_policy: str,
    regime: str,
    config: Mapping[str, Any] | None = None,
) -> IntelligenceDecision:
    cfg = _cfg(config)
    if not bool(cfg.get("overfit_governance_enabled", True)):
        return IntelligenceDecision(True, "OVERFIT_GOVERNANCE", 100.0, 1.0, ("disabled",))

    side = _direction_text(side)
    engine = str(engine or "NO_TRADE").upper()
    entry_type = str(entry_type or "UNKNOWN").upper()
    exit_policy = str(exit_policy or "UNKNOWN").upper()
    regime = str(regime or "MIXED").upper()
    keys = (
        f"{side}:{engine}:{entry_type}:{exit_policy}:{regime}",
        f"{side}:{engine}:{entry_type}:{exit_policy}",
        f"{side}:{engine}:{exit_policy}",
        f"{side}:{engine}",
        f"exit:{exit_policy}",
        "global",
    )
    row = _stats_for_key(meta_stats, keys)
    samples = int(_finite(row.get("sample_count", row.get("trades", 0)), 0) or 0) if row else 0
    expectancy = _finite(row.get("expectancy_r", row.get("avg_pnl_r")), None) if row else None
    oos_expectancy = _finite(row.get("oos_expectancy_r"), None) if row else None
    profit_factor = _finite(row.get("profit_factor"), None) if row else None
    pbo = _finite(row.get("pbo"), None) if row else None

    min_samples = int(cfg.get("overfit_min_samples", 12) or 12)
    blockers: list[str] = []
    reasons: list[str] = []
    risk = 1.0
    score = 72.0
    if samples <= 0:
        risk = min(risk, float(cfg.get("overfit_warmup_risk_multiplier", 0.92) or 0.92))
        reasons.append("overfit governance warmup: no realized samples")
    elif samples < min_samples:
        risk = min(risk, float(cfg.get("overfit_warmup_risk_multiplier", 0.92) or 0.92))
        reasons.append(f"overfit governance warmup {samples}/{min_samples}")
        score -= max(0, min_samples - samples) * 1.2
    else:
        reasons.append(f"overfit samples {samples}")
        if expectancy is not None:
            score += _clamp(expectancy * 18.0, -18.0, 18.0)
            if expectancy < float(cfg.get("overfit_expectancy_block_below", -0.12) or -0.12):
                blockers.append(f"overfit expectancy {expectancy:.3f}R/{samples}")
        if oos_expectancy is not None:
            score += _clamp(oos_expectancy * 16.0, -16.0, 16.0)
            if oos_expectancy < float(cfg.get("overfit_oos_expectancy_block_below", -0.05) or -0.05):
                blockers.append(f"overfit OOS expectancy {oos_expectancy:.3f}R")
        if profit_factor is not None and profit_factor < float(cfg.get("overfit_min_profit_factor", 0.92) or 0.92):
            risk = min(risk, 0.70)
            blockers.append(f"overfit profit factor {profit_factor:.2f}")
        if pbo is not None and pbo > float(cfg.get("overfit_max_pbo", 0.65) or 0.65):
            risk = min(risk, 0.65)
            blockers.append(f"overfit PBO {pbo:.2f}")

    return IntelligenceDecision(
        allowed=not blockers,
        name="OVERFIT_GOVERNANCE",
        score=_clamp(score, 0.0, 100.0),
        risk_multiplier=_clamp(risk, 0.0, 1.0),
        reasons=tuple(reasons),
        blockers=tuple(blockers),
        components={
            "sample_count": float(samples),
            "expectancy_r": float(expectancy or 0.0),
            "oos_expectancy_r": float(oos_expectancy or 0.0),
            "profit_factor": float(profit_factor or 0.0),
            "pbo": float(pbo or 0.0),
        },
    )


def run_strategy_replay(candidates=None, bars_by_symbol=None, config=None):
    """Replay candidate signals through the existing shadow triple barrier."""
    cfg = _cfg(config)
    if not bool(cfg.get("strategy_replay_engine_enabled", True)):
        return []
    results = []
    bars_by_symbol = bars_by_symbol or {}
    for candidate in candidates or []:
        if not isinstance(candidate, Mapping):
            continue
        symbol = str(candidate.get("symbol") or "")
        side = candidate.get("side")
        bars = bars_by_symbol.get(symbol, bars_by_symbol.get(symbol.replace(":USDT", ""), []))
        result = evaluate_shadow_triple_barrier(
            side=side,
            entry_price=candidate.get("entry_price"),
            stop_loss=candidate.get("stop_loss"),
            take_profit=candidate.get("take_profit"),
            risk_distance=candidate.get("risk_distance"),
            take_profit_r_multiple=candidate.get("take_profit_r_multiple", candidate.get("rr_multiple", 2.0)),
            decision_ts=candidate.get("decision_ts", candidate.get("decision_candle_ts")),
            bars=bars,
            max_bars=candidate.get("max_bars", cfg.get("shadow_triple_barrier_max_bars", 24)),
        )
        if isinstance(result, Mapping):
            row = dict(candidate)
            row.update({f"replay_{key}": value for key, value in result.items()})
            results.append(row)
    return results


def run_overfit_backtest(trades=None, config=None, *, number_of_trials=None) -> OverfitBacktestReport:
    cfg = _cfg(config)
    trades = list(trades or [])
    trials = int(number_of_trials or cfg.get("overfit_multiple_testing_trials", 24) or 24)
    metrics = calculate_performance_metrics(trades)
    penalized = apply_multiple_testing_penalty(
        {
            "expectancy_r": metrics.get("avg_r", 0.0),
            "profit_factor": metrics.get("profit_factor", 0.0),
        },
        trials,
        cfg,
    )
    splits = walk_forward_splits(
        trades,
        train_size=int(cfg.get("overfit_walk_forward_train_size", 20) or 20),
        test_size=int(cfg.get("overfit_walk_forward_test_size", 10) or 10),
    )
    oos_passes = [
        (split.get("test") or {}).get("avg_r", 0.0) > 0
        and (split.get("test") or {}).get("profit_factor", 0.0) >= float(cfg.get("overfit_min_profit_factor", 0.92) or 0.92)
        for split in splits
    ]
    pbo = pbo_proxy(oos_passes)
    sharpe_pass = deflated_sharpe_proxy(
        metrics.get("sharpe_ratio", 0.0),
        n_obs=len(trades),
        n_trials=trials,
    )
    reasons: list[str] = []
    if len(trades) < int(cfg.get("overfit_min_samples", 12) or 12):
        reasons.append("sample warmup")
    if oos_passes and pbo > float(cfg.get("overfit_max_pbo", 0.65) or 0.65):
        reasons.append(f"PBO {pbo:.2f}")
    if not sharpe_pass and len(trades) >= int(cfg.get("overfit_min_samples", 12) or 12):
        reasons.append("deflated sharpe proxy failed")
    adjusted_expectancy = _finite(penalized.get("adjusted_expectancy_r"), 0.0) or 0.0
    adjusted_pf = _finite(penalized.get("adjusted_profit_factor"), 0.0) or 0.0
    if adjusted_expectancy <= 0 and len(trades) >= int(cfg.get("overfit_min_samples", 12) or 12):
        reasons.append(f"adjusted expectancy {adjusted_expectancy:.3f}R")

    passed = not reasons or reasons == ["sample warmup"]
    return OverfitBacktestReport(
        passed=passed,
        pbo=float(pbo),
        adjusted_expectancy_r=float(adjusted_expectancy),
        adjusted_profit_factor=float(adjusted_pf),
        deflated_sharpe_pass=bool(sharpe_pass),
        train_trade_count=sum((split.get("train") or {}).get("trade_count", 0) for split in splits),
        test_trade_count=sum((split.get("test") or {}).get("trade_count", 0) for split in splits),
        reasons=tuple(reasons),
    )
