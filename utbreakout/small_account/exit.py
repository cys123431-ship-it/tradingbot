"""Pure completed-bar exit decisions for the small-account trend profile."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class ProgressFailureExit:
    """Decision for a trend that made modest progress and then failed."""

    should_exit: bool
    confirmation_count: int
    required_confirmations: int
    reason: str


def evaluate_progress_failure_exit(
    *,
    enabled: bool,
    small_account_active: bool,
    tp1_filled: bool,
    bars_held: Any,
    mark_mfe_r: Any,
    mark_current_r: Any,
    fast_support_lost: bool,
    consecutive_entry_closes_lost: bool,
    impulse_lost: bool,
    minimum_mark_mfe_r: Any = 0.20,
    maximum_mark_mfe_r: Any = 0.75,
    maximum_current_r: Any = -0.10,
    minimum_closed_bars: Any = 2,
    required_confirmations: Any = 2,
) -> ProgressFailureExit:
    """Identify confirmed failure without moving the structural hard stop.

    The trigger excursion and current loss use the exchange mark price.  The
    three confirmations are derived only from completed candles by the caller,
    which prevents a single intrabar wick from forcing a market exit.
    """

    minimum_bars = max(1, _safe_int(minimum_closed_bars, 2))
    required = max(1, min(3, _safe_int(required_confirmations, 2)))
    if not enabled:
        return ProgressFailureExit(False, 0, required, "progress-failure exit disabled")
    if not small_account_active:
        return ProgressFailureExit(False, 0, required, "small-account profile inactive")
    if tp1_filled:
        return ProgressFailureExit(False, 0, required, "partial profit already filled")
    held = max(0, _safe_int(bars_held, 0))
    if held < minimum_bars:
        return ProgressFailureExit(
            False,
            0,
            required,
            f"waiting for completed bars {held}/{minimum_bars}",
        )

    mfe = _finite_or_none(mark_mfe_r)
    current = _finite_or_none(mark_current_r)
    if mfe is None or current is None:
        return ProgressFailureExit(False, 0, required, "mark-price excursion unavailable")
    minimum_mfe = max(0.0, _finite(minimum_mark_mfe_r, 0.20))
    maximum_mfe = max(minimum_mfe, _finite(maximum_mark_mfe_r, 0.75))
    current_ceiling = min(0.0, _finite(maximum_current_r, -0.10))
    if mfe < minimum_mfe:
        return ProgressFailureExit(
            False,
            0,
            required,
            f"insufficient mark progress {mfe:.2f}R<{minimum_mfe:.2f}R",
        )
    if mfe > maximum_mfe:
        return ProgressFailureExit(
            False,
            0,
            required,
            f"mark progress {mfe:.2f}R handed to runner/profit-lock policy",
        )
    if current > current_ceiling:
        return ProgressFailureExit(
            False,
            0,
            required,
            f"reversal not deep enough {current:.2f}R>{current_ceiling:.2f}R",
        )

    labels = []
    if fast_support_lost:
        labels.append("fast support lost")
    if consecutive_entry_closes_lost:
        labels.append("two closes beyond entry")
    if impulse_lost:
        labels.append("two-bar impulse lost")
    count = len(labels)
    should_exit = count >= required
    detail = ", ".join(labels) if labels else "no completed-bar confirmation"
    return ProgressFailureExit(
        should_exit,
        count,
        required,
        (
            f"progress failure: mark MFE={mfe:.2f}R current={current:.2f}R; "
            f"confirmations={count}/{required} ({detail})"
        ),
    )


def _finite_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _finite(value: Any, default: float) -> float:
    parsed = _finite_or_none(value)
    return float(default) if parsed is None else parsed


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)
