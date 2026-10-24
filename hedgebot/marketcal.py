"""US equity session calendar, used to classify Arcus RWA "off-hours".

Arcus defines regular trading hours (RTH) for RWA perps as 04:00-20:00 ET on US
trading days (premarket + regular session + after-hours). Everything else -
20:00-04:00 ET, weekends and exchange holidays - is off-hours. That is also the
window Arcus boosts in its points program, so the bot prefers to rotate there.

Live trading reads `isOutsideRth` straight from Arcus `GET /v1/markets`; this
module exists for the backtest, which needs the flag for historical timestamps.
Early-close days are treated as full days (Arcus closes them early; the
difference is a few hours a year).
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_START = time(4, 0)
RTH_END = time(20, 0)

# NYSE full-day holidays.
US_MARKET_HOLIDAYS = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_MARKET_HOLIDAYS


def is_arcus_off_hours(ts_ms: int) -> bool:
    """True when an Arcus RWA market would be outside its RTH window at ts_ms."""
    et = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(ET)
    if not is_trading_day(et.date()):
        return True
    return not (RTH_START <= et.time() < RTH_END)


def is_us_regular_session(ts_ms: int) -> bool:
    """09:30-16:00 ET on trading days (where single-stock gaps are least likely)."""
    et = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(ET)
    if not is_trading_day(et.date()):
        return False
    return time(9, 30) <= et.time() < time(16, 0)


def minutes_to_session_boundary(ts_ms: int) -> float:
    """Minutes to the nearest RTH boundary (04:00 / 20:00 ET). Margin regimes flip there."""
    et = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(ET)
    minutes = et.hour * 60 + et.minute + et.second / 60
    return min(abs(minutes - 4 * 60), abs(minutes - 20 * 60), abs(minutes - 24 * 60 - 4 * 60))
