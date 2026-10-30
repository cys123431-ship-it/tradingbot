"""Telegram-controlled SMA periods shared by /btcmacross and /macross.

Both cross strategies read ``cfg["fast_period"]`` / ``cfg["slow_period"]`` in
their signal engine, so only normalisation, the Telegram presets and the text
parser are needed here.  Pure functions; no I/O and no exchange access.

Safety rules enforced everywhere:
  * ``1 <= fast < slow <= MAX_PERIOD``  (no inverted or equal lines)
  * ``candle_history`` always covers the slow line plus a warm-up margin,
    otherwise the signal would silently stop being computed.
"""

from __future__ import annotations

DEFAULT_FAST_PERIOD = 3
DEFAULT_SLOW_PERIOD = 200
MAX_PERIOD = 500
MIN_WARMUP_BARS = 10

# Text commands, one set per strategy.  They must never overlap: Telegram runs
# only the first matching handler, so a shared name would change the wrong strategy.
BTC_MA_PERIOD_COMMANDS = ("btcma", "btcmaperiod")   # /btcmacross
MA_CROSS_PERIOD_COMMANDS = ("map", "maperiod")      # /macross
PERIOD_COMMANDS = BTC_MA_PERIOD_COMMANDS + MA_CROSS_PERIOD_COMMANDS

# One-tap presets: (label, fast, slow).  Every slow value is a valid SMA line
# the bot can compute with both strategies' candle_history floors.
PERIOD_PRESETS = (
    ("3/120", 3, 120),
    ("3/200", 3, 200),
    ("5/60", 5, 60),
    ("1/200", 1, 200),
    ("9/21", 9, 21),
)


class PeriodError(ValueError):
    """Invalid fast/slow pair; ``str(exc)`` is a Telegram-ready reason."""


def _as_int(value):
    """Strict integer parse: '200' -> 200, '3.5' -> PeriodError."""
    if isinstance(value, bool):
        raise PeriodError("정수만 입력할 수 있습니다.")
    if isinstance(value, int):
        return value
    text = str(value if value is not None else "").strip()
    if not text:
        raise PeriodError("값이 비어 있습니다.")
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise PeriodError(f"숫자가 아닙니다: {text}") from None
    if number != int(number):
        raise PeriodError(f"소수는 사용할 수 없습니다: {text}")
    return int(number)


def required_candle_history(slow_period):
    """Closed candles needed so the slow SMA and its previous value exist."""
    return max(MIN_WARMUP_BARS + int(slow_period), 210)


def validate_periods(fast, slow):
    """(fast, slow) as ints, or PeriodError with a Korean, user-facing reason."""
    fast_p = _as_int(fast)
    slow_p = _as_int(slow)
    if fast_p < 1:
        raise PeriodError(f"패스트는 1 이상이어야 합니다 (입력: {fast_p}).")
    if slow_p < 2:
        raise PeriodError(f"슬로우는 2 이상이어야 합니다 (입력: {slow_p}).")
    if fast_p >= slow_p:
        raise PeriodError(
            f"패스트({fast_p})는 슬로우({slow_p})보다 작아야 합니다. "
            "(같거나 크면 크로스가 발생하지 않습니다)"
        )
    if slow_p > MAX_PERIOD:
        raise PeriodError(f"슬로우는 {MAX_PERIOD} 이하여야 합니다 (입력: {slow_p}).")
    return fast_p, slow_p


def normalize_periods(cfg, *, default_fast=DEFAULT_FAST_PERIOD, default_slow=DEFAULT_SLOW_PERIOD):
    """Validated (fast, slow) from a raw config, falling back to the defaults.

    Never raises: a corrupted stored value must not stop the bot from starting.
    """
    try:
        return validate_periods(cfg.get("fast_period"), cfg.get("slow_period"))
    except PeriodError:
        pass
    try:
        return validate_periods(default_fast, default_slow)
    except PeriodError:  # pragma: no cover - defaults are known-good constants
        return DEFAULT_FAST_PERIOD, DEFAULT_SLOW_PERIOD


def apply_periods(cfg, *, default_fast=DEFAULT_FAST_PERIOD, default_slow=DEFAULT_SLOW_PERIOD):
    """Write validated periods into ``cfg`` and lift ``candle_history`` if needed."""
    fast_p, slow_p = normalize_periods(cfg, default_fast=default_fast, default_slow=default_slow)
    cfg["fast_period"] = fast_p
    cfg["slow_period"] = slow_p
    needed = required_candle_history(slow_p)
    history = cfg.get("candle_history")
    try:
        history = int(float(history))
    except (TypeError, ValueError):
        history = needed
    # Too few candles silently disables the signal: always cover the slow line.
    cfg["candle_history"] = max(history, needed)
    return fast_p, slow_p


def parse_period_command(args):
    """Parse Telegram command args into ``(fast, slow)``.

    Accepts ``1 200``, ``1/200``, ``1,200``, ``1 200`` and a single
    ``1/200`` argument.  Raises PeriodError with a Telegram-ready message.
    """
    tokens = []
    for arg in list(args or []):
        tokens.extend(part for part in str(arg).replace(",", " ").replace("/", " ").split() if part)
    if not tokens:
        raise PeriodError("값이 없습니다. 예) 1 200")
    if len(tokens) == 1:
        raise PeriodError(f"패스트와 슬로우 두 개를 입력하세요. 예) 1 200 (입력: {tokens[0]})")
    if len(tokens) > 2:
        raise PeriodError(f"값이 너무 많습니다. 예) 1 200 (입력: {' '.join(tokens)})")
    return validate_periods(tokens[0], tokens[1])


def periods_label(fast, slow):
    """Short label such as ``3/120``."""
    return f"{int(fast)}/{int(slow)}"


def is_default_periods(fast, slow, *, default_fast=DEFAULT_FAST_PERIOD, default_slow=DEFAULT_SLOW_PERIOD):
    return int(fast) == int(default_fast) and int(slow) == int(default_slow)


__all__ = (
    "DEFAULT_FAST_PERIOD",
    "DEFAULT_SLOW_PERIOD",
    "MAX_PERIOD",
    "MIN_WARMUP_BARS",
    "BTC_MA_PERIOD_COMMANDS",
    "MA_CROSS_PERIOD_COMMANDS",
    "PERIOD_COMMANDS",
    "PERIOD_PRESETS",
    "PeriodError",
    "apply_periods",
    "is_default_periods",
    "normalize_periods",
    "parse_period_command",
    "periods_label",
    "required_candle_history",
    "validate_periods",
)
