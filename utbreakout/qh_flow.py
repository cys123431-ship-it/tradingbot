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
