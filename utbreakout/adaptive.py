"""Adaptive research helpers for UT Breakout.

The helpers in this module are deliberately pure and dependency-free.  Live
order execution stays in ``emas.py``; this file only evaluates historical
barrier outcomes and produces bounded risk/exit overlays.
"""

from collections import Counter
from math import isfinite


def finite_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def clamp(value, low, high):
    parsed = finite_float(value, low)
    return max(float(low), min(float(high), float(parsed)))


def _bar_value(row, key, index=None):
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, "get"):
        return row.get(key)
    if isinstance(row, (list, tuple)) and index is not None and len(row) > index:
        return row[index]
    return None


def iter_market_bars(bars):
    if bars is None:
        return []
    if hasattr(bars, "iterrows"):
        return [row for _, row in bars.iterrows()]
    return list(bars or [])


def evaluate_shadow_triple_barrier(
    *,
    side,
    entry_price,
    stop_loss=None,
    take_profit=None,
    risk_distance=None,
    take_profit_r_multiple=2.0,
    decision_ts=None,
    bars=None,
    max_bars=24,
    same_bar_policy="stop_first",
):
    """Evaluate a UTBreak candidate against TP/SL/time barriers.

    Returns ``None`` while the observation window is still incomplete.
    """
    side = str(side or "").lower()
    if side not in {"long", "short"}:
        return None
    entry = finite_float(entry_price)
    if entry is None or entry <= 0:
        return None
    risk = finite_float(risk_distance)
    stop = finite_float(stop_loss)
    target = finite_float(take_profit)
    if risk is None or risk <= 0:
        if stop is not None:
            risk = abs(entry - stop)
    if risk is None or risk <= 0:
        return None
    if stop is None:
        stop = entry - risk if side == "long" else entry + risk
    if target is None:
        rr = max(0.1, finite_float(take_profit_r_multiple, 2.0))
        target = entry + risk * rr if side == "long" else entry - risk * rr

    max_bars = max(1, int(max_bars or 1))
    decision_ts = int(decision_ts or 0)
    observed = []
    for row in iter_market_bars(bars):
        ts = int(finite_float(_bar_value(row, "timestamp", 0), 0) or 0)
        if decision_ts and ts <= decision_ts:
            continue
        high = finite_float(_bar_value(row, "high", 2))
        low = finite_float(_bar_value(row, "low", 3))
        close = finite_float(_bar_value(row, "close", 4))
        if high is None or low is None or close is None:
            continue
        observed.append({"timestamp": ts, "high": high, "low": low, "close": close})
        if len(observed) >= max_bars:
            break

    if not observed:
        return None

    mfe_r = 0.0
    mae_r = 0.0
    for index, bar in enumerate(observed, 1):
        high = bar["high"]
        low = bar["low"]
        close = bar["close"]
        if side == "long":
            mfe_r = max(mfe_r, (high - entry) / risk)
            mae_r = max(mae_r, (entry - low) / risk)
            hit_tp = high >= target
            hit_sl = low <= stop
        else:
            mfe_r = max(mfe_r, (entry - low) / risk)
            mae_r = max(mae_r, (high - entry) / risk)
            hit_tp = low <= target
            hit_sl = high >= stop
        if hit_tp or hit_sl:
            if hit_tp and hit_sl and same_bar_policy != "target_first":
                outcome = "sl"
                exit_price = stop
            elif hit_tp:
                outcome = "tp"
                exit_price = target
            else:
                outcome = "sl"
                exit_price = stop
            pnl_r = (exit_price - entry) / risk if side == "long" else (entry - exit_price) / risk
            return {
                "outcome": outcome,
                "code": f"SHADOW_{outcome.upper()}",
                "exit_price": exit_price,
                "pnl_r": pnl_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "bars_elapsed": index,
                "observation_window_bars": max_bars,
                "decision_ts": decision_ts or None,
                "exit_ts": bar["timestamp"] or None,
            }

    if len(observed) < max_bars:
        return None

    last = observed[-1]
    exit_price = last["close"]
    pnl_r = (exit_price - entry) / risk if side == "long" else (entry - exit_price) / risk
    return {
        "outcome": "timeout",
        "code": "SHADOW_TIMEOUT",
        "exit_price": exit_price,
        "pnl_r": pnl_r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "bars_elapsed": len(observed),
        "observation_window_bars": max_bars,
        "decision_ts": decision_ts or None,
        "exit_ts": last["timestamp"] or None,
    }


def summarize_shadow_outcomes(events, *, symbol=None, side=None, set_id=None):
    rows = []
    wanted_symbol = str(symbol or "").upper()
    wanted_side = str(side or "").lower()
    wanted_set = str(set_id or "")
    for event in events or []:
        code = str(event.get("code") or "")
        if event.get("event") != "shadow_outcome" and (not code.startswith("SHADOW_") or code.startswith("SHADOW_RUNNER_")):
            continue
        if wanted_symbol and str(event.get("symbol") or "").upper() != wanted_symbol:
            continue
        if wanted_side and str(event.get("side") or "").lower() != wanted_side:
            continue
        if wanted_set and str(event.get("auto_selected_set_id") or "") != wanted_set:
            continue
        rows.append(event)

    counts = Counter(str(row.get("shadow_outcome") or row.get("outcome") or "").lower() for row in rows)

    def _avg(key):
        values = [finite_float(row.get(key)) for row in rows]
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else None

    total = len(rows)
    tp = counts.get("tp", 0)
    sl = counts.get("sl", 0)
    timeout = counts.get("timeout", 0)
    return {
        "sample_count": total,
        "tp_count": tp,
        "sl_count": sl,
        "timeout_count": timeout,
        "tp_rate": tp / total if total else 0.0,
        "sl_rate": sl / total if total else 0.0,
        "timeout_rate": timeout / total if total else 0.0,
        "avg_pnl_r": _avg("pnl_r"),
        "avg_mfe_r": _avg("mfe_r"),
        "avg_mae_r": _avg("mae_r"),
        "avg_bars_elapsed": _avg("bars_elapsed"),
    }


def _true_ranges(rows):
    ranges = []
    prev_close = None
    for row in rows or []:
        high = finite_float(_bar_value(row, "high", 2))
        low = finite_float(_bar_value(row, "low", 3))
        close = finite_float(_bar_value(row, "close", 4))
        if high is None or low is None or close is None:
            ranges.append(None)
            continue
        if prev_close is None:
            ranges.append(high - low)
        else:
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return ranges


def _wilder_atr_values(rows, length=14):
    rows = list(rows or [])
    length = max(1, int(length or 14))
    trs = _true_ranges(rows)
    values = []
    atr = None
    seed = []
    for tr in trs:
        if tr is None:
            values.append(None)
            continue
        if atr is None:
            seed.append(tr)
            if len(seed) < length:
                values.append(None)
                continue
            atr = sum(seed[-length:]) / length
        else:
            atr = (atr * (length - 1) + tr) / length
        values.append(atr)
    return values


def _split_bars_after_decision(bars, decision_ts):
    prepared = []
    observed = []
    decision_ts = int(decision_ts or 0)
    for row in iter_market_bars(bars):
        ts = int(finite_float(_bar_value(row, "timestamp", 0), 0) or 0)
        high = finite_float(_bar_value(row, "high", 2))
        low = finite_float(_bar_value(row, "low", 3))
        close = finite_float(_bar_value(row, "close", 4))
        open_price = finite_float(_bar_value(row, "open", 1), close)
        if high is None or low is None or close is None:
            continue
        item = {
            "timestamp": ts,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
        }
        prepared.append(item)
        if decision_ts and ts <= decision_ts:
            continue
        observed.append(item)
    return prepared, observed


def build_trend_health_score(cfg, values, side):
    """Compress trend quality into one bounded score and risk multiplier."""
    cfg = dict(cfg or {})
    values = dict(values or {})
    side = str(side or "").lower()
    if not bool(cfg.get("trend_health_enabled", True)):
        return {
            "enabled": False,
            "score": 100.0,
            "state": True,
            "risk_multiplier": 1.0,
            "summary": "trend health OFF",
            "components": {},
        }

    def _score(value, low, high):
        value = finite_float(value)
        if value is None:
            return 50.0
        if high <= low:
            return 50.0
        return clamp((value - low) / (high - low) * 100.0, 0.0, 100.0)

    efficiency = finite_float(values.get("directional_efficiency"))
    efficiency_score = _score(efficiency, 0.12, 0.45)

    htf_score = 55.0
    htf_fast = finite_float(values.get("htf_ema_fast"))
    htf_slow = finite_float(values.get("htf_ema_slow"))
    htf_close = finite_float(values.get("htf_close"))
    htf_supertrend = str(values.get("htf_supertrend_direction") or "").lower()
    if side in {"long", "short"} and htf_fast is not None and htf_slow is not None:
        ema_aligned = htf_fast >= htf_slow if side == "long" else htf_fast <= htf_slow
        close_aligned = True
        if htf_close is not None:
            close_aligned = htf_close >= htf_slow if side == "long" else htf_close <= htf_slow
        htf_score = 100.0 if ema_aligned and close_aligned else 35.0
    if htf_supertrend in {"long", "short"}:
        htf_score = (htf_score * 0.65) + (100.0 if htf_supertrend == side else 20.0) * 0.35

    vol_ratio = finite_float(values.get("volatility_expansion_ratio"))
    if vol_ratio is None:
        vol_ratio = finite_float(values.get("range_expansion_ratio"))
    volatility_score = _score(vol_ratio, 0.75, 1.30)

    entry = finite_float(values.get("entry_price"))
    atr = finite_float(values.get("atr"))
    don_high = finite_float(values.get("donchian_high_prev"))
    don_low = finite_float(values.get("donchian_low_prev"))
    keltner_upper = finite_float(values.get("keltner_upper"))
    keltner_lower = finite_float(values.get("keltner_lower"))
    breakout_quality = 45.0
    if entry is not None and atr is not None and atr > 0 and side in {"long", "short"}:
        quality_parts = []
        if side == "long":
            if don_high is not None:
                quality_parts.append(_score((entry - don_high) / atr, -0.20, 0.50))
            if keltner_upper is not None:
                quality_parts.append(_score((entry - keltner_upper) / atr, -0.20, 0.50))
        else:
            if don_low is not None:
                quality_parts.append(_score((don_low - entry) / atr, -0.20, 0.50))
            if keltner_lower is not None:
                quality_parts.append(_score((keltner_lower - entry) / atr, -0.20, 0.50))
        if quality_parts:
            breakout_quality = sum(quality_parts) / len(quality_parts)

    score = (
        efficiency_score * 0.35
        + htf_score * 0.25
        + volatility_score * 0.25
        + breakout_quality * 0.15
    )
    hard_block_below = finite_float(cfg.get("trend_health_hard_block_below"), 40.0)
    reduce_below = finite_float(cfg.get("trend_health_reduce_below"), 55.0)
    full_score = max(reduce_below, finite_float(cfg.get("trend_health_full_score"), 75.0))
    min_multiplier = clamp(cfg.get("trend_health_min_multiplier", 0.35), 0.05, 1.0)

    if score < hard_block_below:
        state = False
        multiplier = 0.0
    elif score < reduce_below:
        state = "reduced"
        multiplier = min_multiplier
    elif score < full_score:
        state = "reduced"
        scale = (score - reduce_below) / max(full_score - reduce_below, 1e-9)
        multiplier = min_multiplier + (1.0 - min_multiplier) * clamp(scale, 0.0, 1.0)
    else:
        state = True
        multiplier = 1.0

    components = {
        "directional_efficiency": round(efficiency_score, 2),
        "htf_alignment": round(htf_score, 2),
        "volatility_expansion": round(volatility_score, 2),
        "breakout_quality": round(breakout_quality, 2),
    }
    state_label = "BLOCK" if state is False else "REDUCE" if state == "reduced" else "PASS"
    return {
        "enabled": True,
        "score": round(score, 2),
        "state": state,
        "risk_multiplier": round(multiplier, 4),
        "components": components,
        "summary": (
            f"trend health {state_label} {score:.1f}/100 x{multiplier:.2f} "
            f"(eff {components['directional_efficiency']:.0f}, htf {components['htf_alignment']:.0f}, "
            f"vol {components['volatility_expansion']:.0f}, brk {components['breakout_quality']:.0f})"
        ),
    }


def build_dynamic_chandelier_stop(
    *,
    side,
    current_stop,
    entry_price,
    current_close,
    atr_value,
    highest_high=None,
    lowest_low=None,
    recent_swing_low=None,
    recent_swing_high=None,
    risk_distance=None,
    trend_health=None,
    cfg=None,
):
    """Return a monotonic runner stop based on Chandelier + recent structure."""
    cfg = dict(cfg or {})
    side = str(side or "").lower()
    if side not in {"long", "short"}:
        return None
    entry = finite_float(entry_price)
    close = finite_float(current_close)
    atr = finite_float(atr_value)
    if entry is None or close is None or atr is None or entry <= 0 or close <= 0 or atr <= 0:
        return None

    base_mult = finite_float(cfg.get("runner_chandelier_multiplier"), cfg.get("atr_trailing_multiplier", 2.0))
    min_mult = finite_float(cfg.get("runner_chandelier_multiplier_min"), 1.4)
    max_mult = finite_float(cfg.get("runner_chandelier_multiplier_max"), 3.2)
    health_score = finite_float((trend_health or {}).get("score"))
    if bool(cfg.get("runner_dynamic_multiplier_enabled", True)) and health_score is not None:
        if health_score >= 75.0:
            base_mult += 0.35
        elif health_score < 55.0:
            base_mult -= 0.25
    risk = finite_float(risk_distance)
    if risk and risk > 0:
        if side == "long":
            mfe_r = (finite_float(highest_high, close) - entry) / risk
        else:
            mfe_r = (entry - finite_float(lowest_low, close)) / risk
        tighten_r = finite_float(cfg.get("runner_mfe_tighten_r"), 3.0)
        tighten_delta = finite_float(cfg.get("runner_mfe_tighten_delta"), 0.20)
        if mfe_r >= tighten_r:
            base_mult -= tighten_delta
    mult = clamp(base_mult, min_mult, max_mult)
    buffer_atr = max(0.0, finite_float(cfg.get("runner_structure_buffer_atr"), 0.20))
    current_stop = finite_float(current_stop, 0.0)

    if side == "long":
        high_anchor = finite_float(highest_high, close)
        chandelier = high_anchor - atr * mult
        candidates = [chandelier]
        swing_low = finite_float(recent_swing_low)
        if swing_low is not None:
            candidates.append(swing_low - atr * buffer_atr)
        if bool(cfg.get("atr_trailing_breakeven_enabled", True)):
            candidates.append(entry)
        raw_stop = max(candidates)
        if raw_stop >= close:
            raw_stop = close - max(atr * 0.15, close * 0.0005)
        new_stop = max(current_stop, raw_stop) if current_stop > 0 else raw_stop
        if new_stop <= 0 or new_stop >= close:
            return None
    else:
        low_anchor = finite_float(lowest_low, close)
        chandelier = low_anchor + atr * mult
        candidates = [chandelier]
        swing_high = finite_float(recent_swing_high)
        if swing_high is not None:
            candidates.append(swing_high + atr * buffer_atr)
        if bool(cfg.get("atr_trailing_breakeven_enabled", True)):
            candidates.append(entry)
        raw_stop = min(candidates)
        if raw_stop <= close:
            raw_stop = close + max(atr * 0.15, close * 0.0005)
        new_stop = min(current_stop, raw_stop) if current_stop > 0 else raw_stop
        if new_stop <= close:
            return None

    return {
        "stop_price": round(float(new_stop), 10),
        "multiplier": round(float(mult), 4),
        "chandelier_stop": round(float(chandelier), 10),
        "structure_stop": (
            round(float(candidates[1]), 10)
            if len(candidates) > 1 else None
        ),
        "mode": "dynamic_chandelier",
    }


def evaluate_shadow_runner_exit(
    *,
    side,
    entry_price,
    stop_loss=None,
    risk_distance=None,
    decision_ts=None,
    bars=None,
    cfg=None,
    max_bars=None,
):
    """Simulate partial profit plus a Chandelier runner stop."""
    cfg = dict(cfg or {})
    side = str(side or "").lower()
    if side not in {"long", "short"}:
        return None
    entry = finite_float(entry_price)
    stop = finite_float(stop_loss)
    risk = finite_float(risk_distance)
    if entry is None or entry <= 0:
        return None
    if risk is None or risk <= 0:
        if stop is not None:
            risk = abs(entry - stop)
    if risk is None or risk <= 0:
        return None
    if stop is None:
        stop = entry - risk if side == "long" else entry + risk

    max_bars = max(1, int(max_bars or cfg.get("shadow_runner_max_bars", 48) or 48))
    all_bars, observed = _split_bars_after_decision(bars, decision_ts)
    if not observed:
        return None
    observed = observed[:max_bars]
    if len(observed) < max_bars:
        return None

    atr_length = max(1, int(cfg.get("atr_length", 14) or 14))
    atr_values = _wilder_atr_values(all_bars, atr_length)
    atr_by_ts = {
        row["timestamp"]: atr
        for row, atr in zip(all_bars, atr_values)
        if atr is not None
    }
    partial_r = max(0.1, finite_float(cfg.get("partial_take_profit_r_multiple"), 1.2))
    partial_ratio = clamp(cfg.get("partial_take_profit_ratio", 0.35), 0.0, 0.95)
    activation_r = max(0.0, finite_float(cfg.get("atr_trailing_activation_r"), partial_r))
    lookback = max(2, int(cfg.get("runner_chandelier_lookback", 22) or 22))
    structure_lookback = max(2, int(cfg.get("runner_structure_lookback", 5) or 5))

    current_stop = stop
    partial_filled = False
    remaining_ratio = 1.0
    realized_pnl_r = 0.0
    highest = entry
    lowest = entry
    mfe_r = 0.0
    mae_r = 0.0
    trail_active = False
    post_rows = []

    for index, bar in enumerate(observed, 1):
        high = bar["high"]
        low = bar["low"]
        close = bar["close"]
        stop_at_bar_open = current_stop
        if side == "long":
            mfe_r = max(mfe_r, (high - entry) / risk)
            mae_r = max(mae_r, (entry - low) / risk)
            if low <= stop_at_bar_open:
                exit_r = (stop_at_bar_open - entry) / risk
                total_pnl = realized_pnl_r + remaining_ratio * exit_r
                return {
                    "outcome": "runner_stop" if partial_filled or trail_active else "initial_sl",
                    "code": "SHADOW_RUNNER_STOP" if partial_filled or trail_active else "SHADOW_RUNNER_INITIAL_SL",
                    "exit_price": stop_at_bar_open,
                    "pnl_r": total_pnl,
                    "mfe_r": mfe_r,
                    "mae_r": mae_r,
                    "mfe_capture_ratio": total_pnl / max(mfe_r, 1e-9) if mfe_r > 0 else 0.0,
                    "partial_filled": partial_filled,
                    "bars_elapsed": index,
                    "observation_window_bars": max_bars,
                    "decision_ts": int(decision_ts or 0) or None,
                    "exit_ts": bar["timestamp"] or None,
                }
            partial_price = entry + risk * partial_r
            if not partial_filled and high >= partial_price and partial_ratio > 0:
                partial_filled = True
                remaining_ratio = max(0.0, 1.0 - partial_ratio)
                realized_pnl_r += partial_ratio * partial_r
                trail_active = True
        else:
            mfe_r = max(mfe_r, (entry - low) / risk)
            mae_r = max(mae_r, (high - entry) / risk)
            if high >= stop_at_bar_open:
                exit_r = (entry - stop_at_bar_open) / risk
                total_pnl = realized_pnl_r + remaining_ratio * exit_r
                return {
                    "outcome": "runner_stop" if partial_filled or trail_active else "initial_sl",
                    "code": "SHADOW_RUNNER_STOP" if partial_filled or trail_active else "SHADOW_RUNNER_INITIAL_SL",
                    "exit_price": stop_at_bar_open,
                    "pnl_r": total_pnl,
                    "mfe_r": mfe_r,
                    "mae_r": mae_r,
                    "mfe_capture_ratio": total_pnl / max(mfe_r, 1e-9) if mfe_r > 0 else 0.0,
                    "partial_filled": partial_filled,
                    "bars_elapsed": index,
                    "observation_window_bars": max_bars,
                    "decision_ts": int(decision_ts or 0) or None,
                    "exit_ts": bar["timestamp"] or None,
                }
            partial_price = entry - risk * partial_r
            if not partial_filled and low <= partial_price and partial_ratio > 0:
                partial_filled = True
                remaining_ratio = max(0.0, 1.0 - partial_ratio)
                realized_pnl_r += partial_ratio * partial_r
                trail_active = True

        highest = max(highest, high)
        lowest = min(lowest, low)
        post_rows.append(bar)
        favorable_close_r = (close - entry) / risk if side == "long" else (entry - close) / risk
        trail_active = trail_active or favorable_close_r >= activation_r
        atr = atr_by_ts.get(bar["timestamp"])
        if trail_active and atr is not None and atr > 0:
            recent = post_rows[-max(lookback, structure_lookback):]
            recent_low = min(row["low"] for row in recent[-structure_lookback:])
            recent_high = max(row["high"] for row in recent[-structure_lookback:])
            stop_info = build_dynamic_chandelier_stop(
                side=side,
                current_stop=current_stop,
                entry_price=entry,
                current_close=close,
                atr_value=atr,
                highest_high=highest,
                lowest_low=lowest,
                recent_swing_low=recent_low,
                recent_swing_high=recent_high,
                risk_distance=risk,
                cfg=cfg,
            )
            if isinstance(stop_info, dict):
                current_stop = float(stop_info["stop_price"])

    last = observed[-1]
    exit_price = last["close"]
    exit_r = (exit_price - entry) / risk if side == "long" else (entry - exit_price) / risk
    total_pnl = realized_pnl_r + remaining_ratio * exit_r
    return {
        "outcome": "runner_timeout",
        "code": "SHADOW_RUNNER_TIMEOUT",
        "exit_price": exit_price,
        "pnl_r": total_pnl,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "mfe_capture_ratio": total_pnl / max(mfe_r, 1e-9) if mfe_r > 0 else 0.0,
        "partial_filled": partial_filled,
        "bars_elapsed": len(observed),
        "observation_window_bars": max_bars,
        "decision_ts": int(decision_ts or 0) or None,
        "exit_ts": last["timestamp"] or None,
    }


def summarize_runner_outcomes(events, *, symbol=None, side=None, set_id=None):
    rows = []
    wanted_symbol = str(symbol or "").upper()
    wanted_side = str(side or "").lower()
    wanted_set = str(set_id or "")
    for event in events or []:
        code = str(event.get("code") or "")
        if event.get("event") != "runner_shadow_outcome" and not code.startswith("SHADOW_RUNNER_") and event.get("event") != "runner_outcome":
            continue
        if wanted_symbol and str(event.get("symbol") or "").upper() != wanted_symbol:
            continue
        if wanted_side and str(event.get("side") or "").lower() != wanted_side:
            continue
        if wanted_set and str(event.get("auto_selected_set_id") or "") != wanted_set:
            continue
        rows.append(event)

    counts = Counter(str(row.get("runner_outcome") or row.get("outcome") or "").lower() for row in rows)

    def _avg(key):
        values = [finite_float(row.get(key)) for row in rows]
        values = [value for value in values if value is not None]
        return sum(values) / len(values) if values else None

    total = len(rows)
    return {
        "sample_count": total,
        "outcomes": counts.most_common(),
        "runner_stop_count": counts.get("runner_stop", 0),
        "initial_sl_count": counts.get("initial_sl", 0),
        "timeout_count": counts.get("runner_timeout", 0),
        "runner_stop_rate": counts.get("runner_stop", 0) / total if total else 0.0,
        "initial_sl_rate": counts.get("initial_sl", 0) / total if total else 0.0,
        "timeout_rate": counts.get("runner_timeout", 0) / total if total else 0.0,
        "avg_pnl_r": _avg("pnl_r"),
        "avg_mfe_r": _avg("mfe_r"),
        "avg_mae_r": _avg("mae_r"),
        "avg_mfe_capture_ratio": _avg("mfe_capture_ratio"),
        "avg_bars_elapsed": _avg("bars_elapsed"),
    }


def build_volatility_risk_multiplier(cfg, atr_pct):
    if not bool((cfg or {}).get("volatility_targeting_enabled", True)):
        return 1.0, "vol target OFF"
    atr = finite_float(atr_pct)
    if atr is None or atr <= 0:
        return 1.0, "vol target neutral: ATR% unavailable"
    target = max(0.01, finite_float((cfg or {}).get("volatility_target_atr_pct"), 1.0))
    min_mult = clamp((cfg or {}).get("volatility_target_min_multiplier", 0.25), 0.05, 1.0)
    multiplier = 1.0 if atr <= target else clamp(target / max(atr, 1e-9), min_mult, 1.0)
    return round(multiplier, 4), f"vol target x{multiplier:.2f} (ATR% {atr:.3f} / target {target:.3f})"


def build_meta_risk_multiplier(cfg, stats):
    if not bool((cfg or {}).get("meta_labeling_enabled", True)):
        return 1.0, "meta sizing OFF"
    stats = dict(stats or {})
    min_samples = max(1, int((cfg or {}).get("meta_labeling_min_samples", 12) or 12))
    sample_count = int(stats.get("sample_count") or 0)
    if sample_count < min_samples:
        return 1.0, f"meta sizing neutral: samples {sample_count}/{min_samples}"
    min_mult = clamp((cfg or {}).get("meta_labeling_min_multiplier", 0.5), 0.1, 1.0)
    tp_rate = finite_float(stats.get("tp_rate"), 0.0)
    avg_pnl = finite_float(stats.get("avg_pnl_r"), 0.0)
    timeout_rate = finite_float(stats.get("timeout_rate"), 0.0)
    multiplier = 1.0
    reason = f"meta sizing x1.00: WR {tp_rate:.0%}, avg {avg_pnl:.2f}R"
    if avg_pnl <= -0.25 or tp_rate <= 0.32:
        multiplier = max(min_mult, 0.50)
        reason = f"meta sizing x{multiplier:.2f}: weak shadow edge WR {tp_rate:.0%}, avg {avg_pnl:.2f}R"
    elif avg_pnl <= 0.0 or tp_rate <= 0.40 or (timeout_rate >= 0.40 and avg_pnl < 0.10):
        multiplier = max(min_mult, 0.75)
        reason = f"meta sizing x{multiplier:.2f}: mixed shadow edge WR {tp_rate:.0%}, avg {avg_pnl:.2f}R"
    return round(multiplier, 4), reason


def build_strategy_quality_score(cfg, values, side):
    """Score whether the current breakout looks like tradable trend continuation.

    The score is deliberately soft by default. It should reduce sizing and make
    exits more defensive before it blocks entries, so UTBreak does not become a
    stack of brittle filters.
    """
    cfg = dict(cfg or {})
    values = dict(values or {})
    side = str(side or "").lower()
    if not bool(cfg.get("strategy_quality_enabled", True)):
        return {
            "enabled": False,
            "score": 100.0,
            "state": True,
            "risk_multiplier": 1.0,
            "summary": "strategy quality OFF",
            "components": {},
            "reasons": ["OFF"],
        }
    if side not in {"long", "short"}:
        return {
            "enabled": True,
            "score": 0.0,
            "state": False,
            "risk_multiplier": 0.0,
            "summary": "strategy quality BLOCK invalid side",
            "components": {},
            "reasons": ["invalid side"],
        }

    direction = 1.0 if side == "long" else -1.0

    def _score(value, low, high):
        value = finite_float(value)
        if value is None or high <= low:
            return 50.0
        return clamp((value - low) / (high - low) * 100.0, 0.0, 100.0)

    momentum_values = [
        finite_float(values.get("momentum_6_pct")),
        finite_float(values.get("momentum_12_pct")),
        finite_float(values.get("momentum_24_pct")),
    ]
    clean_momentum = [value for value in momentum_values if value is not None]
    if clean_momentum:
        aligned = [value * direction for value in clean_momentum]
        aligned_share = sum(1 for value in aligned if value > 0) / len(aligned)
        magnitude = sum(abs(value) for value in clean_momentum) / len(clean_momentum)
        magnitude_score = _score(magnitude, 0.20, 2.20)
        momentum_score = aligned_share * 72.0 + magnitude_score * 0.28
        if aligned and aligned[-1] < 0:
            momentum_score -= 12.0
    else:
        momentum_score = 50.0

    slope = finite_float(values.get("trend_slope_pct"))
    slope_score = _score(slope * direction if slope is not None else None, 0.02, 1.10)

    efficiency = finite_float(values.get("directional_efficiency"))
    efficiency_score = _score(efficiency, 0.12, 0.48)
    hurst = finite_float(values.get("hurst_exponent"))
    hurst_score = _score(hurst, 0.48, 0.62)
    persistence_score = (efficiency_score * 0.60) + (hurst_score * 0.40)

    bb_width = finite_float(values.get("bb_width_pct"))
    bb_prev = finite_float(values.get("bb_width_prev_pct"))
    bb_min = finite_float(values.get("bb_width_min_pct"))
    k_width = finite_float(values.get("keltner_width_pct"))
    k_prev = finite_float(values.get("keltner_width_prev_pct"))
    range_expansion = finite_float(values.get("range_expansion_ratio"))
    squeeze_parts = []
    if bb_width is not None and bb_prev is not None:
        squeeze_parts.append(_score((bb_width / max(bb_prev, 1e-9)) - 1.0, 0.00, 0.25))
    if bb_width is not None and bb_min is not None:
        squeeze_parts.append(_score((bb_width / max(bb_min, 1e-9)) - 1.0, 0.02, 0.45))
    if k_width is not None and k_prev is not None:
        squeeze_parts.append(_score((k_width / max(k_prev, 1e-9)) - 1.0, 0.00, 0.18))
    if range_expansion is not None:
        squeeze_parts.append(_score(range_expansion, 0.90, 1.75))
    squeeze_score = sum(squeeze_parts) / len(squeeze_parts) if squeeze_parts else 50.0

    volume = finite_float(values.get("volume_ratio"))
    volume_score = _score(volume, 0.80, 1.80)
    vwap_slope = finite_float(values.get("vwap_slope"))
    close = finite_float(values.get("entry_price"))
    if vwap_slope is not None and close is not None and close > 0:
        vwap_slope_pct = vwap_slope / close * 100.0 * direction
        vwap_score = _score(vwap_slope_pct, 0.0005, 0.035)
    else:
        vwap_score = 50.0
    flow_score = (volume_score * 0.62) + (vwap_score * 0.38)

    close_location = finite_float(values.get("close_location"))
    upper_wick = finite_float(values.get("upper_wick_ratio"))
    lower_wick = finite_float(values.get("lower_wick_ratio"))
    range_ratio = finite_float(values.get("range_expansion_ratio"))
    if side == "long":
        close_quality = 50.0 if close_location is None else _score(close_location, 0.45, 0.85)
        wick_penalty = 0.0 if upper_wick is None else clamp(upper_wick / 0.45 * 38.0, 0.0, 45.0)
    else:
        close_quality = 50.0 if close_location is None else _score(1.0 - close_location, 0.45, 0.85)
        wick_penalty = 0.0 if lower_wick is None else clamp(lower_wick / 0.45 * 38.0, 0.0, 45.0)
    exhaustion_score = close_quality - wick_penalty
    if range_ratio is not None and range_ratio > 2.20:
        exhaustion_score -= clamp((range_ratio - 2.20) / 1.20 * 25.0, 0.0, 25.0)
    if side == "short":
        recent_rebound = finite_float(values.get("recent_rebound_pct"))
        if recent_rebound is not None:
            exhaustion_score -= clamp((recent_rebound - 2.0) / 5.0 * 18.0, 0.0, 18.0)
    exhaustion_score = clamp(exhaustion_score, 0.0, 100.0)

    score = (
        clamp(momentum_score, 0.0, 100.0) * 0.22
        + slope_score * 0.16
        + persistence_score * 0.18
        + squeeze_score * 0.16
        + flow_score * 0.12
        + exhaustion_score * 0.16
    )
    hard_block_below = finite_float(cfg.get("strategy_quality_hard_block_below"), 28.0)
    reduce_below = finite_float(cfg.get("strategy_quality_reduce_below"), 58.0)
    full_score = max(reduce_below, finite_float(cfg.get("strategy_quality_full_score"), 78.0))
    min_multiplier = clamp(cfg.get("strategy_quality_min_multiplier", 0.35), 0.05, 1.0)
    if score < hard_block_below:
        state = False
        multiplier = 0.0
    elif score < reduce_below:
        state = "reduced"
        multiplier = min_multiplier
    elif score < full_score:
        state = "reduced"
        scale = (score - reduce_below) / max(full_score - reduce_below, 1e-9)
        multiplier = min_multiplier + (1.0 - min_multiplier) * clamp(scale, 0.0, 1.0)
    else:
        state = True
        multiplier = 1.0

    components = {
        "multi_speed_momentum": round(clamp(momentum_score, 0.0, 100.0), 2),
        "trend_slope": round(slope_score, 2),
        "persistence": round(persistence_score, 2),
        "squeeze_expansion": round(squeeze_score, 2),
        "volume_flow": round(flow_score, 2),
        "exhaustion_guard": round(exhaustion_score, 2),
    }
    reasons = []
    if components["multi_speed_momentum"] < 45.0:
        reasons.append("momentum disagreement")
    if components["persistence"] < 45.0:
        reasons.append("low persistence")
    if components["squeeze_expansion"] >= 70.0:
        reasons.append("squeeze expansion")
    if components["exhaustion_guard"] < 45.0:
        reasons.append("exhaustion/chasing risk")
    if side == "short" and finite_float(values.get("recent_rebound_pct")) is not None:
        rebound = finite_float(values.get("recent_rebound_pct"), 0.0)
        if rebound >= 4.0:
            reasons.append(f"short rebound {rebound:.2f}%")
    if not reasons:
        reasons.append("balanced")
    state_label = "BLOCK" if state is False else "REDUCE" if state == "reduced" else "PASS"
    return {
        "enabled": True,
        "score": round(score, 2),
        "state": state,
        "risk_multiplier": round(multiplier, 4),
        "components": components,
        "reasons": reasons,
        "summary": (
            f"strategy quality {state_label} {score:.1f}/100 x{multiplier:.2f} "
            f"(mom {components['multi_speed_momentum']:.0f}, slope {components['trend_slope']:.0f}, "
            f"persist {components['persistence']:.0f}, sqz {components['squeeze_expansion']:.0f}, "
            f"flow {components['volume_flow']:.0f}, exhaust {components['exhaustion_guard']:.0f})"
        ),
    }


def build_adaptive_exit_overlay(cfg, stats, side, runner_stats=None, trend_health=None, strategy_quality=None):
    cfg = dict(cfg or {})
    side = str(side or "").lower()
    opportunity_profile = (
        str(cfg.get("effective_profile_version") or "")
        == "profit_opportunity_v4_tp350_runner"
    )
    partial_r = finite_float(
        cfg.get("partial_take_profit_r_multiple"),
        1.0 if opportunity_profile else 1.5,
    )
    partial_ratio = finite_float(
        cfg.get("partial_take_profit_ratio"),
        0.20 if opportunity_profile else 0.5,
    )
    trailing_mult = finite_float(
        cfg.get("atr_trailing_multiplier"),
        3.50 if opportunity_profile else 2.0,
    )
    activation_r = finite_float(cfg.get("atr_trailing_activation_r"), partial_r)
    reasons = []

    if side == "short" and bool(cfg.get("short_asymmetry_enabled", True)):
        partial_r -= finite_float(cfg.get("short_partial_take_profit_r_delta"), 0.20)
        partial_ratio += finite_float(cfg.get("short_partial_take_profit_ratio_add"), 0.10)
        trailing_mult -= finite_float(cfg.get("short_atr_trailing_multiplier_delta"), 0.25)
        activation_r -= finite_float(cfg.get("short_atr_trailing_activation_r_delta"), 0.20)
        reasons.append("short asymmetry")

    stats = dict(stats or {})
    min_samples = max(1, int(cfg.get("adaptive_exit_min_samples", 8) or 8))
    sample_count = int(stats.get("sample_count") or 0)
    if bool(cfg.get("adaptive_exit_enabled", True)) and sample_count >= min_samples:
        tp_rate = finite_float(stats.get("tp_rate"), 0.0)
        timeout_rate = finite_float(stats.get("timeout_rate"), 0.0)
        avg_mfe = finite_float(stats.get("avg_mfe_r"), 0.0)
        avg_pnl = finite_float(stats.get("avg_pnl_r"), 0.0)
        if timeout_rate >= 0.35 and avg_mfe >= partial_r * 0.85:
            partial_r -= 0.15
            partial_ratio += 0.05
            trailing_mult -= 0.10
            activation_r -= 0.10
            reasons.append("timeout harvest")
        elif tp_rate < 0.35 and avg_mfe >= 1.20:
            partial_r -= 0.10
            partial_ratio += 0.05
            reasons.append("MFE before TP")
        elif tp_rate >= 0.55 and avg_mfe >= 2.0 and avg_pnl >= 0.35:
            trailing_mult += 0.15
            partial_ratio -= 0.05
            reasons.append("let winners run")
    elif bool(cfg.get("adaptive_exit_enabled", True)):
        reasons.append(f"shadow samples {sample_count}/{min_samples}")
    else:
        reasons.append("adaptive exit OFF")

    runner_stats = dict(runner_stats or {})
    runner_samples = int(runner_stats.get("sample_count") or 0)
    if bool(cfg.get("runner_exit_enabled", False)) and runner_samples >= min_samples:
        capture = finite_float(runner_stats.get("avg_mfe_capture_ratio"))
        runner_mfe = finite_float(runner_stats.get("avg_mfe_r"), 0.0)
        runner_pnl = finite_float(runner_stats.get("avg_pnl_r"), 0.0)
        if capture is not None and capture < 0.45 and runner_mfe >= 1.8:
            trailing_mult -= 0.15
            activation_r -= 0.10
            reasons.append(f"runner capture low {capture:.0%}")
        elif capture is not None and capture >= 0.70 and runner_pnl > 0.25:
            trailing_mult += 0.10
            partial_ratio -= 0.03
            reasons.append(f"runner capture strong {capture:.0%}")

    health_score = finite_float((trend_health or {}).get("score"))
    if health_score is not None:
        if health_score >= 75.0:
            trailing_mult += 0.20
            partial_ratio -= 0.03
            reasons.append(f"strong trend health {health_score:.0f}")
        elif health_score < 55.0:
            partial_r -= 0.10
            partial_ratio += 0.03
            trailing_mult -= 0.10
            activation_r -= 0.10
            reasons.append(f"weak trend health {health_score:.0f}")

    strategy_quality = dict(strategy_quality or {})
    quality_score = finite_float(strategy_quality.get("score"))
    quality_components = strategy_quality.get("components") if isinstance(strategy_quality.get("components"), dict) else {}
    if quality_score is not None:
        if quality_score >= 78.0:
            trailing_mult += 0.15
            partial_ratio -= 0.03
            reasons.append(f"strong strategy quality {quality_score:.0f}")
        elif quality_score < 58.0:
            partial_r -= 0.10
            partial_ratio += 0.04
            trailing_mult -= 0.10
            activation_r -= 0.10
            reasons.append(f"weak strategy quality {quality_score:.0f}")
    squeeze_score = finite_float(quality_components.get("squeeze_expansion"))
    persistence_score = finite_float(quality_components.get("persistence"))
    exhaustion_score = finite_float(quality_components.get("exhaustion_guard"))
    if squeeze_score is not None and persistence_score is not None and squeeze_score >= 75.0 and persistence_score >= 68.0:
        trailing_mult += 0.10
        reasons.append("persistent squeeze expansion")
    if exhaustion_score is not None and exhaustion_score < 45.0:
        partial_r -= 0.15
        partial_ratio += 0.05
        trailing_mult -= 0.15
        activation_r -= 0.10
        reasons.append(f"exhaustion guard {exhaustion_score:.0f}")

    partial_r = clamp(
        partial_r,
        cfg.get("adaptive_exit_partial_r_min", 1.0),
        cfg.get("adaptive_exit_partial_r_max", 1.2 if opportunity_profile else 1.8),
    )
    partial_ratio = clamp(
        partial_ratio,
        cfg.get("adaptive_exit_ratio_min", 0.20 if opportunity_profile else 0.35),
        cfg.get("adaptive_exit_ratio_max", 0.35 if opportunity_profile else 0.65),
    )
    trailing_mult = clamp(
        trailing_mult,
        cfg.get("adaptive_exit_trailing_multiplier_min", 3.0 if opportunity_profile else 1.4),
        cfg.get("adaptive_exit_trailing_multiplier_max", 4.0 if opportunity_profile else 2.6),
    )
    activation_r = clamp(
        activation_r,
        cfg.get("adaptive_exit_activation_r_min", 1.4 if opportunity_profile else 1.0),
        cfg.get("adaptive_exit_activation_r_max", 1.8),
    )
    return {
        "partial_take_profit_r_multiple": round(partial_r, 4),
        "partial_take_profit_ratio": round(partial_ratio, 4),
        "atr_trailing_multiplier": round(trailing_mult, 4),
        "atr_trailing_activation_r": round(activation_r, 4),
        "summary": (
            f"exit partial {partial_ratio:.0%}@{partial_r:.2f}R, "
            f"trail {trailing_mult:.2f}ATR from {activation_r:.2f}R ({'; '.join(reasons)})"
        ),
        "reasons": reasons,
    }


def build_strategy_adaptation(cfg, stats, *, side, atr_pct, runner_stats=None, trend_health=None, strategy_quality=None):
    vol_multiplier, vol_summary = build_volatility_risk_multiplier(cfg, atr_pct)
    meta_multiplier, meta_summary = build_meta_risk_multiplier(cfg, stats)
    trend_health = dict(trend_health or {})
    trend_multiplier = finite_float(trend_health.get("risk_multiplier"), 1.0)
    trend_summary = trend_health.get("summary") or "trend health neutral"
    strategy_quality = dict(strategy_quality or {})
    quality_multiplier = finite_float(strategy_quality.get("risk_multiplier"), 1.0)
    quality_summary = strategy_quality.get("summary") or "strategy quality neutral"
    min_multiplier = clamp((cfg or {}).get("strategy_adaptive_min_risk_multiplier", 0.25), 0.05, 1.0)
    risk_multiplier = clamp(
        float(vol_multiplier) * float(meta_multiplier) * float(trend_multiplier) * float(quality_multiplier),
        min_multiplier,
        1.0
    )
    exit_overlay = build_adaptive_exit_overlay(
        cfg,
        stats,
        side,
        runner_stats=runner_stats,
        trend_health=trend_health,
        strategy_quality=strategy_quality,
    )
    stats = dict(stats or {})
    runner_stats = dict(runner_stats or {})
    sample_count = int(stats.get("sample_count") or 0)
    runner_count = int(runner_stats.get("sample_count") or 0)
    avg_pnl = finite_float(stats.get("avg_pnl_r"))
    avg_text = "n/a" if avg_pnl is None else f"{avg_pnl:.2f}R"
    capture = finite_float(runner_stats.get("avg_mfe_capture_ratio"))
    capture_text = "n/a" if capture is None else f"{capture:.0%}"
    summary = (
        f"risk x{risk_multiplier:.2f}; {vol_summary}; {meta_summary}; {trend_summary}; {quality_summary}; "
        f"shadow n={sample_count}, WR={finite_float(stats.get('tp_rate'), 0.0):.0%}, avg={avg_text}; "
        f"runner n={runner_count}, capture={capture_text}; "
        f"{exit_overlay['summary']}"
    )
    return {
        "risk_multiplier": round(risk_multiplier, 4),
        "volatility_risk_multiplier": vol_multiplier,
        "meta_label_risk_multiplier": meta_multiplier,
        "trend_health_risk_multiplier": round(trend_multiplier, 4),
        "strategy_quality_risk_multiplier": round(quality_multiplier, 4),
        "volatility_summary": vol_summary,
        "meta_label_summary": meta_summary,
        "trend_health": trend_health,
        "trend_health_summary": trend_summary,
        "strategy_quality": strategy_quality,
        "strategy_quality_summary": quality_summary,
        "shadow_stats": stats,
        "runner_stats": runner_stats,
        "exit_overlay": exit_overlay,
        "summary": summary,
    }
