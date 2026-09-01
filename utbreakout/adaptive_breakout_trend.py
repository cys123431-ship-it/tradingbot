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

from .change_point_flow import (
    default_change_point_flow_config,
    normalize_change_point_flow_config,
)
from .small_account_regime import (
    default_small_account_regime_config,
    normalize_small_account_regime_config,
)


ADAPTIVE_BREAKOUT_TREND_STRATEGY = "adaptive_breakout_trend_v1"
ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION = (
    "adaptive_trend_portfolio_v17_progress_failure_profit_bank"
)


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
        "breakout_entry_enabled": True,
        "channel_lookback_bars": 48,
        "reacceleration_lookback_bars": 12,
        # A Donchian break is actionable only when it expands out of a quiet
        # base with participation.  This opens a second entry path without
        # turning every mature-trend high into a chase entry.
        "compression_breakout_enabled": True,
        "compression_window_bars": 12,
        "compression_lookback_bars": 96,
        "compression_max_range_ratio": 0.72,
        "compression_min_volume_ratio": 1.10,
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
        # normal 1.75/3/5% stop-budget model is replaced, not multiplied.
        # These campaign caps remain aggressive, while quantity now shrinks
        # as the selected ATR/structure stop gets wider.  At the cap, the
        # first 65% stage exposes about 5.2/7.8/10.4%, respectively.
        "small_account_aggressive_enabled": True,
        # The aggressive sub-$1,000 profile is long-only. This gates both new
        # SHORT entries and winner-only SHORT additions without changing the
        # protective management of an already-open position.
        "small_account_short_entries_enabled": False,
        "small_account_equity_threshold_usdt": 1_000.0,
        "small_account_margin_budget_fraction": 0.95,
        "small_account_initial_margin_fraction": 0.65,
        "small_account_base_max_loss_percent": 8.0,
        "small_account_strong_max_loss_percent": 12.0,
        "small_account_elite_max_loss_percent": 16.0,
        # Keep the operator-selected 8/12/16% campaign ceilings. For a new
        # position only, unstable multi-timeframe evidence or a volatility
        # shock may scale both available margin and the loss budget down once.
        # This changes quantity, never the ATR/structure stop price.
        "small_account_stability_risk_enabled": True,
        "small_account_stability_risk_floor": 0.80,
        # After a profitable day, keep trading but protect half of the banked
        # profit by scaling the next campaign's risk budget once. This is not
        # a daily halt and never changes an existing position.
        "small_account_profit_bank_enabled": True,
        "small_account_profit_bank_activation_multiple": 0.75,
        "small_account_profit_bank_protect_fraction": 0.50,
        "small_account_profit_bank_min_risk_scale": 0.50,
        "small_account_daily_loss_limit_percent": 0.0,
        "small_account_cost_buffer_percent": 0.20,
        "small_account_liquidation_stop_buffer_multiple": 1.50,
        # New sub-$1,000 positions remain opportunity-scaled, but the live
        # loss sample no longer justifies 8-15x initial leverage. Event-only,
        # aligned and elite setups now map to a bounded 4-7x ladder.
        "small_account_min_leverage": 4,
        "small_account_strong_leverage": 6,
        "small_account_elite_leverage": 7,
        "small_account_leverage_steps": (4, 5, 6, 7),
        # Protect observed mark-price ROE in rising steps. The requested
        # 5/10/20/30... staircase is retained, while half an ATR determines a
        # 1-3 percentage-point giveback so normal crypto noise is not treated
        # as a new trend reversal.
        "small_account_roe_profit_lock_enabled": True,
        "small_account_roe_profit_lock_first_trigger_percent": 5.0,
        "small_account_roe_profit_lock_second_trigger_percent": 10.0,
        "small_account_roe_profit_lock_step_percent": 10.0,
        "small_account_roe_profit_lock_atr_multiplier": 0.50,
        "small_account_roe_profit_lock_min_gap_percent": 1.0,
        "small_account_roe_profit_lock_max_gap_percent": 3.0,
        "small_account_roe_profit_lock_min_floor_percent": 1.0,
        # Close a new small-account trade only when exchange mark-price
        # progress was real, that progress has fully reversed, and at least
        # two completed-candle failure checks agree. The structural exchange
        # stop remains live until a reduce-only close is confirmed flat.
        "small_account_progress_failure_exit_enabled": True,
        "small_account_progress_failure_min_mark_mfe_r": 0.20,
        "small_account_progress_failure_max_mark_mfe_r": 0.75,
        "small_account_progress_failure_max_current_r": -0.10,
        "small_account_progress_failure_min_closed_bars": 2,
        "small_account_progress_failure_confirmations": 2,
        # The small-account profile keeps its aggressive capital allocation,
        # but refuses a stale continuation when the fast trend sleeve has
        # already decayed relative to the medium/slow sleeves.  This is a
        # failure veto, not another all-signals-must-agree entry rule.
        "small_account_entry_refinement_enabled": True,
        "small_account_min_fast_momentum_retention": 0.55,
        "small_account_max_adverse_signal_move_atr": 0.80,
        "small_account_crossover_max_fast_ema_distance_atr": 2.00,
        # A fast event may lead the slower trend sleeve, but it must not use
        # that privilege to chase a move already extended from the 1h fast
        # EMA.  The limit intentionally matches the existing impulse-entry
        # geometry instead of being fitted to a handful of live outcomes.
        "small_account_event_only_max_fast_ema_distance_atr": 1.50,
        # Fast order-flow/regime entries remain an independent OR path, but a
        # moderate established multi-speed drift in the opposite direction is
        # a genuine regime conflict rather than a missing confirmation.  Weak
        # (<0.20) broad momentum still permits an early reversal.
        "small_account_event_only_broad_conflict_min_momentum": 0.20,
        # Event strength is an entry-timing input, not a calibrated capital
        # confidence score.  Higher initial tiers require alignment with the
        # independent multi-horizon trend; event-only positions can still
        # reach full target size through winner-only pyramiding.
        "small_account_event_only_risk_tier_cap": "base",
        "small_account_lower_timeframe_conflict_veto_enabled": True,
        "small_account_lower_timeframe_conflict_min_alignment": 60.0,
        "small_account_lower_timeframe_conflict_min_ready_timeframes": 2,
        # A mature drift needs more absolute impulse than a fresh pullback
        # resumption or volume-backed breakout.  This separates entry types
        # instead of raising one global threshold for every opportunity.
        "small_account_continuation_minimum_momentum_strength": 0.50,
        "small_account_pullback_min_fast_momentum_retention": 0.30,
        "small_account_pullback_recovery_min_volume_ratio": 1.20,
        "small_account_pullback_recovery_min_close_location": 0.75,
        "small_account_crowded_extension_veto_enabled": True,
        "small_account_crowded_extension_min_fast_ema_atr": 0.80,
        "small_account_crowded_funding_rate": 0.0012,
        "small_account_crowded_basis_pct": 0.40,
        # An event-timeframe regime/flow overlay chooses among alternative
        # entry paths. It is active only for a new sub-$1,000 trend entry.
        "change_point_flow": default_change_point_flow_config(),
        # A separate range/exhaustion challenger is considered only when the
        # primary trend/event router has no valid candidate. It is crypto-only
        # and uses its own finite-target exit profile (no trend runner/pyramid).
        "small_account_regime_ensemble": default_small_account_regime_config(),
        # Entry-shape classifiers. They remain OR-style labels on top of the
        # broad multi-speed trend, not mandatory confirmations.
        "entry_clarity_lookback_bars": 48,
        "pullback_resumption_enabled": True,
        "pullback_touch_lookback_bars": 3,
        "pullback_fast_ema_touch_atr": 0.35,
        "pullback_medium_ema_break_atr": 0.50,
        "pullback_min_close_location": 0.60,
        "impulse_breakout_enabled": True,
        "impulse_breakout_lookback_bars": 12,
        "impulse_breakout_min_volume_ratio": 1.15,
        "impulse_breakout_min_close_location": 0.65,
        "impulse_breakout_min_momentum_strength": 0.35,
        "impulse_breakout_min_fast_retention": 0.55,
        "impulse_breakout_max_fast_ema_distance_atr": 1.50,
        "impulse_breakout_max_range_atr": 2.20,
        # Cross-sectional ranks supplement (and never bypass) the stop,
        # liquidation, liquidity and L2 safety gates.
        "rotation_exit_enabled": True,
        "rotation_min_holding_hours": 8,
        "rotation_max_holding_hours": 12,
        "rotation_max_mfe_r": 0.35,
        "rotation_max_current_r": 0.25,
        "rotation_rank_percentile_floor": 35.0,
        "rotation_rank_confirmations": 2,
        # TradFi perpetuals keep the shared trend model, but add a separate
        # completed-candle pattern overlay and an exchange-compatible 10x cap.
        "tradfi_pattern_profile": {"enabled": True},
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
    supplied_profile = str(supplied.get("profile_version") or "").strip()
    if (
        supplied
        and supplied_profile != ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION
        and not supplied_profile.startswith(
            (
                "adaptive_trend_portfolio_v6_",
                "adaptive_trend_portfolio_v7_",
                "adaptive_trend_portfolio_v8_",
                "adaptive_trend_portfolio_v9_",
                "adaptive_trend_portfolio_v10_",
                "adaptive_trend_portfolio_v11_",
                "adaptive_trend_portfolio_v12_",
                "adaptive_trend_portfolio_v13_",
                "adaptive_trend_portfolio_v14_",
                "adaptive_trend_portfolio_v15_",
                "adaptive_trend_portfolio_v16_",
            )
        )
    ):
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
            "breakout_entry_enabled",
            "compression_breakout_enabled",
            "compression_window_bars",
            "compression_lookback_bars",
            "compression_max_range_ratio",
            "compression_min_volume_ratio",
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
            "small_account_stability_risk_enabled",
            "small_account_stability_risk_floor",
            "small_account_profit_bank_enabled",
            "small_account_profit_bank_activation_multiple",
            "small_account_profit_bank_protect_fraction",
            "small_account_profit_bank_min_risk_scale",
            "small_account_daily_loss_limit_percent",
            "small_account_cost_buffer_percent",
            "small_account_liquidation_stop_buffer_multiple",
            "small_account_min_leverage",
            "small_account_strong_leverage",
            "small_account_elite_leverage",
            "small_account_leverage_steps",
            "small_account_roe_profit_lock_enabled",
            "small_account_roe_profit_lock_first_trigger_percent",
            "small_account_roe_profit_lock_second_trigger_percent",
            "small_account_roe_profit_lock_step_percent",
            "small_account_roe_profit_lock_atr_multiplier",
            "small_account_roe_profit_lock_min_gap_percent",
            "small_account_roe_profit_lock_max_gap_percent",
            "small_account_roe_profit_lock_min_floor_percent",
            "small_account_progress_failure_exit_enabled",
            "small_account_progress_failure_min_mark_mfe_r",
            "small_account_progress_failure_max_mark_mfe_r",
            "small_account_progress_failure_max_current_r",
            "small_account_progress_failure_min_closed_bars",
            "small_account_progress_failure_confirmations",
            "small_account_entry_refinement_enabled",
            "small_account_min_fast_momentum_retention",
            "small_account_max_adverse_signal_move_atr",
            "small_account_crossover_max_fast_ema_distance_atr",
            "small_account_event_only_max_fast_ema_distance_atr",
            "small_account_event_only_broad_conflict_min_momentum",
            "small_account_event_only_risk_tier_cap",
            "small_account_lower_timeframe_conflict_veto_enabled",
            "small_account_lower_timeframe_conflict_min_alignment",
            "small_account_lower_timeframe_conflict_min_ready_timeframes",
            "small_account_continuation_minimum_momentum_strength",
            "small_account_pullback_min_fast_momentum_retention",
            "small_account_pullback_recovery_min_volume_ratio",
            "small_account_pullback_recovery_min_close_location",
            "small_account_crowded_extension_veto_enabled",
            "small_account_crowded_extension_min_fast_ema_atr",
            "small_account_crowded_funding_rate",
            "small_account_crowded_basis_pct",
            "entry_clarity_lookback_bars",
            "pullback_resumption_enabled",
            "pullback_touch_lookback_bars",
            "pullback_fast_ema_touch_atr",
            "pullback_medium_ema_break_atr",
            "pullback_min_close_location",
            "impulse_breakout_enabled",
            "impulse_breakout_lookback_bars",
            "impulse_breakout_min_volume_ratio",
            "impulse_breakout_min_close_location",
            "impulse_breakout_min_momentum_strength",
            "impulse_breakout_min_fast_retention",
            "impulse_breakout_max_fast_ema_distance_atr",
            "impulse_breakout_max_range_atr",
            "rotation_exit_enabled",
            "rotation_min_holding_hours",
            "rotation_max_holding_hours",
            "rotation_max_mfe_r",
            "rotation_max_current_r",
            "rotation_rank_percentile_floor",
            "rotation_rank_confirmations",
            "partial_take_profit_r_multiple",
            "partial_take_profit_ratio",
            "runner_pct",
            "atr_trailing_activation_r",
            "atr_trailing_multiplier",
        )
        for key in profile_keys:
            normalized[key] = defaults[key]
    if supplied_profile.startswith(
        (
            "adaptive_trend_portfolio_v6_",
            "adaptive_trend_portfolio_v7_",
            "adaptive_trend_portfolio_v8_",
            "adaptive_trend_portfolio_v9_",
            "adaptive_trend_portfolio_v10_",
            "adaptive_trend_portfolio_v11_",
            "adaptive_trend_portfolio_v12_",
        )
    ):
        # Preserve the operator's other live risk/exit choices, but migrate
        # the explicitly requested small-account leverage and new-profit-lock
        # policy together. Otherwise a persisted v9 15x ceiling would silently
        # defeat the new 4-7x rule after deployment.
        for key in (
            "small_account_min_leverage",
            "small_account_strong_leverage",
            "small_account_elite_leverage",
            "small_account_leverage_steps",
            "small_account_roe_profit_lock_enabled",
            "small_account_roe_profit_lock_first_trigger_percent",
            "small_account_roe_profit_lock_second_trigger_percent",
            "small_account_roe_profit_lock_step_percent",
            "small_account_roe_profit_lock_atr_multiplier",
            "small_account_roe_profit_lock_min_gap_percent",
            "small_account_roe_profit_lock_max_gap_percent",
            "small_account_roe_profit_lock_min_floor_percent",
            "small_account_event_only_broad_conflict_min_momentum",
        ):
            normalized[key] = defaults[key]
    if supplied_profile.startswith("adaptive_trend_portfolio_v13_"):
        # v13 combined an ATR-wide hard stop with an almost fixed full-margin
        # quantity and 20/30/35% loss ceilings.  Migrate only the sizing caps;
        # operator-selected exits, including the deliberate ROE staircase,
        # remain unchanged.
        for key in (
            "small_account_base_max_loss_percent",
            "small_account_strong_max_loss_percent",
            "small_account_elite_max_loss_percent",
        ):
            normalized[key] = defaults[key]
    # Preserve all other operator-tuned risk/exit values while defaults above
    # add the new entry fields.  This also prevents an open position's
    # runtime policy from changing merely because the entry profile advanced.
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
    for key in (
        "breakout_entry_enabled",
        "compression_breakout_enabled",
        "rotation_exit_enabled",
    ):
        raw = normalized.get(key, defaults[key])
        normalized[key] = (
            raw
            if isinstance(raw, bool)
            else str(raw).strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        )
    compression_window = int(
        _bounded(
            _finite(
                normalized.get("compression_window_bars"),
                defaults["compression_window_bars"],
            ),
            4.0,
            48.0,
        )
    )
    normalized["compression_window_bars"] = compression_window
    normalized["compression_lookback_bars"] = int(
        _bounded(
            _finite(
                normalized.get("compression_lookback_bars"),
                defaults["compression_lookback_bars"],
            ),
            float(compression_window + 1),
            336.0,
        )
    )
    normalized["compression_max_range_ratio"] = _bounded(
        _finite(
            normalized.get("compression_max_range_ratio"),
            defaults["compression_max_range_ratio"],
        ),
        0.10,
        1.00,
    )
    normalized["compression_min_volume_ratio"] = _bounded(
        _finite(
            normalized.get("compression_min_volume_ratio"),
            defaults["compression_min_volume_ratio"],
        ),
        0.50,
        5.00,
    )
    rotation_min_hours = int(
        _bounded(
            _finite(
                normalized.get("rotation_min_holding_hours"),
                defaults["rotation_min_holding_hours"],
            ),
            1.0,
            72.0,
        )
    )
    normalized["rotation_min_holding_hours"] = rotation_min_hours
    normalized["rotation_max_holding_hours"] = int(
        _bounded(
            _finite(
                normalized.get("rotation_max_holding_hours"),
                defaults["rotation_max_holding_hours"],
            ),
            float(rotation_min_hours),
            336.0,
        )
    )
    normalized["rotation_max_mfe_r"] = _bounded(
        _finite(
            normalized.get("rotation_max_mfe_r"),
            defaults["rotation_max_mfe_r"],
        ),
        0.0,
        3.0,
    )
    normalized["rotation_max_current_r"] = _bounded(
        _finite(
            normalized.get("rotation_max_current_r"),
            defaults["rotation_max_current_r"],
        ),
        -1.0,
        3.0,
    )
    normalized["rotation_rank_percentile_floor"] = _bounded(
        _finite(
            normalized.get("rotation_rank_percentile_floor"),
            defaults["rotation_rank_percentile_floor"],
        ),
        5.0,
        75.0,
    )
    normalized["rotation_rank_confirmations"] = int(
        _bounded(
            _finite(
                normalized.get("rotation_rank_confirmations"),
                defaults["rotation_rank_confirmations"],
            ),
            1.0,
            6.0,
        )
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
    for key in (
        "small_account_aggressive_enabled",
        "small_account_short_entries_enabled",
        "small_account_roe_profit_lock_enabled",
        "small_account_profit_bank_enabled",
        "small_account_progress_failure_exit_enabled",
        "small_account_entry_refinement_enabled",
        "small_account_lower_timeframe_conflict_veto_enabled",
        "small_account_crowded_extension_veto_enabled",
        "pullback_resumption_enabled",
        "impulse_breakout_enabled",
    ):
        raw = normalized.get(key, defaults[key])
        normalized[key] = (
            raw
            if isinstance(raw, bool)
            else str(raw).strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
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
    normalized["small_account_profit_bank_activation_multiple"] = _bounded(
        _finite(
            normalized.get("small_account_profit_bank_activation_multiple"),
            defaults["small_account_profit_bank_activation_multiple"],
        ),
        0.10,
        3.00,
    )
    normalized["small_account_profit_bank_protect_fraction"] = _bounded(
        _finite(
            normalized.get("small_account_profit_bank_protect_fraction"),
            defaults["small_account_profit_bank_protect_fraction"],
        ),
        0.0,
        0.95,
    )
    normalized["small_account_profit_bank_min_risk_scale"] = _bounded(
        _finite(
            normalized.get("small_account_profit_bank_min_risk_scale"),
            defaults["small_account_profit_bank_min_risk_scale"],
        ),
        0.10,
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
    normalized["small_account_roe_profit_lock_first_trigger_percent"] = _bounded(
        _finite(
            normalized.get("small_account_roe_profit_lock_first_trigger_percent"),
            defaults["small_account_roe_profit_lock_first_trigger_percent"],
        ),
        0.5,
        100.0,
    )
    normalized["small_account_roe_profit_lock_second_trigger_percent"] = max(
        normalized["small_account_roe_profit_lock_first_trigger_percent"],
        _bounded(
            _finite(
                normalized.get("small_account_roe_profit_lock_second_trigger_percent"),
                defaults["small_account_roe_profit_lock_second_trigger_percent"],
            ),
            0.5,
            200.0,
        ),
    )
    normalized["small_account_roe_profit_lock_step_percent"] = _bounded(
        _finite(
            normalized.get("small_account_roe_profit_lock_step_percent"),
            defaults["small_account_roe_profit_lock_step_percent"],
        ),
        0.5,
        100.0,
    )
    normalized["small_account_roe_profit_lock_atr_multiplier"] = _bounded(
        _finite(
            normalized.get("small_account_roe_profit_lock_atr_multiplier"),
            defaults["small_account_roe_profit_lock_atr_multiplier"],
        ),
        0.0,
        3.0,
    )
    normalized["small_account_roe_profit_lock_min_gap_percent"] = _bounded(
        _finite(
            normalized.get("small_account_roe_profit_lock_min_gap_percent"),
            defaults["small_account_roe_profit_lock_min_gap_percent"],
        ),
        0.1,
        20.0,
    )
    normalized["small_account_roe_profit_lock_max_gap_percent"] = max(
        normalized["small_account_roe_profit_lock_min_gap_percent"],
        _bounded(
            _finite(
                normalized.get("small_account_roe_profit_lock_max_gap_percent"),
                defaults["small_account_roe_profit_lock_max_gap_percent"],
            ),
            0.1,
            30.0,
        ),
    )
    normalized["small_account_roe_profit_lock_min_floor_percent"] = _bounded(
        _finite(
            normalized.get("small_account_roe_profit_lock_min_floor_percent"),
            defaults["small_account_roe_profit_lock_min_floor_percent"],
        ),
        0.0,
        normalized["small_account_roe_profit_lock_first_trigger_percent"],
    )
    normalized["small_account_progress_failure_min_mark_mfe_r"] = _bounded(
        _finite(
            normalized.get("small_account_progress_failure_min_mark_mfe_r"),
            defaults["small_account_progress_failure_min_mark_mfe_r"],
        ),
        0.0,
        3.0,
    )
    normalized["small_account_progress_failure_max_mark_mfe_r"] = max(
        normalized["small_account_progress_failure_min_mark_mfe_r"],
        _bounded(
            _finite(
                normalized.get("small_account_progress_failure_max_mark_mfe_r"),
                defaults["small_account_progress_failure_max_mark_mfe_r"],
            ),
            0.0,
            5.0,
        ),
    )
    normalized["small_account_progress_failure_max_current_r"] = _bounded(
        _finite(
            normalized.get("small_account_progress_failure_max_current_r"),
            defaults["small_account_progress_failure_max_current_r"],
        ),
        -1.0,
        0.0,
    )
    normalized["small_account_progress_failure_min_closed_bars"] = int(
        _bounded(
            _finite(
                normalized.get("small_account_progress_failure_min_closed_bars"),
                defaults["small_account_progress_failure_min_closed_bars"],
            ),
            1.0,
            12.0,
        )
    )
    normalized["small_account_progress_failure_confirmations"] = int(
        _bounded(
            _finite(
                normalized.get("small_account_progress_failure_confirmations"),
                defaults["small_account_progress_failure_confirmations"],
            ),
            1.0,
            3.0,
        )
    )
    normalized["small_account_min_fast_momentum_retention"] = _bounded(
        _finite(
            normalized.get("small_account_min_fast_momentum_retention"),
            defaults["small_account_min_fast_momentum_retention"],
        ),
        0.0,
        1.0,
    )
    normalized["small_account_max_adverse_signal_move_atr"] = _bounded(
        _finite(
            normalized.get("small_account_max_adverse_signal_move_atr"),
            defaults["small_account_max_adverse_signal_move_atr"],
        ),
        0.10,
        3.0,
    )
    normalized["small_account_crossover_max_fast_ema_distance_atr"] = _bounded(
        _finite(
            normalized.get("small_account_crossover_max_fast_ema_distance_atr"),
            defaults["small_account_crossover_max_fast_ema_distance_atr"],
        ),
        0.50,
        6.0,
    )
    normalized["small_account_event_only_max_fast_ema_distance_atr"] = _bounded(
        _finite(
            normalized.get("small_account_event_only_max_fast_ema_distance_atr"),
            defaults["small_account_event_only_max_fast_ema_distance_atr"],
        ),
        0.50,
        4.0,
    )
    normalized[
        "small_account_event_only_broad_conflict_min_momentum"
    ] = _bounded(
        _finite(
            normalized.get(
                "small_account_event_only_broad_conflict_min_momentum"
            ),
            defaults[
                "small_account_event_only_broad_conflict_min_momentum"
            ],
        ),
        0.05,
        0.75,
    )
    event_only_tier_cap = str(
        normalized.get("small_account_event_only_risk_tier_cap", "base")
        or "base"
    ).strip().lower()
    if event_only_tier_cap not in {"base", "strong", "elite"}:
        event_only_tier_cap = "base"
    normalized["small_account_event_only_risk_tier_cap"] = event_only_tier_cap
    normalized["small_account_lower_timeframe_conflict_min_alignment"] = _bounded(
        _finite(
            normalized.get("small_account_lower_timeframe_conflict_min_alignment"),
            defaults["small_account_lower_timeframe_conflict_min_alignment"],
        ),
        0.0,
        100.0,
    )
    normalized["small_account_lower_timeframe_conflict_min_ready_timeframes"] = int(
        _bounded(
            _finite(
                normalized.get(
                    "small_account_lower_timeframe_conflict_min_ready_timeframes"
                ),
                defaults[
                    "small_account_lower_timeframe_conflict_min_ready_timeframes"
                ],
            ),
            1.0,
            8.0,
        )
    )
    normalized["small_account_continuation_minimum_momentum_strength"] = _bounded(
        _finite(
            normalized.get(
                "small_account_continuation_minimum_momentum_strength"
            ),
            defaults["small_account_continuation_minimum_momentum_strength"],
        ),
        0.20,
        1.0,
    )
    normalized["small_account_pullback_min_fast_momentum_retention"] = _bounded(
        _finite(
            normalized.get(
                "small_account_pullback_min_fast_momentum_retention"
            ),
            defaults["small_account_pullback_min_fast_momentum_retention"],
        ),
        0.0,
        1.0,
    )
    for key, lower, upper in (
        ("small_account_crowded_extension_min_fast_ema_atr", 0.25, 3.0),
        ("small_account_crowded_funding_rate", 0.0001, 0.01),
        ("small_account_crowded_basis_pct", 0.05, 5.0),
        ("small_account_pullback_recovery_min_volume_ratio", 0.75, 5.0),
        ("small_account_pullback_recovery_min_close_location", 0.50, 0.95),
        ("pullback_fast_ema_touch_atr", 0.05, 1.50),
        ("pullback_medium_ema_break_atr", 0.05, 2.00),
        ("pullback_min_close_location", 0.50, 0.95),
        ("impulse_breakout_min_volume_ratio", 0.75, 5.0),
        ("impulse_breakout_min_close_location", 0.50, 0.95),
        ("impulse_breakout_min_momentum_strength", 0.10, 1.0),
        ("impulse_breakout_min_fast_retention", 0.0, 1.50),
        ("impulse_breakout_max_fast_ema_distance_atr", 0.50, 4.0),
        ("impulse_breakout_max_range_atr", 0.75, 5.0),
    ):
        normalized[key] = _bounded(
            _finite(normalized.get(key), defaults[key]),
            lower,
            upper,
        )
    for key, lower, upper in (
        ("entry_clarity_lookback_bars", 12, 168),
        ("pullback_touch_lookback_bars", 2, 8),
        ("impulse_breakout_lookback_bars", 6, 48),
    ):
        normalized[key] = int(
            _bounded(
                _finite(normalized.get(key), defaults[key]),
                float(lower),
                float(upper),
            )
        )
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
    normalized["change_point_flow"] = normalize_change_point_flow_config(
        normalized.get("change_point_flow")
        if isinstance(normalized.get("change_point_flow"), Mapping)
        else None
    )
    normalized["small_account_regime_ensemble"] = (
        normalize_small_account_regime_config(
            normalized.get("small_account_regime_ensemble")
            if isinstance(normalized.get("small_account_regime_ensemble"), Mapping)
            else None
        )
    )
    return normalized


def small_account_short_entry_blocked(
    side: str | None,
    *,
    small_account_active: bool,
    config: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the aggressive small-account long-only rule blocks SHORT."""

    source = config if isinstance(config, Mapping) else {}
    raw_enabled = source.get("small_account_short_entries_enabled", False)
    short_enabled = (
        raw_enabled
        if isinstance(raw_enabled, bool)
        else str(raw_enabled).strip().lower()
        in {"1", "true", "yes", "on", "enabled"}
    )
    return bool(
        small_account_active
        and str(side or "").strip().lower() == "short"
        and not short_enabled
    )


def evaluate_small_account_entry_refinement(
    side: str | None,
    metrics: Mapping[str, Any] | None,
    *,
    entry_chase_atr: float | None,
    selector_candidate: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Veto only decayed or invalidated entries in the aggressive profile.

    The broad multi-speed trend signal remains an OR-style weighted model.  A
    continuation is rejected only when its fast sleeve has materially faded,
    the live price has already moved against the completed-candle signal, or
    the fresh execution-timeframe analysis explicitly points the other way.
    """

    cfg = normalize_adaptive_breakout_trend_config(config)
    values = dict(metrics or {})
    candidate = dict(selector_candidate or {})
    normalized_side = str(side or "").strip().lower()
    result = {
        "allowed": True,
        "code": "SMALL_ACCOUNT_ENTRY_REFINED_OK",
        "reason": "small-account entry refinement passed",
        "profile": ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION,
        "fast_momentum_retention": _finite(
            values.get("fast_momentum_retention"),
            None,
        ),
        "entry_chase_atr": _finite(entry_chase_atr, None),
        "lower_timeframe_side": str(
            candidate.get("auto_dominant_side") or ""
        ).strip().lower(),
        "lower_timeframe_alignment": _finite(
            candidate.get("auto_alignment_score"),
            None,
        ),
        "lower_timeframe_ready_count": int(
            _finite(candidate.get("auto_ready_timeframes"), 0.0) or 0
        ),
        "entry_opportunity_score": _finite(
            values.get("entry_opportunity_score"),
            None,
        ),
        "trend_clarity": _finite(values.get("trend_clarity"), None),
        "pullback_resumption": bool(values.get("pullback_resumption")),
        "impulse_breakout": bool(values.get("impulse_breakout")),
        "funding_rate": _finite(candidate.get("funding_rate"), None),
        "basis_pct": _finite(candidate.get("basis_pct"), None),
    }
    if not bool(cfg.get("small_account_entry_refinement_enabled", True)):
        result.update({
            "code": "SMALL_ACCOUNT_ENTRY_REFINEMENT_DISABLED",
            "reason": "small-account entry refinement disabled",
        })
        return result

    chase_atr = result["entry_chase_atr"]
    adverse_limit = float(cfg["small_account_max_adverse_signal_move_atr"])
    if chase_atr is not None and chase_atr < -adverse_limit:
        result.update({
            "allowed": False,
            "code": "REJECTED_SMALL_ACCOUNT_SIGNAL_INVALIDATED",
            "reason": (
                f"live price moved {abs(chase_atr):.2f} ATR against the "
                f"completed-candle signal (limit {adverse_limit:.2f})"
            ),
        })
        return result

    fast_distance = _finite(values.get("signed_fast_ema_distance_atr"), None)
    crossover_limit = float(
        cfg["small_account_crossover_max_fast_ema_distance_atr"]
    )
    if (
        bool(values.get("ema_crossover"))
        and fast_distance is not None
        and fast_distance > crossover_limit
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_SMALL_ACCOUNT_CROSSOVER_EXTENSION",
            "reason": (
                f"EMA crossover is already {fast_distance:.2f} ATR from the "
                f"fast EMA (limit {crossover_limit:.2f})"
            ),
        })
        return result

    retention = result["fast_momentum_retention"]
    pullback_recovery_confirmed = bool(
        result["pullback_resumption"]
        and float(_finite(values.get("volume_ratio"), 0.0) or 0.0)
        >= float(cfg["small_account_pullback_recovery_min_volume_ratio"])
        and float(
            _finite(values.get("directional_close_location"), 0.0) or 0.0
        )
        >= float(cfg["small_account_pullback_recovery_min_close_location"])
    )
    result["pullback_recovery_confirmed"] = pullback_recovery_confirmed
    retention_floor = float(
        cfg["small_account_pullback_min_fast_momentum_retention"]
        if pullback_recovery_confirmed
        else cfg["small_account_min_fast_momentum_retention"]
    )
    if (
        bool(values.get("weighted_continuation"))
        and retention is not None
        and retention < retention_floor
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_SMALL_ACCOUNT_FAST_TREND_DECAY",
            "reason": (
                f"fast trend retained only {retention:.2f} of the stronger "
                f"medium/slow sleeve (minimum {retention_floor:.2f})"
            ),
        })
        return result

    continuation_momentum = abs(
        float(_finite(values.get("weighted_momentum"), 0.0) or 0.0)
    )
    continuation_floor = float(
        cfg["small_account_continuation_minimum_momentum_strength"]
    )
    if (
        bool(values.get("weighted_continuation"))
        and not result["pullback_resumption"]
        and not result["impulse_breakout"]
        and continuation_momentum < continuation_floor
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_SMALL_ACCOUNT_WEAK_MATURE_CONTINUATION",
            "reason": (
                f"mature continuation momentum {continuation_momentum:.2f} "
                f"is below {continuation_floor:.2f} without a fresh pullback "
                "resumption or volume-backed impulse"
            ),
        })
        return result

    fast_distance = float(
        _finite(values.get("signed_fast_ema_distance_atr"), 0.0) or 0.0
    )
    direction = 1.0 if normalized_side == "long" else -1.0
    funding_rate = result["funding_rate"]
    basis_pct = result["basis_pct"]
    adverse_funding = (
        direction * funding_rate if funding_rate is not None else None
    )
    adverse_basis = direction * basis_pct if basis_pct is not None else None
    crowded_funding = bool(
        adverse_funding is not None
        and adverse_funding >= float(cfg["small_account_crowded_funding_rate"])
    )
    crowded_basis = bool(
        adverse_basis is not None
        and adverse_basis >= float(cfg["small_account_crowded_basis_pct"])
    )
    if (
        bool(cfg.get("small_account_crowded_extension_veto_enabled", True))
        and not result["pullback_resumption"]
        and fast_distance
        >= float(cfg["small_account_crowded_extension_min_fast_ema_atr"])
        and (crowded_funding or crowded_basis)
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_SMALL_ACCOUNT_CROWDED_EXTENSION",
            "reason": (
                f"extended {fast_distance:.2f} ATR entry is crowded "
                f"(funding={funding_rate}, basis={basis_pct})"
            ),
        })
        return result

    lower_side = result["lower_timeframe_side"]
    lower_alignment = result["lower_timeframe_alignment"]
    lower_ready_count = result["lower_timeframe_ready_count"]
    minimum_lower_alignment = float(
        cfg["small_account_lower_timeframe_conflict_min_alignment"]
    )
    minimum_lower_ready_count = int(
        cfg["small_account_lower_timeframe_conflict_min_ready_timeframes"]
    )
    if (
        bool(
            cfg.get(
                "small_account_lower_timeframe_conflict_veto_enabled",
                True,
            )
        )
        and normalized_side in {"long", "short"}
        and lower_side in {"long", "short"}
        and lower_side != normalized_side
        and lower_alignment is not None
        and lower_alignment >= minimum_lower_alignment
        and lower_ready_count >= minimum_lower_ready_count
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_SMALL_ACCOUNT_LOWER_TIMEFRAME_CONFLICT",
            "reason": (
                f"lower timeframes strongly favor {lower_side} "
                f"(alignment {lower_alignment:.1f}, ready {lower_ready_count}) "
                f"against the {normalized_side} trend signal"
            ),
        })
    return result


def evaluate_independent_event_context(
    side: str | None,
    trend_metrics: Mapping[str, Any] | None,
    *,
    config: Mapping[str, Any] | None = None,
    multi_timeframe_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Guard a fast event with the full live horizon router when available.

    An event is still an OR-style early entry: EMA alignment, a crossover, and
    fast-sleeve agreement are not required.  Live execution uses the canonical
    multi-timeframe result so a single 1h metric cannot silently veto the
    13-timeframe router.  The legacy 1h-only guard remains only for callers
    that do not yet supply the router context.
    """

    cfg = normalize_adaptive_breakout_trend_config(config)
    metrics = dict(trend_metrics or {})
    normalized_side = str(side or "").strip().lower()
    full_context_supplied = bool(
        isinstance(multi_timeframe_context, Mapping)
        and multi_timeframe_context
    )
    full_context_available = bool(
        full_context_supplied and multi_timeframe_context.get("available")
    )
    if full_context_supplied:
        weighted_momentum = float(
            _finite(
                multi_timeframe_context.get("weighted_direction_score"),
                0.0,
            )
            or 0.0
        )
        broad_side = str(
            multi_timeframe_context.get("direction") or ""
        ).strip().lower()
        if broad_side not in {"long", "short"}:
            broad_side = None
    else:
        weighted_momentum = float(
            _finite(metrics.get("weighted_momentum"), 0.0) or 0.0
        )
        broad_side = (
            "long" if weighted_momentum > 0.0 else
            "short" if weighted_momentum < 0.0 else
            None
        )
    horizon_votes = metrics.get("horizon_votes")
    if not isinstance(horizon_votes, Mapping):
        horizon_votes = {}
    dominant_votes = sum(
        1 for vote in horizon_votes.values() if vote == broad_side
    )
    minimum_votes = max(
        2,
        min(
            max(2, len(horizon_votes)),
            int(cfg.get("minimum_horizon_agreement", 2) or 2),
        ),
    )
    reference_price = _finite(metrics.get("reference_price"), None)
    fast_ema = _finite(metrics.get("fast_ema"), None)
    atr_value = _finite(metrics.get("atr"), None)
    fast_ema_distance_atr = None
    if (
        normalized_side in {"long", "short"}
        and reference_price is not None
        and fast_ema is not None
        and atr_value is not None
        and atr_value > 0.0
    ):
        fast_ema_distance_atr = (
            (reference_price - fast_ema) / atr_value
            if normalized_side == "long"
            else (fast_ema - reference_price) / atr_value
        )
    extension_limit = float(
        cfg["small_account_event_only_max_fast_ema_distance_atr"]
    )
    latest_range_atr = _finite(metrics.get("latest_range_atr"), None)
    volatility_ratio = _finite(metrics.get("volatility_ratio"), None)
    broad_conflict_min_momentum = float(
        cfg["small_account_event_only_broad_conflict_min_momentum"]
    )
    result = {
        "allowed": True,
        "code": "INDEPENDENT_EVENT_CONTEXT_OK",
        "reason": (
            "independent event passed full 13-timeframe context guard"
            if full_context_supplied
            else "independent event passed legacy 1h context guard"
        ),
        "profile": ADAPTIVE_TREND_PORTFOLIO_PROFILE_VERSION,
        "context_source": (
            "full_multi_timeframe_router"
            if full_context_supplied
            else "legacy_1h_metrics"
        ),
        "broad_side": broad_side,
        "weighted_momentum": weighted_momentum,
        "broad_conflict_min_momentum": broad_conflict_min_momentum,
        "dominant_votes": dominant_votes,
        "minimum_votes": minimum_votes,
        "fast_ema_distance_atr": fast_ema_distance_atr,
        "max_fast_ema_distance_atr": extension_limit,
        "latest_range_atr": latest_range_atr,
        "volatility_ratio": volatility_ratio,
    }
    if normalized_side not in {"long", "short"}:
        result.update({
            "allowed": False,
            "code": "REJECTED_INDEPENDENT_EVENT_SIDE",
            "reason": "independent event direction is unavailable",
        })
        return result
    if full_context_supplied:
        if not full_context_available:
            result.update({
                "allowed": False,
                "code": "REJECTED_INDEPENDENT_EVENT_HORIZON_DATA",
                "reason": "full 13-timeframe router does not have sufficient completed-candle coverage",
            })
            return result
        opposing_by_side = multi_timeframe_context.get("strong_opposing_groups")
        opposing_by_side = (
            opposing_by_side if isinstance(opposing_by_side, Mapping) else {}
        )
        opposing_groups = tuple(opposing_by_side.get(normalized_side) or ())
        result["strong_opposing_groups"] = opposing_groups
        if (
            opposing_groups
            or (
                broad_side in {"long", "short"}
                and broad_side != normalized_side
                and abs(weighted_momentum) >= broad_conflict_min_momentum
            )
        ):
            result.update({
                "allowed": False,
                "code": "REJECTED_INDEPENDENT_EVENT_FULL_HORIZON_CONFLICT",
                "reason": (
                    f"independent event {normalized_side} conflicts with the "
                    f"full 13-timeframe router (score={weighted_momentum:+.3f}, "
                    f"opposing_groups={','.join(opposing_groups) or 'none'})"
                ),
            })
        return result
    if (
        broad_side in {"long", "short"}
        and broad_side != normalized_side
        and (
            dominant_votes >= minimum_votes
            or abs(weighted_momentum) >= broad_conflict_min_momentum
        )
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_INDEPENDENT_EVENT_BROAD_TREND_CONFLICT",
            "reason": (
                f"independent event {normalized_side} conflicts with "
                f"{broad_side} multi-horizon direction "
                f"(momentum={weighted_momentum:+.2f}, "
                f"{dominant_votes}/{len(horizon_votes)} votes)"
            ),
        })
        return result
    if (
        fast_ema_distance_atr is not None
        and fast_ema_distance_atr > extension_limit
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_INDEPENDENT_EVENT_HTF_EXTENSION",
            "reason": (
                f"independent event is {fast_ema_distance_atr:.2f} ATR "
                f"beyond the 1h fast EMA (limit {extension_limit:.2f})"
            ),
        })
        return result
    if (
        latest_range_atr is not None
        and latest_range_atr > float(cfg["latest_range_max_atr"])
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_INDEPENDENT_EVENT_EXTREME_RANGE",
            "reason": (
                f"1h range {latest_range_atr:.2f} ATR exceeds "
                f"{float(cfg['latest_range_max_atr']):.2f}"
            ),
        })
        return result
    if (
        volatility_ratio is not None
        and volatility_ratio > float(cfg["volatility_shock_ratio"])
    ):
        result.update({
            "allowed": False,
            "code": "REJECTED_INDEPENDENT_EVENT_VOLATILITY_SHOCK",
            "reason": (
                f"1h volatility ratio {volatility_ratio:.2f} exceeds "
                f"{float(cfg['volatility_shock_ratio']):.2f}"
            ),
        })
    return result


def resolve_independent_event_allocation(
    risk_tier: str | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate event detection strength from unconfirmed initial capital."""

    cfg = normalize_adaptive_breakout_trend_config(config)
    order = {"base": 0, "strong": 1, "elite": 2}
    requested = str(risk_tier or "base").strip().lower()
    if requested not in order:
        requested = "base"
    cap = str(cfg["small_account_event_only_risk_tier_cap"])
    applied = min((requested, cap), key=lambda tier: order[tier])
    flow_cfg = cfg["change_point_flow"]
    return {
        "requested_risk_tier": requested,
        "risk_tier_cap": cap,
        "applied_risk_tier": applied,
        "risk_tier_capped": applied != requested,
        "initial_margin_fraction": float(
            flow_cfg[f"{applied}_initial_margin_fraction"]
        ),
    }


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


def _linear_trend_clarity(
    closes: Sequence[float],
    *,
    side: str | None,
    lookback: int,
) -> tuple[float, float]:
    """Return direction-aware log-price R² and directional-bar ratio."""

    count = max(4, min(len(closes), int(lookback)))
    values = [log(max(float(value), 1e-12)) for value in closes[-count:]]
    center_x = (count - 1) / 2.0
    center_y = sum(values) / count
    covariance = sum(
        (index - center_x) * (value - center_y)
        for index, value in enumerate(values)
    )
    variance_x = sum((index - center_x) ** 2 for index in range(count))
    variance_y = sum((value - center_y) ** 2 for value in values)
    r_squared = (
        covariance * covariance / max(variance_x * variance_y, 1e-18)
        if variance_y > 0.0
        else 0.0
    )
    slope = covariance / max(variance_x, 1e-18)
    direction = 1.0 if side == "long" else -1.0 if side == "short" else 0.0
    if direction and direction * slope <= 0.0:
        r_squared = 0.0
    returns = [values[index] - values[index - 1] for index in range(1, count)]
    directional_ratio = (
        sum(1 for value in returns if direction * value > 0.0) / len(returns)
        if direction and returns
        else 0.0
    )
    return _bounded(r_squared, 0.0, 1.0), _bounded(directional_ratio, 0.0, 1.0)


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
        if bool(cfg.get("compression_breakout_enabled", True)):
            required_values.append(int(cfg.get("compression_lookback_bars", 96)) + 2)
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
    fast_momentum_retention: float | None = None
    if side in {"long", "short"} and len(horizons) >= 2:
        direction = 1.0 if side == "long" else -1.0
        fast_directional = direction * float(horizon_scores[min(horizons)])
        slower_directional = [
            direction * float(horizon_scores[horizon])
            for horizon in horizons
            if horizon != min(horizons)
        ]
        stronger_slow_sleeve = max(slower_directional, default=0.0)
        if stronger_slow_sleeve > 0.0:
            fast_momentum_retention = _bounded(
                fast_directional / stronger_slow_sleeve,
                -2.0,
                2.0,
            )

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

    volumes = [float(row.get("volume") or 0.0) for row in candles]
    baseline_volume = median(volumes[-49:-1]) if len(volumes) >= 49 else median(volumes[:-1])
    volume_ratio = volumes[-1] / max(baseline_volume, 1e-9) if baseline_volume > 0 else 1.0
    compression_ratio: float | None = None
    compression_breakout = False
    if breakout_entry_enabled and bool(cfg.get("compression_breakout_enabled", True)):
        compression_window = max(4, int(cfg.get("compression_window_bars", 12) or 12))
        compression_lookback = max(
            compression_window + 1,
            int(cfg.get("compression_lookback_bars", 96) or 96),
        )
        prior_rows = candles[-compression_lookback - 1:-1]
        prior_ranges = [
            max(0.0, float(row["high"]) - float(row["low"]))
            for row in prior_rows
        ]
        long_range = median(prior_ranges) if prior_ranges else 0.0
        short_range = median(prior_ranges[-compression_window:]) if prior_ranges else 0.0
        if long_range > 0.0:
            compression_ratio = short_range / long_range
            compression_breakout = bool(
                fresh_breakout
                and compression_ratio
                <= float(cfg.get("compression_max_range_ratio", 0.72) or 0.72)
                and volume_ratio
                >= float(cfg.get("compression_min_volume_ratio", 1.10) or 1.10)
            )

    efficiency_period = max(8, int(cfg["efficiency_lookback_bars"]))
    efficiency_closes = closes[-efficiency_period - 1:]
    path = sum(abs(efficiency_closes[i] - efficiency_closes[i - 1]) for i in range(1, len(efficiency_closes)))
    efficiency = abs(efficiency_closes[-1] - efficiency_closes[0]) / max(path, 1e-9)
    trend_r_squared, directional_bar_ratio = _linear_trend_clarity(
        closes,
        side=side,
        lookback=int(cfg.get("entry_clarity_lookback_bars", 48) or 48),
    )
    trend_clarity = _bounded(
        trend_r_squared * 0.70 + directional_bar_ratio * 0.30,
        0.0,
        1.0,
    )
    latest_range_atr = (
        float(candles[-1]["high"]) - float(candles[-1]["low"])
    ) / atr_value
    latest_range = max(
        float(candles[-1]["high"]) - float(candles[-1]["low"]),
        1e-12,
    )
    directional_close_location = (
        (closes[-1] - float(candles[-1]["low"])) / latest_range
        if side == "long"
        else (float(candles[-1]["high"]) - closes[-1]) / latest_range
        if side == "short"
        else 0.0
    )
    directional_close_location = _bounded(directional_close_location, 0.0, 1.0)
    volatility_ratio = short_vol / max(long_vol, 1e-9)
    fast_vote = horizon_votes[min(horizons)]
    slow_vote = horizon_votes[max(horizons)]
    turning_conflict = bool(
        side in {"long", "short"}
        and slow_vote == side
        and fast_vote not in {None, side}
        and not compression_breakout
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
    pullback_lookback = max(
        2,
        min(
            len(candles) - 1,
            int(cfg.get("pullback_touch_lookback_bars", 3) or 3),
        ),
    )
    pullback_fast_touch_atr = float(
        cfg.get("pullback_fast_ema_touch_atr", 0.35) or 0.35
    )
    pullback_medium_break_atr = float(
        cfg.get("pullback_medium_ema_break_atr", 0.50) or 0.50
    )
    controlled_pullback_touch = False
    if side in {"long", "short"}:
        for index in range(len(candles) - pullback_lookback - 1, len(candles) - 1):
            if side == "long":
                touched_fast = (
                    float(candles[index]["low"])
                    <= fast_ema[index] + pullback_fast_touch_atr * atr_value
                )
                preserved_medium = (
                    float(candles[index]["low"])
                    >= medium_ema[index] - pullback_medium_break_atr * atr_value
                )
            else:
                touched_fast = (
                    float(candles[index]["high"])
                    >= fast_ema[index] - pullback_fast_touch_atr * atr_value
                )
                preserved_medium = (
                    float(candles[index]["high"])
                    <= medium_ema[index] + pullback_medium_break_atr * atr_value
                )
            if touched_fast and preserved_medium:
                controlled_pullback_touch = True
                break
    directional_body = (
        closes[-1] > max(float(candles[-1]["open"]), closes[-2])
        if side == "long"
        else closes[-1] < min(float(candles[-1]["open"]), closes[-2])
        if side == "short"
        else False
    )
    pullback_resumption = bool(
        cfg.get("pullback_resumption_enabled", True)
        and ema_aligned
        and controlled_pullback_touch
        and directional_body
        and continuation_reacceleration
        and directional_close_location
        >= float(cfg.get("pullback_min_close_location", 0.60) or 0.60)
        and signed_fast_ema_distance_atr >= 0.0
    )

    impulse_lookback = max(
        6,
        min(
            len(candles) - 1,
            int(cfg.get("impulse_breakout_lookback_bars", 12) or 12),
        ),
    )
    impulse_rows = candles[-impulse_lookback - 1:-1]
    impulse_level = (
        max(float(row["high"]) for row in impulse_rows)
        if side == "long"
        else min(float(row["low"]) for row in impulse_rows)
        if side == "short"
        else None
    )
    impulse_level_broken = bool(
        side == "long" and impulse_level is not None and closes[-1] > impulse_level
    ) or bool(
        side == "short" and impulse_level is not None and closes[-1] < impulse_level
    )
    impulse_breakout = bool(
        cfg.get("impulse_breakout_enabled", True)
        and ema_aligned
        and impulse_level_broken
        and abs(weighted_momentum)
        >= float(cfg.get("impulse_breakout_min_momentum_strength", 0.35) or 0.35)
        and fast_momentum_retention is not None
        and fast_momentum_retention
        >= float(cfg.get("impulse_breakout_min_fast_retention", 0.55) or 0.55)
        and volume_ratio
        >= float(cfg.get("impulse_breakout_min_volume_ratio", 1.15) or 1.15)
        and directional_close_location
        >= float(cfg.get("impulse_breakout_min_close_location", 0.65) or 0.65)
        and 0.0 <= signed_fast_ema_distance_atr
        <= float(
            cfg.get("impulse_breakout_max_fast_ema_distance_atr", 1.50)
            or 1.50
        )
        and latest_range_atr
        <= float(cfg.get("impulse_breakout_max_range_atr", 2.20) or 2.20)
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

    momentum_quality = _bounded(abs(weighted_momentum) / 0.90, 0.0, 1.0)
    retention_quality = _bounded(
        (fast_momentum_retention or 0.0) / 1.00,
        0.0,
        1.0,
    )
    if 0.10 <= signed_fast_ema_distance_atr <= 1.00:
        entry_geometry_quality = 1.0
    elif signed_fast_ema_distance_atr < 0.10:
        entry_geometry_quality = _bounded(
            (signed_fast_ema_distance_atr + 0.40) / 0.50,
            0.0,
            1.0,
        )
    else:
        entry_geometry_quality = _bounded(
            1.0 - (signed_fast_ema_distance_atr - 1.00) / 1.50,
            0.0,
            1.0,
        )
    participation_quality = _bounded((volume_ratio - 0.60) / 1.00, 0.0, 1.0)
    entry_opportunity_score = 100.0 * (
        momentum_quality * 0.30
        + retention_quality * 0.20
        + trend_clarity * 0.20
        + entry_geometry_quality * 0.15
        + participation_quality * 0.15
    )
    if pullback_resumption:
        entry_opportunity_score += 6.0
    if impulse_breakout:
        entry_opportunity_score += 8.0
    entry_opportunity_score = _bounded(entry_opportunity_score, 0.0, 100.0)

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
        "fast_momentum_retention": fast_momentum_retention,
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
        "compression_ratio": compression_ratio,
        "compression_breakout": compression_breakout,
        "turning_conflict": turning_conflict,
        "weighted_continuation": weighted_continuation,
        "continuation_reacceleration": continuation_reacceleration,
        "signed_fast_ema_distance_atr": signed_fast_ema_distance_atr,
        "trend_efficiency": efficiency,
        "trend_r_squared": trend_r_squared,
        "directional_bar_ratio": directional_bar_ratio,
        "trend_clarity": trend_clarity,
        "directional_close_location": directional_close_location,
        "latest_range_atr": latest_range_atr,
        "volume_ratio": volume_ratio,
        "controlled_pullback_touch": controlled_pullback_touch,
        "pullback_resumption": pullback_resumption,
        "impulse_breakout": impulse_breakout,
        "impulse_breakout_level": impulse_level,
        "entry_opportunity_score": entry_opportunity_score,
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
    breakout_entry = bool(compression_breakout)
    metrics["breakout_entry"] = breakout_entry
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
    score += (
        9.0
        if compression_breakout
        else 8.0
        if impulse_breakout
        else 8.0
        if pullback_resumption
        else 7.0
        if ema_crossover
        else 6.0
        if weighted_continuation
        else 5.0
    )
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
        else "compression breakout"
        if compression_breakout
        else "impulse breakout"
        if impulse_breakout
        else "pullback resumption"
        if pullback_resumption
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
    "evaluate_independent_event_context",
    "resolve_independent_event_allocation",
    "evaluate_small_account_entry_refinement",
    "normalize_adaptive_breakout_trend_config",
)
