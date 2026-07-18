"""US equity regular-session and holiday calendar helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

US_EQUITY_MARKET_TZ = ZoneInfo("America/New_York")
US_EQUITY_REGULAR_OPEN = (9, 30)
US_EQUITY_REGULAR_CLOSE = (16, 0)
US_EQUITY_EARLY_CLOSE = (13, 0)


def _nth_weekday_of_month(year, month, weekday, n):
    day = datetime(year, month, 1).date()
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + (n - 1) * 7)


def _last_weekday_of_month(year, month, weekday):
    if month == 12:
        day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        day = datetime(year, month + 1, 1).date() - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _observed_fixed_us_holiday(year, month, day):
    raw = datetime(year, month, day).date()
    if raw.weekday() == 5:
        return raw - timedelta(days=1)
    if raw.weekday() == 6:
        return raw + timedelta(days=1)
    return raw


def _western_easter_date(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day).date()


def _previous_weekday(day):
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _us_equity_market_closed_dates(year):
    closed = {
        _observed_fixed_us_holiday(year, 1, 1),
        _nth_weekday_of_month(year, 1, 0, 3),
        _nth_weekday_of_month(year, 2, 0, 3),
        _western_easter_date(year) - timedelta(days=2),
        _last_weekday_of_month(year, 5, 0),
        _observed_fixed_us_holiday(year, 6, 19),
        _observed_fixed_us_holiday(year, 7, 4),
        _nth_weekday_of_month(year, 9, 0, 1),
        _nth_weekday_of_month(year, 11, 3, 4),
        _observed_fixed_us_holiday(year, 12, 25),
        _observed_fixed_us_holiday(year + 1, 1, 1),
    }
    return {day for day in closed if day.year == year}


def _us_equity_market_early_close_dates(year):
    closed = _us_equity_market_closed_dates(year)
    thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)
    candidates = {
        thanksgiving + timedelta(days=1),
        _previous_weekday(_observed_fixed_us_holiday(year, 7, 4)),
        datetime(year, 12, 24).date(),
    }
    return {
        day
        for day in candidates
        if day.year == year and day.weekday() < 5 and day not in closed
    }


def us_equity_regular_session_status(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(US_EQUITY_MARKET_TZ)
    local_date = local.date()
    closed_dates = _us_equity_market_closed_dates(local_date.year)
    early_close_dates = _us_equity_market_early_close_dates(local_date.year)
    close_hour, close_minute = (
        US_EQUITY_EARLY_CLOSE
        if local_date in early_close_dates
        else US_EQUITY_REGULAR_CLOSE
    )
    open_dt = local.replace(
        hour=US_EQUITY_REGULAR_OPEN[0],
        minute=US_EQUITY_REGULAR_OPEN[1],
        second=0,
        microsecond=0,
    )
    close_dt = local.replace(
        hour=close_hour,
        minute=close_minute,
        second=0,
        microsecond=0,
    )
    if local.weekday() >= 5:
        reason = "weekend"
        is_open = False
    elif local_date in closed_dates:
        reason = "holiday"
        is_open = False
    elif not (open_dt <= local < close_dt):
        reason = "outside_regular_session"
        is_open = False
    else:
        reason = "regular_session_open"
        is_open = True
    return {
        "open": is_open,
        "reason": reason,
        "timezone": "America/New_York",
        "local_time": local.isoformat(),
        "regular_open": open_dt.isoformat(),
        "regular_close": close_dt.isoformat(),
        "early_close": local_date in early_close_dates,
    }


def is_us_equity_regular_session_open(now=None):
    return bool(us_equity_regular_session_status(now).get("open"))

__all__ = (
    'US_EQUITY_MARKET_TZ',
    'US_EQUITY_REGULAR_OPEN',
    'US_EQUITY_REGULAR_CLOSE',
    'US_EQUITY_EARLY_CLOSE',
    '_nth_weekday_of_month',
    '_last_weekday_of_month',
    '_observed_fixed_us_holiday',
    '_western_easter_date',
    '_previous_weekday',
    '_us_equity_market_closed_dates',
    '_us_equity_market_early_close_dates',
    'us_equity_regular_session_status',
    'is_us_equity_regular_session_open',
)
