"""Adaptive multi-horizon trend signal.

The model deliberately keeps forecasting simple and interpretable.  Direction
comes from volatility-normalised time-series momentum across several horizons;
entries use either a recent fast/medium EMA crossover or a volatility-normalised
continuation inside the same established trend.  The continuation path keeps
the signal weighted (rather than requiring every horizon to agree), which lets
the portfolio participate after the first crossover without chasing a move
that is already far from its fast average.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite, log, sqrt
from statistics import median
from typing import Any, Mapping, Sequence


ADAPTIVE_BREAKOUT_TREND_STRATEGY = "adaptive_breakout_trend_v1"
ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION = "adaptive_trend_portfolio_v4_small_account_no_daily_loss"


def default_adaptive_breakout_trend_config() -> dict[str, Any]:
    """Broad defaults intended to remain stable across liquid crypto futures."""

    return {
        "profile_version": ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION,
        "enabled": True,
        "live_enabled": False,
        "universe_mode": "auto",
        "single_symbol": "",
        "timeframe": "1h",
        "fetch_limit": 360,
        "momentum_horizons": (24, 72, 168),
        "momentum_weights": (0.25, 0.35, 0.40),
        "minimum_horizon_agreement": 2,
        "minimum_momentum_strength": 0.18,
        "fast_ema_period": 12,
        "medium_ema_period": 48,
        "slow_ema_period": 144,
        "ema_slope_bars": 6,
        "ema_crossover_entry_enabled": True,
        "ema_crossover_window_bars": 3,
        "ema_crossover_minimum_momentum_strength": 0.0,
        # An early crossover may use a lower momentum floor than a mature
        # continuation, but never less than half of the broad momentum floor.
        # This ties the relaxation to the strategy profile instead of a single
        # hand-tuned absolute observation.
        "ema_crossover_momentum_floor_ratio": 0.50,
        "ema_crossover_minimum_trend_efficiency": 0.0,
        "continuation_entry_enabled": True,
        "continuation_minimum_momentum_strength": 0.26,
        "continuation_minimum_trend_efficiency": 0.18,
        "continuation_max_fast_ema_distance_atr": 1.10,
        "continuation_reacceleration_bars": 2,
        "breakout_entry_enabled": False,
        "channel_lookback_bars": 48,
        "reacceleration_lookback_bars": 12,
        "atr_period": 20,
        "volatility_short_bars": 24,
        "volatility_long_bars": 96,
        "target_hourly_volatility": 0.012,
        "volatility_targeting_power": 0.50,
        "volatility_risk_floor": 0.90,
        "volatility_risk_cap": 1.10,
        "volatility_shock_ratio": 3.00,
        "efficiency_lookback_bars": 48,
        "minimum_trend_efficiency": 0.16,
        "latest_range_max_atr": 2.80,
        "entry_chase_max_atr": 0.80,
        "structure_lookback_bars": 20,
        "structure_buffer_atr": 0.15,
        "stop_atr_multiplier": 2.00,
        "take_profit_r_multiple": 10.00,
        "score_min": 62.0,
        "base_risk_multiplier": 1.00,
        "strong_risk_multiplier": 1.00,
        "elite_risk_multiplier": 1.00,
        "strong_score": 76.0,
        "elite_score": 88.0,
        # Absolute account-risk targets for the standalone trend portfolio.
        # They are intentionally independent from the legacy aggregate 10%
        # setting so several soft sizing overlays cannot shrink the same edge
        # repeatedly.  Margin availability can still cap the submitted size.
        "base_risk_percent": 1.75,
        "base_risk_percent_min": 1.50,
        "base_risk_percent_max": 2.00,
        "strong_risk_percent": 3.00,
        "strong_risk_percent_min": 2.50,
        "strong_risk_percent_max": 3.50,
        "elite_risk_percent": 5.00,
        "elite_risk_percent_min": 4.00,
        "elite_risk_percent_max": 5.00,
        "daily_loss_limit_percent": 10.00,
        "initial_entry_fraction": 0.65,
        "pyramiding_enabled": True,
        "pyramid_trigger_r": (0.50, 1.00, 1.50),
        "pyramid_target_fractions": (0.80, 0.90, 1.00),
        # A separate sizing profile is selected only for a new standalone
        # trend entry when futures equity is strictly below $1,000.  The
        # normal 1.75/3/5% stop-budget model is replaced, not multiplied, so
        # the small-account allocation cannot be shrunk twice.
        "small_account_aggressive_enabled": True,
        "small_account_equity_threshold_usdt": 1_000.0,
        "small_account_margin_budget_fraction": 0.95,
        "small_account_initial_margin_fraction": 0.65,
        "small_account_base_max_loss_percent": 20.0,
        "small_account_strong_max_loss_percent": 30.0,
        "small_account_elite_max_loss_percent": 35.0,
        "small_account_daily_loss_limit_percent": 0.0,
        "small_account_cost_buffer_percent": 0.20,
        "small_account_liquidation_stop_buffer_multiple": 1.50,
        "small_account_min_leverage": 5,
        "small_account_strong_leverage": 8,
        "small_account_elite_leverage": 15,
        "small_account_leverage_steps": (5, 8, 10, 15),
        "partial_take_profit_r_multiple": 2.00,
        "partial_take_profit_ratio": 0.15,
        "runner_pct": 0.85,
        "atr_trailing_activation_r": 2.00,
        "atr_trailing_multiplier": 3.80,
        "time_stop_hours": 168,
    }


def normalize_adaptive_breakout_trend_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable trend configuration, including its scan universe."""

    defaults = default_adaptive_breakout_trend_config()
    normalized = dict(defaults)
    supplied = dict(config) if isinstance(config, Mapping) else {}
    normalized.update(supplied)
    if supplied and supplied.get("profile_version") != ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION:
        # Migrate the persisted conservative v1 profile. Operational choices
        # such as universe and symbol are preserved; the requested strategy,
        # sizing and exit policy move together so an old server config cannot
        # silently keep the former 0.8x risk and 60% runner.
        profile_keys = (
            "continuation_entry_enabled",
            "continuation_minimum_momentum_strength",
            "continuation_minimum_trend_efficiency",
            "continuation_max_fast_ema_distance_atr",
            "continuation_reacceleration_bars",
            "volatility_risk_floor",
            "volatility_risk_cap",
            "take_profit_r_multiple",
            "base_risk_multiplier",
            "strong_risk_multiplier",
            "elite_risk_multiplier",
            "base_risk_percent",
            "base_risk_percent_min",
            "base_risk_percent_max",
            "strong_risk_percent",
            "strong_risk_percent_min",
            "strong_risk_percent_max",
            "elite_risk_percent",
            "elite_risk_percent_min",
            "elite_risk_percent_max",
            "daily_loss_limit_percent",
            "initial_entry_fraction",
            "pyramiding_enabled",
            "pyramid_trigger_r",
            "pyramid_target_fractions",
            "small_account_aggressive_enabled",
            "small_account_equity_threshold_usdt",
            "small_account_margin_budget_fraction",
            "small_account_initial_margin_fraction",
            "small_account_base_max_loss_percent",
            "small_account_strong_max_loss_percent",
            "small_account_elite_max_loss_percent",
            "small_account_daily_loss_limit_percent",
            "small_account_cost_buffer_percent",
            "small_account_liquidation_stop_buffer_multiple",
            "small_account_min_leverage",
            "small_account_strong_leverage",
            "small_account_elite_leverage",
            "small_account_leverage_steps",
            "partial_take_profit_r_multiple",
            "partial_take_profit_ratio",
            "runner_pct",
            "atr_trailing_activation_r",
            "atr_trailing_multiplier",
        )
        for key in profile_keys:
            normalized[key] = defaults[key]
    normalized["profile_version"] = ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION

    universe_mode = str(
        normalized.get("universe_mode", "auto") or "auto"
    ).strip().lower()
    if universe_mode not in {"auto", "single"}:
        universe_mode = "auto"
    single_symbol = str(normalized.get("single_symbol", "") or "").strip().upper()
    normalized["universe_mode"] = universe_mode
    normalized["single_symbol"] = single_symbol
    broad_momentum_floor = _bounded(
        _finite(
            normalized.get("minimum_momentum_strength"),
            defaults["minimum_momentum_strength"],
        ),
        0.0,
        1.0,
    )
    crossover_floor_ratio = _bounded(
        _finite(
            normalized.get("ema_crossover_momentum_floor_ratio"),
            defaults["ema_crossover_momentum_floor_ratio"],
        ),
        0.50,
        1.0,
    )
    configured_crossover_floor = _bounded(
        _finite(
            normalized.get("ema_crossover_minimum_momentum_strength"),
            defaults["ema_crossover_minimum_momentum_strength"],
        ),
        0.0,
        1.0,
    )
    normalized["minimum_momentum_strength"] = broad_momentum_floor
    normalized["ema_crossover_momentum_floor_ratio"] = crossover_floor_ratio
    normalized["ema_crossover_minimum_momentum_strength"] = max(
        configured_crossover_floor,
        broad_momentum_floor * crossover_floor_ratio,
    )
    normalized["initial_entry_fraction"] = _bounded(
        _finite(normalized.get("initial_entry_fraction"), defaults["initial_entry_fraction"]),
        0.40,
        1.00,
    )
    try:
        triggers = tuple(float(value) for value in normalized.get("pyramid_trigger_r", ()))
        targets = tuple(float(value) for value in normalized.get("pyramid_target_fractions", ()))
    except (TypeError, ValueError):
        triggers, targets = (), ()
    if not triggers or len(triggers) != len(targets):
        triggers = tuple(defaults["pyramid_trigger_r"])
        targets = tuple(defaults["pyramid_target_fractions"])
    ordered_stages = sorted(
        (
            max(0.10, trigger),
            _bounded(target, normalized["initial_entry_fraction"], 1.00),
        )
        for trigger, target in zip(triggers, targets)
    )
    monotonic_targets: list[float] = []
    target_floor = normalized["initial_entry_fraction"]
    for _, target in ordered_stages:
        target_floor = max(target_floor, target)
        monotonic_targets.append(target_floor)
    normalized["pyramid_trigger_r"] = tuple(stage[0] for stage in ordered_stages)
    normalized["pyramid_target_fractions"] = tuple(monotonic_targets)
    small_enabled = normalized.get("small_account_aggressive_enabled", True)
    normalized["small_account_aggressive_enabled"] = (
        small_enabled
        if isinstance(small_enabled, bool)
        else str(small_enabled).strip().lower() in {"1", "true", "yes", "on", "enabled"}
    )
    normalized["small_account_equity_threshold_usdt"] = max(
        0.0,
        float(
            _finite(
                normalized.get("small_account_equity_threshold_usdt"),
                defaults["small_account_equity_threshold_usdt"],
            )
        ),
    )
    normalized["small_account_margin_budget_fraction"] = _bounded(
        _finite(
            normalized.get("small_account_margin_budget_fraction"),
            defaults["small_account_margin_budget_fraction"],
        ),
        0.50,
        0.98,
    )
    normalized["small_account_initial_margin_fraction"] = _bounded(
        _finite(
            normalized.get("small_account_initial_margin_fraction"),
            defaults["small_account_initial_margin_fraction"],
        ),
        0.40,
        1.00,
    )
    previous_loss_cap = 0.0
    for tier in ("base", "strong", "elite"):
        key = f"small_account_{tier}_max_loss_percent"
        value = _bounded(
            _finite(normalized.get(key), defaults[key]),
            previous_loss_cap,
            50.0,
        )
        normalized[key] = value
        previous_loss_cap = value
    normalized["small_account_daily_loss_limit_percent"] = _bounded(
        _finite(
            normalized.get("small_account_daily_loss_limit_percent"),
            defaults["small_account_daily_loss_limit_percent"],
        ),
        0.0,
        50.0,
    )
    normalized["small_account_cost_buffer_percent"] = _bounded(
        _finite(
            normalized.get("small_account_cost_buffer_percent"),
            defaults["small_account_cost_buffer_percent"],
        ),
        0.0,
        2.0,
    )
    normalized["small_account_liquidation_stop_buffer_multiple"] = _bounded(
        _finite(
            normalized.get("small_account_liquidation_stop_buffer_multiple"),
            defaults["small_account_liquidation_stop_buffer_multiple"],
        ),
        1.0,
        3.0,
    )
    minimum_leverage = int(
        _bounded(
            _finite(
                normalized.get("small_account_min_leverage"),
                defaults["small_account_min_leverage"],
            ),
            1.0,
            20.0,
        )
    )
    strong_leverage = int(
        _bounded(
            _finite(
                normalized.get("small_account_strong_leverage"),
                defaults["small_account_strong_leverage"],
            ),
            float(minimum_leverage),
            20.0,
        )
    )
    elite_leverage = int(
        _bounded(
            _finite(
                normalized.get("small_account_elite_leverage"),
                defaults["small_account_elite_leverage"],
            ),
            float(strong_leverage),
            20.0,
        )
    )
    normalized["small_account_min_leverage"] = minimum_leverage
    normalized["small_account_strong_leverage"] = strong_leverage
    normalized["small_account_elite_leverage"] = elite_leverage
    try:
        configured_steps = {
            int(float(value))
            for value in normalized.get("small_account_leverage_steps", ())
            if minimum_leverage <= int(float(value)) <= elite_leverage
        }
    except (TypeError, ValueError):
        configured_steps = set()
    configured_steps.update({minimum_leverage, strong_leverage, elite_leverage})
    normalized["small_account_leverage_steps"] = tuple(sorted(configured_steps))
    for tier in ("base", "strong", "elite"):
        floor_key = f"{tier}_risk_percent_min"
        cap_key = f"{tier}_risk_percent_max"
        target_key = f"{tier}_risk_percent"
        floor_value = max(0.0, float(_finite(normalized.get(floor_key), defaults[floor_key])))
        cap_value = max(floor_value, float(_finite(normalized.get(cap_key), defaults[cap_key])))
        normalized[floor_key] = floor_value
        normalized[cap_key] = cap_value
        normalized[target_key] = _bounded(
            _finite(normalized.get(target_key), defaults[target_key]),
            floor_value,
            cap_value,
        )
    return normalized


@dataclass(frozen=True)
class AdaptiveBreakoutTrendDecision:
    allowed: bool = False
    side: str | None = None
    score: float = 0.0
    risk_multiplier: float = 0.0
    reason: str = "waiting"
    metrics: dict[str, Any] = field(default_factory=dict)


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def _clean_rows(rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for row in rows or ():
        close = _finite(row.get("close"))
        high = _finite(row.get("high"))
        low = _finite(row.get("low"))
        open_price = _finite(row.get("open"), close)
        volume = _finite(row.get("volume"), 0.0)
        if close is None or high is None or low is None or close <= 0:
            continue
        if high < low or high <= 0 or low <= 0:
            continue
        cleaned.append({
            "timestamp": row.get("timestamp"),
            "open": open_price if open_price is not None else close,
            "high": high,
            "low": low,
            "close": close,
            "volume": max(0.0, volume or 0.0),
        })
    return cleaned


def _ema(values: Sequence[float], period: int) -> list[float]:
    period = max(2, int(period))
    alpha = 2.0 / (period + 1.0)
    result: list[float] = []
    current = float(values[0])
    for value in values:
        current = alpha * float(value) + (1.0 - alpha) * current
        result.append(current)
    return result


def _atr(rows: Sequence[Mapping[str, Any]], period: int) -> float | None:
    if len(rows) < period + 1:
        return None
    ranges: list[float] = []
    for index in range(1, len(rows)):
        high = float(rows[index]["high"])
        low = float(rows[index]["low"])
        previous_close = float(rows[index - 1]["close"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    window = ranges[-max(2, int(period)):]
    return sum(window) / len(window) if window else None


def _log_returns(closes: Sequence[float]) -> list[float]:
    return [log(float(closes[i]) / float(closes[i - 1])) for i in range(1, len(closes))]


def _rms(values: Sequence[float]) -> float:
    return sqrt(sum(float(value) ** 2 for value in values) / len(values)) if values else 0.0


def _bounded(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _normalized_momentum_horizons(
    horizons_value: Any,
    weights_value: Any,
) -> tuple[tuple[int, ...], tuple[float, ...]]:
    """Normalize horizon/weight pairs without changing their configured mapping."""

    default_cfg = default_adaptive_breakout_trend_config()
    try:
        raw_horizons = tuple(horizons_value)
    except TypeError:
        raw_horizons = ()
    try:
        parsed_horizons = tuple(max(4, int(value)) for value in raw_horizons)
    except (TypeError, ValueError):
        parsed_horizons = ()
    if not parsed_horizons:
        parsed_horizons = tuple(
            max(4, int(value)) for value in default_cfg['momentum_horizons']
        )

    try:
        raw_weights = tuple(float(value) for value in weights_value)
    except (TypeError, ValueError):
        raw_weights = ()
    if (
        len(raw_weights) != len(parsed_horizons)
        or sum(max(0.0, value) for value in raw_weights) <= 0
    ):
        raw_weights = tuple(1.0 for _ in parsed_horizons)

    combined: dict[int, float] = {}
    for horizon, weight in zip(parsed_horizons, raw_weights):
        combined[horizon] = combined.get(horizon, 0.0) + max(0.0, weight)
    ordered = tuple(sorted(combined.items()))
    weight_sum = sum(weight for _, weight in ordered)
    if weight_sum <= 0:
        ordered = tuple((horizon, 1.0) for horizon, _ in ordered)
        weight_sum = float(len(ordered))
    return (
        tuple(horizon for horizon, _ in ordered),
        tuple(weight / weight_sum for _, weight in ordered),
    )


def evaluate_adaptive_breakout_trend(
    rows: Sequence[Mapping[str, Any]] | None,
    l2_gate: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> AdaptiveBreakoutTrendDecision:
    """Evaluate completed candles without placing or modifying an order."""

    cfg = normalize_adaptive_breakout_trend_config(config)
    candles = _clean_rows(rows)
    horizons, weights = _normalized_momentum_horizons(
        cfg.get("momentum_horizons"),
        cfg.get("momentum_weights"),
    )
    breakout_entry_enabled = bool(cfg.get("breakout_entry_enabled", False))
    required_values = [
        max(horizons) + 2,
        int(cfg["slow_ema_period"]) + int(cfg["ema_slope_bars"]) + 2,
        int(cfg["volatility_long_bars"]) + 2,
    ]
    if breakout_entry_enabled:
        required_values.append(int(cfg["channel_lookback_bars"]) + 2)
    required = max(required_values)
    if len(candles) < required:
        return AdaptiveBreakoutTrendDecision(reason="insufficient_completed_candles")

    closes = [float(row["close"]) for row in candles]
    returns = _log_returns(closes)
    atr_value = _atr(candles, max(2, int(cfg["atr_period"])))
    if atr_value is None or atr_value <= 0:
        return AdaptiveBreakoutTrendDecision(reason="atr_unavailable")

    short_window = returns[-max(4, int(cfg["volatility_short_bars"])):]
    long_window = returns[-max(8, int(cfg["volatility_long_bars"])):]
    short_vol = _rms(short_window)
    long_vol = _rms(long_window)
    if short_vol <= 0 or long_vol <= 0:
        return AdaptiveBreakoutTrendDecision(reason="realized_volatility_unavailable")

    horizon_scores: dict[int, float] = {}
    horizon_votes: dict[int, str | None] = {}
    weighted_momentum = 0.0
    for horizon, weight in zip(horizons, weights):
        raw_return = log(closes[-1] / closes[-1 - horizon])
        normalized = raw_return / max(long_vol * sqrt(float(horizon)), 1e-9)
        clipped = _bounded(normalized / 2.0, -1.0, 1.0)
        horizon_scores[horizon] = normalized
        weighted_momentum += clipped * weight
        horizon_votes[horizon] = "long" if normalized > 0.10 else "short" if normalized < -0.10 else None

    long_votes = sum(value == "long" for value in horizon_votes.values())
    short_votes = sum(value == "short" for value in horizon_votes.values())
    minimum_votes = max(2, min(len(horizons), int(cfg["minimum_horizon_agreement"])))
    side = "long" if weighted_momentum > 0 else "short" if weighted_momentum < 0 else None
    dominant_votes = long_votes if side == "long" else short_votes if side == "short" else 0

    fast_ema = _ema(closes, int(cfg["fast_ema_period"]))
    medium_ema = _ema(closes, int(cfg["medium_ema_period"]))
    slow_ema = _ema(closes, int(cfg["slow_ema_period"]))
    slope_bars = max(1, int(cfg["ema_slope_bars"]))
    medium_slope_atr = (medium_ema[-1] - medium_ema[-1 - slope_bars]) / atr_value
    ema_aligned = bool(
        side == "long"
        and closes[-1] > medium_ema[-1] > slow_ema[-1]
        and medium_slope_atr > 0
    ) or bool(
        side == "short"
        and closes[-1] < medium_ema[-1] < slow_ema[-1]
        and medium_slope_atr < 0
    )

    crossover_window = max(1, int(cfg.get("ema_crossover_window_bars", 3)))
    crossover_window = min(crossover_window, len(closes) - 1)
    ema_crossover_side: str | None = None
    ema_crossover_age_bars: int | None = None
    ema_crossover_index: int | None = None
    for age in range(crossover_window):
        index = len(closes) - 1 - age
        if fast_ema[index] > medium_ema[index] and fast_ema[index - 1] <= medium_ema[index - 1]:
            ema_crossover_side = "long"
        elif fast_ema[index] < medium_ema[index] and fast_ema[index - 1] >= medium_ema[index - 1]:
            ema_crossover_side = "short"
        else:
            continue
        ema_crossover_age_bars = age
        ema_crossover_index = index
        break
    ema_crossover = bool(
        cfg.get("ema_crossover_entry_enabled", True)
        and ema_crossover_side == side
        and (
            (side == "long" and fast_ema[-1] > medium_ema[-1])
            or (side == "short" and fast_ema[-1] < medium_ema[-1])
        )
    )

    channel_high: float | None = None
    channel_low: float | None = None
    fresh_breakout = False
    reacceleration = False
    if breakout_entry_enabled:
        channel_period = max(8, int(cfg["channel_lookback_bars"]))
        reacceleration_period = max(4, int(cfg["reacceleration_lookback_bars"]))
        previous_channel = candles[-channel_period - 1:-1]
        previous_reacceleration = candles[-reacceleration_period - 1:-1]
        channel_high = max(float(row["high"]) for row in previous_channel)
        channel_low = min(float(row["low"]) for row in previous_channel)
        reacceleration_high = max(float(row["high"]) for row in previous_reacceleration)
        reacceleration_low = min(float(row["low"]) for row in previous_reacceleration)
        fresh_breakout = bool(
            side == "long" and closes[-1] > channel_high
        ) or bool(
            side == "short" and closes[-1] < channel_low
        )
        reacceleration = bool(
            side == "long"
            and closes[-1] > reacceleration_high
            and fast_ema[-1] > medium_ema[-1]
        ) or bool(
            side == "short"
            and closes[-1] < reacceleration_low
            and fast_ema[-1] < medium_ema[-1]
        )

    efficiency_period = max(8, int(cfg["efficiency_lookback_bars"]))
    efficiency_closes = closes[-efficiency_period - 1:]
    path = sum(abs(efficiency_closes[i] - efficiency_closes[i - 1]) for i in range(1, len(efficiency_closes)))
    efficiency = abs(efficiency_closes[-1] - efficiency_closes[0]) / max(path, 1e-9)
    latest_range_atr = (
        float(candles[-1]["high"]) - float(candles[-1]["low"])
    ) / atr_value
    volatility_ratio = short_vol / max(long_vol, 1e-9)
    fast_vote = horizon_votes[min(horizons)]
    slow_vote = horizon_votes[max(horizons)]
    turning_conflict = bool(
        side in {"long", "short"}
        and slow_vote == side
        and fast_vote not in {None, side}
        and not (breakout_entry_enabled and fresh_breakout)
    )
    continuation_bars = max(
        1,
        min(
            len(closes) - 1,
            int(cfg.get("continuation_reacceleration_bars", 2) or 2),
        ),
    )
    signed_fast_ema_distance_atr = (
        (closes[-1] - fast_ema[-1]) / atr_value
        if side == "long"
        else (fast_ema[-1] - closes[-1]) / atr_value
        if side == "short"
        else 0.0
    )
    continuation_reacceleration = bool(
        side == "long"
        and closes[-1] > closes[-1 - continuation_bars]
        and fast_ema[-1] > fast_ema[-1 - continuation_bars]
    ) or bool(
        side == "short"
        and closes[-1] < closes[-1 - continuation_bars]
        and fast_ema[-1] < fast_ema[-1 - continuation_bars]
    )
    weighted_continuation = bool(
        cfg.get("continuation_entry_enabled", True)
        and ema_aligned
        and dominant_votes >= minimum_votes
        and abs(weighted_momentum)
        >= float(cfg.get("continuation_minimum_momentum_strength", 0.26) or 0.26)
        and efficiency
        >= float(cfg.get("continuation_minimum_trend_efficiency", 0.18) or 0.18)
        and 0.0 <= signed_fast_ema_distance_atr
        <= float(cfg.get("continuation_max_fast_ema_distance_atr", 1.10) or 1.10)
        and continuation_reacceleration
    )

    volumes = [float(row.get("volume") or 0.0) for row in candles]
    baseline_volume = median(volumes[-49:-1]) if len(volumes) >= 49 else median(volumes[:-1])
    volume_ratio = volumes[-1] / max(baseline_volume, 1e-9) if baseline_volume > 0 else 1.0
    structure_window = candles[-max(4, int(cfg["structure_lookback_bars"])) - 1:-1]
    structure_stop = (
        min(float(row["low"]) for row in structure_window)
        if side == "long"
        else max(float(row["high"]) for row in structure_window)
        if side == "short"
        else None
    )

    metrics = {
        "reference_price": closes[-1],
        "signal_candle_ts": candles[-1].get("timestamp"),
        "atr": atr_value,
        "short_volatility": short_vol,
        "long_volatility": long_vol,
        "volatility_ratio": volatility_ratio,
        "weighted_momentum": weighted_momentum,
        "horizon_scores": horizon_scores,
        "horizon_votes": horizon_votes,
        "long_votes": long_votes,
        "short_votes": short_votes,
        "fast_ema": fast_ema[-1],
        "medium_ema": medium_ema[-1],
        "slow_ema": slow_ema[-1],
        "medium_ema_slope_atr": medium_slope_atr,
        "ema_aligned": ema_aligned,
        "ema_crossover": ema_crossover,
        "ema_crossover_side": ema_crossover_side,
        "ema_crossover_age_bars": ema_crossover_age_bars,
        "breakout_entry_enabled": breakout_entry_enabled,
        "channel_high": channel_high,
        "channel_low": channel_low,
        "fresh_breakout": fresh_breakout,
        "reacceleration": reacceleration,
        "turning_conflict": turning_conflict,
        "weighted_continuation": weighted_continuation,
        "continuation_reacceleration": continuation_reacceleration,
        "signed_fast_ema_distance_atr": signed_fast_ema_distance_atr,
        "trend_efficiency": efficiency,
        "latest_range_atr": latest_range_atr,
        "volume_ratio": volume_ratio,
        "structure_stop": structure_stop,
    }

    if side is None or dominant_votes < minimum_votes:
        return AdaptiveBreakoutTrendDecision(side=side, reason="multi_horizon_direction_not_aligned", metrics=metrics)
    minimum_momentum_strength = (
        float(cfg["ema_crossover_minimum_momentum_strength"])
        if ema_crossover
        else float(cfg.get("continuation_minimum_momentum_strength", 0.26))
        if weighted_continuation
        else float(cfg["minimum_momentum_strength"])
    )
    metrics["minimum_momentum_strength_required"] = minimum_momentum_strength
    if abs(weighted_momentum) < minimum_momentum_strength:
        return AdaptiveBreakoutTrendDecision(side=side, reason="momentum_strength_too_low", metrics=metrics)
    if slow_vote not in {None, side}:
        return AdaptiveBreakoutTrendDecision(side=side, reason="slow_horizon_conflict", metrics=metrics)
    if not ema_aligned:
        return AdaptiveBreakoutTrendDecision(side=side, reason="trend_structure_not_aligned", metrics=metrics)
    if turning_conflict:
        return AdaptiveBreakoutTrendDecision(side=side, reason="fast_slow_turning_conflict", metrics=metrics)
    breakout_entry = bool(
        breakout_entry_enabled and (fresh_breakout or reacceleration)
    )
    if not ema_crossover and not breakout_entry and not weighted_continuation:
        return AdaptiveBreakoutTrendDecision(side=side, reason="waiting_for_weighted_trend_entry", metrics=metrics)
    minimum_efficiency = (
        float(cfg.get("ema_crossover_minimum_trend_efficiency", 0.0) or 0.0)
        if ema_crossover
        else float(cfg.get("continuation_minimum_trend_efficiency", 0.18) or 0.18)
        if weighted_continuation
        else float(cfg["minimum_trend_efficiency"])
    )
    if efficiency < minimum_efficiency:
        return AdaptiveBreakoutTrendDecision(side=side, reason="trend_efficiency_too_low", metrics=metrics)
    if volatility_ratio > float(cfg["volatility_shock_ratio"]):
        return AdaptiveBreakoutTrendDecision(side=side, reason="volatility_shock", metrics=metrics)
    if latest_range_atr > float(cfg["latest_range_max_atr"]):
        return AdaptiveBreakoutTrendDecision(side=side, reason="latest_bar_extreme_range", metrics=metrics)
    if l2_gate is not None and not bool(l2_gate.get("allowed", False)):
        return AdaptiveBreakoutTrendDecision(side=side, reason="l2_stressed", metrics=metrics)

    score = 44.0
    score += min(18.0, abs(weighted_momentum) * 24.0)
    score += min(12.0, dominant_votes * 4.0)
    score += 7.0 if ema_crossover else 8.0 if fresh_breakout else 6.0 if weighted_continuation else 5.0
    score += min(10.0, efficiency * 22.0)
    score += min(4.0, max(0.0, volume_ratio - 0.70) * 3.0)
    if slow_vote == side:
        score += 4.0
    score = min(100.0, score)
    metrics["score"] = score
    if score < float(cfg["score_min"]):
        return AdaptiveBreakoutTrendDecision(side=side, score=score, reason="score_below_threshold", metrics=metrics)

    target_vol = max(1e-6, float(cfg["target_hourly_volatility"]))
    targeting_power = _bounded(float(cfg["volatility_targeting_power"]), 0.0, 1.0)
    raw_volatility_scale = (target_vol / max(short_vol, long_vol, 1e-9)) ** targeting_power
    volatility_scale = _bounded(
        raw_volatility_scale,
        float(cfg["volatility_risk_floor"]),
        float(cfg["volatility_risk_cap"]),
    )
    if score >= float(cfg["elite_score"]):
        quality_risk = float(cfg["elite_risk_multiplier"])
        risk_tier = "elite"
    elif score >= float(cfg["strong_score"]):
        quality_risk = float(cfg["strong_risk_multiplier"])
        risk_tier = "strong"
    else:
        quality_risk = float(cfg["base_risk_multiplier"])
        risk_tier = "base"
    l2_multiplier = float((l2_gate or {}).get("risk_multiplier", 1.0) or 0.0)
    risk_multiplier = _bounded(quality_risk * volatility_scale, 0.0, 1.0)
    if l2_gate is not None:
        risk_multiplier = min(risk_multiplier, max(0.0, l2_multiplier))
    target_risk_percent = _bounded(
        float(cfg[f"{risk_tier}_risk_percent"]) * volatility_scale,
        float(cfg[f"{risk_tier}_risk_percent_min"]),
        float(cfg[f"{risk_tier}_risk_percent_max"]),
    )
    metrics.update({
        "raw_volatility_scale": raw_volatility_scale,
        "volatility_scale": volatility_scale,
        "quality_risk_multiplier": quality_risk,
        "risk_tier": risk_tier,
        "risk_multiplier": risk_multiplier,
        "target_risk_percent": target_risk_percent,
    })
    if ema_crossover and ema_crossover_index is not None:
        metrics["reference_price"] = closes[ema_crossover_index]
        metrics["signal_candle_ts"] = candles[ema_crossover_index].get("timestamp")
    mode = (
        "EMA crossover"
        if ema_crossover
        else "fresh breakout"
        if fresh_breakout
        else "weighted continuation"
        if weighted_continuation
        else "trend re-acceleration"
    )
    return AdaptiveBreakoutTrendDecision(
        allowed=True,
        side=side,
        score=score,
        risk_multiplier=risk_multiplier,
        reason=(
            f"Adaptive Breakout Trend {side} {mode}: score={score:.1f} "
            f"momentum={weighted_momentum:+.2f} votes={dominant_votes}/{len(horizons)} "
            f"risk={target_risk_percent:.2f}% ({risk_tier})"
        ),
        metrics=metrics,
    )


__all__ = (
    "ADAPTIVE_BREAKOUT_TREND_STRATEGY",
    "ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION",
    "AdaptiveBreakoutTrendDecision",
    "default_adaptive_breakout_trend_config",
    "evaluate_adaptive_breakout_trend",
    "normalize_adaptive_breakout_trend_config",
)
