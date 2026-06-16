"""Adaptive timeframe selection helpers for UT Breakout.

The selector is deterministic and pure: multi-timeframe metrics are used to
choose one execution timeframe, not as extra AND filters on the live entry.
"""

from math import isfinite


TIMEFRAME_ORDER = ["15m", "30m", "1h", "4h"]
TIMEFRAME_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}
HTF_MAP = {
    "5m": "15m",
    "15m": "1h",
    "30m": "1h",
    "1h": "4h",
    "4h": "1d",
}


def finite_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if isfinite(parsed) else default


def clamp(value, low=0.0, high=100.0):
    value = finite_float(value, low)
    return max(low, min(high, value))


def score_between(value, low, high, *, soft_low=None, soft_high=None):
    value = finite_float(value)
    if value is None:
        return 35.0
    if low <= value <= high:
        return 100.0
    soft_low = low if soft_low is None else soft_low
    soft_high = high if soft_high is None else soft_high
    if value < low:
        if low <= soft_low:
            return 0.0
        return clamp((value - soft_low) / (low - soft_low) * 100.0)
    if soft_high <= high:
        return 0.0
    return clamp((soft_high - value) / (soft_high - high) * 100.0)


def timeframe_rank(tf):
    try:
        return TIMEFRAME_ORDER.index(str(tf))
    except ValueError:
        return len(TIMEFRAME_ORDER)


def _ema_distance_pct(metrics):
    close = finite_float(metrics.get("close"))
    ema_slow = finite_float(metrics.get("ema_slow"))
    if close is None or ema_slow is None or close == 0:
        return None
    return abs(close - ema_slow) / abs(close) * 100.0


def _timeframe_profile_bias(tf, trend_quality, volatility_fit, noise_penalty, signal_quality):
    """Nudge scores toward stable UTBot execution horizons.

    UTBot should not constantly jump between very short and long frames.
    15m is the main execution timeframe.
    30m is the fallback when 15m is noisy.
    1h is confirmation, not the default entry trigger.
    """
    tf = str(tf)
    high_noise = noise_penalty >= 60.0
    clean_entry = trend_quality >= 50.0 and volatility_fit >= 45.0 and signal_quality >= 45.0

    if tf == "15m":
        return 10.0 if clean_entry else -4.0 if high_noise else 4.0
    if tf == "30m":
        return 5.0 if high_noise else 2.0
    if tf == "1h":
        return 2.0 if trend_quality >= 70.0 else -4.0
    if tf == "4h":
        return -10.0
    return 0.0


def score_timeframe(tf, metrics, cfg=None):
    cfg = cfg or {}
    metrics = metrics or {}
    if not isinstance(metrics, dict) or not metrics.get("ready"):
        return {
            "tf": tf,
            "score": 0.0,
            "ready": False,
            "reason": metrics.get("reason") if isinstance(metrics, dict) else "데이터 부족",
        }

    atr_min = finite_float(cfg.get("atr_min_percent"), 0.12)
    atr_max = finite_float(cfg.get("atr_max_percent"), 1.20)
    ema_near_min = finite_float(cfg.get("ema_near_percent"), 0.20)
    don_width_min = finite_float(cfg.get("donchian_width_min_percent"), 0.50)

    adx = finite_float(metrics.get("adx"), 0.0)
    chop = finite_float(metrics.get("chop"), 50.0)
    atr_pct = finite_float(metrics.get("atr_pct"))
    ema_gap = finite_float(metrics.get("ema_gap_pct"), 0.0)
    don_width = finite_float(metrics.get("donchian_width_pct"), 0.0)
    range_expansion = finite_float(metrics.get("range_expansion_ratio"), 1.0)
    volume_ratio = finite_float(metrics.get("volume_ratio"), 1.0)
    ema_distance = _ema_distance_pct(metrics)
    ema_bias = str(metrics.get("ema_bias") or "neutral").lower()

    adx_score = clamp((adx - 12.0) / 18.0 * 100.0)
    chop_trend_score = clamp((62.0 - chop) / 24.0 * 100.0)
    ema_gap_score = clamp(ema_gap / 0.45 * 100.0)
    directional_score = 72.0 if ema_bias in {"long", "short"} else 35.0
    trend_quality = (
        adx_score * 0.34
        + chop_trend_score * 0.26
        + ema_gap_score * 0.22
        + directional_score * 0.18
    )

    volatility_fit = score_between(
        atr_pct,
        atr_min,
        atr_max,
        soft_low=max(0.0, atr_min * 0.35),
        soft_high=max(atr_max * 1.75, atr_max + 0.25),
    )
    don_width_score = clamp(don_width / max(don_width_min * 2.2, 0.01) * 100.0)
    expansion_score = clamp((range_expansion - 0.75) / 0.85 * 100.0)
    volume_score = clamp((volume_ratio - 0.65) / 0.85 * 100.0)
    signal_quality = (
        (100.0 if metrics.get("donchian_ready") else 35.0) * 0.24
        + don_width_score * 0.24
        + expansion_score * 0.22
        + volume_score * 0.16
        + directional_score * 0.14
    )

    ema_noise = 100.0 if ema_distance is not None and ema_distance < ema_near_min else 25.0
    chop_noise = clamp((chop - 50.0) / 18.0 * 100.0)
    high_vol_noise = 100.0 - score_between(
        atr_pct,
        atr_min,
        atr_max,
        soft_low=max(0.0, atr_min * 0.35),
        soft_high=max(atr_max * 1.75, atr_max + 0.25),
    )
    low_signal_noise = 100.0 - signal_quality
    noise_penalty = (
        ema_noise * 0.30
        + chop_noise * 0.30
        + high_vol_noise * 0.22
        + low_signal_noise * 0.18
    )

    session_hour = metrics.get("session_hour_kst")
    active_session = session_hour in set(range(16, 24)) | set(range(0, 3))
    session_score = (75.0 if active_session else 50.0) + min(20.0, max(0.0, (volume_ratio - 1.0) * 20.0))

    profile_bias = _timeframe_profile_bias(
        tf,
        trend_quality,
        volatility_fit,
        noise_penalty,
        signal_quality,
    )
    final_score = (
        trend_quality * 0.34
        + volatility_fit * 0.24
        + signal_quality * 0.25
        + session_score * 0.07
        - noise_penalty * 0.20
        + profile_bias
    )
    final_score = clamp(final_score)
    return {
        "tf": tf,
        "score": round(final_score, 2),
        "ready": True,
        "trend_quality": round(trend_quality, 2),
        "volatility_fit": round(volatility_fit, 2),
        "signal_quality": round(signal_quality, 2),
        "noise_penalty": round(noise_penalty, 2),
        "volume_session_score": round(session_score, 2),
        "profile_bias": round(profile_bias, 2),
        "timestamp": metrics.get("timestamp"),
        "ema_bias": ema_bias,
        "atr_pct": atr_pct,
        "adx": adx,
        "chop": chop,
        "reason": (
            f"trend {trend_quality:.1f}, vol {volatility_fit:.1f}, "
            f"signal {signal_quality:.1f}, noise {noise_penalty:.1f}"
        ),
    }


def select_adaptive_timeframe(timeframe_metrics, cfg=None, state=None, position_side=None):
    cfg = cfg or {}
    state = state or {}
    allowed = cfg.get("adaptive_timeframes") or cfg.get("auto_timeframes") or TIMEFRAME_ORDER
    allowed = [str(tf).strip().lower() for tf in allowed if str(tf).strip()]
    allowed = [tf for tf in allowed if tf in TIMEFRAME_MS and tf != "5m"] or list(TIMEFRAME_ORDER)

    # UTBot should prefer stable 15m/30m execution instead of frequently switching.
    min_score = finite_float(cfg.get("adaptive_timeframe_min_score"), 38.0)
    switch_margin = finite_float(cfg.get("adaptive_timeframe_switch_margin"), 10.0)
    min_hold_candles = int(max(0.0, finite_float(cfg.get("adaptive_timeframe_min_hold_candles"), 6.0)))

    candidates = [
        score_timeframe(tf, (timeframe_metrics or {}).get(tf), cfg)
        for tf in allowed
    ]
    candidates.sort(key=lambda item: (item.get("score", 0.0), -timeframe_rank(item.get("tf"))), reverse=True)
    ready = [item for item in candidates if item.get("ready")]
    top3 = candidates[:3]
    if not ready:
        return {
            "selected_tf": None,
            "previous_tf": state.get("selected_tf"),
            "top3": top3,
            "scores": candidates,
            "decision": "NO_TRADE",
            "reason": "ADAPTIVE TF 데이터 부족",
        }

    best = ready[0]
    previous_tf = str(state.get("selected_tf") or "").lower() or None
    previous_score = next((item.get("score", 0.0) for item in ready if item.get("tf") == previous_tf), None)
    position_side = str(position_side or "none").lower()

    if best.get("score", 0.0) < min_score:
        return {
            "selected_tf": None,
            "previous_tf": previous_tf,
            "top3": top3,
            "scores": candidates,
            "decision": "NO_TRADE",
            "reason": f"ADAPTIVE TF 점수 부족: {best.get('tf')} {best.get('score', 0):.1f} < {min_score:.1f}",
        }

    selected = best
    decision = "SELECTED"
    reason = f"{best.get('tf')} 선택: {best.get('reason')}"

    if position_side in {"long", "short"} and previous_tf and previous_score is not None:
        selected = next(item for item in ready if item.get("tf") == previous_tf)
        decision = "POSITION_LOCKED"
        reason = f"포지션 보유 중이므로 기존 진입 TF {previous_tf} 유지"
    elif previous_tf and previous_score is not None and previous_tf != best.get("tf"):
        last_switch_ts = finite_float(state.get("last_switch_ts"), 0.0) or 0.0
        current_ts = finite_float(best.get("timestamp"), 0.0) or 0.0
        prev_ms = TIMEFRAME_MS.get(previous_tf, TIMEFRAME_MS["15m"])
        hold_elapsed = (current_ts - last_switch_ts) / prev_ms if current_ts and last_switch_ts else min_hold_candles
        margin = best.get("score", 0.0) - previous_score
        if hold_elapsed < min_hold_candles or margin < switch_margin:
            selected = next(item for item in ready if item.get("tf") == previous_tf)
            decision = "HYSTERESIS_KEEP"
            reason = (
                f"기존 TF {previous_tf} 유지: hold {hold_elapsed:.1f}/{min_hold_candles}, "
                f"margin {margin:.1f}/{switch_margin:.1f}"
            )

    return {
        "selected_tf": selected.get("tf"),
        "previous_tf": previous_tf,
        "top3": top3,
        "scores": candidates,
        "decision": decision,
        "reason": reason,
        "selected_score": selected.get("score", 0.0),
        "selected_timestamp": selected.get("timestamp"),
        "htf_timeframe": HTF_MAP.get(selected.get("tf"), "1h"),
    }
