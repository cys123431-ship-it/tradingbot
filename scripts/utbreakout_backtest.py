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

from utbreakout.risk import calculate_risk_plan
from utbreakout.exit_policy import (
    DEFAULT_EXIT_POLICY,
    EXIT_POLICY_CANDIDATES,
    build_exit_policy,
    evaluate_signal_invalid_exit,
    evaluate_time_stop,
    rank_exit_policies,
)
from utbreakout.meta import meta_label_gate
from utbreakout.regime import classify_regime, regime_action
from utbreakout.sizing import calculate_adaptive_risk_pct


def _float(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def load_ohlcv_csv(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            rows.append({
                "timestamp": row.get("timestamp") or row.get("time") or "",
                "open": _float(row.get("open")),
                "high": _float(row.get("high")),
                "low": _float(row.get("low")),
                "close": _float(row.get("close")),
                "volume": _float(row.get("volume")),
            })
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
    meta_label_gate_enabled=False,
    regime_router_enabled=False,
    signal_invalid_exit_enabled=True,
    time_stop_enabled=True,
    volatility_adaptive_tp_enabled=True,
):
    atr, trail, signal, bias = utbot_rows(rows, key_value=key_value, atr_period=atr_period)
    balance = float(initial_balance)
    equity_peak = balance
    max_drawdown = 0.0
    trades = []
    position = None
    counts = {
        "time_stop_count": 0,
        "signal_invalid_exit_count": 0,
        "meta_gate_reject_count": 0,
        "derivatives_veto_count": 0,
        "regime_block_count": 0,
        "size_reduction_count": 0,
        "end_of_data_count": 0,
    }

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

            if funding_bps_per_bar:
                notional = position["entry_price"] * position["qty"]
                funding = notional * funding_bps_per_bar / 10000.0
                position["funding_pnl"] -= funding if side == "long" else -funding
                position["funding_abs"] += abs(funding)

            if position.get("partial_filled") and trailing_atr_multiplier > 0 and atr[idx] is not None:
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
            elif target_hit:
                exit_reason = "TP"
                exit_price = position["take_profit"]
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
            continue

        if signal[idx] not in {"long", "short"} or atr[idx] is None:
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
        entry = _apply_slippage(row["close"], side, slippage_bps, is_entry=True)
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
        position = {
            "entry_idx": idx,
            "side": side,
            "entry_price": entry,
            "qty": plan["qty"],
            "initial_qty": plan["qty"],
            "stop_loss": plan["stop_loss"],
            "take_profit": plan["take_profit"],
            "partial_take_profit": (
                entry + (plan["risk_distance"] * policy.tp1_r)
                if side == "long" else
                entry - (plan["risk_distance"] * policy.tp1_r)
            ) if policy.tp1_r > 0 and policy.tp1_size_pct > 0 else None,
            "partial_take_profit_ratio": max(0.0, min(1.0, policy.tp1_size_pct / 100.0)),
            "partial_filled": False,
            "tp1_filled": False,
            "entry_bar_index": idx,
            "timeframe": timeframe,
            "exit_policy_name": policy.name,
            "time_stop_enabled": bool(policy.time_stop_enabled and time_stop_enabled),
            "signal_invalid_exit_enabled": bool(policy.signal_invalid_exit_enabled and signal_invalid_exit_enabled),
            "volatility_adaptive_tp_enabled": bool(policy.volatility_adaptive_tp_enabled and volatility_adaptive_tp_enabled),
            "breakeven_buffer_r": (fee_bps * 2.0 + slippage_bps * 2.0) / 10000.0,
            "risk_pct": effective_risk_pct,
            "regime": context.get("regime"),
            "meta_probability": gate.probability,
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
        "trades_per_month": len(trades) / months if months and months > 0 else None,
        "long_performance": _performance_summary(long_trades),
        "short_performance": _performance_summary(short_trades),
        "max_drawdown_usdt": max_drawdown,
        "max_drawdown_pct": max_drawdown / max(initial_balance, 1e-9) * 100.0,
        "ending_balance": balance,
        "time_stop_count": time_stop_trades or counts["time_stop_count"],
        "signal_invalid_exit_count": signal_invalid_trades or counts["signal_invalid_exit_count"],
        "meta_gate_reject_count": counts["meta_gate_reject_count"],
        "derivatives_veto_count": counts["derivatives_veto_count"],
        "regime_block_count": counts["regime_block_count"],
        "size_reduction_count": counts["size_reduction_count"],
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
        partial_take_profit_r_multiple=args.partial_tp_r,
        partial_take_profit_ratio=args.partial_tp_ratio,
        breakeven_after_partial=not args.no_breakeven_after_partial,
        trailing_atr_multiplier=args.trailing_atr_mult,
        meta_label_gate_enabled=getattr(args, "meta_label_gate", False),
        regime_router_enabled=getattr(args, "regime_router", False),
        **params,
    )


def walk_forward_report(rows, variants, args):
    train_size = max(50, int(args.wf_train_candles))
    test_size = max(20, int(args.wf_test_candles))
    min_trades = max(1, int(args.wf_min_trades))
    windows = []
    start = 0
    while start + train_size + test_size <= len(rows):
        train_rows = rows[start:start + train_size]
        test_rows = rows[start + train_size:start + train_size + test_size]
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
        else:
            selected_name = None
            test_result = {"trades": 0, "net_pnl": 0.0, "reason": "no_train_candidate"}
        windows.append({
            "start": start,
            "train_end": start + train_size - 1,
            "test_end": start + train_size + test_size - 1,
            "selected": selected_name,
            "train_results": train_results,
            "test_result": test_result,
        })
        start += test_size
    return {
        "windows": windows,
        "window_count": len(windows),
        "out_of_sample_net_pnl": sum(w.get("test_result", {}).get("net_pnl", 0.0) for w in windows),
        "out_of_sample_trades": sum(w.get("test_result", {}).get("trades", 0) for w in windows),
        "oos_pass_rate": (
            sum(1 for w in windows if (w.get("test_result", {}).get("average_R") or 0.0) > 0 and (w.get("test_result", {}).get("profit_factor") or 0.0) >= 1.05)
            / len(windows)
            if windows else 0.0
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
    return candidate_args


def compare_strategy_candidates(rows, variants, args):
    candidates = [
        "BASE_HYBRID_DEFENSIVE",
        "META_FILTERED_BREAKOUT",
        "LOW_CROWDING_BREAKOUT",
        "REGIME_ROUTED_BREAKOUT",
        "DEFENSIVE_BTC_ETH_ONLY",
    ]
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
        if trades >= args.wf_min_trades and expectancy > 0 and pf >= 1.05:
            score = 0.35 * expectancy + 0.25 * min(pf, 3.0) - 0.20 * dd / 20.0
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
    parser.add_argument("--strategy", default="baseline", choices=["baseline", "live-parity", "BASE_HYBRID_DEFENSIVE", "META_FILTERED_BREAKOUT", "LOW_CROWDING_BREAKOUT", "REGIME_ROUTED_BREAKOUT", "DEFENSIVE_BTC_ETH_ONLY"])
    parser.add_argument("--exit-policy", default=DEFAULT_EXIT_POLICY)
    parser.add_argument("--compare-exits", action="store_true")
    parser.add_argument("--compare-candidates", action="store_true")
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
    parser.add_argument("--partial-tp-r", type=float, default=1.5)
    parser.add_argument("--partial-tp-ratio", type=float, default=0.5)
    parser.add_argument("--no-breakeven-after-partial", action="store_true")
    parser.add_argument("--trailing-atr-mult", type=float, default=0.0)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--train-months", type=int, default=None)
    parser.add_argument("--test-months", type=int, default=None)
    parser.add_argument("--wf-train-candles", type=int, default=600)
    parser.add_argument("--wf-test-candles", type=int, default=200)
    parser.add_argument("--wf-min-trades", type=int, default=5)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.train_months:
        args.wf_train_candles = max(args.wf_train_candles, int(args.train_months) * 30)
    if args.test_months:
        args.wf_test_candles = max(args.wf_test_candles, int(args.test_months) * 30)

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
        print(f"CSV: {Path(args.csv)} / candles: {len(rows)}")
        for name, result in report.items():
            if name == "walk_forward":
                print(
                    f"walk_forward: windows={result['window_count']} "
                    f"OOS_trades={result['out_of_sample_trades']} "
                    f"OOS_net={result['out_of_sample_net_pnl']:.2f} "
                    f"OOS_pass={result['oos_pass_rate']:.1%}"
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
                        f"regimeBlock={candidate_result.get('regime_block_count', 0)}"
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


if __name__ == "__main__":
    main()
