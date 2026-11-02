from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from src.schedule import (
    Rule,
    cron_dow,
    cron_expression,
    describe,
    merged_windows,
    parse_schedule,
    rules_for_day,
    weekday_label,
)

BJ = ZoneInfo("Asia/Shanghai")


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=BJ)


# ------------------------------------------------------------------ 解析
def test_parse_single_rule():
    rules = parse_schedule([{"weekday": 1, "time_windows": ["20:00-21:00"]}])
    assert rules == [Rule(weekday=1, windows=((time(20, 0), time(21, 0)),))]


def test_parse_different_windows_per_weekday():
    rules = parse_schedule(
        [
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
            {"weekday": 6, "time_windows": ["14:00-15:00"]},
        ]
    )
    assert rules[0].windows == ((time(20, 0), time(21, 0)),)
    assert rules[1].windows == ((time(14, 0), time(15, 0)),)


def test_parse_multiple_windows_on_one_weekday():
    rules = parse_schedule([{"weekday": 3, "time_windows": ["12:00-13:00", "19:00-21:00"]}])
    assert rules[0].windows == ((time(12, 0), time(13, 0)), (time(19, 0), time(21, 0)))


def test_parse_accepts_windows_alias_and_string_weekday():
    rules = parse_schedule([{"weekday": "6", "windows": ["14:00-15:00"]}])
    assert rules[0].weekday == 6


def test_parse_empty():
    assert parse_schedule(None) == []
    assert parse_schedule([]) == []


@pytest.mark.parametrize("bad", [0, 8, -1, "周一", None])
def test_parse_rejects_bad_weekday(bad):
    with pytest.raises(ValueError):
        parse_schedule([{"weekday": bad, "time_windows": ["20:00-21:00"]}])


def test_parse_rejects_missing_windows():
    with pytest.raises(ValueError, match="time_windows"):
        parse_schedule([{"weekday": 1}])


def test_parse_rejects_non_list():
    with pytest.raises(ValueError):
        parse_schedule({"weekday": 1})


# ------------------------------------------------------------------ 选取
def test_rules_for_day_matches_isoweekday():
    rules = parse_schedule(
        [{"weekday": 1, "time_windows": ["20:00-21:00"]}, {"weekday": 6, "time_windows": ["14:00-15:00"]}]
    )
    monday = moment("2026-11-02T19:00:00")  # 周一
    saturday = moment("2026-11-07T13:00:00")  # 周六
    tuesday = moment("2026-11-03T19:00:00")  # 周二

    assert [r.weekday for r in rules_for_day(rules, monday)] == [1]
    assert [r.weekday for r in rules_for_day(rules, saturday)] == [6]
    assert rules_for_day(rules, tuesday) == []


def test_sunday_is_7_not_0():
    rules = parse_schedule([{"weekday": 7, "time_windows": ["10:00-11:00"]}])
    sunday = moment("2026-11-08T10:00:00")
    assert sunday.isoweekday() == 7
    assert rules_for_day(rules, sunday) == rules


def test_merged_windows_dedupes_and_sorts():
    rules = parse_schedule(
        [
            {"weekday": 1, "time_windows": ["20:00-21:00", "12:00-13:00"]},
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
        ]
    )
    assert merged_windows(rules) == [(time(12, 0), time(13, 0)), (time(20, 0), time(21, 0))]


def test_describe_and_label():
    rules = parse_schedule(
        [{"weekday": 1, "time_windows": ["20:00-21:00"]}, {"weekday": 6, "time_windows": ["14:00-15:00"]}]
    )
    assert rules[0].label == "周一 20:00-21:00"
    assert describe(rules) == "周一 20:00-21:00；周六 14:00-15:00"
    assert weekday_label(moment("2026-11-02T10:00:00")) == "周一"


# ------------------------------------------------------------------ cron 推导
def test_cron_dow_sorted_and_unique():
    rules = parse_schedule(
        [
            {"weekday": 6, "time_windows": ["14:00-15:00"]},
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
            {"weekday": 1, "time_windows": ["12:00-13:00"]},
        ]
    )
    assert cron_dow(rules) == "1,6"


def test_cron_expression_from_active_hours():
    rules = parse_schedule(
        [{"weekday": 1, "time_windows": ["20:00-21:00"]}, {"weekday": 6, "time_windows": ["14:00-15:00"]}]
    )
    # 北京 12:00-20:00 左闭右开 → 覆盖 12..19 点 → UTC 4..11
    assert cron_expression(rules, ("12:00", "20:00")) == "*/5 4-11 * * 1,6"
    # 换窗口也应该跟着变
    assert cron_expression(rules, ("09:00", "20:00")) == "*/5 1-11 * * 1,6"


def test_cron_expression_without_rules_runs_every_day():
    assert cron_dow([]) == "*"
