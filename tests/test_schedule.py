from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from src.schedule import (
    FREE_MINUTES_PER_MONTH,
    Rule,
    Target,
    cron_expression,
    daily_runs,
    describe,
    describes_targets,
    fingerprint,
    format_windows,
    monthly_minutes,
    parse_schedule,
    plan_targets,
    runs_per_hour,
    window_hours,
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


# ------------------------------------------------------------------ cron 与额度
def test_cron_expression_from_window_and_interval():
    assert cron_expression(("09:00", "22:00"), 15) == "*/15 1-13 * * *"
    assert cron_expression(("14:00", "22:00"), 5) == "*/5 6-13 * * *"
    assert cron_expression(("09:00", "22:00"), 1) == "* 1-13 * * *"


def test_cron_runs_every_day():
    assert cron_expression(("09:00", "22:00"), 15).endswith("* * *")


@pytest.mark.parametrize("interval,expected", [(15, 4), (12, 5), (5, 12), (7, 9), (20, 3), (30, 2)])
def test_runs_per_hour_handles_non_divisors(interval, expected):
    # */7 是 0,7,...,56 共 9 次，不是 60/7≈8.6；这就是不能直接用除法的地方
    assert runs_per_hour(interval) == expected


def test_window_hours():
    assert window_hours(("09:00", "22:00")) == 13
    assert window_hours(("14:00", "22:00")) == 8


def test_daily_and_monthly_budget():
    # GitHub 按 job 取整到 1 分钟，所以次数就是分钟数
    assert daily_runs(("09:00", "22:00"), 15) == 52
    assert monthly_minutes(("09:00", "22:00"), 15) == 1612
    assert monthly_minutes(("09:00", "22:00"), 12) == 2015  # 会超


def test_fifteen_minutes_is_the_densest_safe_step():
    """13 小时窗口下，*/15 是不超 2000 分钟的最密整步长。"""
    safe = [n for n in range(1, 60) if monthly_minutes(("09:00", "22:00"), n) <= FREE_MINUTES_PER_MONTH]
    assert min(safe) == 15


def test_interval_is_validated():
    with pytest.raises(ValueError):
        runs_per_hour(0)
    with pytest.raises(ValueError):
        cron_expression(("09:00", "22:00"), 60)


def test_local_cron_handles_cross_midnight_without_all_day_fallback():
    assert cron_expression(("22:00", "06:00"), 5, utc=False) == "*/5 0-5,22-23 * * *"
    assert cron_expression(("06:00", "10:00"), 5) == "*/5 0-1,22-23 * * *"


def test_timezone_conversion_across_week_boundary():
    rules = parse_schedule([{"weekday": 1, "time_windows": ["20:00-21:00"]}])
    sunday_utc = datetime.fromisoformat("2026-11-08T18:00:00+00:00")
    assert plan_targets(rules, sunday_utc)[0].date == date(2026, 11, 9)


@pytest.mark.parametrize("moment,hours,interval,expected", [
    ("2026-11-10T10:00:00", ["10:00", "23:00"], 5, 155),
    ("2026-11-10T22:55:30", ["10:00", "23:00"], 5, 0),
    ("2026-11-10T23:00:00", ["22:00", "06:00"], 5, 11),
    ("2026-11-10T14:07:00", ["14:00", "15:00"], 7, 7),
])
def test_remaining_planned_scans(moment, hours, interval, expected):
    from src.schedule import remaining_runs
    from src.cli import _resolve_now
    assert remaining_runs(_resolve_now(moment), hours, interval) == expected
