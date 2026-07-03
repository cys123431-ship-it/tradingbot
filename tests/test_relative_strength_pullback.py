from utbreakout.relative_strength_pullback import (
    ENTRY_STRATEGY_RELATIVE_STRENGTH_PULLBACK_TREND,
    ENTRY_STRATEGY_UT_BREAKOUT,
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
        "adx_threshold": 10.0,
        "breakout_atr_max": 20.0,
        "extreme_atr_pct": 99.0,
    })
    cfg.update(overrides)
    return cfg


def test_defaults_keep_new_strategy_shadow_only():
    cfg = default_relative_strength_pullback_config()

    assert cfg["entry_strategy"] == ENTRY_STRATEGY_UT_BREAKOUT
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

    assert decisions[0].reason == "waiting_for_pullback"
    assert decisions[0].logs["relative_strength_reason"] == "skipped_few_candidates"


def test_long_blocks_bottom_relative_strength_candidate():
    candidates = ["WEAK/USDT:USDT", "MID/USDT:USDT", "GOOD/USDT:USDT", "BEST/USDT:USDT"]
    slopes = [0.02, 0.06, 0.10, 0.14]
    signal = {symbol: _rows(130, 100.0, slope) for symbol, slope in zip(candidates, slopes)}
    htf = {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000) for symbol in candidates}

    decisions = evaluate_relative_strength_pullback_trend(
        candidates,
        signal,
        htf,
        config=_base_config(relative_strength_min_candidates=4, relative_strength_block_quantile=0.30),
    )
    weak = next(decision for decision in decisions if decision.symbol.startswith("WEAK/"))

    assert weak.entry_ready is False
    assert weak.reason == "relative_strength_too_weak_for_long"
    assert weak.logs["relative_strength_passed_long"] is False


def test_short_blocks_top_relative_strength_candidate():
    candidates = ["STRONG/USDT:USDT", "MID/USDT:USDT", "WEAK/USDT:USDT", "WORST/USDT:USDT"]
    slopes = [-0.02, -0.06, -0.10, -0.14]
    signal = {symbol: _rows(130, 120.0, slope) for symbol, slope in zip(candidates, slopes)}
    htf = {symbol: _rows(220, 120.0, -0.10, timestamp_step=86_400_000) for symbol in candidates}

    decisions = evaluate_relative_strength_pullback_trend(
        candidates,
        signal,
        htf,
        config=_base_config(relative_strength_min_candidates=4, relative_strength_block_quantile=0.30),
    )
    strong = next(decision for decision in decisions if decision.symbol.startswith("STRONG/"))

    assert strong.entry_ready is False
    assert strong.reason == "relative_strength_too_strong_for_short"
    assert strong.logs["relative_strength_passed_short"] is False


def test_donchian_breakout_waits_for_pullback_instead_of_chasing():
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

    assert decision.entry_ready is False
    assert decision.reason == "waiting_for_pullback"
    assert decision.state_update["breakout_side"] == "long"
    assert decision.logs["waiting_for_pullback"] is True


def test_pullback_after_breakout_is_next_open_entry_candidate():
    symbol = "PULL/USDT:USDT"
    base = _rows(130, 100.0, 0.06)
    rows = _with_last(base, open=107.0, high=108.2, low=106.0, close=108.0)

    decisions = evaluate_relative_strength_pullback_trend(
        [symbol],
        {symbol: rows},
        {symbol: _rows(220, 100.0, 0.10, timestamp_step=86_400_000)},
        state_by_symbol={symbol: {"breakout_side": "long", "breakout_price": 111.5}},
        config=_base_config(),
    )
    decision = decisions[0]

    assert decision.entry_ready is True
    assert decision.side == "long"
    assert decision.entry_execution == "next_open"
    assert decision.reason == "pullback_or_rebreakout_confirmed"


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

    assert decisions[0].entry_ready is False
    assert decisions[0].reason != "waiting_for_pullback"
