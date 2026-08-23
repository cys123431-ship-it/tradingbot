"""Runtime integration for the Adaptive Trend research overlay.

The underlying strategy remains the source of trade direction, stop/TP geometry,
and pyramiding rules.  This wrapper acts only after the parent signal has
already built a valid entry plan.  It may reject that plan or scale its size
down; it never increases size and never edits price/protection geometry.
"""

from __future__ import annotations

import functools
from typing import Any, Mapping

from utbreakout.adaptive_research_overlay import (
    ADAPTIVE_RESEARCH_OVERLAY_PROFILE,
    evaluate_adaptive_research_overlay,
)


_SIZE_FIELDS = (
    "qty",
    "risk_usdt",
    "planned_notional",
    "planned_margin",
    "expected_profit_usdt",
    "adaptive_trend_target_qty",
    "adaptive_trend_target_risk_usdt",
    "adaptive_trend_target_notional",
    "position_cap_original_notional",
)


def scale_adaptive_trend_plan(
    plan: Mapping[str, Any] | None,
    multiplier: float,
    overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a size-only scaled copy of an Adaptive Trend plan.

    Price fields, stop distances, take-profit targets, trailing configuration,
    and pyramiding triggers are intentionally not touched.
    """

    result = dict(plan or {})
    factor = _bounded_multiplier(multiplier)
    for key in _SIZE_FIELDS:
        value = _finite(result.get(key))
        if value is not None and value >= 0.0:
            result[key] = value * factor

    current_risk_multiplier = _finite(
        result.get("adaptive_breakout_trend_risk_multiplier")
    )
    if current_risk_multiplier is not None:
        result["adaptive_breakout_trend_risk_multiplier"] = (
            max(0.0, current_risk_multiplier) * factor
        )

    current_risk_percent = _finite(
        result.get("adaptive_breakout_trend_target_risk_percent")
    )
    if current_risk_percent is not None:
        result["adaptive_breakout_trend_target_risk_percent"] = (
            max(0.0, current_risk_percent) * factor
        )

    result["adaptive_research_overlay_profile"] = ADAPTIVE_RESEARCH_OVERLAY_PROFILE
    result["adaptive_research_overlay_risk_multiplier"] = factor
    result["adaptive_research_overlay"] = dict(overlay or {})
    return result


def install_adaptive_research_overlay() -> None:
    """Install the conservative post-signal overlay once per interpreter."""

    from .signal_alpha import SignalAlphaMixin

    current = getattr(
        SignalAlphaMixin,
        "_calculate_adaptive_breakout_trend_signal",
        None,
    )
    if not callable(current):
        return
    if bool(getattr(current, "_adaptive_research_overlay_installed", False)):
        return

    original = current

    @functools.wraps(original)
    async def guarded(
        self,
        symbol,
        df,
        strategy_params,
        *,
        force_reprocess=False,
    ):
        result = await original(
            self,
            symbol,
            df,
            strategy_params,
            force_reprocess=force_reprocess,
        )
        if not isinstance(result, tuple) or len(result) != 3:
            return result

        side, reason, status = result
        if side not in {"long", "short"} or not isinstance(status, dict):
            return result

        plan = status.get("entry_plan")
        if not isinstance(plan, dict):
            return result
        if str(plan.get("strategy") or "") != "adaptive_breakout_trend_v1":
            return result

        canonicalizer = getattr(self, "_canonical_futures_symbol", None)
        canonical = (
            canonicalizer(symbol)
            if callable(canonicalizer)
            else str(symbol or "")
        )

        futures_context: dict[str, Any] = {}
        fetcher = getattr(self, "_fetch_utbreakout_futures_context", None)
        if callable(fetcher):
            try:
                fetched = await fetcher(canonical)
                if isinstance(fetched, dict):
                    futures_context.update(fetched)
            except Exception as exc:
                status["adaptive_research_context_error"] = str(exc)

        metrics = (
            plan.get("adaptive_breakout_trend_metrics")
            if isinstance(plan.get("adaptive_breakout_trend_metrics"), dict)
            else status.get("metrics")
            if isinstance(status.get("metrics"), dict)
            else {}
        )
        base_score = _finite(
            plan.get("adaptive_breakout_trend_score"),
            _finite(status.get("score"), 0.0),
        )

        overlay_cfg = None
        try:
            cfg_getter = getattr(self, "_get_utbot_filtered_breakout_config", None)
            trend_getter = getattr(
                self,
                "_adaptive_breakout_trend_runtime_config",
                None,
            )
            if callable(cfg_getter) and callable(trend_getter):
                broad_cfg = cfg_getter(strategy_params)
                trend_cfg = trend_getter(broad_cfg)
                nested = trend_cfg.get("research_overlay")
                if isinstance(nested, dict):
                    overlay_cfg = nested
        except Exception as exc:
            status["adaptive_research_config_error"] = str(exc)

        overlay = evaluate_adaptive_research_overlay(
            side,
            metrics,
            futures_context,
            overlay_cfg,
            base_score=base_score,
        )
        status["adaptive_research_overlay"] = dict(overlay)
        status["adaptive_research_overlay_profile"] = (
            ADAPTIVE_RESEARCH_OVERLAY_PROFILE
        )

        if not bool(overlay.get("allowed", True)):
            clearer = getattr(
                self,
                "_clear_utbot_filtered_breakout_entry_plan",
                None,
            )
            if callable(clearer):
                clearer(canonical)
            status["allowed"] = False
            status["accepted_side"] = None
            status["stage"] = "waiting"
            status["reject_code"] = overlay.get("code")
            status.pop("entry_plan", None)
            rejected_reason = (
                "Adaptive Trend research overlay waiting: "
                f"{overlay.get('reason') or 'no research edge'}"
            )
            status["reason"] = rejected_reason
            return None, rejected_reason, status

        factor = _bounded_multiplier(overlay.get("risk_multiplier", 1.0))
        adjusted_plan = scale_adaptive_trend_plan(plan, factor, overlay)
        adjusted_plan["adaptive_breakout_trend_score_raw"] = base_score
        adjusted_plan["adaptive_breakout_trend_score"] = _finite(
            overlay.get("adjusted_score"),
            base_score,
        )
        status["score_raw"] = base_score
        status["score"] = adjusted_plan["adaptive_breakout_trend_score"]
        status["risk_multiplier"] = _bounded_multiplier(
            adjusted_plan.get("adaptive_breakout_trend_risk_multiplier", factor)
        )
        status["entry_plan"] = adjusted_plan

        setter = getattr(self, "_set_utbot_filtered_breakout_entry_plan", None)
        if callable(setter):
            setter(canonical, adjusted_plan)

        return side, reason, status

    guarded._adaptive_research_overlay_installed = True
    guarded.__runtime_original__ = original
    SignalAlphaMixin._calculate_adaptive_breakout_trend_signal = guarded


def _bounded_multiplier(value: Any) -> float:
    parsed = _finite(value, 1.0)
    return max(0.0, min(1.0, float(parsed)))


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return default
    return parsed
