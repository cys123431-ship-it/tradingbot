"""Opportunity-aware leverage selection for accepted futures entry plans.

The selector changes margin leverage, not the stop-loss budget.  Position
quantity remains anchored to the strategy's existing risk plan and is only
restored when an earlier fixed-leverage margin cap had reduced that plan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import floor, isfinite
from typing import Any, Mapping


def default_dynamic_leverage_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "min_leverage": 2,
        "base_leverage": 4,
        "max_leverage": 10,
        "small_account_full_margin_enabled": True,
        "small_account_equity_threshold_usdt": 1_000.0,
        "small_account_min_leverage": 5,
        "normal_atr_pct_min": 0.25,
        "normal_atr_pct_max": 3.50,
        "high_volatility_atr_pct": 4.50,
        "opportunity_stop_pct_max": 2.00,
        "high_conviction_stop_pct_max": 1.50,
        "minimum_sane_stop_pct": 0.20,
        "opportunity_score_min": 78.0,
        "high_conviction_score_min": 86.0,
        "strong_single_score_min": 90.0,
        "opportunity_confirmations_min": 2,
        "high_conviction_confirmations_min": 3,
        "high_quality_l2_multiplier_min": 0.85,
        "high_conviction_l2_multiplier_min": 0.95,
        "high_quality_risk_multiplier_min": 0.80,
        "high_conviction_risk_multiplier_min": 0.90,
        "max_fresh_chase_atr": 0.30,
        "max_fresh_extension_atr": 1.50,
        "opportunity_time_stop_bars": 16,
        "high_leverage_time_stop_bars": 8,
        "opportunity_monitor_timeframe": "15m",
        "opportunity_time_stop_min_mfe_r": 0.55,
        "high_leverage_time_stop_min_mfe_r": 0.70,
        "opportunity_trailing_activation_r": 1.25,
        "high_leverage_trailing_activation_r": 1.00,
        "adaptive_trend_enabled": True,
        "adaptive_trend_profile_version": "adaptive_trend_leverage_v2",
        "adaptive_trend_base_leverage": 5,
        "adaptive_trend_opportunity_leverage": 8,
        "adaptive_trend_strong_leverage": 10,
        "adaptive_trend_elite_leverage": 15,
        "adaptive_trend_max_leverage": 15,
        "adaptive_trend_elite_score_min": 94.0,
        "adaptive_trend_strong_score_min": 90.0,
        "adaptive_trend_opportunity_score_min": 84.0,
        "adaptive_trend_elite_atr_pct_max": 1.75,
        "adaptive_trend_strong_atr_pct_max": 2.75,
        "adaptive_trend_opportunity_atr_pct_max": 3.50,
        "adaptive_trend_elite_stop_pct_max": 3.00,
        "adaptive_trend_strong_stop_pct_max": 4.50,
        "adaptive_trend_opportunity_stop_pct_max": 6.00,
        "adaptive_trend_wide_stop_pct": 8.00,
        "adaptive_trend_extreme_stop_pct": 12.00,
        "adaptive_trend_high_volatility_atr_pct": 4.50,
        "adaptive_trend_extreme_volatility_atr_pct": 6.00,
        "adaptive_trend_elite_l2_multiplier_min": 0.85,
        "adaptive_trend_strong_l2_multiplier_min": 0.75,
        "adaptive_trend_opportunity_l2_multiplier_min": 0.65,
        "adaptive_trend_elite_risk_quality_min": 0.80,
        "adaptive_trend_strong_risk_quality_min": 0.70,
        "adaptive_trend_opportunity_risk_quality_min": 0.60,
        "adaptive_trend_stop_buffer_multiple": 2.50,
    }


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _bounded(value: Any, lower: float, upper: float, default: float) -> float:
    number = _finite(value, default)
    return max(lower, min(upper, float(number)))


def normalize_dynamic_leverage_config(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    supplied = dict(config or {})
    cfg = {**default_dynamic_leverage_config(), **supplied}
    migrating_adaptive_trend = bool(
        supplied
        and supplied.get("adaptive_trend_profile_version")
        != "adaptive_trend_leverage_v2"
    )
    if migrating_adaptive_trend:
        for key in (
            "adaptive_trend_base_leverage",
            "adaptive_trend_opportunity_leverage",
            "adaptive_trend_strong_leverage",
            "adaptive_trend_elite_leverage",
            "adaptive_trend_max_leverage",
        ):
            cfg[key] = default_dynamic_leverage_config()[key]
    elif "adaptive_trend_max_leverage" not in supplied and "max_leverage" in supplied:
        cfg["adaptive_trend_max_leverage"] = supplied["max_leverage"]
    cfg["adaptive_trend_profile_version"] = "adaptive_trend_leverage_v2"
    enabled = cfg.get("enabled", True)
    cfg["enabled"] = (
        enabled
        if isinstance(enabled, bool)
        else str(enabled).strip().lower() in {"1", "true", "yes", "on", "enabled"}
    )
    adaptive_enabled = cfg.get("adaptive_trend_enabled", True)
    cfg["adaptive_trend_enabled"] = (
        adaptive_enabled
        if isinstance(adaptive_enabled, bool)
        else str(adaptive_enabled).strip().lower()
        in {"1", "true", "yes", "on", "enabled"}
    )
    full_margin_enabled = cfg.get("small_account_full_margin_enabled", True)
    cfg["small_account_full_margin_enabled"] = (
        full_margin_enabled
        if isinstance(full_margin_enabled, bool)
        else str(full_margin_enabled).strip().lower()
        in {"1", "true", "yes", "on", "enabled"}
    )
    cfg["min_leverage"] = int(_bounded(cfg.get("min_leverage"), 1, 10, 2))
    cfg["small_account_min_leverage"] = int(
        _bounded(cfg.get("small_account_min_leverage"), 1, 10, 5)
    )
    cfg["max_leverage"] = int(
        _bounded(cfg.get("max_leverage"), cfg["min_leverage"], 10, 10)
    )
    cfg["base_leverage"] = int(
        _bounded(cfg.get("base_leverage"), cfg["min_leverage"], cfg["max_leverage"], 4)
    )
    cfg["adaptive_trend_max_leverage"] = int(
        _bounded(cfg.get("adaptive_trend_max_leverage"), 1, 15, 15)
    )
    for key, default in (
        ("adaptive_trend_base_leverage", 5),
        ("adaptive_trend_opportunity_leverage", 8),
        ("adaptive_trend_strong_leverage", 10),
        ("adaptive_trend_elite_leverage", 15),
    ):
        cfg[key] = int(
            _bounded(
                cfg.get(key),
                1,
                cfg["adaptive_trend_max_leverage"],
                min(default, cfg["adaptive_trend_max_leverage"]),
            )
        )
    cfg["adaptive_trend_opportunity_leverage"] = max(
        cfg["adaptive_trend_base_leverage"],
        cfg["adaptive_trend_opportunity_leverage"],
    )
    cfg["adaptive_trend_strong_leverage"] = max(
        cfg["adaptive_trend_opportunity_leverage"],
        cfg["adaptive_trend_strong_leverage"],
    )
    cfg["adaptive_trend_elite_leverage"] = max(
        cfg["adaptive_trend_strong_leverage"],
        cfg["adaptive_trend_elite_leverage"],
    )
    for key, lower, upper in (
        ("normal_atr_pct_min", 0.0, 10.0),
        ("normal_atr_pct_max", 0.1, 20.0),
        ("high_volatility_atr_pct", 0.1, 30.0),
        ("opportunity_stop_pct_max", 0.1, 10.0),
        ("high_conviction_stop_pct_max", 0.1, 10.0),
        ("minimum_sane_stop_pct", 0.01, 5.0),
        ("opportunity_score_min", 0.0, 100.0),
        ("high_conviction_score_min", 0.0, 100.0),
        ("strong_single_score_min", 0.0, 100.0),
        ("high_quality_l2_multiplier_min", 0.0, 1.0),
        ("high_conviction_l2_multiplier_min", 0.0, 1.0),
        ("high_quality_risk_multiplier_min", 0.0, 1.0),
        ("high_conviction_risk_multiplier_min", 0.0, 1.0),
        ("max_fresh_chase_atr", 0.0, 5.0),
        ("max_fresh_extension_atr", 0.0, 10.0),
        ("opportunity_time_stop_min_mfe_r", 0.0, 5.0),
        ("high_leverage_time_stop_min_mfe_r", 0.0, 5.0),
        ("opportunity_trailing_activation_r", 0.1, 10.0),
        ("high_leverage_trailing_activation_r", 0.1, 10.0),
        ("adaptive_trend_elite_score_min", 0.0, 100.0),
        ("adaptive_trend_strong_score_min", 0.0, 100.0),
        ("adaptive_trend_opportunity_score_min", 0.0, 100.0),
        ("adaptive_trend_elite_atr_pct_max", 0.1, 20.0),
        ("adaptive_trend_strong_atr_pct_max", 0.1, 20.0),
        ("adaptive_trend_opportunity_atr_pct_max", 0.1, 20.0),
        ("adaptive_trend_elite_stop_pct_max", 0.1, 30.0),
        ("adaptive_trend_strong_stop_pct_max", 0.1, 30.0),
        ("adaptive_trend_opportunity_stop_pct_max", 0.1, 30.0),
        ("adaptive_trend_wide_stop_pct", 0.1, 50.0),
        ("adaptive_trend_extreme_stop_pct", 0.1, 50.0),
        ("adaptive_trend_high_volatility_atr_pct", 0.1, 30.0),
        ("adaptive_trend_extreme_volatility_atr_pct", 0.1, 30.0),
        ("adaptive_trend_elite_l2_multiplier_min", 0.0, 1.0),
        ("adaptive_trend_strong_l2_multiplier_min", 0.0, 1.0),
        ("adaptive_trend_opportunity_l2_multiplier_min", 0.0, 1.0),
        ("adaptive_trend_elite_risk_quality_min", 0.0, 1.0),
        ("adaptive_trend_strong_risk_quality_min", 0.0, 1.0),
        ("adaptive_trend_opportunity_risk_quality_min", 0.0, 1.0),
        ("adaptive_trend_stop_buffer_multiple", 1.5, 6.0),
        ("small_account_equity_threshold_usdt", 0.0, 1_000_000.0),
    ):
        default = float(default_dynamic_leverage_config()[key])
        cfg[key] = _bounded(cfg.get(key), lower, upper, default)
    cfg["normal_atr_pct_max"] = max(
        cfg["normal_atr_pct_min"], cfg["normal_atr_pct_max"]
    )
    cfg["high_volatility_atr_pct"] = max(
        cfg["normal_atr_pct_max"], cfg["high_volatility_atr_pct"]
    )
    cfg["high_conviction_stop_pct_max"] = min(
        cfg["opportunity_stop_pct_max"], cfg["high_conviction_stop_pct_max"]
    )
    cfg["high_conviction_score_min"] = max(
        cfg["opportunity_score_min"], cfg["high_conviction_score_min"]
    )
    cfg["strong_single_score_min"] = max(
        cfg["high_conviction_score_min"], cfg["strong_single_score_min"]
    )
    cfg["adaptive_trend_strong_score_min"] = max(
        cfg["adaptive_trend_opportunity_score_min"],
        cfg["adaptive_trend_strong_score_min"],
    )
    cfg["adaptive_trend_elite_score_min"] = max(
        cfg["adaptive_trend_strong_score_min"],
        cfg["adaptive_trend_elite_score_min"],
    )
    cfg["adaptive_trend_strong_atr_pct_max"] = max(
        cfg["adaptive_trend_elite_atr_pct_max"],
        cfg["adaptive_trend_strong_atr_pct_max"],
    )
    cfg["adaptive_trend_opportunity_atr_pct_max"] = max(
        cfg["adaptive_trend_strong_atr_pct_max"],
        cfg["adaptive_trend_opportunity_atr_pct_max"],
    )
    cfg["adaptive_trend_strong_stop_pct_max"] = max(
        cfg["adaptive_trend_elite_stop_pct_max"],
        cfg["adaptive_trend_strong_stop_pct_max"],
    )
    cfg["adaptive_trend_opportunity_stop_pct_max"] = max(
        cfg["adaptive_trend_strong_stop_pct_max"],
        cfg["adaptive_trend_opportunity_stop_pct_max"],
    )
    cfg["adaptive_trend_wide_stop_pct"] = max(
        cfg["adaptive_trend_opportunity_stop_pct_max"],
        cfg["adaptive_trend_wide_stop_pct"],
    )
    cfg["adaptive_trend_extreme_stop_pct"] = max(
        cfg["adaptive_trend_wide_stop_pct"],
        cfg["adaptive_trend_extreme_stop_pct"],
    )
    cfg["adaptive_trend_high_volatility_atr_pct"] = max(
        cfg["adaptive_trend_opportunity_atr_pct_max"],
        cfg["adaptive_trend_high_volatility_atr_pct"],
    )
    cfg["adaptive_trend_extreme_volatility_atr_pct"] = max(
        cfg["adaptive_trend_high_volatility_atr_pct"],
        cfg["adaptive_trend_extreme_volatility_atr_pct"],
    )
    cfg["adaptive_trend_strong_l2_multiplier_min"] = max(
        cfg["adaptive_trend_opportunity_l2_multiplier_min"],
        cfg["adaptive_trend_strong_l2_multiplier_min"],
    )
    cfg["adaptive_trend_elite_l2_multiplier_min"] = max(
        cfg["adaptive_trend_strong_l2_multiplier_min"],
        cfg["adaptive_trend_elite_l2_multiplier_min"],
    )
    cfg["adaptive_trend_strong_risk_quality_min"] = max(
        cfg["adaptive_trend_opportunity_risk_quality_min"],
        cfg["adaptive_trend_strong_risk_quality_min"],
    )
    cfg["adaptive_trend_elite_risk_quality_min"] = max(
        cfg["adaptive_trend_strong_risk_quality_min"],
        cfg["adaptive_trend_elite_risk_quality_min"],
    )
    for key in ("opportunity_confirmations_min", "high_conviction_confirmations_min"):
        cfg[key] = int(
            _bounded(cfg.get(key), 1, 5, default_dynamic_leverage_config()[key])
        )
    cfg["high_conviction_confirmations_min"] = max(
        cfg["opportunity_confirmations_min"], cfg["high_conviction_confirmations_min"]
    )
    for key in ("opportunity_time_stop_bars", "high_leverage_time_stop_bars"):
        cfg[key] = int(
            _bounded(cfg.get(key), 1, 96, default_dynamic_leverage_config()[key])
        )
    cfg["high_leverage_time_stop_bars"] = min(
        cfg["opportunity_time_stop_bars"], cfg["high_leverage_time_stop_bars"]
    )
    monitor_timeframe = str(cfg.get("opportunity_monitor_timeframe") or "15m").lower()
    if monitor_timeframe not in {"1m", "3m", "5m", "15m", "30m", "1h"}:
        monitor_timeframe = "15m"
    cfg["opportunity_monitor_timeframe"] = monitor_timeframe
    return cfg


def resolve_small_account_full_margin(
    config: Mapping[str, Any] | None = None,
    *,
    account_equity: float | None,
    free_balance: float | None,
) -> dict[str, Any]:
    """Resolve the explicit <=$1,000 full-margin futures sizing rule."""

    cfg = normalize_dynamic_leverage_config(config)
    equity = max(0.0, float(_finite(account_equity, 0.0)))
    free = max(0.0, float(_finite(free_balance, 0.0)))
    threshold = max(
        0.0,
        float(cfg["small_account_equity_threshold_usdt"]),
    )
    active = bool(
        cfg["small_account_full_margin_enabled"]
        and equity > 0
        and equity <= threshold
        and free > 0
    )
    return {
        "active": active,
        "account_equity": equity,
        "free_balance": free,
        "equity_threshold_usdt": threshold,
        "minimum_leverage": int(cfg["small_account_min_leverage"]),
        "margin_utilization": 1.0 if active else 0.0,
    }


def resolve_adaptive_trend_small_account_profile(
    plan: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    *,
    account_equity: float | None,
    free_balance: float | None,
    selected_leverage: int | float | None,
) -> dict[str, Any]:
    """Resolve the exclusive sub-$1,000 aggressive trend sizing profile.

    The profile replaces the regular stop-budget quantity for one new trend
    position.  It never mutates an existing position: the resulting budget,
    leverage and loss ceiling are persisted in that position's entry plan.
    """

    source = dict(plan or {})
    cfg = normalize_dynamic_leverage_config(config)
    enabled_raw = source.get("small_account_aggressive_enabled", True)
    enabled = (
        enabled_raw
        if isinstance(enabled_raw, bool)
        else str(enabled_raw).strip().lower()
        in {"1", "true", "yes", "on", "enabled"}
    )
    equity = max(0.0, float(_finite(account_equity, 0.0)))
    free = max(0.0, float(_finite(free_balance, 0.0)))
    threshold = max(
        0.0,
        float(_finite(source.get("small_account_equity_threshold_usdt"), 1_000.0)),
    )
    active = bool(enabled and equity > 0.0 and equity < threshold and free > 0.0)
    result: dict[str, Any] = {
        "active": active,
        "blocked": False,
        "block_code": None,
        "reason": "small-account aggressive profile inactive",
        "account_equity": equity,
        "free_balance": free,
        "equity_threshold_usdt": threshold,
    }
    if not active:
        return result

    margin_budget_fraction = _bounded(
        source.get("small_account_margin_budget_fraction"),
        0.50,
        0.98,
        0.95,
    )
    initial_margin_fraction = _bounded(
        source.get("small_account_initial_margin_fraction"),
        0.40,
        1.00,
        0.65,
    )
    capital_basis = min(equity, free)
    margin_budget = capital_basis * margin_budget_fraction
    initial_margin = margin_budget * initial_margin_fraction
    risk_tier = str(
        source.get("adaptive_trend_risk_tier")
        or (source.get("adaptive_breakout_trend_metrics") or {}).get("risk_tier")
        or "base"
    ).strip().lower()
    if risk_tier not in {"base", "strong", "elite"}:
        risk_tier = "base"
    max_loss_percent = _bounded(
        source.get(f"small_account_{risk_tier}_max_loss_percent"),
        0.0,
        50.0,
        {"base": 20.0, "strong": 30.0, "elite": 35.0}[risk_tier],
    )
    # Retain the legacy config field for backward-compatible config loading,
    # but it is deliberately disabled for this dedicated profile.
    daily_limit_percent = 0.0
    cost_buffer_percent = _bounded(
        source.get("small_account_cost_buffer_percent"),
        0.0,
        2.0,
        0.20,
    )
    minimum_leverage = int(
        _bounded(
            source.get("small_account_min_leverage"),
            1.0,
            20.0,
            4.0,
        )
    )
    strong_leverage = int(
        _bounded(
            source.get("small_account_strong_leverage"),
            float(minimum_leverage),
            20.0,
            6.0,
        )
    )
    elite_leverage = int(
        _bounded(
            source.get("small_account_elite_leverage"),
            float(strong_leverage),
            20.0,
            7.0,
        )
    )
    desired_by_tier = {
        "base": minimum_leverage,
        "strong": strong_leverage,
        "elite": elite_leverage,
    }
    # The dedicated small-account tier is the source of truth. Reusing the
    # regular selector's 5/8/10/15x answer here would let a nominal base setup
    # escape the requested 4x base and silently reintroduce the old ladder.
    desired_leverage = desired_by_tier[risk_tier]
    desired_leverage = min(
        desired_leverage,
        int(cfg["adaptive_trend_max_leverage"]),
        elite_leverage,
    )
    explicit_ceiling = _finite(source.get("small_account_aggressive_leverage_ceiling"))
    if explicit_ceiling is not None and explicit_ceiling > 0:
        desired_leverage = min(desired_leverage, int(explicit_ceiling))

    entry = max(0.0, float(_finite(source.get("entry_price"), 0.0)))
    risk_distance = max(0.0, float(_finite(source.get("risk_distance"), 0.0)))
    stop_percent = _nested_metric(source, "risk_distance_pct")
    if stop_percent is None and entry > 0.0 and risk_distance > 0.0:
        stop_percent = risk_distance / entry * 100.0
    if stop_percent is None or stop_percent <= 0.0:
        result.update({
            "blocked": True,
            "block_code": "ENTRY_BLOCKED_SMALL_ACCOUNT_STOP_UNAVAILABLE",
            "reason": "small-account aggressive stop distance unavailable",
        })
        return result

    stop_buffer_multiple = _bounded(
        source.get("small_account_liquidation_stop_buffer_multiple"),
        1.0,
        3.0,
        1.50,
    )
    liquidation_leverage_cap = int(
        max(
            0.0,
            min(
                float(cfg["adaptive_trend_max_leverage"]),
                floor(100.0 / (float(stop_percent) * stop_buffer_multiple)),
            ),
        )
    )
    desired_leverage = min(desired_leverage, liquidation_leverage_cap)
    try:
        leverage_steps = {
            int(float(value))
            for value in source.get("small_account_leverage_steps", (4, 5, 6, 7))
            if minimum_leverage <= int(float(value)) <= elite_leverage
        }
    except (TypeError, ValueError):
        leverage_steps = set()
    leverage_steps.update({minimum_leverage, strong_leverage, elite_leverage})
    candidates = sorted(
        (
            value
            for value in leverage_steps
            if minimum_leverage <= value <= desired_leverage
        ),
        reverse=True,
    )
    # This profile intentionally has no daily-loss stop. Per-position stop
    # risk, liquidation distance and available margin remain enforced.
    daily_pnl = float(
        _finite(source.get("small_account_aggressive_daily_pnl_usdt"), 0.0)
    )
    max_loss_usdt = equity * max_loss_percent / 100.0
    daily_loss_limit_usdt = 0.0
    remaining_daily_loss_usdt = 0.0

    selected = None
    selected_payload: dict[str, float] = {}
    for candidate in candidates:
        initial_notional = initial_margin * float(candidate)
        price_risk_usdt = initial_notional * float(stop_percent) / 100.0
        estimated_cost_usdt = initial_notional * cost_buffer_percent / 100.0
        projected_loss_usdt = price_risk_usdt + estimated_cost_usdt
        if projected_loss_usdt <= max_loss_usdt + 1e-9:
            selected = candidate
            selected_payload = {
                "initial_notional": initial_notional,
                "price_risk_usdt": price_risk_usdt,
                "estimated_cost_usdt": estimated_cost_usdt,
                "projected_loss_usdt": projected_loss_usdt,
            }
            break

    result.update({
        "risk_tier": risk_tier,
        "margin_budget_fraction": margin_budget_fraction,
        "initial_margin_fraction": initial_margin_fraction,
        "capital_basis_usdt": capital_basis,
        "margin_budget_usdt": margin_budget,
        "initial_margin_usdt": initial_margin,
        "minimum_leverage": minimum_leverage,
        "desired_leverage": desired_by_tier[risk_tier],
        "liquidation_leverage_cap": liquidation_leverage_cap,
        "liquidation_stop_buffer_multiple": stop_buffer_multiple,
        "stop_distance_percent": float(stop_percent),
        "cost_buffer_percent": cost_buffer_percent,
        "max_loss_percent": max_loss_percent,
        "max_loss_usdt": max_loss_usdt,
        "daily_loss_limit_percent": daily_limit_percent,
        "daily_loss_limit_usdt": daily_loss_limit_usdt,
        "daily_pnl_usdt": daily_pnl,
        "remaining_daily_loss_usdt": remaining_daily_loss_usdt,
    })
    if selected is None:
        reason = (
            f"no leverage >= {minimum_leverage}x fits loss limits: "
            f"stop={float(stop_percent):.2f}% tierCap={max_loss_percent:.1f}% "
            "dailyLoss=exempt"
        )
        result.update({
            "blocked": True,
            "block_code": "ENTRY_BLOCKED_SMALL_ACCOUNT_AGGRESSIVE_RISK",
            "reason": reason,
        })
        return result

    full_target_notional = margin_budget * float(selected)
    result.update(selected_payload)
    result.update({
        "leverage": int(selected),
        "full_target_notional": full_target_notional,
        "actual_projected_loss_percent": (
            selected_payload["projected_loss_usdt"] / equity * 100.0
        ),
        "reason": (
            f"small-account aggressive {risk_tier}: {int(selected)}x, "
            f"margin={initial_margin:.2f}/{margin_budget:.2f}, "
            f"projectedLoss={selected_payload['projected_loss_usdt']:.2f} "
            f"({selected_payload['projected_loss_usdt'] / equity * 100.0:.2f}%)"
        ),
    })
    return result


def _score_value(value: Any) -> float | None:
    score = _finite(value)
    if score is None:
        return None
    if 0.0 <= score <= 1.0:
        score *= 100.0
    return max(0.0, min(100.0, score))


def _quality_score(plan: Mapping[str, Any]) -> tuple[float, str]:
    preferred = (
        "quad_alpha_score",
        "triple_alpha_score",
        "dual_alpha_score",
        "adaptive_breakout_trend_score",
        "vmt_score",
        "crowding_score",
        "lxr_score",
        "strategy_quality_score",
        "quality_score_v2_score",
        "profit_alpha_score",
        "entry_edge_score",
        "feature_score_value",
        "selector_quality_score",
        "trend_health_score",
    )
    for key in preferred:
        score = _score_value(plan.get(key))
        if score is not None:
            return score, key
    return 72.0, "neutral_default"


def _confirmation_count(plan: Mapping[str, Any]) -> int:
    for key in (
        "quad_alpha_confirmation_count",
        "triple_alpha_confirmation_count",
        "dual_alpha_confirmation_count",
    ):
        value = _finite(plan.get(key))
        if value is not None:
            return max(1, min(5, int(value)))
    return 1


def _nested_metric(plan: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _finite(plan.get(key))
        if value is not None:
            return value
    for container_key in (
        "adaptive_breakout_trend_metrics",
        "vmt_metrics",
        "crowding_metrics",
        "lxr_metrics",
        "rspt_logs",
    ):
        container = plan.get(container_key)
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            value = _finite(container.get(key))
            if value is not None:
                return value
    return None


def _risk_quality_multiplier(plan: Mapping[str, Any]) -> float:
    """Return execution/confidence quality without reusing sizing overlays.

    Strategy-native risk multipliers (VMT, crowding, LXR and RSPT) already
    scale the stop-budget position size.  Treating those same values as a
    leverage-quality veto made high leverage structurally unreachable for
    strategies whose sizing cap is below the selector's 0.80/0.90 quality
    thresholds.  The leverage selector still observes market, volatility,
    allocator, trend, feature and execution quality independently.
    """
    values: list[float] = []
    for key in (
        "strategy_allocator_multiplier",
        "market_quality_risk_multiplier",
        "strategy_quality_risk_multiplier",
        "quality_score_v2_risk_multiplier",
        "volatility_risk_multiplier",
        "meta_label_risk_multiplier",
        "trend_health_risk_multiplier",
        "selector_quality_risk_multiplier",
        "feature_score_risk_multiplier",
    ):
        value = _finite(plan.get(key))
        if value is not None:
            values.append(max(0.0, min(1.0, value)))
    return min(values) if values else 1.0


def _l2_quality(plan: Mapping[str, Any]) -> tuple[str, float, bool, bool]:
    gate = plan.get("l2_gate") if isinstance(plan.get("l2_gate"), Mapping) else {}
    state = str(plan.get("l2_state") or gate.get("state") or "unavailable").lower()
    multiplier = _finite(
        plan.get("l2_risk_multiplier"), _finite(gate.get("risk_multiplier"), 1.0)
    )
    multiplier = max(0.0, min(1.0, float(multiplier)))
    allowed = bool(gate.get("allowed", state not in {"stressed", "stressed_thin"}))
    side = str(plan.get("side") or "").lower()
    support = str(gate.get("direction_support") or "").lower()
    directional_ok = not support or support == side
    high_quality = (
        allowed
        and directional_ok
        and state in {"deep_balanced", "bid_support", "ask_pressure", "calm"}
    )
    observable = state not in {"", "disabled", "unavailable", "unknown", "none"}
    return state, multiplier, high_quality, observable


@dataclass(frozen=True)
class DynamicLeverageDecision:
    leverage: int
    tier: str
    opportunity_score: float
    quality_score: float
    quality_source: str
    confirmation_count: int
    atr_pct: float | None
    stop_distance_pct: float | None
    l2_state: str
    l2_multiplier: float
    risk_quality_multiplier: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _select_adaptive_trend_leverage(
    *,
    cfg: Mapping[str, Any],
    quality: float,
    quality_source: str,
    confirmations: int,
    atr_pct: float | None,
    stop_pct: float | None,
    fresh: bool,
    l2_state: str,
    l2_multiplier: float,
    l2_high_quality: bool,
    l2_observable: bool,
    risk_quality: float,
) -> DynamicLeverageDecision:
    """Select trend leverage from signal quality and stop-to-liquidation room."""

    minimum = int(cfg["min_leverage"])
    maximum = int(cfg["adaptive_trend_max_leverage"])
    base = int(cfg["adaptive_trend_base_leverage"])
    stop_buffer_cap = maximum
    if stop_pct is not None and stop_pct > 0:
        stop_buffer_cap = int(
            max(
                minimum,
                min(
                    maximum,
                    floor(
                        100.0
                        / (
                            stop_pct
                            * float(cfg["adaptive_trend_stop_buffer_multiple"])
                        )
                    ),
                ),
            )
        )

    extreme = bool(
        (atr_pct is not None and atr_pct >= cfg["adaptive_trend_extreme_volatility_atr_pct"])
        or (stop_pct is not None and stop_pct >= cfg["adaptive_trend_extreme_stop_pct"])
    )
    wide = bool(
        (atr_pct is not None and atr_pct >= cfg["adaptive_trend_high_volatility_atr_pct"])
        or (stop_pct is not None and stop_pct >= cfg["adaptive_trend_wide_stop_pct"])
    )
    data_complete = atr_pct is not None and stop_pct is not None and l2_observable

    tier = "adaptive_trend_standard"
    leverage = min(base, maximum)
    if l2_state in {"stressed", "stressed_thin"}:
        tier = "adaptive_trend_defensive_l2"
        leverage = minimum
    elif extreme:
        tier = "adaptive_trend_extreme_volatility"
        leverage = minimum
    elif wide:
        tier = "adaptive_trend_wide_stop"
        leverage = min(3, maximum)
    elif data_complete and all(
        (
            quality >= cfg["adaptive_trend_elite_score_min"],
            atr_pct <= cfg["adaptive_trend_elite_atr_pct_max"],
            stop_pct <= cfg["adaptive_trend_elite_stop_pct_max"],
            fresh,
            l2_high_quality,
            l2_multiplier >= cfg["adaptive_trend_elite_l2_multiplier_min"],
            risk_quality >= cfg["adaptive_trend_elite_risk_quality_min"],
        )
    ):
        tier = "adaptive_trend_elite"
        leverage = min(maximum, int(cfg["adaptive_trend_elite_leverage"]))
    elif data_complete and all(
        (
            quality >= cfg["adaptive_trend_strong_score_min"],
            atr_pct <= cfg["adaptive_trend_strong_atr_pct_max"],
            stop_pct <= cfg["adaptive_trend_strong_stop_pct_max"],
            fresh,
            l2_high_quality,
            l2_multiplier >= cfg["adaptive_trend_strong_l2_multiplier_min"],
            risk_quality >= cfg["adaptive_trend_strong_risk_quality_min"],
        )
    ):
        tier = "adaptive_trend_strong"
        leverage = min(maximum, int(cfg["adaptive_trend_strong_leverage"]))
    elif data_complete and all(
        (
            quality >= cfg["adaptive_trend_opportunity_score_min"],
            atr_pct <= cfg["adaptive_trend_opportunity_atr_pct_max"],
            stop_pct <= cfg["adaptive_trend_opportunity_stop_pct_max"],
            fresh,
            l2_high_quality,
            l2_multiplier >= cfg["adaptive_trend_opportunity_l2_multiplier_min"],
            risk_quality >= cfg["adaptive_trend_opportunity_risk_quality_min"],
        )
    ):
        tier = "adaptive_trend_opportunity"
        leverage = min(maximum, int(cfg["adaptive_trend_opportunity_leverage"]))
    elif not data_complete:
        tier = "adaptive_trend_data_fallback"
        leverage = min(leverage, 4)
    elif risk_quality < 0.60 or l2_multiplier < 0.60:
        tier = "adaptive_trend_defensive_quality"
        leverage = min(leverage, 3)

    leverage = max(minimum, min(maximum, stop_buffer_cap, leverage))
    reason = (
        f"{tier}: quality={quality:.1f}({quality_source}) confirmations={confirmations} "
        f"ATR%={atr_pct if atr_pct is not None else 'n/a'} "
        f"stop%={stop_pct if stop_pct is not None else 'n/a'} "
        f"L2={l2_state}/{l2_multiplier:.2f} riskQuality={risk_quality:.2f} "
        f"stopBufferCap={stop_buffer_cap}x"
    )
    return DynamicLeverageDecision(
        leverage=leverage,
        tier=tier,
        opportunity_score=float(leverage),
        quality_score=quality,
        quality_source=quality_source,
        confirmation_count=confirmations,
        atr_pct=atr_pct,
        stop_distance_pct=stop_pct,
        l2_state=l2_state,
        l2_multiplier=l2_multiplier,
        risk_quality_multiplier=risk_quality,
        reason=reason,
    )


def select_dynamic_leverage(
    plan: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> DynamicLeverageDecision:
    plan = dict(plan or {})
    cfg = normalize_dynamic_leverage_config(config)
    legacy = int(
        _bounded(plan.get("leverage"), 1, cfg["max_leverage"], cfg["base_leverage"])
    )
    quality, quality_source = _quality_score(plan)
    confirmations = _confirmation_count(plan)
    entry = _finite(plan.get("entry_price"))
    stop_pct = _nested_metric(plan, "risk_distance_pct")
    if stop_pct is None and entry and entry > 0:
        distance = _finite(plan.get("risk_distance"))
        if distance is not None:
            stop_pct = abs(distance) / entry * 100.0
    atr_pct = _nested_metric(plan, "atr_pct")
    if atr_pct is None and entry and entry > 0:
        atr = _nested_metric(plan, "atr")
        if atr is not None:
            atr_pct = abs(atr) / entry * 100.0
    chase = _nested_metric(plan, "entry_chase_atr", "rspt_entry_chase_atr")
    extension = _nested_metric(
        plan,
        "ev_extension_atr",
        "bias_continuation_extension_atr",
        "extension_atr",
    )
    risk_quality = _risk_quality_multiplier(plan)
    l2_state, l2_multiplier, l2_high_quality, l2_observable = _l2_quality(plan)

    if not cfg["enabled"]:
        return DynamicLeverageDecision(
            leverage=legacy,
            tier="fixed",
            opportunity_score=0.0,
            quality_score=quality,
            quality_source=quality_source,
            confirmation_count=confirmations,
            atr_pct=atr_pct,
            stop_distance_pct=stop_pct,
            l2_state=l2_state,
            l2_multiplier=l2_multiplier,
            risk_quality_multiplier=risk_quality,
            reason=f"dynamic disabled; configured {legacy}x",
        )
    if atr_pct is None and not l2_observable:
        return DynamicLeverageDecision(
            leverage=legacy,
            tier="data_fallback",
            opportunity_score=float(legacy),
            quality_score=quality,
            quality_source=quality_source,
            confirmation_count=confirmations,
            atr_pct=atr_pct,
            stop_distance_pct=stop_pct,
            l2_state=l2_state,
            l2_multiplier=l2_multiplier,
            risk_quality_multiplier=risk_quality,
            reason=f"data_fallback: ATR and observable L2 unavailable; preserve {legacy}x",
        )

    normal_vol = (
        atr_pct is not None
        and cfg["normal_atr_pct_min"] <= atr_pct <= cfg["normal_atr_pct_max"]
    )
    high_vol = atr_pct is not None and atr_pct >= cfg["high_volatility_atr_pct"]
    sane_stop = stop_pct is not None and stop_pct >= cfg["minimum_sane_stop_pct"]
    opportunity_stop = sane_stop and stop_pct <= cfg["opportunity_stop_pct_max"]
    high_stop = sane_stop and stop_pct <= cfg["high_conviction_stop_pct_max"]
    fresh = (chase is None or chase <= cfg["max_fresh_chase_atr"]) and (
        extension is None or extension <= cfg["max_fresh_extension_atr"]
    )
    if (
        cfg["adaptive_trend_enabled"]
        and str(plan.get("strategy") or "").strip().lower()
        == "adaptive_breakout_trend_v1"
    ):
        return _select_adaptive_trend_leverage(
            cfg=cfg,
            quality=quality,
            quality_source=quality_source,
            confirmations=confirmations,
            atr_pct=atr_pct,
            stop_pct=stop_pct,
            fresh=fresh,
            l2_state=l2_state,
            l2_multiplier=l2_multiplier,
            l2_high_quality=l2_high_quality,
            l2_observable=l2_observable,
            risk_quality=risk_quality,
        )
    l2_opportunity = (
        l2_high_quality and l2_multiplier >= cfg["high_quality_l2_multiplier_min"]
    )
    opportunity = all(
        (
            confirmations >= cfg["opportunity_confirmations_min"],
            quality >= cfg["opportunity_score_min"],
            normal_vol,
            opportunity_stop,
            fresh,
            l2_opportunity,
            risk_quality >= cfg["high_quality_risk_multiplier_min"],
        )
    )
    high_conviction = all(
        (
            confirmations >= cfg["high_conviction_confirmations_min"],
            quality >= cfg["high_conviction_score_min"],
            normal_vol,
            high_stop,
            fresh,
            l2_high_quality,
            l2_multiplier >= cfg["high_conviction_l2_multiplier_min"],
            risk_quality >= cfg["high_conviction_risk_multiplier_min"],
        )
    )
    strong_single = all(
        (
            confirmations == 1,
            quality >= cfg["strong_single_score_min"],
            normal_vol,
            high_stop,
            fresh,
            l2_opportunity,
            risk_quality >= cfg["high_conviction_risk_multiplier_min"],
        )
    )

    opportunity_score = float(cfg["base_leverage"])
    if quality >= cfg["high_conviction_score_min"]:
        opportunity_score += 2.0
    elif quality >= cfg["opportunity_score_min"]:
        opportunity_score += 1.0
    elif quality < 68.0:
        opportunity_score -= 1.0
    if normal_vol:
        opportunity_score += 1.0
    elif high_vol:
        opportunity_score -= 2.0
    if high_stop:
        opportunity_score += 1.0
    elif stop_pct is not None and stop_pct > cfg["opportunity_stop_pct_max"]:
        opportunity_score -= 1.0
    if l2_opportunity:
        opportunity_score += 1.0
    elif l2_multiplier < 0.75:
        opportunity_score -= 1.0
    if confirmations >= 2:
        opportunity_score += 1.0
    if confirmations >= 3:
        opportunity_score += 1.0
    if not fresh:
        opportunity_score -= 2.0
    if risk_quality < 0.75:
        opportunity_score -= 1.0

    leverage = int(round(opportunity_score))
    cap = 5
    tier = "standard"
    if high_conviction:
        cap = cfg["max_leverage"]
        tier = "high_conviction"
    elif opportunity:
        cap = min(cfg["max_leverage"], 8)
        tier = "opportunity"
    elif strong_single:
        # Exceptional single-strategy signals are intentionally allowed to use
        # the short-horizon 8x profile.  Their stop-risk size remains governed
        # by the aggregate single-signal multiplier and strategy sizing overlay.
        cap = min(cfg["max_leverage"], 8)
        tier = "strong_single"
    elif confirmations >= 2 and quality >= cfg["opportunity_score_min"]:
        cap = min(cfg["max_leverage"], 6)
        tier = "confirmed_standard"

    if not l2_observable:
        cap = min(cap, 5)
    if high_vol or (
        stop_pct is not None and stop_pct > cfg["opportunity_stop_pct_max"] * 1.5
    ):
        cap = min(cap, 4)
        tier = "defensive_volatility"
    if risk_quality < 0.60 or (not l2_high_quality and l2_multiplier < 0.60):
        cap = min(cap, 3)
        tier = "defensive_quality"
    if l2_state in {"stressed", "stressed_thin"}:
        cap = cfg["min_leverage"]
        tier = "defensive_l2"

    leverage = max(cfg["min_leverage"], min(cfg["max_leverage"], cap, leverage))
    reason = (
        f"{tier}: quality={quality:.1f}({quality_source}) confirmations={confirmations} "
        f"ATR%={atr_pct if atr_pct is not None else 'n/a'} "
        f"stop%={stop_pct if stop_pct is not None else 'n/a'} "
        f"L2={l2_state}/{l2_multiplier:.2f} riskQuality={risk_quality:.2f}"
    )
    return DynamicLeverageDecision(
        leverage=leverage,
        tier=tier,
        opportunity_score=opportunity_score,
        quality_score=quality,
        quality_source=quality_source,
        confirmation_count=confirmations,
        atr_pct=atr_pct,
        stop_distance_pct=stop_pct,
        l2_state=l2_state,
        l2_multiplier=l2_multiplier,
        risk_quality_multiplier=risk_quality,
        reason=reason,
    )


def apply_dynamic_leverage_to_plan(
    plan: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
    *,
    free_balance: float | None = None,
    account_equity: float | None = None,
    safety_buffer: float = 0.98,
) -> dict[str, Any]:
    updated = dict(plan or {})
    strategy_owned_small_account_min_leverage = _finite(
        updated.get("small_account_min_leverage")
    )
    cfg = normalize_dynamic_leverage_config(config)
    original_exit_fields = {
        "ev_time_stop_enabled": "dynamic_leverage_original_ev_time_stop_enabled",
        "ev_time_stop_bars": "dynamic_leverage_original_ev_time_stop_bars",
        "ev_time_stop_min_mfe_r": "dynamic_leverage_original_ev_time_stop_min_mfe_r",
        "atr_trailing_activation_r": "dynamic_leverage_original_atr_trailing_activation_r",
    }
    for field, original_field in original_exit_fields.items():
        if original_field not in updated and field in updated:
            updated[original_field] = updated.get(field)
        elif original_field in updated:
            updated[field] = updated.get(original_field)
    decision = select_dynamic_leverage(updated, cfg)
    leverage = int(decision.leverage)
    is_adaptive_trend = (
        str(updated.get("strategy") or "").strip().lower()
        == "adaptive_breakout_trend_v1"
    )
    small_account_policy = resolve_small_account_full_margin(
        cfg,
        account_equity=account_equity,
        free_balance=free_balance,
    )
    aggressive_trend_policy = resolve_adaptive_trend_small_account_profile(
        updated,
        cfg,
        account_equity=account_equity,
        free_balance=free_balance,
        selected_leverage=leverage,
    ) if is_adaptive_trend else {"active": False, "blocked": False}
    if aggressive_trend_policy.get("active") and not aggressive_trend_policy.get("blocked"):
        leverage = int(aggressive_trend_policy["leverage"])
    elif small_account_policy["active"]:
        leverage = max(
            leverage,
            int(small_account_policy["minimum_leverage"]),
        )
        leverage_cap = (
            int(cfg["adaptive_trend_max_leverage"])
            if is_adaptive_trend
            else int(cfg["max_leverage"])
        )
        leverage = min(
            leverage,
            max(
                leverage_cap,
                int(small_account_policy["minimum_leverage"]),
            ),
        )
    decision_tier = decision.tier
    decision_reason = decision.reason
    if aggressive_trend_policy.get("active"):
        decision_tier = f"{decision_tier}+small_account_aggressive"
        decision_reason = (
            f"{decision_reason}; {aggressive_trend_policy.get('reason')}"
        )
    elif small_account_policy["active"]:
        small_account_label = (
            "small_account_min_leverage"
            if is_adaptive_trend
            else "small_account_full_margin"
        )
        decision_tier = f"{decision_tier}+{small_account_label}"
        decision_reason = (
            f"{decision_reason}; small account {small_account_label}: "
            f"equity={small_account_policy['account_equity']:.2f} "
            f"<= {small_account_policy['equity_threshold_usdt']:.2f}, "
            f"free={small_account_policy['free_balance']:.2f}, "
            f"minimum={small_account_policy['minimum_leverage']}x"
        )
    strategy_ceiling = _finite(updated.get("strategy_leverage_ceiling"))
    if strategy_ceiling is not None and strategy_ceiling > 0:
        capped_leverage = min(leverage, max(1, int(strategy_ceiling)))
        if capped_leverage < leverage:
            decision_tier = f"{decision_tier}+strategy_cap"
            decision_reason = (
                f"{decision_reason}; strategy leverage ceiling "
                f"{int(strategy_ceiling)}x"
            )
            leverage = capped_leverage
    decision_payload = decision.as_dict()
    decision_payload.update(
        {
            "leverage": leverage,
            "tier": decision_tier,
            "reason": decision_reason,
        }
    )
    updated.update(
        {
            "leverage": leverage,
            "dynamic_leverage_applied": bool(cfg["enabled"]),
            "dynamic_leverage_tier": decision_tier,
            "dynamic_leverage_score": float(decision.opportunity_score),
            "dynamic_leverage_reason": decision_reason,
            "dynamic_leverage_decision": decision_payload,
            "small_account_full_margin_applied": bool(
                small_account_policy["active"] and not is_adaptive_trend
            ),
            "small_account_equity_usdt": float(
                small_account_policy["account_equity"]
            ),
            "small_account_equity_threshold_usdt": float(
                small_account_policy["equity_threshold_usdt"]
            ),
            "small_account_min_leverage": int(
                strategy_owned_small_account_min_leverage
                if (
                    is_adaptive_trend
                    and strategy_owned_small_account_min_leverage is not None
                )
                else small_account_policy["minimum_leverage"]
            ),
            "small_account_aggressive_active": bool(
                aggressive_trend_policy.get("active")
            ),
            "small_account_aggressive_blocked": bool(
                aggressive_trend_policy.get("blocked")
            ),
            "small_account_aggressive_block_code": aggressive_trend_policy.get(
                "block_code"
            ),
            "small_account_aggressive_reason": aggressive_trend_policy.get(
                "reason"
            ),
        }
    )

    if aggressive_trend_policy.get("active"):
        updated.update({
            "small_account_equity_usdt": float(
                aggressive_trend_policy.get("account_equity", 0.0)
            ),
            "small_account_equity_threshold_usdt": float(
                aggressive_trend_policy.get("equity_threshold_usdt", 1_000.0)
            ),
            "small_account_min_leverage": int(
                aggressive_trend_policy.get("minimum_leverage", 4)
            ),
            "small_account_target_margin_usdt": float(
                aggressive_trend_policy.get("margin_budget_usdt", 0.0)
            ),
            "small_account_margin_utilization": float(
                aggressive_trend_policy.get("initial_margin_fraction", 0.65)
            ),
            "small_account_aggressive_initial_margin_usdt": float(
                aggressive_trend_policy.get("initial_margin_usdt", 0.0)
            ),
            "small_account_aggressive_max_loss_percent": float(
                aggressive_trend_policy.get("max_loss_percent", 0.0)
            ),
            "small_account_aggressive_max_loss_usdt": float(
                aggressive_trend_policy.get("max_loss_usdt", 0.0)
            ),
            "small_account_aggressive_daily_loss_limit_percent": float(
                aggressive_trend_policy.get("daily_loss_limit_percent", 0.0)
            ),
            "small_account_aggressive_daily_loss_limit_usdt": float(
                aggressive_trend_policy.get("daily_loss_limit_usdt", 0.0)
            ),
            "small_account_aggressive_projected_loss_usdt": float(
                aggressive_trend_policy.get("projected_loss_usdt", 0.0)
            ),
            "small_account_aggressive_projected_loss_percent": float(
                aggressive_trend_policy.get("actual_projected_loss_percent", 0.0)
            ),
            "small_account_aggressive_cost_buffer_percent": float(
                aggressive_trend_policy.get("cost_buffer_percent", 0.0)
            ),
            "small_account_aggressive_risk_tier": aggressive_trend_policy.get(
                "risk_tier"
            ),
            "risk_budget_mode": "adaptive_trend_small_account_aggressive",
        })
        if aggressive_trend_policy.get("blocked"):
            updated.update({
                "qty": 0.0,
                "planned_notional": 0.0,
                "planned_margin": 0.0,
                "risk_usdt": 0.0,
                "expected_profit_usdt": 0.0,
            })
            return updated

    if (
        not cfg["enabled"]
        and not (small_account_policy["active"] and not is_adaptive_trend)
        and not aggressive_trend_policy.get("active")
    ):
        current_notional = max(
            0.0, float(_finite(updated.get("planned_notional"), 0.0))
        )
        if current_notional > 0:
            updated["planned_margin"] = current_notional / max(float(leverage), 1.0)
        return updated

    entry_for_notional = max(0.0, float(_finite(updated.get("entry_price"), 0.0)))
    qty_for_notional = max(0.0, float(_finite(updated.get("qty"), 0.0)))
    current_notional = max(
        0.0,
        float(
            _finite(
                updated.get("planned_notional"), qty_for_notional * entry_for_notional
            )
        ),
    )
    desired_notional = max(
        current_notional,
        float(_finite(updated.get("position_cap_original_notional"), 0.0)),
    )
    free = _finite(free_balance)
    if free is not None:
        full_margin_active = bool(
            small_account_policy["active"] and not is_adaptive_trend
        )
        effective_safety_buffer = (
            1.0
            if full_margin_active
            else max(0.0, min(1.0, safety_buffer))
        )
        margin_cap_notional = (
            max(0.0, free) * leverage * effective_safety_buffer
        )
        aggressive_trend_active = bool(
            aggressive_trend_policy.get("active")
            and not aggressive_trend_policy.get("blocked")
        )
        if aggressive_trend_active:
            target_notional = min(
                float(aggressive_trend_policy["initial_notional"]),
                margin_cap_notional,
            )
        else:
            target_notional = (
                margin_cap_notional
                if full_margin_active
                else min(desired_notional, margin_cap_notional)
            )
        entry = max(0.0, float(_finite(updated.get("entry_price"), 0.0)))
        risk_distance = max(0.0, float(_finite(updated.get("risk_distance"), 0.0)))
        rr = max(
            0.0,
            float(
                _finite(
                    updated.get("effective_rr_multiple"),
                    _finite(updated.get("rr_multiple"), 0.0),
                )
            ),
        )
        if entry > 0 and target_notional >= 0:
            qty = target_notional / entry
            updated.update(
                {
                    "qty": qty,
                    "planned_notional": target_notional,
                    "planned_margin": target_notional / max(float(leverage), 1.0),
                    "risk_usdt": qty * risk_distance,
                    "expected_profit_usdt": qty * risk_distance * rr,
                    "position_cap_applied": target_notional + 1e-9 < desired_notional,
                    "position_cap_reason": (
                        "dynamic_leverage_margin_cap"
                        if target_notional + 1e-9 < desired_notional
                        else "dynamic_leverage_restored_risk_plan"
                    ),
                    "position_cap_original_notional": desired_notional,
                    "position_cap_max_notional": margin_cap_notional,
                    "dynamic_leverage_restored_notional": max(
                        0.0, target_notional - current_notional
                    ),
                    "small_account_target_margin_usdt": (
                        max(0.0, free)
                        if full_margin_active
                        else 0.0
                    ),
                    "small_account_margin_utilization": (
                        1.0 if full_margin_active else 0.0
                    ),
                }
            )
            if aggressive_trend_active:
                full_target_notional = float(
                    aggressive_trend_policy["full_target_notional"]
                )
                full_target_qty = full_target_notional / entry
                full_target_risk = full_target_qty * risk_distance
                initial_fraction = float(
                    aggressive_trend_policy["initial_margin_fraction"]
                )
                updated.update({
                    "adaptive_trend_target_qty": full_target_qty,
                    "adaptive_trend_target_notional": full_target_notional,
                    "adaptive_trend_target_risk_usdt": full_target_risk,
                    "adaptive_trend_initial_fraction": initial_fraction,
                    "position_cap_original_notional": target_notional,
                    "position_cap_max_notional": margin_cap_notional,
                    "position_cap_applied": target_notional + 1e-9 < float(
                        aggressive_trend_policy["initial_notional"]
                    ),
                    "position_cap_reason": (
                        "small_account_aggressive_margin_changed"
                        if target_notional + 1e-9 < float(
                            aggressive_trend_policy["initial_notional"]
                        )
                        else "small_account_aggressive_initial_stage"
                    ),
                    "small_account_target_margin_usdt": float(
                        aggressive_trend_policy["margin_budget_usdt"]
                    ),
                    "small_account_margin_utilization": initial_fraction,
                    "dynamic_leverage_restored_notional": 0.0,
                    "adaptive_breakout_trend_target_risk_percent": float(
                        aggressive_trend_policy["actual_projected_loss_percent"]
                    ),
                })
    elif current_notional > 0:
        updated["planned_margin"] = current_notional / max(float(leverage), 1.0)

    if is_adaptive_trend and leverage >= 6:
        # High leverage changes margin efficiency, not the trend's economic
        # horizon.  The previous generic shortcut forced an elite 1h trend
        # into an 8-bar time stop and 1R trail, cutting the large winners the
        # strategy is designed to capture.
        updated["dynamic_leverage_monitor_timeframe"] = cfg[
            "opportunity_monitor_timeframe"
        ]
    elif leverage >= 8:
        updated["dynamic_leverage_monitor_timeframe"] = cfg[
            "opportunity_monitor_timeframe"
        ]
        updated["ev_time_stop_enabled"] = True
        updated["ev_time_stop_bars"] = min(
            int(
                updated.get("ev_time_stop_bars", cfg["high_leverage_time_stop_bars"])
                or cfg["high_leverage_time_stop_bars"]
            ),
            cfg["high_leverage_time_stop_bars"],
        )
        updated["ev_time_stop_min_mfe_r"] = max(
            float(updated.get("ev_time_stop_min_mfe_r", 0.0) or 0.0),
            cfg["high_leverage_time_stop_min_mfe_r"],
        )
        updated["atr_trailing_activation_r"] = min(
            float(
                updated.get(
                    "atr_trailing_activation_r",
                    cfg["high_leverage_trailing_activation_r"],
                )
                or cfg["high_leverage_trailing_activation_r"]
            ),
            cfg["high_leverage_trailing_activation_r"],
        )
    elif leverage >= 6:
        updated["dynamic_leverage_monitor_timeframe"] = cfg[
            "opportunity_monitor_timeframe"
        ]
        updated["ev_time_stop_enabled"] = True
        updated["ev_time_stop_bars"] = min(
            int(
                updated.get("ev_time_stop_bars", cfg["opportunity_time_stop_bars"])
                or cfg["opportunity_time_stop_bars"]
            ),
            cfg["opportunity_time_stop_bars"],
        )
        updated["ev_time_stop_min_mfe_r"] = max(
            float(updated.get("ev_time_stop_min_mfe_r", 0.0) or 0.0),
            cfg["opportunity_time_stop_min_mfe_r"],
        )
        updated["atr_trailing_activation_r"] = min(
            float(
                updated.get(
                    "atr_trailing_activation_r",
                    cfg["opportunity_trailing_activation_r"],
                )
                or cfg["opportunity_trailing_activation_r"]
            ),
            cfg["opportunity_trailing_activation_r"],
        )
    else:
        updated.pop("dynamic_leverage_monitor_timeframe", None)
    return updated
