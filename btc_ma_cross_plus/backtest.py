"""Local-only backtest for /macross: compares all 1..5 feature combinations.

Not deployed to Azure (the runner lives in ``scripts/macross_backtest.py``).
Pure computation over candle rows so it runs offline on downloaded klines.

Assumptions are explicit:
  * entry fills at the signal candle close plus a slippage buffer,
  * stop / trail exits fill at the stop price minus slippage,
  * fees are charged on entry and exit notional,
  * funding is charged per 8h window the position stays open.
"""

from __future__ import annotations

from itertools import combinations

from .config import FEATURES, FEATURE_FIELDS
from .strategy import evaluate, indicator_frame, trailing_stop


def all_feature_modes(min_features=1, max_features=len(FEATURES)):
    """Every feature combination from 1 to 5 features, in a stable order."""
    modes = []
    for size in range(min_features, max_features + 1):
        for combo in combinations(FEATURES, size):
            modes.append("".join(combo))
    return modes


def cfg_for_mode(mode, base_cfg):
    """``base_cfg`` with exactly the features in ``mode`` switched on."""
    cfg = dict(base_cfg)
    for name, field in FEATURE_FIELDS.items():
        cfg[field] = 1 if name in mode else 0
    # Feature D replaces the fixed profit lock with the chandelier trail.
    cfg["locking"] = "D" not in mode
    return cfg


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run_backtest(rows, cfg, *, initial_equity=10_000.0, fee_rate=None, slippage=None,
                 funding_rate_per_8h=0.0, bar_ms=None):
    """Single-pass backtest of one feature combination (summary + trade log)."""
    fee_rate = _f(fee_rate if fee_rate is not None else cfg.get("estimated_taker_fee_rate"), 0.0005)
    slippage = _f(slippage if slippage is not None else cfg.get("slippage_buffer_pct"), 0.0005)
    bar_ms = int(bar_ms or 4 * 3_600_000)
    leverage = int(cfg["leverage"])
    margin_fraction = _f(cfg["margin_fraction"], 0.5)
    equity = float(initial_equity)
    peak = equity
    max_dd = 0.0
    trades = []
    frame = indicator_frame(rows, cfg)
    position = None
    state = {"equity": equity}

    def _close(index, price, reason):
        if position is None:
            return
        side = position["side"]
        sign = 1.0 if side == "LONG" else -1.0
        exit_price = price if reason == "END" else price * (1 - sign * slippage)
        gross = (exit_price - position["entry"]) * position["qty"] * sign
        exit_fee = abs(exit_price * position["qty"]) * fee_rate
        held_ms = max(0, int(rows[index][0]) - position["entry_ms"])
        funding = sign * position["entry"] * position["qty"] * funding_rate_per_8h * (held_ms / (8 * 3_600_000))
        net = gross - position["entry_fee"] - exit_fee - funding
        state["equity"] += net
        position.update(net=net, exit=exit_price, exit_ms=int(rows[index][0]), reason=reason,
                        equity_running=state["equity"])
        trades.append(position)
        return position

    for index in range(1, len(rows)):
        row = rows[index]
        high, low, close = row[2], row[3], row[4]
        atr_value = frame["atr"][index] if index < len(frame["atr"]) else None
        if position is not None:
            sign = 1.0 if position["side"] == "LONG" else -1.0
            if high > position["max_price"]:
                position["max_price"] = high
            if low < position["min_price"]:
                position["min_price"] = low
            position["mfe"] = max(position["mfe"], (high - position["entry"]) * sign)
            position["mae"] = min(position["mae"], (low - position["entry"]) * sign)
            stop = position.get("stop")
            if stop is not None:
                hit = low <= stop if position["side"] == "LONG" else high >= stop
                if hit:
                    _close(index, stop, position["stop_reason"])
                    position = None
                    continue
            if cfg.get("feature_d_chandelier") and atr_value:
                new_stop = trailing_stop(position["side"], position, close, atr_value, cfg)
                if new_stop is not None:
                    position["stop"] = new_stop
                    position["stop_reason"] = "TRAIL"
            decision = evaluate(rows, cfg, index, frame)
            if decision["side"] and decision["side"] != position["side"]:
                _close(index, close, "REVERSE")
                position = None
                continue
            if cfg.get("locking", True):
                entry = position["entry"]
                roi = (close / entry - 1) * leverage * 100 * sign
                best_roi = (position["mfe"] / entry) * leverage * 100
                start = _f(cfg["lock_start_roi_percent"], 5.0)
                step = _f(cfg["lock_step_percent"], 5.0)
                gap = _f(cfg["lock_gap_percent"], 1.0)
                if best_roi >= start:
                    reached = start + int((best_roi - start) // step) * step
                    locked = reached - gap
                    if roi <= locked:
                        lock_price = entry * (1 + sign * locked / (leverage * 100))
                        _close(index, lock_price, "PROFIT_LOCK")
                        position = None
                        continue
            continue

        decision = evaluate(rows, cfg, index, frame)
        if not decision["side"]:
            continue
        side = decision["side"]
        sign = 1.0 if side == "LONG" else -1.0
        scale = _f(decision.get("size_multiplier"), 1.0)
        notional = state["equity"] * margin_fraction * scale * leverage
        if notional <= 0 or close <= 0:
            continue
        qty = notional / close
        entry_price = close * (1 + sign * slippage)
        fee = abs(entry_price * qty) * fee_rate
        state["equity"] -= fee
        position = {
            "side": side, "entry": entry_price, "qty": qty, "entry_ms": int(row[0]),
            "entry_fee": fee, "features": decision["features"], "trigger": decision["trigger"],
            "stop": decision.get("initial_stop"), "stop_reason": "ATR_STOP",
            "mfe": 0.0, "mae": 0.0, "max_price": high, "min_price": low,
        }

    if position is not None:
        _close(len(rows) - 1, rows[-1][4], "END")

    final_equity = state["equity"]
    max_dd = 0.0
    for trade in trades:
        peak = max(peak, trade["equity_running"])
        if peak > 0:
            max_dd = max(max_dd, (peak - trade["equity_running"]) / peak)
    return _summarize(trades, cfg, initial_equity, final_equity, max_dd, bar_ms)


def _summarize(trades, cfg, initial_equity, final_equity, max_dd, bar_ms):
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = abs(sum(t["net"] for t in losses))
    returns = [t["net"] / initial_equity for t in trades]
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = (sum((r - mean) ** 2 for r in returns) / len(returns)) if len(returns) > 1 else 0.0
    std = variance ** 0.5
    reasons = {}
    for trade in trades:
        reasons[trade["reason"]] = reasons.get(trade["reason"], 0) + 1
    return {
        "mode": "".join(name for name, field in FEATURE_FIELDS.items() if cfg.get(field)),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "net_pnl": sum(t["net"] for t in trades),
        "return_pct": (final_equity / initial_equity - 1) * 100,
        "profit_factor": (gross_win / gross_loss) if gross_loss else (float("inf") if gross_win else 0.0),
        "max_drawdown_pct": abs(max_dd) * 100,
        "avg_trade": (sum(t["net"] for t in trades) / len(trades)) if trades else 0.0,
        "sharpe_per_trade": (mean / std) if std > 0 else 0.0,
        "final_equity": final_equity,
        "exit_reasons": reasons,
        "trade_log": trades,
    }


def compare_modes(rows, base_cfg, **kwargs):
    """Backtest every feature combination; returns summaries sorted by return."""
    results = []
    for mode in all_feature_modes():
        cfg = cfg_for_mode(mode, base_cfg)
        summary = run_backtest(rows, cfg, **kwargs)
        summary["mode"] = mode
        results.append(summary)
    results.sort(key=lambda item: item["return_pct"], reverse=True)
    return results
