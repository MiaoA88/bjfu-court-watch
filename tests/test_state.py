import json
from datetime import date, datetime

from src.state import SCHEMA_VERSION, State


def test_roundtrip(tmp_path):
    path = tmp_path / "notified.json"
    state = State()
    state.mark_notified("54|2026-11-05|18:00|19:00|7", datetime(2026, 11, 3, 10, 0))
    state.record_failure()
    state.auth_alert_on = "2026-11-03"
    state.resources["54"] = [{"id": "7", "name": "7号场"}]
    state.save(path)

    loaded = State.load(path)
    assert loaded.notified == state.notified
    assert loaded.consecutive_failures == 1
    assert loaded.auth_alert_on == "2026-11-03"
    assert loaded.resources["54"] == [{"id": "7", "name": "7号场"}]


def test_missing_file_yields_empty_state(tmp_path):
    assert State.load(tmp_path / "nope.json").notified == {}


def test_corrupt_file_does_not_crash(tmp_path):
    path = tmp_path / "notified.json"
    path.write_text("{not json", encoding="utf-8")
    assert State.load(path).notified == {}


def test_schema_version_mismatch_resets(tmp_path):
    path = tmp_path / "notified.json"
    path.write_text(json.dumps({"schema_version": 99, "notified": {"a": "b"}}), encoding="utf-8")
    assert State.load(path).notified == {}


def test_prune_drops_past_dates_only():
    state = State()
    state.notified = {
        "54|2026-11-01|18:00|19:00|7": "x",  # 已过
        "54|2026-11-02|18:00|19:00|7": "x",  # 今天：目标日期已到，仍保留
        "54|2026-11-05|18:00|19:00|7": "x",  # 未来
    }
    removed = state.prune(date(2026, 11, 2))
    assert removed == 1
    assert set(state.notified) == {"54|2026-11-02|18:00|19:00|7", "54|2026-11-05|18:00|19:00|7"}


def test_prune_removes_malformed_keys():
    state = State()
    state.notified = {"garbage": "x", "54|not-a-date|18:00|19:00|7": "x"}
    assert state.prune(date(2026, 11, 2)) == 2
    assert state.notified == {}


def test_failure_counter():
    state = State()
    assert state.record_failure() == 1
    assert state.record_failure() == 2
    state.record_success()
    assert state.consecutive_failures == 0


def test_saved_payload_carries_schema_version(tmp_path):
    path = tmp_path / "notified.json"
    State().save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
