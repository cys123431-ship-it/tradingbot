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
    initial_balance=1000.0,
    risk_per_trade_percent=0.5,
    max_risk_per_trade_usdt=10.0,
    leverage=5.0,
    stop_atr_multiplier=1.5,
    rr_multiple=2.0,
    partial_take_profit_r_multiple=1.0,
    partial_take_profit_ratio=0.5,
    breakeven_after_partial=True,
    trailing_atr_multiplier=0.0,
    fee_bps=4.0,
    slippage_bps=1.0,
    funding_bps_per_bar=0.0,
):
    atr, trail, signal, _ = utbot_rows(rows, key_value=key_value, atr_period=atr_period)
    balance = float(initial_balance)
    equity_peak = balance
    max_drawdown = 0.0
    trades = []
    position = None

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
                if breakeven_after_partial:
                    position["stop_loss"] = position["entry_price"]
                target_hit = row["high"] >= position["take_profit"] if side == "long" else row["low"] <= position["take_profit"]
                if target_hit and position["qty"] > 0:
                    exit_reason = "TP2"
                    exit_price = position["take_profit"]
            elif target_hit:
                exit_reason = "TP"
                exit_price = position["take_profit"]
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
        entry = _apply_slippage(row["close"], side, slippage_bps, is_entry=True)
        try:
            plan = calculate_risk_plan(
                side=side,
                entry_price=entry,
                atr_value=atr[idx],
                stop_atr_multiplier=stop_atr_multiplier,
                ut_stop=trail[idx],
                take_profit_r_multiple=rr_multiple,
                min_risk_reward=rr_multiple,
                balance_usdt=balance,
                risk_per_trade_percent=risk_per_trade_percent,
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
                entry + (plan["risk_distance"] * partial_take_profit_r_multiple)
                if side == "long" else
                entry - (plan["risk_distance"] * partial_take_profit_r_multiple)
            ) if partial_take_profit_r_multiple > 0 and partial_take_profit_ratio > 0 else None,
            "partial_take_profit_ratio": max(0.0, min(1.0, partial_take_profit_ratio)),
            "partial_filled": False,
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
    return {
        "trades": len(trades),
        "wins": sum(1 for t in trades if t["pnl"] > 0),
        "losses": sum(1 for t in trades if t["pnl"] <= 0),
        "win_rate": (sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100.0) if trades else None,
        "net_pnl": net_pnl,
        "total_return_pct": net_pnl / max(initial_balance, 1e-9) * 100.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "expectancy": net_pnl / len(trades) if trades else None,
        "average_R": sum(t.get("r_multiple", 0.0) for t in trades) / len(trades) if trades else None,
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
        "trades_detail": trades,
    }


def _simulate_variant(rows, params, args):
    return simulate_utbot_rr(
        rows,
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
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="OHLCV CSV path")
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--risk-pct", type=float, default=0.5)
    parser.add_argument("--max-risk-usdt", type=float, default=10.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--fee-bps", type=float, default=4.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--funding-bps-per-bar", type=float, default=0.0)
    parser.add_argument("--partial-tp-r", type=float, default=1.0)
    parser.add_argument("--partial-tp-ratio", type=float, default=0.5)
    parser.add_argument("--no-breakeven-after-partial", action="store_true")
    parser.add_argument("--trailing-atr-mult", type=float, default=0.0)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--wf-train-candles", type=int, default=600)
    parser.add_argument("--wf-test-candles", type=int, default=200)
    parser.add_argument("--wf-min-trades", type=int, default=5)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    rows = load_ohlcv_csv(args.csv)
    variants = {
        "UTBot_2_10": {"key_value": 2.0, "atr_period": 10},
        "UTBot_2_5_14": {"key_value": 2.5, "atr_period": 14},
    }
    report = {}
    for name, params in variants.items():
        result = _simulate_variant(rows, params, args)
        result.pop("trades_detail", None)
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
                    f"OOS_net={result['out_of_sample_net_pnl']:.2f}"
                )
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
                f"fee={result['fee_burden_pct'] if result['fee_burden_pct'] is not None else 0:.3f}%"
            )


if __name__ == "__main__":
    main()
