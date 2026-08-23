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
    """Refresh pyramid TP without touching the currently live exchange SL.

    ``_place_tp_sl_orders`` normally begins by cancelling every protection
    order and then recreates SL/TP. During a winner add this creates a dangerous
    gap: a transient replacement failure can remove a healthy stop and trigger
    an unnecessary emergency market close. In the Adaptive Trend pyramid
    context the normal builder is therefore used only for TP placement. The
    existing SL stays live until the immediately following fee-aware SL
    replacement/fallback and the final exchange-verified postcondition decide
    whether a full-quantity replacement is valid.
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
