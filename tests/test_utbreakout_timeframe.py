from utbreakout.timeframe import select_adaptive_timeframe


def _metrics(**overrides):
    base = {
        "ready": True,
        "timestamp": 1_000_000,
        "close": 100.0,
        "ema_slow": 96.0,
        "ema_gap_pct": 0.45,
        "ema_bias": "long",
        "adx": 26.0,
        "chop": 45.0,
        "atr_pct": 0.35,
        "donchian_ready": True,
        "donchian_width_pct": 0.9,
        "range_expansion_ratio": 1.2,
        "volume_ratio": 1.2,
        "session_hour_kst": 22,
    }
    base.update(overrides)
    return base


def test_adaptive_timeframe_prefers_fast_tf_when_clean_trend():
    metrics = {
        "15m": _metrics(timestamp=900_000, adx=30.0, chop=41.0, atr_pct=0.30),
        "30m": _metrics(timestamp=900_000, adx=27.0, chop=46.0, atr_pct=0.32),
        "1h": _metrics(timestamp=900_000, adx=24.0, chop=49.0, atr_pct=0.28),
    }

    decision = select_adaptive_timeframe(metrics, {"adaptive_timeframe_min_score": 40.0})

    assert decision["selected_tf"] in {"15m", "30m"}
    assert decision["decision"] == "SELECTED"


def test_adaptive_timeframe_moves_slower_when_fast_tf_is_noisy():
    metrics = {
        "15m": _metrics(timestamp=900_000, adx=17.0, chop=64.0, atr_pct=1.45, ema_slow=99.95),
        "30m": _metrics(timestamp=900_000, adx=21.0, chop=57.0, atr_pct=1.05),
        "1h": _metrics(timestamp=900_000, adx=28.0, chop=46.0, atr_pct=0.70),
    }

    decision = select_adaptive_timeframe(metrics, {"adaptive_timeframe_min_score": 40.0})

    assert decision["selected_tf"] in {"30m", "1h"}


def test_adaptive_timeframe_returns_no_trade_in_chop():
    metrics = {
        "15m": _metrics(adx=10.0, chop=68.0, atr_pct=0.04, donchian_width_pct=0.1, volume_ratio=0.4),
        "30m": _metrics(adx=11.0, chop=67.0, atr_pct=0.05, donchian_width_pct=0.1, volume_ratio=0.4),
        "1h": _metrics(adx=12.0, chop=65.0, atr_pct=0.05, donchian_width_pct=0.15, volume_ratio=0.5),
    }

    decision = select_adaptive_timeframe(metrics, {"adaptive_timeframe_min_score": 55.0})

    assert decision["selected_tf"] is None
    assert decision["decision"] == "NO_TRADE"


def test_adaptive_timeframe_hysteresis_keeps_previous_tf():
    metrics = {
        "15m": _metrics(timestamp=1_800_000, adx=27.0, chop=44.0),
        "30m": _metrics(timestamp=1_800_000, adx=29.0, chop=43.0),
    }
    state = {"selected_tf": "15m", "last_switch_ts": 1_000_000}

    decision = select_adaptive_timeframe(
        metrics,
        {"adaptive_timeframe_min_score": 40.0, "adaptive_timeframe_switch_margin": 20.0},
        state=state,
    )

    assert decision["selected_tf"] == "15m"
    assert decision["decision"] == "HYSTERESIS_KEEP"


def test_adaptive_timeframe_position_lock_keeps_entry_tf():
    metrics = {
        "15m": _metrics(timestamp=3_600_000, adx=18.0, chop=58.0),
        "1h": _metrics(timestamp=3_600_000, adx=32.0, chop=40.0),
    }
    state = {"selected_tf": "15m", "last_switch_ts": 900_000}

    decision = select_adaptive_timeframe(metrics, state=state, position_side="long")

    assert decision["selected_tf"] == "15m"
    assert decision["decision"] == "POSITION_LOCKED"
