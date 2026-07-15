#!/usr/bin/env python
"""Baseline UTBot ATR/RR backtest harness.

This is intentionally conservative and small. It gives us a reproducible
baseline before promoting more live UT Breakout logic into pure modules.

CSV columns: timestamp, open, high, low, close, volume
"""

import argparse
import csv
import json
import math
from pathlib import Path
import sys
from datetime import datetime

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utbreakout.risk import calculate_risk_plan  # noqa: E402
from utbreakout.exit_policy import (  # noqa: E402
    DEFAULT_EXIT_POLICY,
    EXIT_POLICY_CANDIDATES,
    build_exit_policy,
    evaluate_signal_invalid_exit,
    evaluate_time_stop,
    rank_exit_policies,
)
from utbreakout.engine_router import ALPHA_ENGINE_NAMES, evaluate_final_trade_decision  # noqa: E402
from utbreakout.market_context import build_market_context  # noqa: E402
from utbreakout.meta import meta_label_gate  # noqa: E402
from utbreakout.regime import classify_regime, regime_action  # noqa: E402
from utbreakout.sizing import calculate_adaptive_risk_pct  # noqa: E402


def _float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def candles_for_months(months, timeframe, days_per_month=30):
    """Convert calendar-style months to candles for the selected timeframe."""
    text = str(timeframe or "15m").strip().lower()
    if not text:
        raise ValueError("timeframe is required")
    unit_seconds = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        if text.isdigit():
            seconds = int(text) * 60
        else:
            seconds = int(text[:-1]) * unit_seconds[text[-1]]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"unsupported timeframe: {timeframe}") from None
    if seconds <= 0:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    total_seconds = max(0, int(months or 0)) * max(1, int(days_per_month or 30)) * 86400
    return max(1, math.ceil(total_seconds / seconds)) if total_seconds else 0


def load_ohlcv_csv(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            parsed = {
                "timestamp": row.get("timestamp") or row.get("time") or "",
                "open": _float(row.get("open")),
                "high": _float(row.get("high")),
                "low": _float(row.get("low")),
                "close": _float(row.get("close")),
                "volume": _float(row.get("volume")),
            }
            if row.get("funding_rate") not in (None, ""):
                parsed["funding_rate"] = _float(row.get("funding_rate"))
            if row.get("funding_timestamp") not in (None, ""):
                parsed["funding_timestamp"] = row.get("funding_timestamp")
            funding_event = str(row.get("funding_event") or row.get("is_funding_time") or "").lower()
            if funding_event:
                parsed["funding_event"] = funding_event in {"1", "true", "yes", "y"}
            rows.append(parsed)
    return [row for row in rows if row["open"] > 0 and row["high"] > 0 and row["low"] > 0 and row["close"] > 0]


def wilder_atr(rows, period):
    tr = []
    for idx, row in enumerate(rows):
        prev_close = rows[idx - 1]["close"] if idx > 0 else row["close"]
        tr.append(max(
            abs(row["high"] - row["low"]),
            abs(row["high"] - prev_close),
            abs(row["low"] - prev_close),
        ))
    atr = [None] * len(rows)
    if len(rows) < period:
        return atr
    atr[period - 1] = sum(tr[:period]) / period
    for idx in range(period, len(rows)):
        atr[idx] = ((atr[idx - 1] * (period - 1)) + tr[idx]) / period
    return atr


def utbot_rows(rows, key_value=2.0, atr_period=10):
    atr = wilder_atr(rows, atr_period)
    trail = [None] * len(rows)
    signal = [None] * len(rows)
    bias = [None] * len(rows)
    for idx, row in enumerate(rows):
        if atr[idx] is None:
            continue
        src = row["close"]
        nloss = atr[idx] * key_value
        if idx == 0 or trail[idx - 1] is None:
            trail[idx] = src - nloss
        else:
            prev_stop = trail[idx - 1]
            prev_src = rows[idx - 1]["close"]
            if src > prev_stop and prev_src > prev_stop:
                trail[idx] = max(prev_stop, src - nloss)
            elif src < prev_stop and prev_src < prev_stop:
                trail[idx] = min(prev_stop, src + nloss)
            elif src > prev_stop:
                trail[idx] = src - nloss
            else:
                trail[idx] = src + nloss
            if src > trail[idx] and prev_src <= prev_stop:
                signal[idx] = "long"
            elif src < trail[idx] and prev_src >= prev_stop:
                signal[idx] = "short"
        bias[idx] = "long" if src > trail[idx] else "short" if src < trail[idx] else None
    return atr, trail, signal, bias


def _rolling_percentile_at(values, idx, lookback=100):
    value = values[idx] if 0 <= idx < len(values) else None
    if value is None:
        return None
    start = max(0, idx - max(1, int(lookback or 100)) + 1)
    sample = [item for item in values[start:idx + 1] if item is not None]
    if not sample:
        return None
    below_or_equal = sum(1 for item in sample if item <= value)
    return below_or_equal / len(sample) * 100.0


def _volume_ratio_at(rows, idx, lookback=20):
    volume = rows[idx]["volume"] if 0 <= idx < len(rows) else 0.0
    start = max(0, idx - max(1, int(lookback or 20)) + 1)
    sample = [row["volume"] for row in rows[start:idx + 1] if row.get("volume", 0.0) > 0]
    avg = sum(sample) / len(sample) if sample else 0.0
    return volume / avg if avg > 0 else 1.0


def _donchian_mid_at(rows, idx, length=20):
    if idx <= 0:
        return None
    start = max(0, idx - int(length or 20))
    sample = rows[start:idx]
    if not sample:
        return None
    return (max(row["high"] for row in sample) + min(row["low"] for row in sample)) / 2.0


def _bar_with_index(row, idx):
    copy = dict(row)
    copy["index"] = idx
    copy["idx"] = idx
    return copy


def _backtest_context(rows, idx, atr, bias, side=None, *, timeframe="15m", symbol=None):
    atr_percentile = _rolling_percentile_at(atr, idx)
    volume_ratio = _volume_ratio_at(rows, idx)
    donchian_mid = _donchian_mid_at(rows, idx)
    direction = str(bias[idx] or side or "").lower() if 0 <= idx < len(bias) else str(side or "").lower()
    htf_trend = "UP" if direction == "long" else "DOWN" if direction == "short" else "FLAT"
    plus_di = 30.0 if direction == "long" else 12.0
    minus_di = 30.0 if direction == "short" else 12.0
    context = {
        "symbol": symbol or "UNKNOWN",
        "timeframe": timeframe,
        "side": side,
        "atr_percentile": atr_percentile,
        "volume_ratio": volume_ratio,
        "squeeze_percentile": atr_percentile,
        "utbot_direction": direction.upper() if direction else "",
        "adx": 28.0 if direction in {"long", "short"} else 10.0,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "close": rows[idx]["close"],
        "donchian_mid": donchian_mid,
        "htf_trend": htf_trend,
        "spread_bps": 1.0,
        "abnormal_candle_zscore": 0.0,
        "derivatives_crowding_score": 0.0,
        "range_expansion_ratio": 1.0,
        "trend_health_score": 60.0,
        "strategy_quality_score": 60.0,
        "adaptive_timeframe_score": 60.0,
    }
    context["regime"] = classify_regime(context)
    return context


def _advanced_market_context(rows, idx, atr, bias, side=None, *, timeframe="15m", symbol=None):
    base = _backtest_context(rows, idx, atr, bias, side, timeframe=timeframe, symbol=symbol)
    row = rows[idx]
    values = {
        **base,
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "close": row.get("close"),
        "volume": row.get("volume"),
        "atr": atr[idx],
        "utbot_direction": base.get("utbot_direction") or str(side or "").upper(),
        "side": side,
        "funding_rate": row.get("funding_rate"),
        "funding_delta": row.get("funding_delta"),
        "oi_change_pct": row.get("oi_change_pct"),
        "long_short_ratio": row.get("long_short_ratio"),
        "taker_buy_sell_ratio": row.get("taker_buy_sell_ratio"),
        "liquidation_spike_score": row.get("liquidation_spike_score"),
        "spread_bps": row.get("spread_bps", base.get("spread_bps")),
        "orderbook_imbalance": row.get("orderbook_imbalance"),
        "macro_risk_flag": bool(row.get("macro_risk_flag", False)),
        "news_spike_flag": bool(row.get("news_spike_flag", False)),
    }
    return build_market_context(rows, idx, symbol=symbol or "UNKNOWN", timeframe=timeframe, values=values)


def _advanced_config(args_or_config=None):
    source = vars(args_or_config) if hasattr(args_or_config, "__dict__") else dict(args_or_config or {})
    enabled_engines = source.get("enabled_engines")
    return {
        "advanced_alpha_engine_enabled": bool(source.get("advanced_alpha", source.get("advanced_alpha_enabled", False))),
        "adaptive_ladder_tp_enabled": bool(source.get("adaptive_ladder_tp", source.get("adaptive_ladder_tp_enabled", False))),
        "macro_guard_enabled": bool(source.get("macro_guard", source.get("macro_guard_enabled", False))),
        "engine_kill_switch_enabled": bool(source.get("engine_kill_switch_enabled", True)),
        "engine": source.get("engine") or "ALL",
        "enabled_engines": enabled_engines,
        "risk_per_trade_pct": source.get("risk_pct", source.get("risk_per_trade_percent", 0.5)),
        "max_risk_per_trade_pct": min(1.0, float(source.get("max_risk_per_trade_pct", 1.0) or 1.0)),
        "min_risk_per_trade_pct": 0.0,
        "max_total_open_risk_pct": 2.0,
        "max_same_direction_positions": 2,
        "soft_reduce_consecutive_losses": 2,
        "hard_stop_consecutive_losses": 3,
        "daily_loss_limit_r": 2.0,
        "weekly_loss_limit_r": 5.0,
        "macro_guard_mode": "BLOCK" if bool(source.get("macro_guard", False)) else "OFF",
        "meta_label_gate_enabled": bool(source.get("meta_label_gate", False)),
        "min_alpha_confidence": float(source.get("min_alpha_confidence", 0.55) or 0.55),
        "allow_advanced_reversal_in_chop": bool(source.get("allow_advanced_reversal_in_chop", False)),
        "derivatives_data_required": bool(source.get("derivatives_data_required", False)),
        "require_engine_stats": bool(source.get("require_engine_stats", False)),
        "default_engine_expected_r": float(source.get("default_engine_expected_r", 0.30) or 0.30),
    }


def _apply_slippage(price, side, bps, is_entry):
    direction = 1.0
    if side == "short":
        direction = -1.0
    if not is_entry:
        direction *= -1.0
    return price * (1.0 + direction * (bps / 10000.0))


def _timestamp_seconds(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = float(text)
        if parsed > 1_000_000_000_000:
            return parsed / 1000.0
        if parsed > 1_000_000_000:
            return parsed
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _max_consecutive_losses(trades):
    worst = 0
    current = 0
    for trade in trades:
        if trade.get("pnl", 0.0) <= 0:
            current += 1
            worst = max(worst, current)
        else:
            current = 0
    return worst


def _performance_summary(trades):
    if not trades:
        return {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "win_rate": None}
    net = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = len(trades) - wins
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "net_pnl": net,
        "win_rate": wins / len(trades) * 100.0,
        "average_R": sum(t.get("r_multiple", 0.0) for t in trades) / len(trades),
    }


def _funding_payment_for_bar(row, position, funding_mode, funding_bps_per_bar):
    mode = str(funding_mode or "").lower()
    if mode == "disabled":
        return 0.0
    notional = float(position["entry_price"]) * float(position["qty"])
    if mode == "estimated":
        rate = float(funding_bps_per_bar or 0.0) / 10000.0
    elif mode == "actual":
        has_event = bool(
            row.get("funding_event")
            or row.get("is_funding_time")
            or row.get("funding_timestamp") not in (None, "")
        )
        if not has_event or row.get("funding_rate") in (None, ""):
            return 0.0
        rate = float(row.get("funding_rate") or 0.0)
    else:
        raise ValueError(f"unsupported funding_mode: {funding_mode}")
    payment = notional * rate
    return -payment if position["side"] == "long" else payment


def _mark_to_market_equity(balance, position, close, fee_bps):
    if not position:
        return float(balance), 0.0, 0.0
    qty = float(position.get("qty") or 0.0)
    entry = float(position.get("entry_price") or 0.0)
    unrealized = (float(close) - entry) * qty
    if position.get("side") == "short":
        unrealized *= -1.0
    estimated_close_cost = abs(float(close) * qty) * float(fee_bps or 0.0) / 10000.0
    equity = (
        float(balance)
        + unrealized
        + float(position.get("funding_pnl") or 0.0)
        - estimated_close_cost
    )
    return equity, unrealized, estimated_close_cost


def simulate_utbot_rr(
    rows,
    *,
    key_value,
    atr_period,
    strategy="baseline",
    exit_policy_name=DEFAULT_EXIT_POLICY,
    timeframe="15m",
    symbol=None,
    initial_balance=1000.0,
    risk_per_trade_percent=0.5,
    max_risk_per_trade_usdt=10.0,
    leverage=5.0,
    stop_atr_multiplier=1.5,
    rr_multiple=2.0,
    partial_take_profit_r_multiple=1.5,
    partial_take_profit_ratio=0.5,
    breakeven_after_partial=True,
    trailing_atr_multiplier=0.0,
    fee_bps=4.0,
    slippage_bps=1.0,
    funding_bps_per_bar=0.0,
    funding_mode=None,
    meta_label_gate_enabled=False,
    regime_router_enabled=False,
    signal_invalid_exit_enabled=True,
    time_stop_enabled=True,
    volatility_adaptive_tp_enabled=True,
    advanced_alpha_enabled=False,
    adaptive_ladder_tp_enabled=False,
    macro_guard_enabled=False,
    engine=None,
    enabled_engines=None,
    min_alpha_confidence=0.55,
):
    atr, trail, signal, bias = utbot_rows(rows, key_value=key_value, atr_period=atr_period)
    balance = float(initial_balance)
    equity_peak = balance
    max_drawdown = 0.0
    equity_curve = []
    trades = []
    position = None
    counts = {
        "time_stop_count": 0,
        "signal_invalid_exit_count": 0,
        "meta_gate_reject_count": 0,
        "derivatives_veto_count": 0,
        "macro_block_count": 0,
        "engine_kill_count": 0,
        "regime_block_count": 0,
        "size_reduction_count": 0,
        "ladder_tp1_count": 0,
        "ladder_tp2_count": 0,
        "ladder_tp3_count": 0,
        "stop_loss_count": 0,
        "end_of_data_count": 0,
    }

    effective_funding_mode = str(
        funding_mode or ("estimated" if funding_bps_per_bar else "disabled")
    ).lower()
    if effective_funding_mode not in {"actual", "estimated", "disabled"}:
        raise ValueError(f"invalid funding_mode: {effective_funding_mode}")

    for idx in range(len(rows)):
        row = rows[idx]
        if position:
            side = position["side"]
            if side == "long":
                favorable = row["high"] - position["entry_price"]
                adverse = position["entry_price"] - row["low"]
            else:
                favorable = position["entry_price"] - row["low"]
                adverse = row["high"] - position["entry_price"]
            position["mfe_r"] = max(position["mfe_r"], favorable / max(position["risk_distance"], 1e-9))
            position["mae_r"] = max(position["mae_r"], adverse / max(position["risk_distance"], 1e-9))

            funding_pnl = _funding_payment_for_bar(
                row,
                position,
                effective_funding_mode,
                funding_bps_per_bar,
            )
            position["funding_pnl"] += funding_pnl
            position["funding_abs"] += abs(funding_pnl)

            marked_equity, unrealized_pnl, estimated_close_cost = _mark_to_market_equity(
                balance,
                position,
                row["close"],
                fee_bps,
            )
            equity_peak = max(equity_peak, marked_equity)
            max_drawdown = max(max_drawdown, equity_peak - marked_equity)
            equity_curve.append(
                {
                    "index": idx,
                    "timestamp": row.get("timestamp"),
                    "cash": balance,
                    "unrealized_pnl": unrealized_pnl,
                    "estimated_close_cost": estimated_close_cost,
                    "equity": marked_equity,
                    "equity_peak": equity_peak,
                    "drawdown_usdt": equity_peak - marked_equity,
                }
            )

            stop_hit = row["low"] <= position["stop_loss"] if side == "long" else row["high"] >= position["stop_loss"]
            target_hit = row["high"] >= position["take_profit"] if side == "long" else row["low"] <= position["take_profit"]
            partial_hit = (
                not position.get("partial_filled")
                and position.get("partial_take_profit")
                and (
                    row["high"] >= position["partial_take_profit"]
                    if side == "long" else row["low"] <= position["partial_take_profit"]
                )
            )
            exit_reason = None
            exit_price = None
            # Conservative intrabar rule: if the same candle can hit SL and TP, SL wins.
            if stop_hit:
                exit_reason = "SL"
                exit_price = position["stop_loss"]
                counts["stop_loss_count"] += 1
            elif position.get("ladder_targets"):
                ladder_targets = position.get("ladder_targets") or []
                for target in ladder_targets[:-1]:
                    if target.get("filled"):
                        continue
                    hit = row["high"] >= target["price"] if side == "long" else row["low"] <= target["price"]
                    if not hit:
                        continue
                    target_qty = min(position["qty"], position["initial_qty"] * target["qty_ratio"])
                    if target_qty <= 0:
                        target["filled"] = True
                        continue
                    partial_price = _apply_slippage(target["price"], side, slippage_bps, is_entry=False)
                    gross = (partial_price - position["entry_price"]) * target_qty
                    if side == "short":
                        gross *= -1.0
                    fees = (position["entry_price"] * target_qty + partial_price * target_qty) * fee_bps / 10000.0
                    pnl = gross - fees
                    balance += pnl
                    equity_peak = max(equity_peak, balance)
                    max_drawdown = max(max_drawdown, equity_peak - balance)
                    position["qty"] -= target_qty
                    position["realized_pnl"] += pnl
                    position["fee_paid"] += fees
                    target["filled"] = True
                    label = str(target.get("label") or "").upper()
                    if label == "TP1":
                        position["partial_filled"] = True
                        position["tp1_filled"] = True
                        counts["ladder_tp1_count"] += 1
                        if position.get("move_sl_to_be_after_tp1", True):
                            buffer_r = max(
                                0.0,
                                _float(position.get("breakeven_buffer_r"), (fee_bps * 2.0 + slippage_bps * 2.0) / 10000.0),
                            )
                            if side == "long":
                                position["stop_loss"] = max(position["stop_loss"], position["entry_price"] + position["risk_distance"] * buffer_r)
                            else:
                                position["stop_loss"] = min(position["stop_loss"], position["entry_price"] - position["risk_distance"] * buffer_r)
                    elif label == "TP2":
                        counts["ladder_tp2_count"] += 1
                        if position.get("move_sl_to_tp1_after_tp2", False):
                            tp1_target = next((item for item in ladder_targets if item.get("label") == "TP1"), None)
                            if tp1_target:
                                if side == "long":
                                    position["stop_loss"] = max(position["stop_loss"], tp1_target["price"])
                                else:
                                    position["stop_loss"] = min(position["stop_loss"], tp1_target["price"])
                final_target = ladder_targets[-1] if ladder_targets else None
                if final_target:
                    final_hit = row["high"] >= final_target["price"] if side == "long" else row["low"] <= final_target["price"]
                    if final_hit and position["qty"] > 0:
                        label = str(final_target.get("label") or "TP").upper()
                        if label == "TP2":
                            counts["ladder_tp2_count"] += 1
                            exit_reason = "TP2_FINAL"
                        elif label == "TP3":
                            counts["ladder_tp3_count"] += 1
                            exit_reason = "TP3_FINAL"
                        else:
                            exit_reason = "TP"
                        exit_price = final_target["price"]
            elif partial_hit:
                partial_qty = min(
                    position["qty"],
                    position["initial_qty"] * position["partial_take_profit_ratio"],
                )
                partial_price = _apply_slippage(position["partial_take_profit"], side, slippage_bps, is_entry=False)
                gross = (partial_price - position["entry_price"]) * partial_qty
                if side == "short":
                    gross *= -1.0
                fees = (position["entry_price"] * partial_qty + partial_price * partial_qty) * fee_bps / 10000.0
                pnl = gross - fees
                balance += pnl
                equity_peak = max(equity_peak, balance)
                max_drawdown = max(max_drawdown, equity_peak - balance)
                position["qty"] -= partial_qty
                position["realized_pnl"] += pnl
                position["fee_paid"] += fees
                position["partial_filled"] = True
                position["tp1_filled"] = True
                counts["ladder_tp1_count"] += 1
                if breakeven_after_partial:
                    buffer_r = max(
                        0.0,
                        _float(position.get("breakeven_buffer_r"), (fee_bps * 2.0 + slippage_bps * 2.0) / 10000.0),
                    )
                    if side == "long":
                        position["stop_loss"] = position["entry_price"] + position["risk_distance"] * buffer_r
                    else:
                        position["stop_loss"] = position["entry_price"] - position["risk_distance"] * buffer_r
                target_hit = row["high"] >= position["take_profit"] if side == "long" else row["low"] <= position["take_profit"]
                if target_hit and position["qty"] > 0:
                    exit_reason = "TP2"
                    exit_price = position["take_profit"]
                    counts["ladder_tp2_count"] += 1
            elif target_hit:
                exit_reason = "TP"
                exit_price = position["take_profit"]
                counts["ladder_tp2_count"] += 1
            if not exit_reason and position.get("time_stop_enabled", False):
                time_stop = evaluate_time_stop(
                    position,
                    _bar_with_index(row, idx),
                    {
                        "time_stop_enabled": time_stop_enabled,
                        "timeframe": timeframe,
                        "max_bars_to_tp1": {"5m": 12, "15m": 8, "30m": 6, "1h": 4},
                    },
                )
                if time_stop.should_exit:
                    counts["time_stop_count"] += 1
                    exit_reason = time_stop.reason
                    exit_price = row["close"]
            if not exit_reason and position.get("signal_invalid_exit_enabled", False):
                context = _backtest_context(rows, idx, atr, bias, side, timeframe=timeframe, symbol=symbol)
                invalid_exit = evaluate_signal_invalid_exit(
                    position,
                    context,
                    {
                        "signal_invalid_exit_enabled": signal_invalid_exit_enabled,
                        "htf_invalid_exit_enabled": False,
                    },
                )
                if invalid_exit.should_exit:
                    counts["signal_invalid_exit_count"] += 1
                    exit_reason = invalid_exit.reason
                    exit_price = row["close"]
            if exit_reason:
                exit_price = _apply_slippage(exit_price, side, slippage_bps, is_entry=False)
                qty = position["qty"]
                gross = (exit_price - position["entry_price"]) * qty
                if side == "short":
                    gross *= -1.0
                fees = (position["entry_price"] * qty + exit_price * qty) * fee_bps / 10000.0
                closing_pnl = gross - fees
                total_trade_pnl = closing_pnl + position.get("realized_pnl", 0.0) + position.get("funding_pnl", 0.0)
                balance += closing_pnl + position.get("funding_pnl", 0.0)
                fee_paid = position.get("fee_paid", 0.0) + fees
                risk_usdt = max(position.get("risk_usdt", 0.0), 1e-9)
                trade = dict(position)
                trade.update({
                    "exit_idx": idx,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "pnl": total_trade_pnl,
                    "r_multiple": total_trade_pnl / risk_usdt,
                    "fee_paid": fee_paid,
                    "balance": balance,
                })
                trades.append(trade)
                equity_peak = max(equity_peak, balance)
                max_drawdown = max(max_drawdown, equity_peak - balance)
                position = None
            elif position.get("partial_filled") and trailing_atr_multiplier > 0 and atr[idx] is not None:
                # The close of this candle is only known after its high/low path.
                # Store the new trailing stop for the next candle.
                if side == "long":
                    position["stop_loss"] = max(
                        position["stop_loss"],
                        row["close"] - (atr[idx] * trailing_atr_multiplier),
                    )
                else:
                    position["stop_loss"] = min(
                        position["stop_loss"],
                        row["close"] + (atr[idx] * trailing_atr_multiplier),
                    )
            continue

        equity_peak = max(equity_peak, balance)
        max_drawdown = max(max_drawdown, equity_peak - balance)
        equity_curve.append(
            {
                "index": idx,
                "timestamp": row.get("timestamp"),
                "cash": balance,
                "unrealized_pnl": 0.0,
                "estimated_close_cost": 0.0,
                "equity": balance,
                "equity_peak": equity_peak,
                "drawdown_usdt": equity_peak - balance,
            }
        )

        if atr[idx] is None:
            continue
        decision = None
        ladder = None
        if advanced_alpha_enabled:
            provisional_side = signal[idx] or bias[idx]
            context = _advanced_market_context(rows, idx, atr, bias, provisional_side, timeframe=timeframe, symbol=symbol)
            advanced_cfg = _advanced_config({
                "advanced_alpha": True,
                "adaptive_ladder_tp": adaptive_ladder_tp_enabled,
                "macro_guard": macro_guard_enabled,
                "engine": engine or "ALL",
                "enabled_engines": enabled_engines,
                "risk_pct": risk_per_trade_percent,
                "max_risk_per_trade_pct": min(1.0, max(0.0, risk_per_trade_percent * 2.0)),
                "meta_label_gate": meta_label_gate_enabled,
                "min_alpha_confidence": min_alpha_confidence,
            })
            decision = evaluate_final_trade_decision(
                row,
                context,
                advanced_cfg,
                models=None,
                stats={},
            )
            if not decision.valid:
                joined = ",".join(decision.reasons)
                if "MACRO" in joined:
                    counts["macro_block_count"] += 1
                if "DERIVATIVES" in joined:
                    counts["derivatives_veto_count"] += 1
                if "META" in joined:
                    counts["meta_gate_reject_count"] += 1
                if "REGIME_BLOCK" in joined:
                    counts["regime_block_count"] += 1
                continue
            side = decision.side.lower()
            ladder = decision.ladder_tp
            effective_risk_pct = decision.risk_pct
            policy = build_exit_policy(
                exit_policy_name,
                context,
                {
                    "tp1_r": partial_take_profit_r_multiple,
                    "tp1_size_pct": partial_take_profit_ratio * 100.0,
                    "tp2_r": rr_multiple,
                },
            )
        else:
            if signal[idx] not in {"long", "short"}:
                continue
            side = signal[idx]
            context = _backtest_context(rows, idx, atr, bias, side, timeframe=timeframe, symbol=symbol)
            policy = build_exit_policy(
                exit_policy_name,
                context,
                {
                    "tp1_r": partial_take_profit_r_multiple,
                    "tp1_size_pct": partial_take_profit_ratio * 100.0,
                    "tp2_r": rr_multiple,
                },
            )
            action = {"risk_multiplier": 1.0}
            if regime_router_enabled:
                regime = classify_regime(context)
                context["regime"] = regime
                action = regime_action(
                    regime,
                    {"side": side, "has_breakout": True, "volume_ratio": context.get("volume_ratio", 1.0)},
                )
                if (side == "long" and not action.allow_long) or (side == "short" and not action.allow_short):
                    counts["regime_block_count"] += 1
                    continue
            gate = meta_label_gate(
                {"side": side},
                context,
                model=None,
                config={"meta_label_gate_enabled": meta_label_gate_enabled, "meta_gate_min_prob": 0.55},
            )
            if not gate.allow:
                counts["meta_gate_reject_count"] += 1
                continue
            effective_risk_pct = calculate_adaptive_risk_pct(
                {"side": side},
                {**context, "account": {}, "portfolio": {}},
                action,
                {"size_multiplier": 1.0},
                gate,
                {
                    "risk_per_trade_pct": risk_per_trade_percent,
                    "min_risk_per_trade_pct": 0.0,
                    "max_risk_per_trade_pct": min(1.0, max(0.0, risk_per_trade_percent * 2.0)),
                },
            )
        if effective_risk_pct <= 0:
            counts["size_reduction_count"] += 1
            continue
        if effective_risk_pct < risk_per_trade_percent:
            counts["size_reduction_count"] += 1
        entry_idx = idx + 1
        if entry_idx >= len(rows):
            continue
        entry_row = rows[entry_idx]
        entry = _apply_slippage(entry_row["open"], side, slippage_bps, is_entry=True)
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry,
                atr_value=atr[idx],
                stop_atr_multiplier=stop_atr_multiplier,
                ut_stop=trail[idx],
                take_profit_r_multiple=policy.tp2_r,
                min_risk_reward=min(rr_multiple, policy.tp2_r),
                balance_usdt=balance,
                risk_per_trade_percent=effective_risk_pct,
                max_risk_per_trade_usdt=max_risk_per_trade_usdt,
                leverage=leverage,
            )
        except ValueError:
            continue
        ladder_targets = None
        if ladder:
            ladder_targets = []
            for target in ladder.targets():
                r_multiple = _float(target.get("r"), 0.0)
                if r_multiple <= 0:
                    continue
                price = entry + (plan["risk_distance"] * r_multiple) if side == "long" else entry - (plan["risk_distance"] * r_multiple)
                ladder_targets.append({
                    "label": target.get("label"),
                    "r": r_multiple,
                    "qty_ratio": max(0.0, min(1.0, _float(target.get("pct"), 0.0) / 100.0)),
                    "price": price,
                    "filled": False,
                })
            total_ratio = sum(item["qty_ratio"] for item in ladder_targets)
            if ladder_targets and abs(total_ratio - 1.0) > 1e-9:
                for item in ladder_targets:
                    item["qty_ratio"] = item["qty_ratio"] / max(total_ratio, 1e-9)
            if not ladder_targets:
                ladder_targets = None
        tp1_r = ladder_targets[0]["r"] if ladder_targets else policy.tp1_r
        final_tp_r = ladder_targets[-1]["r"] if ladder_targets else policy.tp2_r
        position = {
            "entry_idx": entry_idx,
            "signal_idx": idx,
            "side": side,
            "entry_price": entry,
            "signal_price": row["close"],
            "entry_fill_price_source": "NEXT_OPEN",
            "entry_signal_timestamp": row.get("timestamp"),
            "entry_fill_timestamp": entry_row.get("timestamp"),
            "qty": plan["qty"],
            "initial_qty": plan["qty"],
            "stop_loss": plan["stop_loss"],
            "take_profit": (
                entry + (plan["risk_distance"] * final_tp_r)
                if side == "long" else
                entry - (plan["risk_distance"] * final_tp_r)
            ),
            "partial_take_profit": (
                entry + (plan["risk_distance"] * tp1_r)
                if side == "long" else
                entry - (plan["risk_distance"] * tp1_r)
            ) if (ladder_targets or (policy.tp1_r > 0 and policy.tp1_size_pct > 0)) else None,
            "partial_take_profit_ratio": (
                ladder_targets[0]["qty_ratio"] if ladder_targets else max(0.0, min(1.0, policy.tp1_size_pct / 100.0))
            ),
            "ladder_targets": ladder_targets,
            "move_sl_to_be_after_tp1": bool(getattr(ladder, "move_sl_to_be_after_tp1", True)),
            "move_sl_to_tp1_after_tp2": bool(getattr(ladder, "move_sl_to_tp1_after_tp2", False)),
            "partial_filled": False,
            "tp1_filled": False,
            "entry_bar_index": entry_idx,
            "timeframe": timeframe,
            "exit_policy_name": policy.name,
            "time_stop_enabled": bool(policy.time_stop_enabled and time_stop_enabled),
            "signal_invalid_exit_enabled": bool(policy.signal_invalid_exit_enabled and signal_invalid_exit_enabled),
            "volatility_adaptive_tp_enabled": bool(policy.volatility_adaptive_tp_enabled and volatility_adaptive_tp_enabled),
            "alpha_engine": decision.engine if decision else None,
            "alpha_confidence": decision.confidence if decision else None,
            "alpha_expected_r": decision.expected_r if decision else None,
            "breakeven_buffer_r": (fee_bps * 2.0 + slippage_bps * 2.0) / 10000.0,
            "risk_pct": effective_risk_pct,
            "regime": decision.regime if decision else context.get("regime"),
            "meta_probability": None if decision else gate.probability,
            "realized_pnl": 0.0,
            "funding_pnl": 0.0,
            "funding_abs": 0.0,
            "fee_paid": 0.0,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "risk_distance": plan["risk_distance"],
            "risk_usdt": plan["risk_usdt"],
            "planned_margin": plan["planned_margin"],
            "planned_notional": plan["planned_notional"],
        }

    if position and rows:
        idx = len(rows) - 1
        row = rows[idx]
        side = position["side"]
        exit_price = _apply_slippage(row["close"], side, slippage_bps, is_entry=False)
        qty = position["qty"]
        gross = (exit_price - position["entry_price"]) * qty
        if side == "short":
            gross *= -1.0
        fees = (position["entry_price"] * qty + exit_price * qty) * fee_bps / 10000.0
        closing_pnl = gross - fees
        total_trade_pnl = closing_pnl + position.get("realized_pnl", 0.0) + position.get("funding_pnl", 0.0)
        balance += closing_pnl + position.get("funding_pnl", 0.0)
        fee_paid = position.get("fee_paid", 0.0) + fees
        risk_usdt = max(position.get("risk_usdt", 0.0), 1e-9)
        trade = dict(position)
        trade.update({
            "exit_idx": idx,
            "exit_price": exit_price,
            "exit_reason": "END_OF_DATA",
            "pnl": total_trade_pnl,
            "r_multiple": total_trade_pnl / risk_usdt,
            "fee_paid": fee_paid,
            "balance": balance,
        })
        trades.append(trade)
        counts["end_of_data_count"] += 1
        equity_peak = max(equity_peak, balance)
        max_drawdown = max(max_drawdown, equity_peak - balance)
        position = None

    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    net_pnl = sum(t["pnl"] for t in trades)
    first_ts = _timestamp_seconds(rows[0]["timestamp"]) if rows else None
    last_ts = _timestamp_seconds(rows[-1]["timestamp"]) if rows else None
    months = None
    if first_ts is not None and last_ts is not None and last_ts > first_ts:
        months = (last_ts - first_ts) / (30.4375 * 24 * 60 * 60)
    total_fee = sum(t.get("fee_paid", 0.0) for t in trades)
    total_funding_abs = sum(t.get("funding_abs", 0.0) for t in trades)
    total_notional = sum(t.get("planned_notional", 0.0) for t in trades)
    long_trades = [t for t in trades if t.get("side") == "long"]
    short_trades = [t for t in trades if t.get("side") == "short"]
    engine_performance = {}
    for engine_name in ALPHA_ENGINE_NAMES:
        engine_trades = [t for t in trades if t.get("alpha_engine") == engine_name]
        if engine_trades:
            engine_performance[engine_name] = _performance_summary(engine_trades)
    end_of_data_trades = sum(1 for t in trades if t.get("exit_reason") == "END_OF_DATA")
    time_stop_trades = sum(1 for t in trades if str(t.get("exit_reason") or "").startswith("TIME_STOP"))
    signal_invalid_trades = sum(1 for t in trades if str(t.get("exit_reason") or "").startswith("SIGNAL_INVALID"))
    return {
        "strategy": strategy,
        "exit_policy": exit_policy_name,
        "trades": len(trades),
        "total_trades": len(trades),
        "wins": sum(1 for t in trades if t["pnl"] > 0),
        "losses": sum(1 for t in trades if t["pnl"] <= 0),
        "win_rate": (sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100.0) if trades else None,
        "net_pnl": net_pnl,
        "total_return_pct": net_pnl / max(initial_balance, 1e-9) * 100.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "expectancy": net_pnl / len(trades) if trades else None,
        "average_R": sum(t.get("r_multiple", 0.0) for t in trades) / len(trades) if trades else None,
        "expectancy_r": sum(t.get("r_multiple", 0.0) for t in trades) / len(trades) if trades else None,
        "max_consecutive_losses": _max_consecutive_losses(trades),
        "average_MFE_R": sum(t.get("mfe_r", 0.0) for t in trades) / len(trades) if trades else None,
        "average_MAE_R": sum(t.get("mae_r", 0.0) for t in trades) / len(trades) if trades else None,
        "fee_burden_pct": total_fee / max(total_notional, 1e-9) * 100.0 if trades else None,
        "funding_burden_pct": total_funding_abs / max(total_notional, 1e-9) * 100.0 if trades else None,
        "funding_mode": effective_funding_mode,
        "trades_per_month": len(trades) / months if months and months > 0 else None,
        "long_performance": _performance_summary(long_trades),
        "short_performance": _performance_summary(short_trades),
        "long_trades": len(long_trades),
        "short_trades": len(short_trades),
        "engine_performance": engine_performance,
        "max_drawdown_usdt": max_drawdown,
        "max_drawdown_pct": max_drawdown / max(initial_balance, 1e-9) * 100.0,
        "ending_balance": balance,
        "equity_curve": equity_curve,
        "time_stop_count": time_stop_trades or counts["time_stop_count"],
        "signal_invalid_exit_count": signal_invalid_trades or counts["signal_invalid_exit_count"],
        "meta_gate_reject_count": counts["meta_gate_reject_count"],
        "derivatives_veto_count": counts["derivatives_veto_count"],
        "macro_block_count": counts["macro_block_count"],
        "engine_kill_count": counts["engine_kill_count"],
        "regime_block_count": counts["regime_block_count"],
        "size_reduction_count": counts["size_reduction_count"],
        "ladder_tp1_count": counts["ladder_tp1_count"],
        "ladder_tp2_count": counts["ladder_tp2_count"],
        "ladder_tp3_count": counts["ladder_tp3_count"],
        "stop_loss_count": counts["stop_loss_count"],
        "END_OF_DATA_trades": end_of_data_trades,
        "trades_detail": trades,
    }


def _simulate_variant(rows, params, args):
    return simulate_utbot_rr(
        rows,
        strategy=getattr(args, "strategy", "baseline"),
        exit_policy_name=getattr(args, "exit_policy", DEFAULT_EXIT_POLICY),
        timeframe=getattr(args, "timeframe", "15m"),
        symbol=getattr(args, "symbol", None),
        initial_balance=args.initial_balance,
        risk_per_trade_percent=args.risk_pct,
        max_risk_per_trade_usdt=args.max_risk_usdt,
        leverage=args.leverage,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        funding_bps_per_bar=args.funding_bps_per_bar,
        funding_mode=getattr(args, "funding_mode", None),
        partial_take_profit_r_multiple=args.partial_tp_r,
        partial_take_profit_ratio=args.partial_tp_ratio,
        breakeven_after_partial=not args.no_breakeven_after_partial,
        trailing_atr_multiplier=args.trailing_atr_mult,
        meta_label_gate_enabled=getattr(args, "meta_label_gate", False),
        regime_router_enabled=getattr(args, "regime_router", False),
        advanced_alpha_enabled=getattr(args, "advanced_alpha", False),
        adaptive_ladder_tp_enabled=getattr(args, "adaptive_ladder_tp", False),
        macro_guard_enabled=getattr(args, "macro_guard", False),
        engine=getattr(args, "engine", None),
        enabled_engines=getattr(args, "enabled_engines", None),
        min_alpha_confidence=getattr(args, "min_alpha_confidence", 0.55),
        **params,
    )


def walk_forward_report(rows, variants, args):
    train_size = max(50, int(args.wf_train_candles))
    test_size = max(20, int(args.wf_test_candles))
    purge_size = max(0, int(getattr(args, "wf_purge_candles", 4) or 0))
    min_trades = max(1, int(args.wf_min_trades))
    concentration_warning_threshold = max(
        0.0,
        min(1.0, float(getattr(args, "wf_selection_warning_threshold", 0.60) or 0.60)),
    )
    windows = []
    start = 0
    while start + train_size + purge_size + test_size <= len(rows):
        train_end = start + train_size
        test_start = train_end + purge_size
        test_end = test_start + test_size
        train_rows = rows[start:train_end]
        test_rows = rows[test_start:test_end]
        train_results = {}
        candidates = []
        for name, params in variants.items():
            result = _simulate_variant(train_rows, params, args)
            result.pop("trades_detail", None)
            train_results[name] = result
            if result["trades"] < min_trades:
                continue
            pf = result["profit_factor"] if result["profit_factor"] is not None else 0.0
            avg_r = result["average_R"] if result["average_R"] is not None else 0.0
            score = pf + avg_r - (result["max_drawdown_pct"] * 0.03)
            candidates.append((score, name))
        if candidates:
            _, selected_name = max(candidates, key=lambda item: item[0])
            selected_params = variants[selected_name]
            test_result = _simulate_variant(test_rows, selected_params, args)
            test_result.pop("trades_detail", None)
            train_selected_result = train_results[selected_name]
            train_expectancy = train_selected_result.get(
                "average_R", train_selected_result.get("expectancy_r", 0.0)
            ) or 0.0
            test_expectancy = test_result.get(
                "average_R", test_result.get("expectancy_r", 0.0)
            ) or 0.0
            expectancy_retention = (
                test_expectancy / train_expectancy if train_expectancy > 0 else None
            )
            generalization_gap_r = train_expectancy - test_expectancy
        else:
            selected_name = None
            test_result = {"trades": 0, "net_pnl": 0.0, "reason": "no_train_candidate"}
            expectancy_retention = None
            generalization_gap_r = None
        windows.append({
            "start": start,
            "train_end": train_end - 1,
            "purge_start": train_end,
            "purge_end": test_start - 1 if purge_size else None,
            "test_start": test_start,
            "test_end": test_end - 1,
            "selected": selected_name,
            "train_results": train_results,
            "test_result": test_result,
            "expectancy_retention": expectancy_retention,
            "generalization_gap_r": generalization_gap_r,
        })
        start += test_size
    selected_names = [window["selected"] for window in windows if window.get("selected")]
    selection_counts = {
        name: selected_names.count(name)
        for name in sorted(set(selected_names))
    }
    selection_concentration = (
        max(selection_counts.values()) / len(selected_names)
        if selected_names
        else 0.0
    )
    retention_values = [
        float(window["expectancy_retention"])
        for window in windows
        if window.get("expectancy_retention") is not None
    ]
    gap_values = [
        float(window["generalization_gap_r"])
        for window in windows
        if window.get("generalization_gap_r") is not None
    ]
    return {
        "windows": windows,
        "window_count": len(windows),
        "purge_candles": purge_size,
        "out_of_sample_net_pnl": sum(w.get("test_result", {}).get("net_pnl", 0.0) for w in windows),
        "out_of_sample_trades": sum(w.get("test_result", {}).get("trades", 0) for w in windows),
        "oos_pass_rate": (
            sum(1 for w in windows if (w.get("test_result", {}).get("average_R") or 0.0) > 0 and (w.get("test_result", {}).get("profit_factor") or 0.0) >= 1.05)
            / len(windows)
            if windows else 0.0
        ),
        "selection_counts": selection_counts,
        "selection_concentration": selection_concentration,
        "selection_concentration_warning": (
            bool(selected_names)
            and selection_concentration > concentration_warning_threshold
        ),
        "mean_expectancy_retention": (
            sum(retention_values) / len(retention_values) if retention_values else None
        ),
        "mean_generalization_gap_r": (
            sum(gap_values) / len(gap_values) if gap_values else None
        ),
    }


def compare_exit_policies(rows, variants, args):
    params = next(iter(variants.values()))
    results = {}
    for policy_name in EXIT_POLICY_CANDIDATES:
        policy_args = argparse.Namespace(**vars(args))
        policy_args.exit_policy = policy_name
        result = _simulate_variant(rows, params, policy_args)
        result.pop("trades_detail", None)
        results[policy_name] = result
    return {
        "results": results,
        "ranked": [
            {"score": score, "name": name, "trades": report.get("trades"), "expectancy_r": report.get("expectancy_r"), "profit_factor": report.get("profit_factor")}
            for score, name, report in rank_exit_policies(results, {"min_policy_trade_count": args.wf_min_trades})
        ],
    }


def _candidate_args(args, candidate):
    candidate_args = argparse.Namespace(**vars(args))
    candidate_args.strategy = candidate
    candidate_args.exit_policy = DEFAULT_EXIT_POLICY
    if candidate == "BASE_HYBRID_DEFENSIVE":
        candidate_args.meta_label_gate = False
        candidate_args.regime_router = False
    elif candidate == "META_FILTERED_BREAKOUT":
        candidate_args.meta_label_gate = True
        candidate_args.regime_router = False
    elif candidate == "LOW_CROWDING_BREAKOUT":
        candidate_args.meta_label_gate = True
        candidate_args.regime_router = False
        candidate_args.risk_pct = min(candidate_args.risk_pct, 0.4)
    elif candidate == "REGIME_ROUTED_BREAKOUT":
        candidate_args.meta_label_gate = True
        candidate_args.regime_router = True
    elif candidate == "DEFENSIVE_BTC_ETH_ONLY":
        candidate_args.meta_label_gate = True
        candidate_args.regime_router = True
        candidate_args.risk_pct = min(candidate_args.risk_pct, 0.5)
        candidate_args.max_risk_usdt = min(candidate_args.max_risk_usdt, 5.0)
    elif candidate == "BI_DIRECTIONAL_ALPHA":
        candidate_args.advanced_alpha = True
        candidate_args.adaptive_ladder_tp = True
        candidate_args.engine = "ALL"
        candidate_args.enabled_engines = [
            "TREND_CONTINUATION_LONG",
            "TREND_CONTINUATION_SHORT",
            "SQUEEZE_BREAKOUT_LONG",
            "SQUEEZE_BREAKOUT_SHORT",
        ]
        candidate_args.risk_pct = min(candidate_args.risk_pct, 0.5)
        candidate_args.max_risk_usdt = min(candidate_args.max_risk_usdt, 10.0)
    elif candidate == "TREND_AND_REVERSAL_COMBO":
        candidate_args.advanced_alpha = True
        candidate_args.adaptive_ladder_tp = True
        candidate_args.engine = "ALL"
        candidate_args.enabled_engines = [
            "TREND_CONTINUATION_LONG",
            "TREND_CONTINUATION_SHORT",
            "EXHAUSTION_REVERSAL_LONG",
            "EXHAUSTION_REVERSAL_SHORT",
        ]
        candidate_args.risk_pct = min(candidate_args.risk_pct, 0.25)
    elif candidate == "LIQUIDITY_SWEEP_ENGINE":
        candidate_args.advanced_alpha = True
        candidate_args.adaptive_ladder_tp = True
        candidate_args.engine = "ALL"
        candidate_args.enabled_engines = [
            "LIQUIDITY_SWEEP_REVERSAL_LONG",
            "LIQUIDITY_SWEEP_REVERSAL_SHORT",
        ]
        candidate_args.risk_pct = min(candidate_args.risk_pct, 0.25)
    elif candidate == "AGGRESSIVE_BUT_CAPPED_ALPHA":
        candidate_args.advanced_alpha = True
        candidate_args.adaptive_ladder_tp = True
        candidate_args.engine = "ALL"
        candidate_args.enabled_engines = list(ALPHA_ENGINE_NAMES)
        candidate_args.min_alpha_confidence = 0.72
        candidate_args.risk_pct = min(candidate_args.risk_pct, 1.0)
    return candidate_args


def compare_strategy_candidates(rows, variants, args):
    candidates = [
        "BASE_HYBRID_DEFENSIVE",
        "META_FILTERED_BREAKOUT",
        "LOW_CROWDING_BREAKOUT",
        "REGIME_ROUTED_BREAKOUT",
        "DEFENSIVE_BTC_ETH_ONLY",
    ]
    if getattr(args, "include_advanced_alpha", False):
        candidates.extend([
            "BI_DIRECTIONAL_ALPHA",
            "TREND_AND_REVERSAL_COMBO",
            "LIQUIDITY_SWEEP_ENGINE",
            "AGGRESSIVE_BUT_CAPPED_ALPHA",
        ])
    params = next(iter(variants.values()))
    results = {}
    ranked = []
    for candidate in candidates:
        candidate_args = _candidate_args(args, candidate)
        report = _simulate_variant(rows, params, candidate_args)
        report.pop("trades_detail", None)
        results[candidate] = report
        trades = report.get("trades", 0)
        expectancy = report.get("expectancy_r") or 0.0
        pf = report.get("profit_factor") or 0.0
        dd = report.get("max_drawdown_pct") or 0.0
        fee = report.get("fee_burden_pct") or 0.0
        losses = report.get("max_consecutive_losses") or 0
        if trades >= args.wf_min_trades and expectancy > 0 and pf >= 1.05 and dd <= 20:
            score = (
                0.25 * expectancy
                + 0.20 * min(pf, 3.0)
                - 0.20 * dd / 20.0
                - 0.10 * fee
                - 0.10 * losses / 10.0
            )
            ranked.append((round(score, 8), candidate))
    ranked.sort(reverse=True)
    return {"results": results, "ranked": [{"score": score, "name": name} for score, name in ranked]}


def _decision_for_report(report):
    trades = int(report.get("trades", report.get("total_trades", 0)) or 0)
    pf = report.get("profit_factor") or 0.0
    expectancy = report.get("expectancy_r", report.get("average_R")) or 0.0
    if trades < 10:
        return "NEED_MORE_DATA"
    if expectancy > 0 and pf >= 1.05:
        return "APPROVE_FOR_PAPER"
    return "REJECT"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="OHLCV CSV path")
    parser.add_argument("--strategy", default="baseline", choices=[
        "baseline",
        "live-parity",
        "BASE_HYBRID_DEFENSIVE",
        "META_FILTERED_BREAKOUT",
        "LOW_CROWDING_BREAKOUT",
        "REGIME_ROUTED_BREAKOUT",
        "DEFENSIVE_BTC_ETH_ONLY",
        "BI_DIRECTIONAL_ALPHA",
        "TREND_AND_REVERSAL_COMBO",
        "LIQUIDITY_SWEEP_ENGINE",
        "AGGRESSIVE_BUT_CAPPED_ALPHA",
    ])
    parser.add_argument("--exit-policy", default=DEFAULT_EXIT_POLICY)
    parser.add_argument("--compare-exits", action="store_true")
    parser.add_argument("--compare-candidates", action="store_true")
    parser.add_argument("--include-advanced-alpha", action="store_true")
    parser.add_argument("--advanced-alpha", action="store_true")
    parser.add_argument("--engine", choices=["ALL", *ALPHA_ENGINE_NAMES], default="ALL")
    parser.add_argument("--adaptive-ladder-tp", action="store_true")
    parser.add_argument("--macro-guard", action="store_true")
    parser.add_argument("--min-alpha-confidence", type=float, default=0.55)
    parser.add_argument("--meta-label-gate", action="store_true")
    parser.add_argument("--regime-router", action="store_true")
    parser.add_argument("--symbol", default="UNKNOWN")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--risk-pct", type=float, default=0.5)
    parser.add_argument("--max-risk-usdt", type=float, default=10.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--fee-bps", type=float, default=4.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--funding-bps-per-bar", type=float, default=0.0)
    parser.add_argument(
        "--funding-mode",
        choices=["actual", "estimated", "disabled"],
        default=None,
        help="actual requires event-marked historical funding rows",
    )
    parser.add_argument("--partial-tp-r", type=float, default=1.5)
    parser.add_argument("--partial-tp-ratio", type=float, default=0.5)
    parser.add_argument("--no-breakeven-after-partial", action="store_true")
    parser.add_argument("--trailing-atr-mult", type=float, default=0.0)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--train-months", type=int, default=None)
    parser.add_argument("--test-months", type=int, default=None)
    parser.add_argument("--wf-train-candles", type=int, default=600)
    parser.add_argument("--wf-test-candles", type=int, default=200)
    parser.add_argument(
        "--wf-purge-candles",
        type=int,
        default=4,
        help="candles excluded between each train and OOS test window",
    )
    parser.add_argument("--wf-min-trades", type=int, default=5)
    parser.add_argument(
        "--wf-selection-warning-threshold",
        type=float,
        default=0.60,
        help="warn when one variant dominates more than this share of walk-forward windows",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.strategy in {"BI_DIRECTIONAL_ALPHA", "TREND_AND_REVERSAL_COMBO", "LIQUIDITY_SWEEP_ENGINE", "AGGRESSIVE_BUT_CAPPED_ALPHA"}:
        args.advanced_alpha = True
        args.adaptive_ladder_tp = True if args.strategy != "baseline" else args.adaptive_ladder_tp
        args.include_advanced_alpha = True
        if args.strategy == "BI_DIRECTIONAL_ALPHA":
            args.enabled_engines = [
                "TREND_CONTINUATION_LONG",
                "TREND_CONTINUATION_SHORT",
                "SQUEEZE_BREAKOUT_LONG",
                "SQUEEZE_BREAKOUT_SHORT",
            ]
        elif args.strategy == "TREND_AND_REVERSAL_COMBO":
            args.enabled_engines = [
                "TREND_CONTINUATION_LONG",
                "TREND_CONTINUATION_SHORT",
                "EXHAUSTION_REVERSAL_LONG",
                "EXHAUSTION_REVERSAL_SHORT",
            ]
        elif args.strategy == "LIQUIDITY_SWEEP_ENGINE":
            args.enabled_engines = [
                "LIQUIDITY_SWEEP_REVERSAL_LONG",
                "LIQUIDITY_SWEEP_REVERSAL_SHORT",
            ]
        else:
            args.enabled_engines = list(ALPHA_ENGINE_NAMES)
            args.min_alpha_confidence = max(args.min_alpha_confidence, 0.72)
    else:
        args.enabled_engines = None
    if args.train_months:
        args.wf_train_candles = max(
            args.wf_train_candles,
            candles_for_months(args.train_months, args.timeframe),
        )
    if args.test_months:
        args.wf_test_candles = max(
            args.wf_test_candles,
            candles_for_months(args.test_months, args.timeframe),
        )

    rows = load_ohlcv_csv(args.csv)
    variants = {
        "UTBot_2_10": {"key_value": 2.0, "atr_period": 10},
        "UTBot_2_5_14": {"key_value": 2.5, "atr_period": 14},
    }
    report = {}
    if args.compare_exits:
        report["exit_policy_comparison"] = compare_exit_policies(rows, variants, args)
    elif args.compare_candidates:
        report["strategy_candidate_comparison"] = compare_strategy_candidates(rows, variants, args)
    else:
        for name, params in variants.items():
            result = _simulate_variant(rows, params, args)
            result.pop("trades_detail", None)
            result["decision"] = _decision_for_report(result)
            report[name] = result
    if args.walk_forward:
        report["walk_forward"] = walk_forward_report(rows, variants, args)

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if args.advanced_alpha or args.include_advanced_alpha:
            print("# Advanced UT Breakout Research Report")
            print(f"Candidate: {args.strategy}")
            print(f"Engine: {args.engine}")
            print(f"Mode: {'advanced-alpha' if args.advanced_alpha else 'comparison'}")
        print(f"CSV: {Path(args.csv)} / candles: {len(rows)}")
        for name, result in report.items():
            if name == "walk_forward":
                print(
                    f"walk_forward: windows={result['window_count']} "
                    f"purge={result['purge_candles']} "
                    f"OOS_trades={result['out_of_sample_trades']} "
                    f"OOS_net={result['out_of_sample_net_pnl']:.2f} "
                    f"OOS_pass={result['oos_pass_rate']:.1%} "
                    f"selection_concentration={result['selection_concentration']:.1%}"
                )
                continue
            if name == "exit_policy_comparison":
                print("# Exit Policy Comparison")
                for policy_name, policy_result in result["results"].items():
                    pf = policy_result["profit_factor"]
                    pf_text = f"{pf:.2f}" if pf is not None else "n/a"
                    print(
                        f"{policy_name}: trades={policy_result['trades']} "
                        f"avgR={(policy_result.get('expectancy_r') or 0.0):.2f}R "
                        f"PF={pf_text} MDD={policy_result['max_drawdown_pct']:.2f}% "
                        f"timeStop={policy_result.get('time_stop_count', 0)} "
                        f"invalid={policy_result.get('signal_invalid_exit_count', 0)} "
                        f"EOD={policy_result.get('END_OF_DATA_trades', 0)}"
                    )
                selected = result["ranked"][0]["name"] if result["ranked"] else "NEED_MORE_DATA"
                print(f"Selected Exit Policy: {selected}")
                continue
            if name == "strategy_candidate_comparison":
                print("# Strategy Candidate Comparison")
                for candidate_name, candidate_result in result["results"].items():
                    pf = candidate_result["profit_factor"]
                    pf_text = f"{pf:.2f}" if pf is not None else "n/a"
                    print(
                        f"{candidate_name}: trades={candidate_result['trades']} "
                        f"avgR={(candidate_result.get('expectancy_r') or 0.0):.2f}R "
                        f"PF={pf_text} MDD={candidate_result['max_drawdown_pct']:.2f}% "
                        f"metaReject={candidate_result.get('meta_gate_reject_count', 0)} "
                        f"regimeBlock={candidate_result.get('regime_block_count', 0)} "
                        f"macroBlock={candidate_result.get('macro_block_count', 0)}"
                    )
                selected = result["ranked"][0]["name"] if result["ranked"] else "NEED_MORE_DATA"
                print(f"Selected Candidate: {selected}")
                continue
            pf = result["profit_factor"]
            exp = result["expectancy"]
            pf_text = f"{pf:.2f}" if pf is not None else "n/a"
            exp_text = f"{exp:.4f}" if exp is not None else "n/a"
            win_rate = result["win_rate"]
            win_text = f"{win_rate:.1f}%" if win_rate is not None else "n/a"
            avg_r = result["average_R"]
            avg_r_text = f"{avg_r:.2f}R" if avg_r is not None else "n/a"
            print(
                f"{name}: trades={result['trades']} net={result['net_pnl']:.2f} "
                f"PF={pf_text} WR={win_text} avgR={avg_r_text} "
                f"EXP={exp_text} "
                f"MDD={result['max_drawdown_pct']:.2f}% "
                f"fee={result['fee_burden_pct'] if result['fee_burden_pct'] is not None else 0:.3f}% "
                f"policy={result.get('exit_policy')} decision={result.get('decision')}"
            )
            if args.advanced_alpha or args.adaptive_ladder_tp:
                print(
                    f"  engines={','.join(sorted(result.get('engine_performance', {}).keys())) or 'none'} "
                    f"long={result.get('long_trades', 0)} short={result.get('short_trades', 0)} "
                    f"macro={result.get('macro_block_count', 0)} derivVeto={result.get('derivatives_veto_count', 0)} "
                    f"metaReject={result.get('meta_gate_reject_count', 0)} "
                    f"TP1={result.get('ladder_tp1_count', 0)} TP2={result.get('ladder_tp2_count', 0)} "
                    f"TP3={result.get('ladder_tp3_count', 0)} SL={result.get('stop_loss_count', 0)}"
                )


if __name__ == "__main__":
    main()
