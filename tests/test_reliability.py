"""Regressions covering delivery failures across persisted watch cycles."""
import argparse
from datetime import timedelta

import pytest
import yaml

from src import cli
from src.notify import LoggingNotifier, NotifyError
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
    assert cell(21).key(54) in saved.pending
    assert saved.health["status"] == "ok"
    assert saved.health["notification"] == ("failed" if mode == "failure" else "unconfigured")
    monkeypatch.setattr(h.recorder, "send", original)
    h.recorder.delivers = True
    assert h.run([cell(21)])[1] == 1
    assert State.load(tmp_path / "state.json").pending == {}
    assert h.run([cell(21)])[1] == 0


def test_pending_opportunity_is_cancelled_if_taken(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.recorder.delivers = False
    h.run([cell(21)])
    assert State.load(tmp_path / "state.json").pending
    h.recorder.delivers = True
    assert h.run([cell(21, status=3)])[1] == 0
    assert not State.load(tmp_path / "state.json").pending
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


def test_failure_alert_retries_after_cooldown_and_resets_on_recovery(tmp_path):
    state = State(consecutive_failures=5)
    moment = cli._resolve_now("2026-11-02T15:00:00")
    class Sender:
        delivers = True
        attempts = 0
        fail = True
        def send(self, *args):
            self.attempts += 1
            if self.fail:
                raise NotifyError("offline")
    sender = Sender()
    path = tmp_path / "state.json"
    def run(at):
        nonlocal state
        cli._handle_error(state, path, sender, at, RuntimeError("upstream"), kind="other")
        state = State.load(path)
    run(moment)
    assert sender.attempts == 1
    sender.fail = False
    run(moment + timedelta(minutes=5))
    assert sender.attempts == 1
    run(moment + timedelta(minutes=15))
    assert sender.attempts == 2 and state.failure_alert_sent
    run(moment + timedelta(hours=1))
    assert sender.attempts == 2
    state.record_success()
    assert not state.failure_alert_sent and state.failure_alert_attempt_at is None


def test_logging_channel_does_not_acknowledge_auth_alert(tmp_path):
    state = State()
    cli._handle_error(state, tmp_path / "s.json", LoggingNotifier(),
                      cli._resolve_now("2026-11-02T15:00:00"), RuntimeError(), kind="auth")
    assert state.auth_alert_on is None


def test_dry_run_does_not_change_persisted_state(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.run([cell(21, status=3)])
    path = tmp_path / "state.json"
    before = path.read_bytes()
    h.slots = [cell(21)]
    args = argparse.Namespace(config=str(h.cfg), now="2026-11-02T15:00:00", dry_run=True, sleep_between=0)
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: cli.ConsoleNotifier())
    assert cli.cmd_watch(args) == 0
    assert path.read_bytes() == before


def test_partial_scan_failure_does_not_acknowledge_observations(tmp_path, monkeypatch):
    from src.ehall import SchemaChanged
    h = Harness(tmp_path, monkeypatch)
    h.run([cell(21, status=3)])
    cfg = yaml.safe_load(h.cfg.read_text(encoding="utf-8"))
    cfg["schedule"].append({"weekday": 2, "time_windows": ["20:00-22:00"]})
    h.cfg.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    def fetch(*args, **kwargs):
        if kwargs["day"].isoweekday() == 2:
            raise SchemaChanged("bad page")
        return [cell(21)]
    monkeypatch.setattr(cli, "fetch_slots", fetch)
    h.run([])
    state = State.load(tmp_path / "state.json")
    assert state.was_occupied(cell(21).key(54))
    assert state.health["status"] == "other_error"
    assert state.health["days"] == 1
