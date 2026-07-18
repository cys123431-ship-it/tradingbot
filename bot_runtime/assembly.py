"""Final compatibility assembly for legacy method-style live helpers."""

from __future__ import annotations

from typing import Any, Mapping


LIVE_RUNTIME_HELPER_NAMES = (
    "_fetch_live_ohlcv_rows_for_context",
    "_build_live_indicator_values",
    "build_live_context_for_symbol",
    "_calculate_live_adx_dmi",
    "_calculate_live_htf_trend",
    "_ema_last",
    "_fetch_live_derivatives_values",
    "_fetch_live_liquidity_values",
    "_validate_live_context_or_block",
    "assert_binance_futures_one_way_mode",
    "_fetch_futures_account_equity",
    "preflight_live_real_check",
    "_normalize_live_order_plan_for_exchange",
    "_get_min_notional_for_symbol",
    "_get_min_amount_for_symbol",
    "_get_amount_step_for_symbol",
    "_build_tp_ladder_orders",
    "_format_tp_ladder_lines",
    "_collapse_tp_targets_for_exchange",
    "_collapse_state_tp_plan_for_exchange",
    "_validate_live_order_plan_for_exchange",
    "_extract_liquidation_stop_price",
    "_preflight_liquidation_safety",
    "_fetch_position_with_liquidation",
    "_verify_actual_liquidation_safety",
    "_handle_liquidation_safety_failure",
    "execute_live_order_plan",
    "run_live_real_once",
    "get_runtime_models",
    "get_runtime_stats",
    "_rebuild_plan_after_fill",
    "_place_initial_sl_from_plan",
    "_place_reduce_only_tp_from_plan",
    "_handle_sl_failure_with_persistent_pause",
    "_cancel_labeled_tp_orders",
    "_repair_missing_tp_order",
    "_repair_missing_tp2_order",
    "_audit_and_repair_live_ladder_protection",
    "_fetch_tp2_price_snapshot",
    "_maybe_tp2_fallback_close",
    "_register_live_ladder_position_state",
    "_manage_live_ladder_exit_policy",
    "_refresh_ladder_fill_state",
    "_calculate_be_plus_fees_stop",
    "_calculate_tp1_area_stop",
    "_close_position_reduce_only_market",
)


def bind_runtime_methods(
    namespace: Mapping[str, Any],
    *,
    logger: Any = None,
) -> None:
    """Expose extracted live functions through the established class API."""

    controller_class = namespace.get("MainController")
    if controller_class is None:
        return

    missing = []
    for name in LIVE_RUNTIME_HELPER_NAMES:
        function = namespace.get(name)
        if function is None:
            missing.append(name)
            continue
        setattr(controller_class, name, function)

    signal_class = namespace.get("SignalEngine")
    if signal_class is not None:
        tema_class = namespace.get("TemaEngine")
        if tema_class is not None:
            signal_class._resolve_closed_trade_accounting = (
                tema_class._resolve_closed_trade_accounting
            )
            signal_class._record_closed_trade_accounting = (
                tema_class._record_closed_trade_accounting
            )
        for name in LIVE_RUNTIME_HELPER_NAMES:
            function = namespace.get(name)
            if function is not None:
                setattr(signal_class, name, function)
        if hasattr(signal_class, "entry"):
            controller_class.entry = signal_class.entry

    if missing and logger is not None:
        logger.error(
            "Missing live runtime helper functions required by the controller: %s",
            missing,
        )
    still_missing = [
        name
        for name in LIVE_RUNTIME_HELPER_NAMES
        if not hasattr(controller_class, name)
    ]
    if still_missing:
        raise RuntimeError(
            "Failed to bind live runtime helpers to MainController: "
            f"{still_missing}"
        )


__all__ = ("LIVE_RUNTIME_HELPER_NAMES", "bind_runtime_methods")
