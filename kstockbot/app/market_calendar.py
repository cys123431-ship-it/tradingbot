import datetime
from zoneinfo import ZoneInfo
from .settings import Settings

try:
    KST = ZoneInfo(Settings.TIMEZONE)
except Exception:
    # Fallback if system lacks ZoneInfo data for Asia/Seoul
    # Use UTC+9
    KST = datetime.timezone(datetime.timedelta(hours=9))

def now_kst() -> datetime.datetime:
    return datetime.datetime.now(tz=KST)

def is_weekday(dt=None) -> bool:
    dt = dt or now_kst()
    return dt.weekday() < 5

def is_holiday(dt=None) -> bool:
    # MVP: config/holidays.json 연동은 나중 단계
    return False

def is_regular_session(dt=None) -> bool:
    dt = dt or now_kst()
    if not is_weekday(dt) or is_holiday(dt):
        return False
    t = dt.time()
    return datetime.time(9, 0) <= t <= datetime.time(15, 30)

def can_buy_now(no_buy_before: str = "09:10", no_buy_after: str = "15:10") -> tuple[bool, str]:
    dt = now_kst()
    if not is_weekday(dt):
        return False, "Korean market is closed: weekend"
    if is_holiday(dt):
        return False, "Korean market is closed: holiday"
    current = dt.strftime("%H:%M")
    if current < no_buy_before:
        return False, f"Before buy window: {no_buy_before}"
    if current > no_buy_after:
        return False, f"After buy window: {no_buy_after}"
    return True, "KST buy window OK"
