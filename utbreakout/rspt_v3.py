"""Third-generation RSPT residual-strength helpers.

RSPT-v3 retains the v2 impulse/pullback/risk model, but expands residualization
from BTC/ETH alone to BTC, ETH, an equal-weight alt-market factor and a broad
cross-sectional volatility factor. Missing optional factors degrade gracefully
back to the available references.
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any, Mapping, Sequence

try:
    from .rspt_v2 import evaluate_pullback_setup, volatility_risk_multiplier
except ImportError:  # isolated pure-module tests
    def evaluate_pullback_setup(*args, **kwargs):
        raise RuntimeError("rspt_v2 is required for pullback evaluation")

    def volatility_risk_multiplier(*args, **kwargs):
        raise RuntimeError("rspt_v2 is required for volatility sizing")


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if isfinite(parsed) else float(default)


def _symbol_key(symbol: str) -> str:
    return "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())


def _find_symbol_rows(rows_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], symbol: str):
    if symbol in rows_by_symbol:
        return rows_by_symbol.get(symbol) or []
    wanted = _symbol_key(symbol)
    for key, rows in (rows_by_symbol or {}).items():
        if _symbol_key(key) == wanted:
            return rows or []
    return []


def _timestamp(value: Any) -> int:
    parsed = _finite(value, 0.0)
    if parsed <= 0:
        return 0
    return int(parsed * 1000.0 if parsed < 10_000_000_000 else parsed)


def _returns_by_timestamp(rows: Sequence[Mapping[str, Any]] | None) -> dict[int, float]:
    result: dict[int, float] = {}
    previous = None
    for row in rows or []:
        close = _finite((row or {}).get("close"), 0.0)
        ts = _timestamp((row or {}).get("timestamp"))
        if previous is not None and previous > 0 and close > 0 and ts > 0:
            result[ts] = close / previous - 1.0
        if close > 0:
            previous = close
    return result


def _solve_linear_system(matrix, vector):
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
    if not features or len(features) != len(targets):
        return None
    width = len(features[0])
    matrix = [[0.0] * width for _ in range(width)]
    vector = [0.0] * width
    for row, target in zip(features, targets):
        for left in range(width):
            vector[left] += row[left] * target
            for right in range(width):
                matrix[left][right] += row[left] * row[right]
    for idx in range(width):
        matrix[idx][idx] += max(0.0, float(ridge))
    return _solve_linear_system(matrix, vector)


def _std(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sqrt(max(0.0, sum((value - mean) ** 2 for value in values) / (len(values) - 1)))


def _cross_sectional_factors(
    target_symbol: str,
    rows_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    reference_symbols: Sequence[str],
    config: Mapping[str, Any],
) -> tuple[dict[int, float] | None, dict[int, float] | None, int]:
    target_key = _symbol_key(target_symbol)
    reference_keys = {_symbol_key(symbol) for symbol in reference_symbols}
    maps = []
    for symbol, rows in (rows_by_symbol or {}).items():
        key = _symbol_key(symbol)
        if key == target_key or key in reference_keys:
            continue
        values = _returns_by_timestamp(rows)
        if values:
            maps.append(values)
    minimum = max(2, int(config.get("alt_common_factor_min_symbols", 4) or 4))
    if len(maps) < minimum:
        return None, None, len(maps)
    timestamps = sorted(set().union(*(set(values) for values in maps)))
    alt_factor: dict[int, float] = {}
    vol_factor: dict[int, float] = {}
    for ts in timestamps:
        samples = [values[ts] for values in maps if ts in values]
        if len(samples) < minimum:
            continue
        alt_factor[ts] = sum(samples) / len(samples)
        vol_factor[ts] = sum(abs(value) for value in samples) / len(samples)
    return alt_factor or None, vol_factor or None, len(maps)


def residual_momentum_score(symbol, rows_by_symbol, config):
    target_returns = _returns_by_timestamp(_find_symbol_rows(rows_by_symbol, symbol))
    references = list(config.get("relative_strength_reference_symbols") or [])
    factor_maps = []
    factor_names = []
    used_references = []
    target_key = _symbol_key(symbol)
    for reference in references:
        if _symbol_key(reference) == target_key:
            continue
        values = _returns_by_timestamp(_find_symbol_rows(rows_by_symbol, reference))
        if values:
            factor_maps.append(values)
            factor_names.append(str(reference))
            used_references.append(reference)
    alt_factor, vol_factor, alt_symbol_count = _cross_sectional_factors(
        symbol, rows_by_symbol, references, config
    )
    if bool(config.get("alt_common_factor_enabled", True)) and alt_factor:
        factor_maps.append(alt_factor)
        factor_names.append("ALT_EQUAL_WEIGHT")
    if bool(config.get("market_volatility_factor_enabled", True)) and vol_factor:
        factor_maps.append(vol_factor)
        factor_names.append("ALT_CROSS_SECTION_VOL")
    if not target_returns or not factor_maps:
        return None, {"reason": "residual_benchmark_missing", "reference_symbols": used_references}

    timestamps = set(target_returns)
    for factor_map in factor_maps:
        timestamps &= set(factor_map)
    timestamps = sorted(timestamps)
    long_lb = max(2, int(config.get("relative_strength_long_lookback_bars", 84) or 84))
    short_lb = max(1, int(config.get("relative_strength_short_lookback_bars", 30) or 30))
    minimum = max(10, int(config.get("residual_strength_min_aligned_returns", 40) or 40), len(factor_maps) + 4)
    if len(timestamps) < minimum:
        return None, {
            "reason": "residual_data_insufficient",
            "aligned_returns": len(timestamps),
            "reference_symbols": used_references,
            "factor_names": factor_names,
        }
    timestamps = timestamps[-max(long_lb, minimum):]
    features = [[factor_map[ts] for factor_map in factor_maps] for ts in timestamps]
    targets = [target_returns[ts] for ts in timestamps]
    feature_means = [sum(row[idx] for row in features) / len(features) for idx in range(len(factor_maps))]
    target_mean = sum(targets) / len(targets)
    centered_features = [[value - feature_means[idx] for idx, value in enumerate(row)] for row in features]
    centered_targets = [target - target_mean for target in targets]
    coefficients = _ridge_regression(centered_features, centered_targets, _finite(config.get("residual_strength_ridge"), 1e-6))
    if coefficients is None:
        return None, {"reason": "residual_regression_failed", "factor_names": factor_names}
    residuals = [target - sum(coefficient * value for coefficient, value in zip(coefficients, row)) for row, target in zip(features, targets)]
    long_window = residuals[-min(long_lb, len(residuals)):]
    short_window = residuals[-min(short_lb, len(residuals)):]
    volatility = _std(long_window)
    factor_volatility = max((_std([factor_map[ts] for ts in timestamps]) for factor_map in factor_maps), default=0.0)
    scoring_volatility = max(volatility, factor_volatility * _finite(config.get("residual_strength_volatility_floor_ratio"), 0.50), 1e-12)
    long_component, short_component = sum(long_window), sum(short_window)
    score = (0.65 * long_component + 0.35 * short_component) / (scoring_volatility * sqrt(max(len(long_window), 1)))
    return score, {
        "reason": "ok",
        "method": "btc_eth_alt_vol_residual_momentum",
        "aligned_returns": len(timestamps),
        "reference_symbols": used_references,
        "factor_names": factor_names,
        "alt_factor_symbol_count": alt_symbol_count,
        "residual_volatility": volatility,
        "scoring_volatility": scoring_volatility,
        "factor_volatility": factor_volatility,
        "residual_long_return": long_component,
        "residual_short_return": short_component,
        "regression_coefficients": coefficients,
    }


def residual_strength_percentiles(symbols, rows_by_symbol, config):
    candidate_count = len(symbols)
    minimum = max(2, int(config.get("relative_strength_min_candidates", 4) or 4))
    if candidate_count < minimum:
        return {
            symbol: {
                "percentile": None,
                "reason": "skipped_few_candidates",
                "candidate_count": candidate_count,
                "ranked_count": 0,
                "method": "btc_eth_alt_vol_residual_momentum",
            }
            for symbol in symbols
        }
    values, diagnostics = [], {}
    for symbol in symbols:
        score, detail = residual_momentum_score(symbol, rows_by_symbol, config)
        diagnostics[symbol] = detail
        if score is not None:
            values.append((symbol, score))
    if len(values) < minimum:
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
    ranked = {
        symbol: {
            "percentile": rank / denominator,
            "score": score,
            "candidate_count": candidate_count,
            "ranked_count": len(values),
            **diagnostics.get(symbol, {}),
        }
        for rank, (symbol, score) in enumerate(values)
    }
    return {
        symbol: ranked.get(symbol, {
            "percentile": None,
            "reason": diagnostics.get(symbol, {}).get("reason", "missing_rs_data"),
            "candidate_count": candidate_count,
            "ranked_count": len(values),
            **diagnostics.get(symbol, {}),
        })
        for symbol in symbols
    }
