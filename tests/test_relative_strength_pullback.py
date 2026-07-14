from utbreakout.relative_strength_pullback import (
    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    ENTRY_STRATEGY_UT_BREAKOUT,
    _adx_gate,
    default_relative_strength_pullback_config,
    evaluate_relative_strength_pullback_trend,
    resolve_entry_strategy,
)


def _rows(count, start=100.0, step=0.1, *, timestamp_step=21_600_000):
    rows = []
    for idx in range(count):
        close = start + step * idx
        open_ = close - step * 0.4
        high = max(open_, close) + max(abs(step), 0.1) * 0.8
        low = min(open_, close) - max(abs(step), 0.1) * 0.8
        rows.append({
            "timestamp": idx * timestamp_step,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0 + idx,
        })
    return rows


def _with_last(rows, **updates):
    copied = [dict(row) for row in rows]
    copied[-1].update(updates)
    return copied


def _base_config(**overrides):
    cfg = default_relative_strength_pullback_config()
    cfg.update({
        "relative_strength_min_candidates": 1,
        "adx_pass": 10.0,
        "adx_soft_min": 5.0,
        "breakout_atr_max": 20.0,
        "extreme_atr_pct": 99.0,
        "rspt_v2_enabled": False,
        "independent_direction_enabled": False,
        "allow_breakout_continuation": True,
        "pullback_tolerance_atr": 0.50,
        "forced_direction": "long",
        "direction_source": "UTBreakout",
    })
    cfg.update(overrides)
    return cfg


def test_defaults_keep_new_strategy_shadow_only():
    cfg = default_relative_strength_pullback_config()

    assert cfg["entry_strategy"] == ENTRY_STRATEGY_UT_BREAKOUT
    assert cfg["signal_tf"] == "4h"
    assert cfg["trend_htf"] == "1d"
    assert cfg["entry_execution"] == "market"
    assert cfg["exclude_incomplete_live_candle"] is True
    assert cfg["forced_direction"] is None
    assert cfg["direction_source"] == "RSPT-v3 BTC/ETH/alt/vol residual strength"
    assert cfg["require_internal_trend_confirmation"] is True
    assert cfg["rspt_v2_enabled"] is True
    assert cfg["rspt_v3_enabled"] is True
    assert cfg["alt_common_factor_enabled"] is True
    assert cfg["market_volatility_factor_enabled"] is True
    assert cfg["independent_direction_enabled"] is True
    assert cfg["allow_breakout_continuation"] is False
    assert cfg["relative_strength_pullback_trend_shadow_enabled"] is True
    assert cfg["relative_strength_pullback_trend_live_enabled"] is False
    assert cfg["relative_strength_pullback_trend_paper_enabled"] is False
    assert resolve_entry_strategy({"entry_strategy": "relative_strength_pullback"}) == ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND
    assert resolve_entry_strategy({"entry_strategy": "ut_breakout"}) == ENTRY_STRATEGY_UT_BREAKOUT


def test_strategy_uses_only_scanner_candidates_without_hardcoded_universe():
    candidates = [{"symbol": "FOO/USDT:USDT"}, {"symbol": "BAR/USDT:USDT"}]
    signal = {item["symbol"]: _rows(130, 100.0 + idx, 0.08 + idx * 0.02) for idx, item in enumerate(candidates)}
    htf = {item["symbol"]: _rows(220, 100.0 + idx, 0.10 + idx * 0.02, timestamp_step=86_400_000) for idx, item in enumerate(candidates)}

    decisions = evaluate_relative_strength_pullback_trend(candidates, signal, htf, config=_base_config())

    assert [decision.symbol for decision in decisions] == ["FOO/USDT:USDT", "BAR/USDT:USDT"]
    assert "BTC/USDT:USDT" not in {decision.symbol for decision in decisions}
    assert all(decision.logs["scanner_passed"] is True for decision in decisions)
    assert all(decision.logs["direction_by"] == "UTBreakout" for decision in decisions)


def test_strategy_waits_without_utbreakout_forced_direction():
    symbol = "FOO/USDT:USDT"
    signal = {symbol: _breakout_rows(100.0, 0.08)}
    htf = {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)}

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        signal,
        htf,
        config=_base_config(forced_direction=None),
    )

    assert decisions[0].entry_ready is False
    assert decisions[0].side is None
    assert decisions[0].reason == "no_ut_direction"
    assert decisions[0].logs["rejected_reason"] == "no_ut_direction"


def test_candidate_symbol_prefers_exchange_symbol_from_scanner_item():
    candidate = {
        "symbol": "FOO",
        "normalized_symbol": "FOO/USDT",
        "exchange_symbol": "FOO/USDT:USDT",
    }
    signal = {"FOO/USDT:USDT": _rows(130, 100.0, 0.08)}
    htf = {"FOO/USDT:USDT": _rows(220, 100.0, 0.10, timestamp_step=86_400_000)}

    decisions = evaluate_relative_strength_pullback_trend([candidate], signal, htf, config=_base_config())

    assert [decision.symbol for decision in decisions] == ["FOO/USDT:USDT"]


def test_single_candidate_skips_relative_strength_instead_of_self_ranking_bottom():
    symbol = "SOLO/USDT:USDT"
    base = _rows(130, 100.0, 0.06)
    rows = _with_last(base, open=108.0, high=112.0, low=107.5, close=111.5)

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        config=_base_config(relative_strength_min_candidates=1),
    )

    assert decisions[0].entry_ready is True
    assert decisions[0].logs["relative_strength_reason"] == "skipped_few_candidates"


def _breakout_rows(start, slope, *, side="long"):
    rows = _rows(130, start, slope)
    prev = rows[-21:-1]
    if side == "long":
        prev_high = max(row["high"] for row in prev)
        return _with_last(rows, open=prev_high + 0.05, high=prev_high + 0.45, low=prev_high - 0.05, close=prev_high + 0.30)
    prev_low = min(row["low"] for row in prev)
    return _with_last(rows, open=prev_low - 0.05, high=prev_low + 0.05, low=prev_low - 0.45, close=prev_low - 0.30)


def test_long_hard_blocks_bottom_relative_strength_only_with_enough_candidates():
    candidates = [f"COIN{idx}/USDT:USDT" for idx in range(10)]
    slopes = [0.02 + idx * 0.015 for idx in range(10)]
    signal = {symbol: _breakout_rows(100.0, slope, side="long") for symbol, slope in zip(candidates, slopes)}
    htf = {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000) for symbol in candidates}

    decisions = evaluate_relative_strength_pullback_trend(
        candidates,
        signal,
        htf,
        config=_base_config(
            forced_direction="long",
            relative_strength_min_candidates=10,
            rs_hard_filter_min_candidates=10,
        ),
    )
    weak = next(decision for decision in decisions if decision.symbol.startswith("COIN0/"))

    assert weak.entry_ready is False
    assert weak.reason == "relative_strength_hard_block"
    assert weak.logs["rejected_reason"] == "relative_strength_hard_block"


def test_short_hard_blocks_top_relative_strength_only_with_enough_candidates():
    candidates = [f"COIN{idx}/USDT:USDT" for idx in range(10)]
    slopes = [-0.02 - idx * 0.015 for idx in range(10)]
    signal = {symbol: _breakout_rows(120.0, slope, side="short") for symbol, slope in zip(candidates, slopes)}
    htf = {symbol: _rows(220, 120.0, -0.10, timestamp_step=86_400_000) for symbol in candidates}

    decisions = evaluate_relative_strength_pullback_trend(
        candidates,
        signal,
        htf,
        config=_base_config(
            forced_direction="short",
            relative_strength_min_candidates=10,
            rs_hard_filter_min_candidates=10,
        ),
    )
    strong = next(decision for decision in decisions if decision.symbol.startswith("COIN0/"))

    assert strong.entry_ready is False
    assert strong.reason == "relative_strength_hard_block"
    assert strong.logs["rejected_reason"] == "relative_strength_hard_block"


def test_breakout_continuation_enters_without_waiting_for_pullback():
    symbol = "PULL/USDT:USDT"
    base = _rows(130, 100.0, 0.06)
    rows = _with_last(base, open=108.0, high=112.0, low=107.5, close=111.5)

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        config=_base_config(),
    )
    decision = decisions[0]

    assert decision.entry_ready is True
    assert decision.reason == "breakout_continuation_confirmed"
    assert decision.logs["setup_type"] == "breakout_continuation"


def test_trend_pullback_enters_without_prior_breakout_state():
    symbol = "PULL/USDT:USDT"
    base = _rows(130, 100.0, 0.06)
    rows = _with_last(base, open=107.2, high=107.7, low=106.2, close=107.6)

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        config=_base_config(),
    )
    decision = decisions[0]

    assert decision.entry_ready is True
    assert decision.side == "long"
    assert decision.entry_execution == "market"
    assert decision.logs["signal_candle_closed"] is True
    assert decision.logs["signal_basis"] == "last_completed_candle"
    assert decision.logs["entry_execution_policy"] == "market_immediately_after_completed_candle"
    assert decision.reason == "trend_pullback_confirmed"
    assert decision.logs["setup_type"] == "trend_pullback"


def test_htf_ema100_fallback_prevents_insufficient_data_when_1d_history_is_short():
    symbol = "FALLBACK/USDT:USDT"
    signal_rows = _breakout_rows(100.0, 0.06, side="long")
    htf_rows = _rows(130, 100.0, 0.10, timestamp_step=86_400_000)

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: signal_rows},
        {symbol: htf_rows},
        config=_base_config(),
    )
    decision = decisions[0]

    assert decision.reason != "insufficient_data"
    assert decision.entry_ready is True
    assert decision.logs["htf_fallback_used"] is True
    assert decision.logs["ema_htf_effective"] == 100


def test_forced_utbreakout_direction_allows_setup_without_internal_trend_confirmation():
    symbol = "SHORT/USDT:USDT"
    rows = _breakout_rows(120.0, -0.06, side="short")

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        config=_base_config(forced_direction="short"),
    )
    decision = decisions[0]

    assert decision.entry_ready is True
    assert decision.side == "short"
    assert decision.reason == "breakout_continuation_confirmed"
    assert decision.logs["internal_trend_confirmation_required"] is False
    assert decision.logs["rspt_internal_direction_disabled"] is True
    assert decision.logs["rspt_direction_authority"] == "UTBreakout"
    assert decision.logs["htf_trend_passed_short"] is False
    assert decision.logs["trend_filter_passed_short"] is False


def test_legacy_internal_trend_confirmation_setting_cannot_override_ut_direction():
    symbol = "SHORT/USDT:USDT"
    rows = _breakout_rows(120.0, -0.06, side="short")

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        config=_base_config(
            forced_direction="short",
            require_internal_trend_confirmation=True,
        ),
    )
    decision = decisions[0]

    assert decision.entry_ready is True
    assert decision.side == "short"
    assert decision.reason == "breakout_continuation_confirmed"
    assert decision.logs["internal_trend_confirmation_required"] is False
    assert decision.logs["internal_trend_confirmation_requested_but_ignored"] is True
    assert decision.logs["rspt_internal_direction_disabled"] is True
    assert decision.logs["rspt_direction_authority"] == "UTBreakout"


def test_adx_soft_zone_reduces_size_instead_of_hard_blocking():
    gate = _adx_gate(17.0, default_relative_strength_pullback_config())

    assert gate["passed"] is True
    assert gate["reason"] == "adx_weak_size_reduced"
    assert 0.0 < gate["risk_multiplier"] < 1.0


def test_small_candidate_set_uses_relative_strength_size_reduction_not_hard_block():
    candidates = ["WEAK/USDT:USDT", "MID/USDT:USDT", "GOOD/USDT:USDT", "BEST/USDT:USDT"]
    slopes = [0.02, 0.06, 0.10, 0.14]
    signal = {symbol: _breakout_rows(100.0, slope, side="long") for symbol, slope in zip(candidates, slopes)}
    htf = {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000) for symbol in candidates}

    decisions = evaluate_relative_strength_pullback_trend(
        candidates,
        signal,
        htf,
        config=_base_config(relative_strength_min_candidates=4, rs_hard_filter_min_candidates=10),
    )
    weak = next(decision for decision in decisions if decision.symbol.startswith("WEAK/"))

    assert weak.entry_ready is True
    assert weak.logs["relative_strength_gate_reason"] == "relative_strength_size_reduced"
    assert weak.logs["size_multiplier"] < 1.0


def test_stale_signal_is_rejected_after_configured_entry_window():
    symbol = "STALE/USDT:USDT"
    tf_ms = 14_400_000
    rows = _breakout_rows(100.0, 0.06, side="long")
    for idx, row in enumerate(rows):
        row["timestamp"] = idx * tf_ms
    last_close_ms = rows[-1]["timestamp"] + tf_ms

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        config=_base_config(signal_tf="4h", stale_entry_minutes_4h=30),
        now_ms=last_close_ms + 31 * 60_000,
    )

    assert decisions[0].entry_ready is False
    assert decisions[0].reason == "stale_signal"
    assert decisions[0].logs["rejected_reason"] == "stale_signal"


def test_unfinished_last_candle_is_excluded_from_signal_calculation():
    symbol = "WAIT/USDT:USDT"
    closed = _rows(130, 100.0, 0.03)
    incomplete_ts = closed[-1]["timestamp"] + 21_600_000
    rows = closed + [{
        "timestamp": incomplete_ts,
        "open": 110.0,
        "high": 130.0,
        "low": 109.0,
        "close": 128.0,
        "volume": 9999.0,
    }]

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        config=_base_config(signal_tf="6h"),
        now_ms=incomplete_ts + 1,
    )

    decision = decisions[0]
    assert decision.entry_ready is False
    assert decision.reason != "waiting_for_pullback"
    assert decision.logs["signal_candle_ts"] == closed[-1]["timestamp"] * 1000
    assert decision.logs["signal_candle_closed"] is True
    assert decision.logs["entry_execution"] == "market"


def test_legacy_next_open_and_incomplete_candle_settings_are_ignored():
    symbol = "LEGACY/USDT:USDT"
    rows = _breakout_rows(100.0, 0.06, side="long")

    decision = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        config=_base_config(
            entry_execution="next_open",
            exclude_incomplete_live_candle=False,
        ),
    )[0]

    assert decision.entry_ready is True
    assert decision.entry_execution == "market"
    assert decision.logs["signal_candle_closed"] is True
    assert decision.logs["signal_basis"] == "last_completed_candle"
