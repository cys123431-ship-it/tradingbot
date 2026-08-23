"""Adaptive Trend pyramid protection rebuild helpers.

These helpers keep the pre-add stop alive while the enlarged position's
protection is rebuilt. They are activated only inside the Adaptive Trend
pyramid call path by ``pyramid_runtime_patch``.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Awaitable, Callable


_ADAPTIVE_PYRAMID_REBUILD_ACTIVE: ContextVar[bool] = ContextVar(
    "adaptive_pyramid_rebuild_active",
    default=False,
)

_BLANKET_REFRESH_REASONS = {
    "before new protection placement",
    "stale protection still open before placement",
}


def activate_adaptive_pyramid_rebuild():
    return _ADAPTIVE_PYRAMID_REBUILD_ACTIVE.set(True)


def reset_adaptive_pyramid_rebuild(token) -> None:
    _ADAPTIVE_PYRAMID_REBUILD_ACTIVE.reset(token)


def adaptive_pyramid_rebuild_active() -> bool:
    return bool(_ADAPTIVE_PYRAMID_REBUILD_ACTIVE.get())


async def cancel_preserving_pyramid_sl(
    engine: Any,
    original_cancel: Callable[..., Awaitable[int]],
    symbol: str,
    *,
    reason: str = "protection cleanup",
    orders=None,
):
    """During the pyramid TP refresh, blanket cleanup may cancel TP but not SL."""

    if (
        not adaptive_pyramid_rebuild_active()
        or str(reason or "").strip().lower() not in _BLANKET_REFRESH_REASONS
    ):
        return await original_cancel(engine, symbol, reason=reason, orders=orders)

    if orders is None:
        collector = getattr(engine, "_collect_protection_orders", None)
        try:
            open_orders = await collector(symbol) if callable(collector) else []
        except Exception:
            open_orders = []
    else:
        open_orders = list(orders or [])

    classifier = getattr(engine, "_classify_protection_order", None)
    tp_orders = []
    for order in open_orders:
        try:
            if callable(classifier) and classifier(order) == "tp":
                tp_orders.append(order)
        except Exception:
            continue
    if not tp_orders:
        return 0
    return await original_cancel(engine, symbol, reason=reason, orders=tp_orders)


async def place_pyramid_protection_preserving_sl(
    engine: Any,
    original_place: Callable[..., Awaitable[Any]],
    symbol: str,
    side: str,
    entry_price: float,
    qty: float,
    *,
    tp_distance=None,
    sl_distance=None,
    tp_qty_ratio=1.0,
    tp_targets=None,
    preserve_runner_qty=False,
):
    """Refresh pyramid TP while keeping/rebuilding an exchange SL first.

    ``_place_tp_sl_orders`` normally starts by cancelling every protection
    order. That is appropriate for an initial build but unsafe immediately
    after a pyramid fill because a transient new-SL failure can turn a healthy
    winner into an emergency market close. For Adaptive Trend pyramid adds we
    first attempt to cover the enlarged quantity at the still-live pre-add
    stop, then run the normal TP builder with ``sl_distance=None``. The
    context-aware cancellation wrapper keeps SL orders out of blanket cleanup.
    """

    if not adaptive_pyramid_rebuild_active():
        return await original_place(
            engine,
            symbol,
            side,
            entry_price,
            qty,
            tp_distance=tp_distance,
            sl_distance=sl_distance,
            tp_qty_ratio=tp_qty_ratio,
            tp_targets=tp_targets,
            preserve_runner_qty=preserve_runner_qty,
        )

    side = str(side or "").lower()
    try:
        entry_value = float(entry_price or 0.0)
        distance_value = float(sl_distance or 0.0)
    except (TypeError, ValueError):
        entry_value = 0.0
        distance_value = 0.0

    pre_add_stop = None
    if side == "long" and entry_value > 0 and distance_value > 0:
        pre_add_stop = entry_value - distance_value
    elif side == "short" and entry_value > 0 and distance_value > 0:
        pre_add_stop = entry_value + distance_value

    # Best effort only. If this replacement cannot be confirmed, the normal
    # post-fill exchange guard performs additional retries and decides whether
    # fail-closed liquidation is truly necessary.
    if pre_add_stop is not None:
        fetcher = getattr(engine, "_fetch_server_position_checked", None)
        replacer = getattr(engine, "_replace_stop_loss_order", None)
        try:
            fetch_ok, live_pos = await fetcher(symbol) if callable(fetcher) else (False, None)
        except Exception:
            fetch_ok, live_pos = False, None
        if (
            fetch_ok
            and isinstance(live_pos, dict)
            and str(live_pos.get("side") or "").lower() == side
            and callable(replacer)
        ):
            try:
                await replacer(
                    symbol,
                    live_pos,
                    pre_add_stop,
                    reason="Adaptive Trend pyramid cover enlarged quantity before TP refresh",
                )
            except Exception:
                # Do not force-close here. The exchange-verified postcondition
                # gets the final say after retries/fallbacks.
                pass

    return await original_place(
        engine,
        symbol,
        side,
        entry_price,
        qty,
        tp_distance=tp_distance,
        sl_distance=None,
        tp_qty_ratio=tp_qty_ratio,
        tp_targets=tp_targets,
        preserve_runner_qty=preserve_runner_qty,
    )


__all__ = (
    "activate_adaptive_pyramid_rebuild",
    "adaptive_pyramid_rebuild_active",
    "cancel_preserving_pyramid_sl",
    "place_pyramid_protection_preserving_sl",
    "reset_adaptive_pyramid_rebuild",
)
