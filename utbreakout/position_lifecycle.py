"""Pure position-lifecycle calculations shared by live exit managers."""

from __future__ import annotations

from typing import Any

import pandas as pd

from trading_safety.time_utils import timestamp_ms_or_none


def full_post_entry_closed_bars(
    closed: pd.DataFrame | None,
    *,
    entry_timestamp: Any,
    timeframe_ms: int,
) -> tuple[pd.DataFrame | None, bool]:
    """Return complete bars that opened after the entry-containing bar.

    The boolean indicates whether the real-timestamp gate was applied. Synthetic
    index-based test data remains supported without weakening the production
    epoch-timestamp path.
    """

    if closed is None or getattr(closed, "empty", True):
        return closed, False
    if "timestamp" not in closed.columns:
        return closed, False
    entry_ms = timestamp_ms_or_none(entry_timestamp)
    if entry_ms is None:
        return closed, False
    resolved_timeframe_ms = max(1, int(timeframe_ms or 0))
    entry_bar_open_ms = int(entry_ms // resolved_timeframe_ms) * resolved_timeframe_ms

    raw_timestamps = pd.to_numeric(closed["timestamp"], errors="coerce")
    valid = raw_timestamps.dropna()
    if valid.empty:
        return closed, False
    timestamp_ms = raw_timestamps.astype(float)
    if float(valid.abs().max()) < 10_000_000_000:
        timestamp_ms = timestamp_ms * 1000.0
    latest_ms = float(timestamp_ms.dropna().max())
    if abs(latest_ms - float(entry_ms)) > (
        20 * 365 * 24 * 60 * 60 * 1000
    ):
        return closed, False
    filtered = closed.loc[
        timestamp_ms > float(entry_bar_open_ms)
    ].copy().reset_index(drop=True)
    return filtered, True
