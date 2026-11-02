from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from src.matcher import Hit, Slot, drop_started, parse_windows, select

DAY = date(2026, 11, 5)


def slot(start: str, end: str, remaining: int = 1, rid: str = "7", name: str = "7号场", day=DAY) -> Slot:
    return Slot(
        date=day,
        start=time.fromisoformat(start),
        end=time.fromisoformat(end),
        resource_id=rid,
        resource_name=name,
        remaining=remaining,
    )


def test_parse_windows_accepts_common_separators():
    assert parse_windows(["18:00-19:00", "19:00~20:00", ["20:00", "21:00"]]) == [
        (time(18, 0), time(19, 0)),
        (time(19, 0), time(20, 0)),
        (time(20, 0), time(21, 0)),
    ]


def test_parse_windows_rejects_inverted_range():
    try:
        parse_windows(["19:00-18:00"])
    except ValueError as exc:
        assert "起点" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当抛 ValueError")


def test_filters_out_fully_booked_and_other_windows():
    slots = [
        slot("18:00", "19:00", remaining=2),
        slot("19:00", "20:00", remaining=0),
        slot("07:00", "08:00", remaining=5),
    ]
    hits = select(slots, target_dates=[DAY], windows=parse_windows(["18:00-20:00"]))
    assert [h.label for h in hits] == ["18:00-19:00"]


def test_window_overlap_not_string_equality():
    # 30 分钟粒度也能命中 18:00-19:00 的窗口
    slots = [slot("18:30", "19:00")]
    hits = select(slots, target_dates=[DAY], windows=parse_windows(["18:00-19:00"]))
    assert len(hits) == 1
    assert hits[0].start == time(18, 30)


def test_consecutive_merge_requires_min_slots():
    slots = [
        slot("18:00", "19:00"),
        slot("19:00", "20:00"),
        slot("20:00", "21:00"),
    ]
    windows = parse_windows(["18:00-21:00"])
    single = select(slots, target_dates=[DAY], windows=windows, min_consecutive_slots=1)
    assert len(single) == 1
    assert single[0].label == "18:00-21:00"

    two = select(slots, target_dates=[DAY], windows=windows, min_consecutive_slots=4)
    assert two == []


def test_non_contiguous_slots_split_into_separate_hits():
    slots = [
        slot("18:00", "19:00"),
        slot("19:30", "20:00"),  # 中间空 30 分钟
    ]
    hits = select(slots, target_dates=[DAY], windows=parse_windows(["18:00-20:00"]))
    assert [h.label for h in hits] == ["18:00-19:00", "19:30-20:00"]


def test_include_exclude_filters():
    slots = [
        slot("18:00", "19:00", rid="7", name="7号场"),
        slot("18:00", "19:00", rid="9", name="9号场"),
    ]
    windows = parse_windows(["18:00-19:00"])
    only_nine = select(slots, target_dates=[DAY], windows=windows, include=["9号"])
    assert [h.resource_name for h in only_nine] == ["9号场"]

    exclude_nine = select(slots, target_dates=[DAY], windows=windows, exclude=["9号"])
    assert [h.resource_name for h in exclude_nine] == ["7号场"]


def test_dates_outside_target_are_ignored():
    slots = [slot("18:00", "19:00", day=date(2026, 11, 6))]
    hits = select(slots, target_dates=[DAY], windows=parse_windows(["18:00-19:00"]))
    assert hits == []


def test_hit_key_is_stable_and_hall_scoped():
    hit = Hit(
        date=DAY,
        start=time(18, 0),
        end=time(19, 0),
        resource_id="7",
        resource_name="7号场",
        slots=(slot("18:00", "19:00"),),
    )
    assert hit.key(54) == "54|2026-11-05|18:00|19:00|7"
    assert hit.key("54") == hit.key(54)
    assert hit.min_remaining == 1


def test_status_five_is_not_available():
    """学校接口在"未开放"时也返回 margin=1，必须靠 status 拦住。"""
    unopened = Slot(
        date=DAY,
        start=time(18, 0),
        end=time(19, 0),
        resource_id="7",
        resource_name="7号场",
        remaining=1,
        status=5,
    )
    assert unopened.available is False
    assert select([unopened], target_dates=[DAY], windows=parse_windows(["18:00-19:00"])) == []


def test_slot_without_status_falls_back_to_remaining():
    no_status = Slot(
        date=DAY, start=time(18, 0), end=time(19, 0), resource_id="7", resource_name="7号场", remaining=2
    )
    assert no_status.available is True


def test_drop_started_removes_only_past_spans():
    moment = datetime(2026, 11, 5, 19, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    slots = [
        slot("18:00", "19:00"),  # 今天，已开始
        slot("19:30", "20:30"),  # 今天，未开始
        slot("18:00", "19:00", day=date(2026, 11, 6)),  # 明天
    ]
    kept = drop_started(slots, moment)
    assert [s.start for s in kept] == [time(19, 30), time(18, 0)]
    assert kept[1].date == date(2026, 11, 6)
