"""Exchange stop targets for EMA200 margin-ROI profit protection.

This is independent of UT candle exits and of the loss-stage emergency stop.
"""

from __future__ import annotations

from math import floor, isfinite


def ema200_profit_stop_target(
    side,
    entry_price,
    mark_price,
    leverage,
    step_percent=5.0,
    start_percent=None,
):
    """Return (achieved margin ROI, locked ROI, stop price), or None.

    Legacy (``start_percent`` None): the trigger is strictly above a 5-point
    step and a 6% margin return locks exactly 5%.

    With ``start_percent`` (strategy revision 2): nothing is locked until the
    ROI is strictly above ``start_percent``; afterwards the lock trails one
    step below the achieved step (15.1% locks 10%, 20.1% locks 15%), leaving
    the winner a full step of room instead of a stop at the market.
    """
    if side not in {'long', 'short'}:
        return None
    try:
        entry = float(entry_price)
        mark = float(mark_price)
        lev = float(leverage)
        step = float(step_percent)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(isfinite(value) and value > 0 for value in (entry, mark, lev, step)):
        return None
    direction = 1 if side == 'long' else -1
    roi = direction * (mark / entry - 1.0) * lev * 100.0
    if start_percent is not None:
        try:
            start = float(start_percent)
        except (TypeError, ValueError, OverflowError):
            return None
        if not isfinite(start) or roi <= start + 1e-9:
            return None
        stage = int(floor((roi - 1e-9) / step))
        floor_roi = max(step, (stage - 1) * step)
        price = entry * (1 + direction * floor_roi / (lev * 100.0))
        if not isfinite(price) or price <= 0:
            return None
        return roi, floor_roi, price
    if roi <= step + 1e-9:
        return None
    # The strict trigger also applies at 10%, 15%, ...: at exactly 10%, the
    # 5% floor remains; only a value above 10% raises the floor to 10%.
    stage = max(1, int(floor((roi - 1e-9) / step)))
    floor_roi = stage * step
    price = entry * (1 + direction * floor_roi / (lev * 100.0))
    if not isfinite(price) or price <= 0:
        return None
    return roi, floor_roi, price
