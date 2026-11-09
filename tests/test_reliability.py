"""Regressions covering delivery failures across persisted watch cycles."""
import argparse

import pytest
import yaml

from src import cli
from src.notify import ConsoleNotifier, NotifyError
from src.ehall import AuthExpired, SchemaChanged, TransportError
from src.state import State
from test_dedup import Harness, cell


@pytest.mark.parametrize("mode", ["failure", "missing_spt"])
def test_released_slot_retries_until_delivery(tmp_path, monkeypatch, mode):
    h = Harness(tmp_path, monkeypatch)
    h.run([cell(21)])
    h.run([cell(21, status=3)])
    original = h.recorder.send
    if mode == "failure":
        def fail(*args):
            raise NotifyError("offline")
        monkeypatch.setattr(h.recorder, "send", fail)
    else:
        h.recorder.delivers = False
    rc, _ = h.run([cell(21)])
    assert rc == (1 if mode == "failure" else 0)
    saved = State.load(tmp_path / "state.json")
    assert cell(21).key(54) not in saved.notified
    monkeypatch.setattr(h.recorder, "send", original)
    h.recorder.delivers = True
    assert h.run([cell(21)])[1] == 1
    assert cell(21).key(54) in State.load(tmp_path / "state.json").notified
    assert h.run([cell(21)])[1] == 0


def test_unsent_free_cell_taken_then_released(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.recorder.delivers = False
    h.run([cell(21)])
    assert not State.load(tmp_path / "state.json").notified
    h.recorder.delivers = True
    assert h.run([cell(21, status=3)])[1] == 0
    assert not State.load(tmp_path / "state.json").notified
    assert h.run([cell(21)])[1] == 1


def test_schedule_notice_is_not_acknowledged_on_failure(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    cfg = yaml.safe_load(h.cfg.read_text(encoding="utf-8"))
    cfg["notify"]["announce_schedule_changes"] = True
    h.cfg.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    original = h.recorder.send
    def fail(*args):
        raise NotifyError("offline")
    monkeypatch.setattr(h.recorder, "send", fail)
    h.run([])
    assert State.load(tmp_path / "state.json").schedule_fingerprint is None
    monkeypatch.setattr(h.recorder, "send", original)
    h.run([])
    assert len(h.recorder.sent) == 1
    assert State.load(tmp_path / "state.json").schedule_fingerprint
    h.run([])
    assert len(h.recorder.sent) == 1


def test_dry_run_does_not_change_persisted_state(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.run([cell(21, status=3)])
    path = tmp_path / "state.json"
    before = path.read_bytes()
    h.slots = [cell(21)]
    args = argparse.Namespace(config=str(h.cfg), now="2026-11-02T15:00:00", dry_run=True, sleep_between=0)
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: ConsoleNotifier())
    assert cli.cmd_watch(args) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("first_day_free", [False, True])
def test_partial_scan_failure_does_not_change_observations(tmp_path, monkeypatch, first_day_free):
    h = Harness(tmp_path, monkeypatch)
    h.run([cell(21, status=3 if first_day_free else 1)])
    before = State.load(tmp_path / "state.json").notified
    cfg = yaml.safe_load(h.cfg.read_text(encoding="utf-8"))
    cfg["schedule"].append({"weekday": 2, "time_windows": ["20:00-22:00"]})
    h.cfg.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    def fetch(*args, **kwargs):
        if kwargs["day"].isoweekday() == 2:
            raise SchemaChanged("bad page")
        return [cell(21, status=1 if first_day_free else 3)]
    monkeypatch.setattr(cli, "fetch_slots", fetch)
    assert h.run([]) == (0, 0)
    assert State.load(tmp_path / "state.json").notified == before


@pytest.mark.parametrize("mode", ["delivered", "failure", "missing_spt"])
def test_auth_notice_suppressed_across_days_until_successful_scan(tmp_path, monkeypatch, mode):
    h = Harness(tmp_path, monkeypatch)
    cfg = yaml.safe_load(h.cfg.read_text(encoding="utf-8"))
    cfg["schedule"] = [{"weekday": 7, "time_windows": ["20:00-22:00"]}]
    h.cfg.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    original = h.recorder.send
    def fail(*args):
        raise NotifyError("offline")
    if mode == "failure":
        monkeypatch.setattr(h.recorder, "send", fail)
    elif mode == "missing_spt":
        h.recorder.delivers = False
    def expired(*args, **kwargs):
        raise AuthExpired("expired")
    monkeypatch.setattr(cli, "fetch_slots", expired)
    h.run([])
    assert State.load(tmp_path / "state.json").auth_alert_sent == (mode == "delivered")
    monkeypatch.setattr(h.recorder, "send", original)
    h.recorder.delivers = True
    h.recorder.sent.clear()
    for moment in ("2026-11-02T15:05:00", "2026-11-03T15:00:00"):
        h.run([], now=moment)
    assert len(h.recorder.sent) == (0 if mode == "delivered" else 1)
    # 普通接口错误不能当成登录恢复。
    def offline(*args, **kwargs):
        raise TransportError("offline")
    monkeypatch.setattr(cli, "fetch_slots", offline)
    h.run([])
    assert State.load(tmp_path / "state.json").auth_alert_sent
    monkeypatch.setattr(cli, "fetch_slots", lambda *a, **k: [])
    h.run([])
    assert not State.load(tmp_path / "state.json").auth_alert_sent
    monkeypatch.setattr(cli, "fetch_slots", expired)
    h.run([])
    assert len(h.recorder.sent) == (1 if mode == "delivered" else 2)
