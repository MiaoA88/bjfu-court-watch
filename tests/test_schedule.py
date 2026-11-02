from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from src.schedule import (
    Rule,
    Target,
    cron_expression,
    describe,
    describes_targets,
    fingerprint,
    format_windows,
    parse_schedule,
    plan_targets,
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
            {"weekday": 6, "time_windows": ["14:00-22:00"]},
        ]
    )
    assert rules[0].windows == ((time(20, 0), time(21, 0)),)
    assert rules[1].windows == ((time(14, 0), time(22, 0)),)


def test_parse_multiple_windows_on_one_weekday():
    rules = parse_schedule([{"weekday": 3, "time_windows": ["12:00-13:00", "19:00-21:00"]}])
    assert rules[0].windows == ((time(12, 0), time(13, 0)), (time(19, 0), time(21, 0)))


def test_parse_accepts_single_string_window():
    rules = parse_schedule([{"weekday": 6, "time_windows": "14:00-22:00"}])
    assert rules[0].windows == ((time(14, 0), time(22, 0)),)


def test_parse_dedupes_and_sorts_windows():
    rules = parse_schedule(
        [{"weekday": 6, "time_windows": ["19:00-21:00", "14:00-16:00", "19:00-21:00"]}]
    )
    assert rules[0].windows == ((time(14, 0), time(16, 0)), (time(19, 0), time(21, 0)))


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


def test_parse_rejects_inverted_window():
    with pytest.raises(ValueError, match="起点"):
        parse_schedule([{"weekday": 1, "time_windows": ["21:00-20:00"]}])


# ------------------------------------------------------------------ 目标日期
def test_plan_targets_includes_today_when_weekday_matches():
    rules = parse_schedule([{"weekday": 1, "time_windows": ["20:00-21:00"]}])
    targets = plan_targets(rules, moment("2026-11-02T15:00:00"))  # 周一
    assert targets == [Target(date=date(2026, 11, 2), windows=((time(20, 0), time(21, 0)),))]


def test_plan_targets_skips_weekday_already_passed_this_week():
    """周三不再查周一——系统每周一只放本周的场，周一的场次已经开场了。"""
    rules = parse_schedule([{"weekday": 1, "time_windows": ["20:00-21:00"]}])
    targets = plan_targets(rules, moment("2026-11-04T15:00:00"))  # 周三
    assert targets == []


def test_plan_targets_on_wednesday_only_returns_remaining_weekdays():
    rules = parse_schedule(
        [{"weekday": 1, "time_windows": ["20:00-21:00"]}, {"weekday": 6, "time_windows": ["14:00-15:00"]}]
    )
    targets = plan_targets(rules, moment("2026-11-04T15:00:00"))  # 周三
    assert [t.date for t in targets] == [date(2026, 11, 7)]  # 只剩本周六


def test_plan_targets_on_monday_covers_whole_week():
    """周一早上刚放完本周的场，一次就能把本周配置的周几全查了。"""
    rules = parse_schedule(
        [
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
            {"weekday": 3, "time_windows": ["19:00-20:00"]},
            {"weekday": 6, "time_windows": ["14:00-15:00"]},
        ]
    )
    targets = plan_targets(rules, moment("2026-11-02T14:00:00"))  # 周一
    assert [t.date for t in targets] == [date(2026, 11, 2), date(2026, 11, 4), date(2026, 11, 7)]


def test_plan_targets_empty_late_in_week():
    """周日且只配了工作日：本周没有可查的。"""
    rules = parse_schedule(
        [{"weekday": 1, "time_windows": ["20:00-21:00"]}, {"weekday": 6, "time_windows": ["14:00-15:00"]}]
    )
    assert plan_targets(rules, moment("2026-11-08T15:00:00")) == []


def test_plan_targets_never_exceeds_six_days():
    rules = parse_schedule([{"weekday": d, "time_windows": ["20:00-21:00"]} for d in range(1, 8)])
    today = moment("2026-11-02T15:00:00")
    for target in plan_targets(rules, today):
        assert 0 <= (target.date - today.date()).days <= 6


def test_plan_targets_merges_same_date_and_dedupes_windows():
    rules = parse_schedule(
        [
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
            {"weekday": 1, "time_windows": ["14:00-15:00"]},
        ]
    )
    targets = plan_targets(rules, moment("2026-11-02T10:00:00"))
    assert len(targets) == 1
    assert targets[0].windows == ((time(14, 0), time(15, 0)), (time(20, 0), time(21, 0)))


def test_plan_targets_sorted_by_date():
    rules = parse_schedule(
        [{"weekday": 6, "time_windows": ["14:00-15:00"]}, {"weekday": 1, "time_windows": ["20:00-21:00"]}]
    )
    targets = plan_targets(rules, moment("2026-11-02T10:00:00"))  # 周一
    assert [t.date for t in targets] == [date(2026, 11, 2), date(2026, 11, 7)]


def test_plan_targets_sunday_slot_from_monday():
    rules = parse_schedule([{"weekday": 7, "time_windows": ["10:00-11:00"]}])
    targets = plan_targets(rules, moment("2026-11-02T10:00:00"))  # 周一 → 本周日
    assert targets[0].date == date(2026, 11, 8)


def test_plan_targets_empty_for_no_rules():
    assert plan_targets([], moment("2026-11-02T10:00:00")) == []


# ------------------------------------------------------------------ 文案与指纹
def test_labels_and_describe():
    rules = parse_schedule(
        [{"weekday": 1, "time_windows": ["20:00-21:00"]}, {"weekday": 6, "time_windows": ["14:00-15:00"]}]
    )
    assert rules[0].label == "周一 20:00-21:00"
    assert describe(rules) == "周一 20:00-21:00；周六 14:00-15:00"

    targets = plan_targets(rules, moment("2026-11-02T10:00:00"))
    assert describes_targets(targets) == "11-02 周一 20:00-21:00；11-07 周六 14:00-15:00"


def test_format_windows_multi_span():
    windows = ((time(14, 0), time(16, 0)), (time(19, 0), time(21, 0)))
    assert format_windows(windows) == "14:00-16:00、19:00-21:00"


def test_fingerprint_changes_only_with_schedule():
    a = parse_schedule([{"weekday": 1, "time_windows": ["20:00-21:00"]}])
    b = parse_schedule([{"weekday": 1, "time_windows": ["20:00-21:00"]}])
    c = parse_schedule([{"weekday": 1, "time_windows": ["19:00-21:00"]}])
    d = parse_schedule([{"weekday": 2, "time_windows": ["20:00-21:00"]}])
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(c)
    assert fingerprint(a) != fingerprint(d)
    assert len(fingerprint(a)) == 16


# ------------------------------------------------------------------ cron 推导
def test_cron_expression_from_active_hours():
    # 北京 14:00-22:00 左闭右开 → 覆盖 14..21 点 → UTC 6..13
    assert cron_expression(("14:00", "22:00")) == "*/5 6-13 * * *"
    assert cron_expression(("12:00", "20:00")) == "*/5 4-11 * * *"


def test_cron_runs_every_day():
    assert cron_expression(("14:00", "22:00")).endswith("* * *")
