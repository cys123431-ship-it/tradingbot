"""Quarter-hour order-flow strategy and shared L2 liquidity gate.

The module is deliberately pure: callers supply Binance aggregate trades,
order-book snapshots, and derivative context. It does not place orders or keep a
historical database. Runtime code may cache one evaluation per quarter-hour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite, sqrt
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

QH_FLOW_STRATEGY = "qh_flow_v1"
TRIPLE_ALPHA_STRATEGY = "triple_alpha_v1"
QUAD_ALPHA_STRATEGY = "quad_alpha_v1"


def default_qh_flow_config() -> dict[str, Any]:
    return {
        "qh_flow_enabled": True,
        "qh_flow_live_enabled": False,
        "capture_seconds": 10,
        "signal_valid_seconds": 120,
        "baseline_windows": 8,
        "baseline_min_windows": 4,
        "min_trade_notional_usdt": 5_000.0,
        "imbalance_abs_min": 0.18,
        "imbalance_z_min": 2.0,
        "notional_ratio_min": 1.25,
        "trade_count_ratio_min": 1.10,
        "price_retention_min_bps": 0.5,
        "score_min": 62.0,
        "stop_atr_multiplier": 1.25,
        "take_profit_r_multiple": 2.50,
        "risk_multiplier_floor": 0.35,
        "funding_crowding_abs": 0.0010,
        "basis_crowding_abs_pct": 0.25,
        "long_short_crowding_ratio": 1.80,
        "l2_gate_enabled": True,
        "l2_calm_spread_max_pct": 0.025,
        "l2_stressed_spread_min_pct": 0.070,
        "l2_calm_min_depth_usdt": 50_000.0,
        "l2_stressed_min_depth_usdt": 12_000.0,
        "l2_extreme_imbalance_pct": 82.0,
        "l2_mixed_risk_multiplier": 0.65,
        "qh_confirmation_enabled": True,
        "qh_confirmation_pre_boundary_seconds": 180,
        "qh_confirmation_no_signal_multiplier": 0.60,
        "qh_confirmation_opposite_blocks": True,
        "triple_three_signal_multiplier": 1.00,
        "triple_two_signal_multiplier": 0.85,
        "triple_single_signal_multiplier": 0.55,
        "quad_four_signal_multiplier": 1.00,
        "quad_three_signal_multiplier": 0.90,
        "quad_two_signal_multiplier": 0.75,
        "quad_single_signal_multiplier": 0.45,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def quarter_hour_boundary_ms(now_ms: int | float) -> int:
    now = max(0, int(now_ms or 0))
    quarter = 15 * 60 * 1000
    return now - (now % quarter)


def seconds_to_next_boundary(now_ms: int | float) -> float:
    now = max(0, int(now_ms or 0))
    quarter = 15 * 60 * 1000
    return (quarter - (now % quarter)) / 1000.0


def boundary_phase(now_ms: int | float, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = {**default_qh_flow_config(), **dict(config or {})}
    now = max(0, int(now_ms or 0))
    boundary = quarter_hour_boundary_ms(now)
    age_seconds = max(0.0, (now - boundary) / 1000.0)
    capture_seconds = max(1.0, float(cfg["capture_seconds"]))
    valid_seconds = max(capture_seconds, float(cfg["signal_valid_seconds"]))
    if age_seconds < capture_seconds:
        phase = "collecting"
    elif age_seconds <= valid_seconds:
        phase = "ready"
    else:
        phase = "stale"
    return {
        "phase": phase,
        "boundary_ms": boundary,
        "capture_end_ms": boundary + int(capture_seconds * 1000),
        "age_seconds": age_seconds,
        "seconds_to_next_boundary": seconds_to_next_boundary(now),
    }


def summarize_agg_trades(
    trades: Iterable[Mapping[str, Any]] | None,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict[str, Any]:
    buy_notional = 0.0
    sell_notional = 0.0
    buy_count = 0
    sell_count = 0
    prices: list[tuple[int, float]] = []
    included = 0
    for raw in trades or []:
        ts = int(_finite(raw.get("T", raw.get("time", raw.get("timestamp"))), 0.0) or 0)
        if start_ms is not None and ts < int(start_ms):
            continue
        if end_ms is not None and ts > int(end_ms):
            continue
        price = _finite(raw.get("p", raw.get("price")))
        qty = _finite(raw.get("q", raw.get("qty", raw.get("amount"))))
        if price is None or qty is None or price <= 0 or qty <= 0:
            continue
        notional = price * qty
        buyer_is_maker = bool(raw.get("m", raw.get("buyerIsMaker", False)))
        if buyer_is_maker:
            sell_notional += notional
            sell_count += 1
        else:
            buy_notional += notional
            buy_count += 1
        prices.append((ts, price))
        included += 1

    total_notional = buy_notional + sell_notional
    total_count = buy_count + sell_count
    imbalance = (
        (buy_notional - sell_notional) / total_notional if total_notional > 0 else 0.0
    )
    prices.sort(key=lambda item: item[0])
    first_price = prices[0][1] if prices else None
    last_price = prices[-1][1] if prices else None
    return_pct = (
        (last_price - first_price) / first_price * 100.0
        if first_price and last_price and first_price > 0
        else 0.0
    )
    return {
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "total_notional": total_notional,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "trade_count": total_count,
        "imbalance": imbalance,
        "imbalance_pct": imbalance * 100.0,
        "first_price": first_price,
        "last_price": last_price,
        "return_pct": return_pct,
        "included_trades": included,
    }


def _mean_std(values: Sequence[float]) -> tuple[float, float]:
    clean = [float(value) for value in values if isfinite(float(value))]
    if not clean:
        return 0.0, 0.0
    mean = sum(clean) / len(clean)
    variance = sum((value - mean) ** 2 for value in clean) / len(clean)
    return mean, sqrt(max(0.0, variance))


def enrich_with_baseline(
    current: Mapping[str, Any],
    baseline: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    rows = [dict(row) for row in baseline or [] if _finite(row.get("total_notional"), 0.0) > 0]
    imbalance_values = [float(row.get("imbalance", 0.0) or 0.0) for row in rows]
    notional_values = [float(row.get("total_notional", 0.0) or 0.0) for row in rows]
    count_values = [float(row.get("trade_count", 0.0) or 0.0) for row in rows]
    imbalance_mean, imbalance_std = _mean_std(imbalance_values)
    current_imbalance = float(current.get("imbalance", 0.0) or 0.0)
    fallback_std = max(0.05, abs(imbalance_mean) * 0.50)
    imbalance_z = (current_imbalance - imbalance_mean) / max(imbalance_std, fallback_std)
    base_notional = median(notional_values) if notional_values else 0.0
    base_count = median(count_values) if count_values else 0.0
    return {
        **dict(current),
        "baseline_count": len(rows),
        "baseline_imbalance_mean": imbalance_mean,
        "baseline_imbalance_std": imbalance_std,
        "imbalance_z": imbalance_z,
        "notional_ratio": (
            float(current.get("total_notional", 0.0) or 0.0) / base_notional
            if base_notional > 0
            else 0.0
        ),
        "trade_count_ratio": (
            float(current.get("trade_count", 0.0) or 0.0) / base_count
            if base_count > 0
            else 0.0
        ),
        "baseline_notional_median": base_notional,
        "baseline_trade_count_median": base_count,
    }


def evaluate_l2_gate(
    depth: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = {**default_qh_flow_config(), **dict(config or {})}
    bids = list((depth or {}).get("bids") or [])[:20]
    asks = list((depth or {}).get("asks") or [])[:20]

    def parse(rows: Sequence[Any]) -> list[tuple[float, float]]:
        parsed: list[tuple[float, float]] = []
        for row in rows:
            try:
                price = float(row[0])
                qty = float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if price > 0 and qty > 0:
                parsed.append((price, qty))
        return parsed

    bid_levels = parse(bids)
    ask_levels = parse(asks)
    if not bid_levels or not ask_levels:
        return {
            "state": "stressed",
            "allowed": False,
            "risk_multiplier": 0.0,
            "reason": "order book unavailable",
            "spread_pct": None,
            "bid_depth_usdt": 0.0,
            "ask_depth_usdt": 0.0,
            "imbalance_pct": None,
        }

    best_bid = bid_levels[0][0]
    best_ask = ask_levels[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_pct = (best_ask - best_bid) / max(mid, 1e-9) * 100.0

    def depth_usdt(levels: Sequence[tuple[float, float]], size: int) -> float:
        return sum(price * qty for price, qty in levels[:size])

    bid_depth_5 = depth_usdt(bid_levels, 5)
    ask_depth_5 = depth_usdt(ask_levels, 5)
    bid_depth_10 = depth_usdt(bid_levels, 10)
    ask_depth_10 = depth_usdt(ask_levels, 10)
    bid_depth_20 = depth_usdt(bid_levels, 20)
    ask_depth_20 = depth_usdt(ask_levels, 20)
    total_depth = bid_depth_20 + ask_depth_20
    imbalance_pct = (
        (bid_depth_20 - ask_depth_20) / total_depth * 100.0 if total_depth > 0 else 0.0
    )
    min_depth = min(bid_depth_20, ask_depth_20)

    stressed = (
        spread_pct >= float(cfg["l2_stressed_spread_min_pct"])
        or min_depth < float(cfg["l2_stressed_min_depth_usdt"])
        or best_bid >= best_ask
    )
    mixed = (
        spread_pct > float(cfg["l2_calm_spread_max_pct"])
        or min_depth < float(cfg["l2_calm_min_depth_usdt"])
        or abs(imbalance_pct) >= float(cfg["l2_extreme_imbalance_pct"])
    )
    if stressed:
        state = "stressed"
        risk_multiplier = 0.0
    elif mixed:
        state = "mixed"
        risk_multiplier = max(0.0, min(1.0, float(cfg["l2_mixed_risk_multiplier"])))
    else:
        state = "calm"
        risk_multiplier = 1.0
    return {
        "state": state,
        "allowed": state != "stressed",
        "risk_multiplier": risk_multiplier,
        "reason": (
            f"{state}: spread={spread_pct:.4f}% minDepth={min_depth:.0f} "
            f"imbalance={imbalance_pct:+.1f}%"
        ),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_depth_5_usdt": bid_depth_5,
        "ask_depth_5_usdt": ask_depth_5,
        "bid_depth_10_usdt": bid_depth_10,
        "ask_depth_10_usdt": ask_depth_10,
        "bid_depth_usdt": bid_depth_20,
        "ask_depth_usdt": ask_depth_20,
        "min_depth_usdt": min_depth,
        "imbalance_pct": imbalance_pct,
    }


@dataclass(frozen=True)
class QHFlowDecision:
    side: str | None = None
    allowed: bool = False
    score: float = 0.0
    risk_multiplier: float = 0.0
    reason: str = "not_evaluated"
    metrics: dict[str, Any] = field(default_factory=dict)


def evaluate_qh_flow(
    current: Mapping[str, Any],
    baseline: Sequence[Mapping[str, Any]] | None,
    l2_gate: Mapping[str, Any],
    derivatives: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> QHFlowDecision:
    cfg = {**default_qh_flow_config(), **dict(config or {})}
    metrics = enrich_with_baseline(current, baseline)
    derivatives = dict(derivatives or {})
    metrics["l2_gate"] = dict(l2_gate or {})
    metrics["derivatives"] = derivatives

    baseline_count = int(metrics.get("baseline_count") or 0)
    if baseline_count < int(cfg["baseline_min_windows"]):
        return QHFlowDecision(reason="insufficient_baseline_windows", metrics=metrics)
    if float(metrics.get("total_notional", 0.0) or 0.0) < float(cfg["min_trade_notional_usdt"]):
        return QHFlowDecision(reason="insufficient_trade_notional", metrics=metrics)
    imbalance = float(metrics.get("imbalance", 0.0) or 0.0)
    side = "long" if imbalance > 0 else "short" if imbalance < 0 else None
    if side is None or abs(imbalance) < float(cfg["imbalance_abs_min"]):
        return QHFlowDecision(reason="weak_taker_imbalance", metrics=metrics)
    signed_z = float(metrics.get("imbalance_z", 0.0) or 0.0) * (1.0 if side == "long" else -1.0)
    if signed_z < float(cfg["imbalance_z_min"]):
        return QHFlowDecision(side=side, reason="imbalance_z_below_threshold", metrics=metrics)
    notional_ratio = float(metrics.get("notional_ratio", 0.0) or 0.0)
    if notional_ratio < float(cfg["notional_ratio_min"]):
        return QHFlowDecision(side=side, reason="notional_ratio_below_threshold", metrics=metrics)
    count_ratio = float(metrics.get("trade_count_ratio", 0.0) or 0.0)
    if count_ratio < float(cfg["trade_count_ratio_min"]):
        return QHFlowDecision(side=side, reason="trade_count_ratio_below_threshold", metrics=metrics)
    return_bps = float(metrics.get("return_pct", 0.0) or 0.0) * 100.0
    signed_return_bps = return_bps if side == "long" else -return_bps
    if signed_return_bps < float(cfg["price_retention_min_bps"]):
        return QHFlowDecision(side=side, reason="price_move_not_retained", metrics=metrics)
    if not bool((l2_gate or {}).get("allowed", False)):
        return QHFlowDecision(side=side, reason="l2_stressed", metrics=metrics)

    funding = _finite(derivatives.get("funding_rate"), 0.0) or 0.0
    basis = _finite(derivatives.get("basis_pct"), 0.0) or 0.0
    long_short = _finite(derivatives.get("long_short_ratio"), 1.0) or 1.0
    crowding_multiplier = 1.0
    if side == "long" and (
        funding >= float(cfg["funding_crowding_abs"])
        or basis >= float(cfg["basis_crowding_abs_pct"])
        or long_short >= float(cfg["long_short_crowding_ratio"])
    ):
        crowding_multiplier = 0.65
    if side == "short" and (
        funding <= -float(cfg["funding_crowding_abs"])
        or basis <= -float(cfg["basis_crowding_abs_pct"])
        or long_short <= 1.0 / max(float(cfg["long_short_crowding_ratio"]), 1e-9)
    ):
        crowding_multiplier = 0.65

    score = 0.0
    score += min(30.0, abs(imbalance) / max(float(cfg["imbalance_abs_min"]), 1e-9) * 18.0)
    score += min(25.0, signed_z / max(float(cfg["imbalance_z_min"]), 1e-9) * 18.0)
    score += min(20.0, notional_ratio / max(float(cfg["notional_ratio_min"]), 1e-9) * 14.0)
    score += min(10.0, count_ratio / max(float(cfg["trade_count_ratio_min"]), 1e-9) * 7.0)
    score += min(10.0, signed_return_bps / max(float(cfg["price_retention_min_bps"]), 1e-9) * 5.0)
    score += 5.0 if str((l2_gate or {}).get("state")) == "calm" else 2.0
    score = min(100.0, score)
    if score < float(cfg["score_min"]):
        return QHFlowDecision(side=side, score=score, reason="score_below_threshold", metrics=metrics)

    risk_multiplier = min(
        1.0,
        max(float(cfg["risk_multiplier_floor"]), score / 100.0),
        max(0.0, float((l2_gate or {}).get("risk_multiplier", 0.0) or 0.0)),
        crowding_multiplier,
    )
    metrics["crowding_multiplier"] = crowding_multiplier
    metrics["score"] = score
    return QHFlowDecision(
        side=side,
        allowed=True,
        score=score,
        risk_multiplier=risk_multiplier,
        reason=(
            f"QH {side} score={score:.1f} imbalance={imbalance:+.3f} "
            f"z={signed_z:.2f} volume={notional_ratio:.2f}x L2={l2_gate.get('state')}"
        ),
        metrics=metrics,
    )

# QH-Flow v2 compatibility-preserving overrides.
QH_FLOW_VERSION = "v2"
_default_qh_flow_config_v1 = default_qh_flow_config

def default_qh_flow_config() -> dict[str, Any]:
    base = dict(_default_qh_flow_config_v1())
    base.update({
        "strategy_version": QH_FLOW_VERSION,
        "persistence_seconds": 20,
        "persistence_imbalance_ratio_min": 0.35,
        "persistence_return_reversal_bps": 2.0,
        "persistence_required": True,
        "persistence_phase_enabled": True,
        "benchmark_confirmation_enabled": True,
        "benchmark_symbols": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "benchmark_imbalance_abs_min": 0.08,
        "benchmark_min_confirmations": 1,
        "benchmark_opposite_blocks": True,
        "benchmark_no_confirmation_multiplier": 0.75,
        "l2_direction_conflict_multiplier": 0.35,
        "l2_dynamic_min_samples": 2,
        "l2_replenishment_ratio_min": 0.08,
        "l2_depletion_ratio_min": 0.15,
        "l2_cancellation_ratio_min": 0.25,
        "liquidity_high_depth_usdt": 250000.0,
        "liquidity_medium_depth_usdt": 60000.0,
        "liquidity_high_risk_multiplier": 1.0,
        "liquidity_medium_risk_multiplier": 0.80,
        "liquidity_low_risk_multiplier": 0.50,
        "liquidity_low_score_add": 8.0,
        "liquidity_low_imbalance_multiplier": 1.20,
        "liquidity_low_notional_multiplier": 1.20,
    })
    return base

def _bounded(value: Any, lower: float = 0.0, upper: float = 1.0) -> float:
    parsed = _finite(value, lower)
    return max(lower, min(upper, float(parsed if parsed is not None else lower)))

def boundary_phase(now_ms: int | float, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = {**default_qh_flow_config(), **dict(config or {})}
    now = max(0, int(now_ms or 0))
    boundary = quarter_hour_boundary_ms(now)
    age_seconds = max(0.0, (now - boundary) / 1000.0)
    capture_seconds = max(1.0, float(cfg["capture_seconds"]))
    persistence_seconds = max(0.0, float(cfg.get("persistence_seconds", 0.0) or 0.0))
    persistence_phase = bool((config or {}).get("persistence_phase_enabled", False))
    ready_after = capture_seconds + (persistence_seconds if persistence_phase and cfg.get("persistence_required", True) else 0.0)
    valid_seconds = max(ready_after, float(cfg["signal_valid_seconds"]))
    if age_seconds < capture_seconds:
        phase = "collecting"
    elif persistence_phase and cfg.get("persistence_required", True) and age_seconds < ready_after:
        phase = "confirming"
    elif age_seconds <= valid_seconds:
        phase = "ready"
    else:
        phase = "stale"
    return {
        "phase": phase,
        "boundary_ms": boundary,
        "capture_end_ms": boundary + int(capture_seconds * 1000),
        "persistence_end_ms": boundary + int(ready_after * 1000),
        "age_seconds": age_seconds,
        "seconds_to_next_boundary": seconds_to_next_boundary(now),
    }

def _parse_depth(depth: Mapping[str, Any] | None) -> dict[str, Any]:
    bids = list((depth or {}).get("bids") or [])[:20]
    asks = list((depth or {}).get("asks") or [])[:20]

    def parse(rows: Sequence[Any]) -> list[tuple[float, float]]:
        parsed: list[tuple[float, float]] = []
        for row in rows:
            try:
                price, qty = float(row[0]), float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if price > 0 and qty > 0:
                parsed.append((price, qty))
        return parsed

    bid_levels, ask_levels = parse(bids), parse(asks)
    if not bid_levels or not ask_levels:
        return {}
    best_bid, best_ask = bid_levels[0][0], ask_levels[0][0]
    mid = (best_bid + best_ask) / 2.0

    def depth_usdt(levels: Sequence[tuple[float, float]], size: int) -> float:
        return sum(price * qty for price, qty in levels[:size])

    bid5, ask5 = depth_usdt(bid_levels, 5), depth_usdt(ask_levels, 5)
    bid10, ask10 = depth_usdt(bid_levels, 10), depth_usdt(ask_levels, 10)
    bid20, ask20 = depth_usdt(bid_levels, 20), depth_usdt(ask_levels, 20)
    total = bid20 + ask20
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_pct": (best_ask - best_bid) / max(mid, 1e-9) * 100.0,
        "bid_depth_5_usdt": bid5,
        "ask_depth_5_usdt": ask5,
        "bid_depth_10_usdt": bid10,
        "ask_depth_10_usdt": ask10,
        "bid_depth_usdt": bid20,
        "ask_depth_usdt": ask20,
        "min_depth_usdt": min(bid20, ask20),
        "imbalance_pct": (bid20 - ask20) / total * 100.0 if total > 0 else 0.0,
    }


def classify_liquidity_tier(
    depth_metrics: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    symbol: str | None = None,
) -> str:
    cfg = {**default_qh_flow_config(), **dict(config or {})}
    base = "".join(ch for ch in str(symbol or "").upper().split("/")[0] if ch.isalnum())
    if base in {"BTC", "ETH", "SOL", "XRP", "BNB"}:
        return "high"
    minimum = float((depth_metrics or {}).get("min_depth_usdt", 0.0) or 0.0)
    if minimum >= float(cfg["liquidity_high_depth_usdt"]):
        return "high"
    if minimum >= float(cfg["liquidity_medium_depth_usdt"]):
        return "medium"
    return "low"


def _history_dynamics(history: Sequence[Mapping[str, Any]] | None, current: Mapping[str, Any]) -> dict[str, float]:
    rows = [dict(row) for row in history or [] if isinstance(row, Mapping)]
    if not rows:
        return {
            "samples": 1.0,
            "bid_change_ratio": 0.0,
            "ask_change_ratio": 0.0,
            "imbalance_delta": 0.0,
            "spread_change_ratio": 0.0,
        }
    previous = rows[-1]

    def change(key: str) -> float:
        before = float(previous.get(key, 0.0) or 0.0)
        after = float(current.get(key, 0.0) or 0.0)
        return (after - before) / max(abs(before), 1e-9)

    return {
        "samples": float(len(rows) + 1),
        "bid_change_ratio": change("bid_depth_usdt"),
        "ask_change_ratio": change("ask_depth_usdt"),
        "imbalance_delta": float(current.get("imbalance_pct", 0.0) or 0.0) - float(previous.get("imbalance_pct", 0.0) or 0.0),
        "spread_change_ratio": change("spread_pct"),
    }


def evaluate_l2_gate(
    depth: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    *,
    history: Sequence[Mapping[str, Any]] | None = None,
    side: str | None = None,
    liquidity_tier: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    cfg = {**default_qh_flow_config(), **dict(config or {})}
    metrics = _parse_depth(depth)
    if not metrics:
        return {
            "state": "stressed_thin",
            "legacy_state": "stressed",
            "allowed": False,
            "risk_multiplier": 0.0,
            "reason": "order book unavailable",
            "spread_pct": None,
            "bid_depth_usdt": 0.0,
            "ask_depth_usdt": 0.0,
            "imbalance_pct": None,
            "liquidity_tier": liquidity_tier or "low",
        }
    tier = str(liquidity_tier or classify_liquidity_tier(metrics, cfg, symbol)).lower()
    tier_scale = {"high": 1.0, "medium": 0.55, "low": 0.25}.get(tier, 0.25)
    stressed_depth = float(cfg["l2_stressed_min_depth_usdt"]) * tier_scale
    calm_depth = float(cfg["l2_calm_min_depth_usdt"]) * tier_scale
    dynamics = _history_dynamics(history, metrics)
    spread = float(metrics["spread_pct"])
    minimum = float(metrics["min_depth_usdt"])
    imbalance = float(metrics["imbalance_pct"])
    invalid = metrics["best_bid"] >= metrics["best_ask"]
    cancellation = float(cfg["l2_cancellation_ratio_min"])
    thin = (
        invalid
        or spread >= float(cfg["l2_stressed_spread_min_pct"])
        or minimum < stressed_depth
        or dynamics["bid_change_ratio"] <= -cancellation
        and dynamics["ask_change_ratio"] <= -cancellation
    )
    replenish = float(cfg["l2_replenishment_ratio_min"])
    deplete = float(cfg["l2_depletion_ratio_min"])
    bid_support = (
        dynamics["bid_change_ratio"] >= replenish
        and dynamics["ask_change_ratio"] <= -deplete
    ) or (imbalance >= 25.0 and dynamics["imbalance_delta"] > 0)
    ask_pressure = (
        dynamics["ask_change_ratio"] >= replenish
        and dynamics["bid_change_ratio"] <= -deplete
    ) or (imbalance <= -25.0 and dynamics["imbalance_delta"] < 0)
    mixed = (
        spread > float(cfg["l2_calm_spread_max_pct"])
        or minimum < calm_depth
        or abs(imbalance) >= float(cfg["l2_extreme_imbalance_pct"])
    )
    if thin:
        state, legacy, allowed, multiplier = "stressed_thin", "stressed", False, 0.0
    elif bid_support and not ask_pressure:
        state, legacy, allowed = "bid_support", "calm", True
        multiplier = 1.0 if str(side or "").lower() != "short" else float(cfg["l2_direction_conflict_multiplier"])
    elif ask_pressure and not bid_support:
        state, legacy, allowed = "ask_pressure", "calm", True
        multiplier = 1.0 if str(side or "").lower() != "long" else float(cfg["l2_direction_conflict_multiplier"])
    else:
        state, legacy, allowed = "deep_balanced", "mixed" if mixed else "calm", True
        multiplier = float(cfg["l2_mixed_risk_multiplier"]) if mixed else 1.0
    tier_multiplier = float(cfg.get(f"liquidity_{tier}_risk_multiplier", 0.50) or 0.50)
    multiplier = min(multiplier, tier_multiplier)
    dynamic_requested = any((history is not None, side, liquidity_tier, symbol))
    result = {
        **metrics,
        **dynamics,
        "state": state if dynamic_requested else legacy,
        "dynamic_state": state,
        "legacy_state": legacy,
        "allowed": allowed,
        "risk_multiplier": _bounded(multiplier),
        "liquidity_tier": tier,
        "direction_support": (
            "long" if state == "bid_support" else "short" if state == "ask_pressure" else None
        ),
    }
    result["reason"] = (
        f"{result['state']}: tier={tier} spread={spread:.4f}% minDepth={minimum:.0f} "
        f"imbalance={imbalance:+.1f}% bidΔ={dynamics['bid_change_ratio']:+.2f} "
        f"askΔ={dynamics['ask_change_ratio']:+.2f}"
    )
    return result


@dataclass(frozen=True)
class QHFlowDecision:
    side: str | None = None
    allowed: bool = False
    score: float = 0.0
    risk_multiplier: float = 0.0
    reason: str = "not_evaluated"
    metrics: dict[str, Any] = field(default_factory=dict)


def _benchmark_confirmation(
    side: str,
    benchmarks: Mapping[str, Mapping[str, Any]] | None,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    if not cfg.get("benchmark_confirmation_enabled", True):
        return {"allowed": True, "confirmations": 0, "opposites": 0, "multiplier": 1.0, "reason": "disabled"}
    threshold = float(cfg["benchmark_imbalance_abs_min"])
    confirmations = opposites = 0
    details = {}
    wanted_sign = 1.0 if side == "long" else -1.0
    for symbol, row in (benchmarks or {}).items():
        imbalance = float((row or {}).get("imbalance", 0.0) or 0.0)
        signed = imbalance * wanted_sign
        if signed >= threshold:
            confirmations += 1
            status = "confirm"
        elif signed <= -threshold:
            opposites += 1
            status = "opposite"
        else:
            status = "neutral"
        details[symbol] = {"imbalance": imbalance, "status": status}
    if opposites and cfg.get("benchmark_opposite_blocks", True):
        return {"allowed": False, "confirmations": confirmations, "opposites": opposites, "multiplier": 0.0, "reason": "benchmark_direction_conflict", "details": details}
    minimum = max(0, int(cfg.get("benchmark_min_confirmations", 1) or 1))
    multiplier = 1.0 if confirmations >= minimum else float(cfg["benchmark_no_confirmation_multiplier"])
    return {"allowed": True, "confirmations": confirmations, "opposites": opposites, "multiplier": _bounded(multiplier), "reason": "benchmark_confirmed" if confirmations >= minimum else "benchmark_unconfirmed", "details": details}


def _persistence_confirmation(
    side: str,
    current: Mapping[str, Any],
    persistence: Mapping[str, Any] | None,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    if not cfg.get("persistence_required", True):
        return {"allowed": True, "multiplier": 1.0, "reason": "disabled"}
    if persistence is None:
        return {"allowed": True, "multiplier": 0.75, "reason": "persistence_unavailable_compat"}
    row = dict(persistence or {})
    if float(row.get("total_notional", 0.0) or 0.0) <= 0:
        return {"allowed": False, "multiplier": 0.0, "reason": "persistence_data_missing"}
    initial = float(current.get("imbalance", 0.0) or 0.0)
    later = float(row.get("imbalance", 0.0) or 0.0)
    signed_initial = initial if side == "long" else -initial
    signed_later = later if side == "long" else -later
    ratio = signed_later / max(abs(signed_initial), 1e-9)
    return_bps = float(row.get("return_pct", 0.0) or 0.0) * 100.0
    signed_return = return_bps if side == "long" else -return_bps
    reversal_limit = -abs(float(cfg["persistence_return_reversal_bps"]))
    allowed = ratio >= float(cfg["persistence_imbalance_ratio_min"]) and signed_return >= reversal_limit
    return {
        "allowed": allowed,
        "multiplier": 1.0 if allowed else 0.0,
        "reason": "persistence_confirmed" if allowed else "flow_not_persistent",
        "imbalance_ratio": ratio,
        "signed_return_bps": signed_return,
        "snapshot": row,
    }


def evaluate_qh_flow(
    current: Mapping[str, Any],
    baseline: Sequence[Mapping[str, Any]] | None,
    l2_gate: Mapping[str, Any],
    derivatives: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    *,
    benchmarks: Mapping[str, Mapping[str, Any]] | None = None,
    persistence: Mapping[str, Any] | None = None,
) -> QHFlowDecision:
    cfg = {**default_qh_flow_config(), **dict(config or {})}
    metrics = enrich_with_baseline(current, baseline)
    derivatives = dict(derivatives or {})
    tier = str((l2_gate or {}).get("liquidity_tier") or "medium").lower()
    metrics.update({"l2_gate": dict(l2_gate or {}), "derivatives": derivatives, "liquidity_tier": tier})
    if int(metrics.get("baseline_count") or 0) < int(cfg["baseline_min_windows"]):
        return QHFlowDecision(reason="insufficient_baseline_windows", metrics=metrics)
    tier_threshold = 1.0 if tier != "low" else float(cfg["liquidity_low_notional_multiplier"])
    if float(metrics.get("total_notional", 0.0) or 0.0) < float(cfg["min_trade_notional_usdt"]) * tier_threshold:
        return QHFlowDecision(reason="insufficient_trade_notional", metrics=metrics)
    imbalance = float(metrics.get("imbalance", 0.0) or 0.0)
    side = "long" if imbalance > 0 else "short" if imbalance < 0 else None
    imbalance_min = float(cfg["imbalance_abs_min"]) * (float(cfg["liquidity_low_imbalance_multiplier"]) if tier == "low" else 1.0)
    if side is None or abs(imbalance) < imbalance_min:
        return QHFlowDecision(reason="weak_taker_imbalance", metrics=metrics)
    signed_z = float(metrics.get("imbalance_z", 0.0) or 0.0) * (1.0 if side == "long" else -1.0)
    if signed_z < float(cfg["imbalance_z_min"]):
        return QHFlowDecision(side=side, reason="imbalance_z_below_threshold", metrics=metrics)
    notional_ratio = float(metrics.get("notional_ratio", 0.0) or 0.0)
    if notional_ratio < float(cfg["notional_ratio_min"]) * tier_threshold:
        return QHFlowDecision(side=side, reason="notional_ratio_below_threshold", metrics=metrics)
    count_ratio = float(metrics.get("trade_count_ratio", 0.0) or 0.0)
    if count_ratio < float(cfg["trade_count_ratio_min"]):
        return QHFlowDecision(side=side, reason="trade_count_ratio_below_threshold", metrics=metrics)
    signed_return_bps = float(metrics.get("return_pct", 0.0) or 0.0) * 100.0 * (1.0 if side == "long" else -1.0)
    if signed_return_bps < float(cfg["price_retention_min_bps"]):
        return QHFlowDecision(side=side, reason="price_move_not_retained", metrics=metrics)
    if not bool((l2_gate or {}).get("allowed", False)):
        return QHFlowDecision(side=side, reason="l2_stressed", metrics=metrics)
    direction_support = str((l2_gate or {}).get("direction_support") or "")
    if direction_support and direction_support != side and float((l2_gate or {}).get("risk_multiplier", 0.0) or 0.0) <= 0:
        return QHFlowDecision(side=side, reason="l2_direction_conflict", metrics=metrics)
    persistence_result = _persistence_confirmation(side, current, persistence, cfg)
    metrics["persistence"] = persistence_result
    if not persistence_result["allowed"]:
        return QHFlowDecision(side=side, reason=persistence_result["reason"], metrics=metrics)
    benchmark_result = _benchmark_confirmation(side, benchmarks, cfg)
    metrics["benchmark_confirmation"] = benchmark_result
    if not benchmark_result["allowed"]:
        return QHFlowDecision(side=side, reason=benchmark_result["reason"], metrics=metrics)

    funding = _finite(derivatives.get("funding_rate"), 0.0) or 0.0
    basis = _finite(derivatives.get("basis_pct"), 0.0) or 0.0
    long_short = _finite(derivatives.get("long_short_ratio"), 1.0) or 1.0
    crowding_multiplier = 1.0
    if side == "long" and (funding >= float(cfg["funding_crowding_abs"]) or basis >= float(cfg["basis_crowding_abs_pct"]) or long_short >= float(cfg["long_short_crowding_ratio"])):
        crowding_multiplier = 0.65
    if side == "short" and (funding <= -float(cfg["funding_crowding_abs"]) or basis <= -float(cfg["basis_crowding_abs_pct"]) or long_short <= 1.0 / max(float(cfg["long_short_crowding_ratio"]), 1e-9)):
        crowding_multiplier = 0.65

    score = 0.0
    score += min(30.0, abs(imbalance) / max(imbalance_min, 1e-9) * 18.0)
    score += min(25.0, signed_z / max(float(cfg["imbalance_z_min"]), 1e-9) * 18.0)
    score += min(20.0, notional_ratio / max(float(cfg["notional_ratio_min"]), 1e-9) * 14.0)
    score += min(10.0, count_ratio / max(float(cfg["trade_count_ratio_min"]), 1e-9) * 7.0)
    score += min(10.0, signed_return_bps / max(float(cfg["price_retention_min_bps"]), 1e-9) * 5.0)
    score += 5.0 if str((l2_gate or {}).get("legacy_state")) == "calm" else 2.0
    score += min(5.0, float(benchmark_result["confirmations"]) * 2.5)
    score = min(100.0, score)
    score_min = float(cfg["score_min"]) + (float(cfg["liquidity_low_score_add"]) if tier == "low" else 0.0)
    if score < score_min:
        return QHFlowDecision(side=side, score=score, reason="score_below_threshold", metrics=metrics)
    tier_multiplier = float(cfg.get(f"liquidity_{tier}_risk_multiplier", 0.50) or 0.50)
    risk_multiplier = min(
        1.0,
        max(float(cfg["risk_multiplier_floor"]), score / 100.0),
        max(0.0, float((l2_gate or {}).get("risk_multiplier", 0.0) or 0.0)),
        crowding_multiplier,
        float(benchmark_result["multiplier"]),
        tier_multiplier,
    )
    metrics.update({"crowding_multiplier": crowding_multiplier, "score": score, "strategy_version": QH_FLOW_VERSION})
    return QHFlowDecision(
        side=side,
        allowed=True,
        score=score,
        risk_multiplier=risk_multiplier,
        reason=(
            f"QH-v2 {side} score={score:.1f} imbalance={imbalance:+.3f} "
            f"z={signed_z:.2f} volume={notional_ratio:.2f}x "
            f"bench={benchmark_result['confirmations']} L2={l2_gate.get('state')} tier={tier}"
        ),
        metrics=metrics,
    )
