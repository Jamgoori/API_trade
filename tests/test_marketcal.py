from datetime import datetime
from zoneinfo import ZoneInfo

from hedgebot.marketcal import is_arcus_off_hours, minutes_to_session_boundary

ET = ZoneInfo("America/New_York")


def ms(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=ET).timestamp() * 1000)


def test_rth_window_is_4am_to_8pm_et_on_trading_days():
    assert not is_arcus_off_hours(ms(2026, 10, 21, 10))  # Wednesday regular session
    assert not is_arcus_off_hours(ms(2026, 10, 21, 4, 0))  # premarket counts as RTH on Arcus
    assert not is_arcus_off_hours(ms(2026, 10, 21, 19, 59))
    assert is_arcus_off_hours(ms(2026, 10, 21, 20, 0))
    assert is_arcus_off_hours(ms(2026, 10, 21, 3, 59))


def test_weekends_and_holidays_are_off_hours():
    assert is_arcus_off_hours(ms(2026, 10, 24, 12))  # Saturday
    assert is_arcus_off_hours(ms(2026, 11, 26, 12))  # Thanksgiving
    assert is_arcus_off_hours(ms(2026, 7, 3, 12))  # Independence Day observed


def test_minutes_to_boundary():
    assert minutes_to_session_boundary(ms(2026, 10, 21, 19, 58)) == 2
    assert minutes_to_session_boundary(ms(2026, 10, 21, 4, 1)) == 1
    assert minutes_to_session_boundary(ms(2026, 10, 21, 16, 0)) == 240
    assert minutes_to_session_boundary(ms(2026, 10, 21, 12, 0)) == 480
