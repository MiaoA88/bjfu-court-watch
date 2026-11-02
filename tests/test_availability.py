from datetime import date, time

from src.availability import (
    parse_clock,
    parse_day,
    parse_list_page,
    parse_remaining,
    parse_span,
    summarize_table,
)
from src.matcher import parse_windows, select

DAY = date(2026, 11, 3)

PAYLOAD = {
    "total": 1,
    "list": [
        {
            "id": 156,
            "name": "羽毛球综合场馆（收费）",
            "table": {
                "595": [
                    {
                        "abscissa": "1号塑胶场地",
                        "yaxis": "18:00-19:00",
                        "row": {"status": 1, "total": 1, "margin": 1},
                        "date": "2026-11-03",
                    },
                    {
                        "abscissa": "1号塑胶场地",
                        "yaxis": "19:00-20:00",
                        "row": {"status": 5, "total": 1, "margin": 1},  # 未开放，margin 却是 1
                        "date": "2026-11-03",
                    },
                    {
                        "abscissa": "2号塑胶场地",
                        "yaxis": "18:00-19:00",
                        "row": {"status": 3, "total": 1, "margin": 0},  # 已约满
                        "date": "2026-11-03",
                    },
                ]
            },
        }
    ],
}


def test_parse_span():
    assert parse_span("18:00-19:00") == (time(18, 0), time(19, 0))
    assert parse_span("8:00–9:00") == (time(8, 0), time(9, 0))  # 全角连字符
    assert parse_span("19:00-18:00") is None  # 倒序
    assert parse_span("18:00") is None
    assert parse_span(None) is None


def test_parse_list_page_extracts_all_cells():
    slots = parse_list_page(PAYLOAD, DAY)
    assert len(slots) == 3
    first = slots[0]
    assert first.date == DAY
    assert first.start == time(18, 0)
    assert first.end == time(19, 0)
    assert first.resource_name == "1号塑胶场地"
    assert first.resource_id == "156#1号塑胶场地"
    assert first.remaining == 1
    assert first.status == 1


def test_status5_is_not_available_despite_margin():
    """核心回归：未开放(status=5) 的格子 margin=1，但绝不能算空场。"""
    slots = parse_list_page(PAYLOAD, DAY)
    by_span = {(s.resource_name, s.start): s for s in slots}

    open_slot = by_span[("1号塑胶场地", time(18, 0))]
    assert open_slot.available is True

    unopened = by_span[("1号塑胶场地", time(19, 0))]
    assert unopened.remaining == 1
    assert unopened.status == 5
    assert unopened.available is False

    full = by_span[("2号塑胶场地", time(18, 0))]
    assert full.available is False


def test_matcher_only_sees_bookable_slots():
    slots = parse_list_page(PAYLOAD, DAY)
    hits = select(slots, target_dates=[DAY], windows=parse_windows(["18:00-20:00"]))
    assert len(hits) == 1
    assert hits[0].resource_name == "1号塑胶场地"
    assert hits[0].label == "18:00-19:00"


def test_summarize_table_reports_status_distribution():
    report = summarize_table(PAYLOAD)
    info = report["羽毛球综合场馆（收费）"]
    assert info["courts"] == 2
    assert info["spans"] == 2
    assert info["cells"] == 3
    assert info["status"]["可预约"] == 1
    assert info["status"]["未开放"] == 1
    assert info["status"]["已约满"] == 1


def test_custom_available_status_is_respected():
    # 若学校改成 status=2 表示可约，只改配置即可
    slots = parse_list_page(PAYLOAD, DAY, available_status=5)
    assert [s.available for s in slots] == [False, True, False]


def test_missing_table_yields_empty():
    assert parse_list_page({"list": [{"id": 1, "name": "x"}]}, DAY) == []
    assert parse_list_page({}, DAY) == []
    assert parse_list_page(None, DAY) == []


def test_parse_clock_variants():
    assert parse_clock("18:00") == time(18, 0)
    assert parse_clock("18:00:00") == time(18, 0)
    assert parse_clock("1800") == time(18, 0)
    assert parse_clock(1800) == time(18, 0)
    assert parse_clock("19：30") == time(19, 30)  # 全角冒号
    assert parse_clock("abc") is None


def test_parse_day_variants():
    assert parse_day("2026-11-05") == date(2026, 11, 5)
    assert parse_day("2026/11/05") == date(2026, 11, 5)
    assert parse_day("20261105") == date(2026, 11, 5)
    assert parse_day("not-a-date") is None


def test_parse_remaining_variants():
    assert parse_remaining(3) == 3
    assert parse_remaining("剩余 2 人") == 2
    assert parse_remaining(True) == 1
    assert parse_remaining("") is None
