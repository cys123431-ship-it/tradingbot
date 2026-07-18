"""Timestamp normalization shared by trading safety subsystems."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


def timestamp_ms_or_none(value: Any) -> float | None:
    """Normalize numeric, datetime, and ISO timestamps to Unix milliseconds."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed_dt = value
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = None
        if numeric is not None and math.isfinite(numeric):
            if numeric <= 0:
                return None
            return float(
                numeric * 1000.0
                if numeric < 10_000_000_000
                else numeric
            )
        try:
            parsed_dt = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    return float(
        parsed_dt.astimezone(timezone.utc).timestamp() * 1000.0
    )
