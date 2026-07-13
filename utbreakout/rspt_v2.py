"""Pure helpers for the second-generation RSPT strategy.

RSPT-v2 deliberately separates direction selection from UTBreakout.  It ranks
scanner candidates by residual momentum after removing common BTC/ETH returns,
requires a real impulse before a pullback can qualify, and reduces risk when
4h volatility is elevated.
"""

from math import isfinite, sqrt


def _finite(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if isfinite(parsed) else float(default)


def _row_value(row, key, default=0.0):
    if isinstance(row, dict):
        return _finite(row.get(key), default)
    return _finite(getattr(row, key, default), default)


def _timestamp(value):
    parsed = _finite(value, 0.0)
    if parsed <= 0:
        return 0
    return int(parsed * 1000.0 if parsed < 10_000_000_000 else parsed)


def _symbol_key(symbol):
    return "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())


def _find_symbol_rows(rows_by_symbol, symbol):
    if symbol in rows_by_symbol:
        return rows_by_symbol.get(symbol) or []
    wanted = _symbol_key(symbol)
    for key, rows in (rows_by_symbol or {}).items():
        if _symbol_key(key) == wanted:
            return rows or []
    return []


def _returns_by_timestamp(rows):
    result = {}
    previous = None
    for row in rows or []:
        close = _row_value(row, "close")
        ts = _timestamp(row.get("timestamp") if isinstance(row, dict) else None)
        if previous is not None and previous > 0 and close > 0 and ts > 0:
            result[ts] = close / previous - 1.0
        if close > 0:
            previous = close
    return result


def _solve_linear_system(matrix, vector):
    """Solve a small dense system with pivoted Gaussian elimination."""
    size = len(vector)
    augmented = [list(map(float, matrix[row])) + [float(vector[row])] for row in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        augmented[col] = [value / divisor for value in augmented[col]]
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor == 0:
                continue
            augmented[row] = [
                augmented[row][idx] - factor * augmented[col][idx]
                for idx in range(size + 1)
            ]
    return [augmented[row][-1] for row in range(size)]


def _ridge_regression(features, targets, ridge):
    if not features or not targets or len(features) != len(targets):
        return None
    width = len(features[0])
    matrix = [[0.0 for _ in range(width)] for _ in range(width)]
    vector = [0.0 for _ in range(width)]
    for row, target in zip(features, targets):
        for left in range(width):
            vector[left] += row[left] * target
            for right in range(width):
                matrix[left][right] += row[left] * row[right]
    for idx in range(width):
        matrix[idx][idx] += max(0.0, float(ridge))
    return _solve_linear_system(matrix, vector)


def _standard_deviation(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return sqrt(max(variance, 0.0))


def residual_momentum_score(symbol, rows_by_symbol, config):
    target_rows = _find_symbol_rows(rows_by_symbol, symbol)
    target_returns = _returns_by_timestamp(target_rows)
    references = list(config.get("relative_strength_reference_symbols") or [])
    factor_maps = []
    used_references = []
    target_key = _symbol_key(symbol)
    for reference in references:
        if _symbol_key(reference) == target_key:
            continue
        reference_returns = _returns_by_timestamp(_find_symbol_rows(rows_by_symbol, reference))
        if reference_returns:
            factor_maps.append(reference_returns)
            used_references.append(reference)
    if not target_returns or not factor_maps:
        return None, {
            "reason": "residual_benchmark_missing",
            "reference_symbols": used_references,
        }

    timestamps = set(target_returns)
    for factor_map in factor_maps:
        timestamps &= set(factor_map)
    timestamps = sorted(timestamps)
    long_lb = max(2, int(config.get("relative_strength_long_lookback_bars", 84) or 84))
    short_lb = max(1, int(config.get("relative_strength_short_lookback_bars", 30) or 30))
    min_aligned = max(
        10,
        int(config.get("residual_strength_min_aligned_returns", 40) or 40),
        len(factor_maps) + 4,
    )
    if len(timestamps) < min_aligned:
        return None, {
            "reason": "residual_data_insufficient",
            "aligned_returns": len(timestamps),
            "reference_symbols": used_references,
        }
    timestamps = timestamps[-max(long_lb, min_aligned):]
    # Estimate beta on demeaned returns so a persistent symbol-specific drift
    # is not absorbed by the BTC/ETH coefficients.  The drift is deliberately
    # preserved in the residual series because that is the cross-sectional
    # momentum signal RSPT-v2 is trying to rank.
    features = [[factor_map[ts] for factor_map in factor_maps] for ts in timestamps]
    targets = [target_returns[ts] for ts in timestamps]
    feature_means = [
        sum(row[idx] for row in features) / len(features)
        for idx in range(len(factor_maps))
    ]
    target_mean = sum(targets) / len(targets)
    centered_features = [
        [value - feature_means[idx] for idx, value in enumerate(row)]
        for row in features
    ]
    centered_targets = [target - target_mean for target in targets]
    coefficients = _ridge_regression(
        centered_features,
        centered_targets,
        _finite(config.get("residual_strength_ridge"), 1e-6),
    )
    if coefficients is None:
        return None, {
            "reason": "residual_regression_failed",
            "aligned_returns": len(timestamps),
            "reference_symbols": used_references,
        }
    residuals = [
        target - sum(coefficient * value for coefficient, value in zip(coefficients, row))
        for row, target in zip(features, targets)
    ]
    long_window = residuals[-min(long_lb, len(residuals)):]
    short_window = residuals[-min(short_lb, len(residuals)):]
    volatility = _standard_deviation(long_window)
    factor_volatility = max(
        (_standard_deviation([factor_map[ts] for ts in timestamps]) for factor_map in factor_maps),
        default=0.0,
    )
    volatility_floor_ratio = max(
        0.0,
        _finite(config.get("residual_strength_volatility_floor_ratio"), 0.50),
    )
    scoring_volatility = max(
        volatility,
        factor_volatility * volatility_floor_ratio,
        1e-12,
    )
    long_component = sum(long_window)
    short_component = sum(short_window)
    raw = 0.65 * long_component + 0.35 * short_component
    score = raw / (scoring_volatility * sqrt(max(len(long_window), 1)))
    return score, {
        "reason": "ok",
        "method": "btc_eth_residual_momentum",
        "aligned_returns": len(timestamps),
        "reference_symbols": used_references,
        "residual_volatility": volatility,
        "scoring_volatility": scoring_volatility,
        "factor_volatility": factor_volatility,
        "residual_long_return": long_component,
        "residual_short_return": short_component,
        "regression_coefficients": coefficients,
    }


def residual_strength_percentiles(symbols, rows_by_symbol, config):
    candidate_count = len(symbols)
    min_candidates = max(2, int(config.get("relative_strength_min_candidates", 4) or 4))
    if candidate_count < min_candidates:
        return {
            symbol: {
                "percentile": None,
                "reason": "skipped_few_candidates",
                "candidate_count": candidate_count,
                "ranked_count": 0,
                "method": "btc_eth_residual_momentum",
            }
            for symbol in symbols
        }
    values = []
    diagnostics = {}
    for symbol in symbols:
        score, detail = residual_momentum_score(symbol, rows_by_symbol, config)
        diagnostics[symbol] = detail
        if score is not None:
            values.append((symbol, score))
    if len(values) < min_candidates:
        return {
            symbol: {
                "percentile": None,
                "reason": diagnostics.get(symbol, {}).get("reason", "residual_data_insufficient"),
                "candidate_count": candidate_count,
                "ranked_count": len(values),
                **diagnostics.get(symbol, {}),
            }
            for symbol in symbols
        }
    values.sort(key=lambda item: item[1])
    denominator = max(len(values) - 1, 1)
    ranked = {}
    for rank, (symbol, score) in enumerate(values):
        ranked[symbol] = {
            "percentile": rank / denominator,
            "score": score,
            "candidate_count": candidate_count,
            "ranked_count": len(values),
            **diagnostics.get(symbol, {}),
        }
    return {
        symbol: ranked.get(
            symbol,
            {
                "percentile": None,
                "reason": diagnostics.get(symbol, {}).get("reason", "missing_rs_data"),
                "candidate_count": candidate_count,
                "ranked_count": len(values),
                **diagnostics.get(symbol, {}),
            },
        )
        for symbol in symbols
    }


def _ema(values, length):
    clean = [_finite(value, float("nan")) for value in values]
    clean = [value for value in clean if isfinite(value)]
    if not clean:
        return None
    alpha = 2.0 / (max(1, int(length or 1)) + 1.0)
    result = clean[0]
    for value in clean[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _true_ranges(rows):
    result = []
    for idx, row in enumerate(rows):
        high = _row_value(row, "high")
        low = _row_value(row, "low")
        previous_close = _row_value(rows[idx - 1], "close") if idx else _row_value(row, "close")
        result.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return result


def _atr(rows, length):
    ranges = _true_ranges(rows)
    if not ranges:
        return None
    window = ranges[-max(1, int(length or 14)):]
    return sum(window) / len(window)


def _previous_donchian(rows, length):
    length = max(1, int(length or 20))
    if len(rows) < length + 1:
        return None
    previous = rows[-length - 1:-1]
    return {
        "high": max(_row_value(row, "high") for row in previous),
        "low": min(_row_value(row, "low") for row in previous),
    }


def _recent_impulse(side, rows, config):
    minimum_offset = max(1, int(config.get("impulse_lookback_min_bars", 2) or 2))
    maximum_offset = max(minimum_offset, int(config.get("impulse_lookback_max_bars", 8) or 8))
    donchian_length = max(2, int(config.get("donchian_length", 20) or 20))
    atr_length = max(2, int(config.get("atr_length", 14) or 14))
    body_atr_min = max(0.0, _finite(config.get("impulse_body_atr_min"), 0.55))
    for offset in range(minimum_offset, maximum_offset + 1):
        index = len(rows) - 1 - offset
        if index <= donchian_length:
            continue
        history = rows[:index + 1]
        row = rows[index]
        atr = _atr(history, atr_length)
        channel = _previous_donchian(history, donchian_length)
        if not atr or atr <= 0 or not channel:
            continue
        open_ = _row_value(row, "open")
        close = _row_value(row, "close")
        body_atr = abs(close - open_) / atr
        if side == "long":
            breakout = close > channel["high"] and close > open_
            extreme = _row_value(row, "high")
            level = channel["high"]
        else:
            breakout = close < channel["low"] and close < open_
            extreme = _row_value(row, "low")
            level = channel["low"]
        if breakout and body_atr >= body_atr_min:
            return {
                "index": index,
                "offset": offset,
                "breakout_level": level,
                "impulse_extreme": extreme,
                "impulse_body_atr": body_atr,
                "impulse_atr": atr,
                "impulse_timestamp": _timestamp(row.get("timestamp") if isinstance(row, dict) else None),
            }
    return None


def evaluate_pullback_setup(side, rows, trend, atr, config):
    if len(rows) < 3 or not atr or atr <= 0:
        return False, {"reason": "v2_data_not_ready"}
    impulse = _recent_impulse(side, rows, config)
    if impulse is None:
        return False, {"reason": "prior_impulse_missing"}
    row = rows[-1]
    previous = rows[-2]
    close = _row_value(row, "close")
    open_ = _row_value(row, "open")
    high = _row_value(row, "high")
    low = _row_value(row, "low")
    candle_range = max(high - low, 0.0)
    body_atr = abs(close - open_) / atr
    ema_pullback = trend.get("signal_ema_pullback")
    if ema_pullback is None:
        ema_pullback = _ema([_row_value(item, "close") for item in rows], config.get("ema_pullback", 20))
    breakout_level = float(impulse["breakout_level"])
    tolerance = atr * max(0.0, _finite(config.get("pullback_tolerance_atr"), 0.25))
    minimum_depth = atr * max(0.0, _finite(config.get("pullback_depth_atr_min"), 0.40))
    maximum_depth = atr * max(0.0, _finite(config.get("pullback_depth_atr_max"), 1.20))
    minimum_body = max(0.0, _finite(config.get("pullback_body_atr_min"), 0.25))
    max_wick = max(0.0, min(1.0, _finite(config.get("pullback_max_wick_ratio"), 0.45)))
    segment = rows[impulse["index"] + 1:]

    if side == "long":
        structure_stop = min(_row_value(item, "low") for item in segment)
        depth = float(impulse["impulse_extreme"]) - structure_stop
        target = max(float(ema_pullback), breakout_level)
        near_target = low <= target + tolerance
        reclaimed = close > float(ema_pullback) and close > _row_value(previous, "high")
        wick_ratio = (high - max(open_, close)) / candle_range if candle_range > 0 else 0.0
        trend_intact = close > _finite(trend.get("signal_ema_trend"), close - 1.0)
    else:
        structure_stop = max(_row_value(item, "high") for item in segment)
        depth = structure_stop - float(impulse["impulse_extreme"])
        target = min(float(ema_pullback), breakout_level)
        near_target = high >= target - tolerance
        reclaimed = close < float(ema_pullback) and close < _row_value(previous, "low")
        wick_ratio = (min(open_, close) - low) / candle_range if candle_range > 0 else 0.0
        trend_intact = close < _finite(trend.get("signal_ema_trend"), close + 1.0)

    depth_valid = minimum_depth <= depth <= maximum_depth
    body_valid = body_atr >= minimum_body
    wick_valid = wick_ratio <= max_wick
    passed = all((near_target, reclaimed, depth_valid, body_valid, wick_valid, trend_intact))
    detail = {
        "reason": "v2_pullback_confirmed" if passed else "v2_pullback_not_confirmed",
        "prior_impulse_found": True,
        "impulse_offset_bars": impulse["offset"],
        "impulse_timestamp": impulse["impulse_timestamp"],
        "impulse_body_atr": impulse["impulse_body_atr"],
        "breakout_level": breakout_level,
        "pullback_target": target,
        "pullback_tolerance": tolerance,
        "pullback_depth_atr": depth / atr,
        "pullback_depth_valid": depth_valid,
        "pullback_near": near_target,
        "pullback_reclaim": reclaimed,
        "pullback_body_atr": body_atr,
        "pullback_body_valid": body_valid,
        "pullback_wick_ratio": wick_ratio,
        "pullback_wick_valid": wick_valid,
        "pullback_trend_intact": trend_intact,
        "pullback_structure_stop": structure_stop,
    }
    return passed, detail


def volatility_risk_multiplier(atr, close, config):
    if not atr or atr <= 0 or not close or close <= 0:
        return 1.0, "volatility_unknown", None
    atr_pct = atr / close * 100.0
    high = max(0.0, _finite(config.get("volatility_high_atr_pct"), 3.5))
    extreme = max(high, _finite(config.get("volatility_extreme_atr_pct"), 5.5))
    if atr_pct >= extreme:
        return (
            max(0.0, min(1.0, _finite(config.get("volatility_extreme_multiplier"), 0.35))),
            "volatility_extreme_size_reduced",
            atr_pct,
        )
    if atr_pct >= high:
        return (
            max(0.0, min(1.0, _finite(config.get("volatility_high_multiplier"), 0.70))),
            "volatility_high_size_reduced",
            atr_pct,
        )
    return 1.0, "volatility_normal", atr_pct
