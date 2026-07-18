"""Idempotent signal-decision lifecycle helpers.

The functions here are deliberately independent of the Telegram controller and
exchange client so the same decision rules are used by every entry path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .order_state import OrderState
from .time_utils import timestamp_ms_or_none


DECISION_TIMESTAMP_KEYS = (
    "decision_candle_ts",
    "signal_timestamp",
    "signal_ts",
    "signal_candle_ts",
    "closed_candle_ts",
)

NON_CONSUMING_ENTRY_STATES = frozenset(
    {
        OrderState.PLANNED.value,
        OrderState.CANCELED.value,
        OrderState.FAILED.value,
    }
)


def decision_timestamp(*sources: Mapping[str, Any] | None) -> int | None:
    """Return the first normalized closed-candle decision timestamp."""

    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in DECISION_TIMESTAMP_KEYS:
            parsed = timestamp_ms_or_none(source.get(key))
            if parsed is not None:
                return int(parsed)
    return None


def records_contain_consumed_decision(
    records: Iterable[Any],
    target_timestamp: Any,
) -> bool:
    """Whether a non-failed entry record already owns this decision."""

    parsed_target = timestamp_ms_or_none(target_timestamp)
    if parsed_target is None:
        return False
    target = int(parsed_target)
    for record in records or ():
        intent = str(
            getattr(record, "order_intent", "ENTRY") or "ENTRY"
        ).upper()
        if intent != "ENTRY":
            continue
        order_state = str(
            getattr(record, "order_state", "") or ""
        ).upper()
        if order_state in NON_CONSUMING_ENTRY_STATES:
            continue
        record_timestamp = timestamp_ms_or_none(
            getattr(record, "signal_timestamp", None)
        )
        if record_timestamp is not None and int(record_timestamp) == target:
            return True
    return False
