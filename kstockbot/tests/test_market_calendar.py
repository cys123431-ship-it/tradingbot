import pytest
import datetime
from zoneinfo import ZoneInfo
from kstockbot.app.market_calendar import is_weekday, is_regular_session, can_buy_now

def test_is_weekday():
    # 2023-10-25 is Wednesday (weekday)
    dt = datetime.datetime(2023, 10, 25, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert is_weekday(dt) is True

    # 2023-10-28 is Saturday
    dt = datetime.datetime(2023, 10, 28, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert is_weekday(dt) is False

def test_is_regular_session():
    # Wednesday 10:00 (Open)
    dt = datetime.datetime(2023, 10, 25, 10, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert is_regular_session(dt) is True

    # Wednesday 16:00 (Closed)
    dt = datetime.datetime(2023, 10, 25, 16, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert is_regular_session(dt) is False
