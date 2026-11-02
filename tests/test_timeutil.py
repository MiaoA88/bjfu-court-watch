from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from src.timeutil import (
    TZ,
    in_active_window,
    parse_hhmm,
    resolve_dates,
    weekday_cn,
)


def bj(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


@pytest.mark.parametrize(
    "moment, expected",
    [
        ("2026-11-03T08:59:00", False),
        ("2026-11-03T09:00:00", True),   # 左闭
        ("2026-11-03T12:00:00", True),
        ("2026-11-03T19:59:00", True),
        ("2026-11-03T20:00:00", False),  # 右开：系统 20:00 关闭
        ("2026-11-03T20:01:00", False),
        ("2026-11-03T03:00:00", False),
    ],
)
def test_active_window_boundaries(moment, expected):
    assert in_active_window(bj(moment), ["09:00", "20:00"]) is expected


def test_active_window_uses_beijing_time_not_utc():
    utc = ZoneInfo("UTC")
    # UTC 01:00 == 北京 09:00，必须判定为窗口内
    assert in_active_window(datetime(2026, 11, 3, 1, 0, tzinfo=utc), ["09:00", "20:00"]) is True
    # UTC 12:00 == 北京 20:00，判定为窗口外
    assert in_active_window(datetime(2026, 11, 3, 12, 0, tzinfo=utc), ["09:00", "20:00"]) is False


def test_parse_hhmm_tolerates_padding():
    assert parse_hhmm(" 9:05 ") == time(9, 5)
    with pytest.raises(ValueError):
        parse_hhmm("0900")


def test_resolve_dates_merges_and_sorts():
    today = date(2026, 11, 2)
    got = resolve_dates([2, 1, 1], ["2026-11-02"], today)
    assert got == [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]


def test_weekday_cn():
    assert weekday_cn(date(2026, 11, 2)) == "一"
    assert weekday_cn(date(2026, 11, 8)) == "日"
