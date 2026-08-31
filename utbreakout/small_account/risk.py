"""Pure stop-loss geometry rules for live small-account protection."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class StopGeometry:
    valid: bool
    crossed_live_mark: bool = False
    reason: str = ""


def position_mark_price(position: Mapping[str, Any] | None) -> float | None:
    """Read the exchange mark from normalized or raw position fields."""

    if not isinstance(position, Mapping):
        return None
    info = position.get("info") if isinstance(position.get("info"), Mapping) else {}
    for value in (
        position.get("markPrice"),
        position.get("mark_price"),
        info.get("markPrice"),
        info.get("mark_price"),
        position.get("lastPrice"),
        info.get("lastPrice"),
    ):
        parsed = _positive_float(value)
        if parsed is not None:
            return parsed
    return None


def classify_stop_geometry(
    *,
    side: str,
    stop_price: Any,
    entry_price: Any,
    mark_price: Any = None,
    bot_managed: bool,
) -> StopGeometry:
    """Classify a stop without confusing profit protection with invalid SL.

    A bot-managed winner stop may correctly sit above a long entry (or below a
    short entry).  Once accepted by the exchange it is preserved even if the
    latest polled mark has crossed it; cancelling at that instant creates an
    unprotected gap exactly when the exchange trigger should be allowed to
    execute.  ``crossed_live_mark`` is diagnostic, not a cancellation signal.
    """

    normalized_side = str(side or "").strip().lower()
    stop = _positive_float(stop_price)
    entry = _positive_float(entry_price)
    mark = _positive_float(mark_price)
    if normalized_side not in {"long", "short"}:
        return StopGeometry(False, reason="INVALID_POSITION_SIDE")
    if stop is None:
        return StopGeometry(False, reason="INVALID_STOP_PRICE")

    if bot_managed:
        crossed = bool(
            mark is not None
            and (
                stop >= mark
                if normalized_side == "long"
                else stop <= mark
            )
        )
        return StopGeometry(
            True,
            crossed_live_mark=crossed,
            reason=(
                "MANAGED_STOP_TRIGGER_PENDING"
                if crossed
                else "MANAGED_STOP_VALID"
            ),
        )

    if entry is None:
        return StopGeometry(False, reason="ENTRY_PRICE_UNAVAILABLE")
    valid = stop < entry if normalized_side == "long" else stop > entry
    return StopGeometry(
        valid,
        reason="EXTERNAL_STOP_VALID" if valid else "EXTERNAL_STOP_WRONG_ENTRY_SIDE",
    )


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed
