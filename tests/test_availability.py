from datetime import date, time

from src.availability import FieldMap, diagnose, extract_slots, parse_clock, parse_day, parse_remaining


def test_flat_list_payload():
    payload = [
        {
            "resourceId": "7",
            "resourceName": "7号场",
            "date": "2026-11-05",
            "startTime": "18:00",
            "endTime": "19:00",
            "surplus": 2,
        },
        {
            "resourceId": "7",
            "resourceName": "7号场",
            "date": "2026-11-05",
            "startTime": "19:00",
            "endTime": "20:00",
            "surplus": 0,
        },
    ]
    slots = extract_slots(payload)
    assert len(slots) == 2
    assert slots[0].start == time(18, 0)
    assert slots[0].remaining == 2
    assert slots[1].remaining == 0


def test_date_inherited_from_ancestor():
    payload = {
        "hallId": 54,
        "date": "2026-11-05",
        "resources": [
            {
                "resourceId": "7",
                "resourceName": "7号场",
                "times": [{"beginTime": "18:00:00", "endTime": "19:00:00", "remain": 1}],
            }
        ],
    }
    slots = extract_slots(payload)
    assert len(slots) == 1
    assert slots[0].date == date(2026, 11, 5)
    assert slots[0].resource_id == "7"


def test_compact_int_times_and_boolean_availability():
    payload = {
        "items": [
            {
                "id": "9",
                "name": "9号场",
                "date": "20261105",
                "start": 1800,
                "end": 1900,
                "canReserve": True,
            }
        ]
    }
    slots = extract_slots(payload)
    assert len(slots) == 1
    assert slots[0].start == time(18, 0)
    assert slots[0].end == time(19, 0)
    assert slots[0].remaining == 1


def test_missing_end_time_is_skipped():
    payload = [{"resourceId": "7", "date": "2026-11-05", "startTime": "18:00", "surplus": 1}]
    assert extract_slots(payload) == []


def test_garbage_payload_returns_empty():
    assert extract_slots({"msg": "维护中"}) == []
    assert extract_slots(None) == []
    assert extract_slots([]) == []


def test_custom_field_map():
    payload = [{"court": "3号场", "day": "2026-11-05", "from": "18:00", "to": "19:00", "free": 4}]
    fields = FieldMap(
        start=["from"],
        end=["to"],
        remaining=["free"],
        resource_name=["court"],
        resource_id=["court"],
        date=["day"],
    )
    slots = extract_slots(payload, fields)
    assert len(slots) == 1
    assert slots[0].resource_name == "3号场"
    assert slots[0].remaining == 4


def test_parse_clock_variants():
    assert parse_clock("18:00") == time(18, 0)
    assert parse_clock("18:00:00") == time(18, 0)
    assert parse_clock("1800") == time(18, 0)
    assert parse_clock(1800) == time(18, 0)
    assert parse_clock(900) == time(9, 0)
    assert parse_clock("19：30") == time(19, 30)  # 全角冒号
    assert parse_clock("") is None
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
    assert parse_remaining(False) == 0
    assert parse_remaining("有") == 1
    assert parse_remaining("约满") == 0
    assert parse_remaining("") is None


def test_diagnose_flags_slot_like_nodes():
    payload = {"date": "2026-11-05", "list": [{"beginTime": "18:00", "endTime": "19:00", "surplus": 1}]}
    report = diagnose(payload)
    assert report["node_count"] > 0
    assert report["slot_like"]
