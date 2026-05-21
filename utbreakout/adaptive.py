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
        if event.get("event") != "shadow_outcome" and not code.startswith("SHADOW_"):
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


def build_adaptive_exit_overlay(cfg, stats, side):
    cfg = dict(cfg or {})
    side = str(side or "").lower()
    partial_r = finite_float(cfg.get("partial_take_profit_r_multiple"), 1.5)
    partial_ratio = finite_float(cfg.get("partial_take_profit_ratio"), 0.5)
    trailing_mult = finite_float(cfg.get("atr_trailing_multiplier"), 2.0)
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

    partial_r = clamp(partial_r, cfg.get("adaptive_exit_partial_r_min", 1.0), cfg.get("adaptive_exit_partial_r_max", 1.8))
    partial_ratio = clamp(partial_ratio, cfg.get("adaptive_exit_ratio_min", 0.35), cfg.get("adaptive_exit_ratio_max", 0.65))
    trailing_mult = clamp(trailing_mult, cfg.get("adaptive_exit_trailing_multiplier_min", 1.4), cfg.get("adaptive_exit_trailing_multiplier_max", 2.6))
    activation_r = clamp(activation_r, cfg.get("adaptive_exit_activation_r_min", 1.0), cfg.get("adaptive_exit_activation_r_max", 1.8))
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


def build_strategy_adaptation(cfg, stats, *, side, atr_pct):
    vol_multiplier, vol_summary = build_volatility_risk_multiplier(cfg, atr_pct)
    meta_multiplier, meta_summary = build_meta_risk_multiplier(cfg, stats)
    min_multiplier = clamp((cfg or {}).get("strategy_adaptive_min_risk_multiplier", 0.25), 0.05, 1.0)
    risk_multiplier = clamp(float(vol_multiplier) * float(meta_multiplier), min_multiplier, 1.0)
    exit_overlay = build_adaptive_exit_overlay(cfg, stats, side)
    stats = dict(stats or {})
    sample_count = int(stats.get("sample_count") or 0)
    avg_pnl = finite_float(stats.get("avg_pnl_r"))
    avg_text = "n/a" if avg_pnl is None else f"{avg_pnl:.2f}R"
    summary = (
        f"risk x{risk_multiplier:.2f}; {vol_summary}; {meta_summary}; "
        f"shadow n={sample_count}, WR={finite_float(stats.get('tp_rate'), 0.0):.0%}, avg={avg_text}; "
        f"{exit_overlay['summary']}"
    )
    return {
        "risk_multiplier": round(risk_multiplier, 4),
        "volatility_risk_multiplier": vol_multiplier,
        "meta_label_risk_multiplier": meta_multiplier,
        "volatility_summary": vol_summary,
        "meta_label_summary": meta_summary,
        "shadow_stats": stats,
        "exit_overlay": exit_overlay,
        "summary": summary,
    }
