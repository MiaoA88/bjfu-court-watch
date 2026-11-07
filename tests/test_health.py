from datetime import timedelta

from src import cli
from src.health import check_stale, report
from src.notify import LoggingNotifier, NotifyError
from src.state import State


class Sender:
    delivers = True
    def __init__(self):
        self.sent = []
    def send(self, *args):
        self.sent.append(args)


def test_stale_warning_and_cooldown_survive_reload(tmp_path):
    t = cli._resolve_now("2026-11-02T15:00:00")
    state = State(health={"last_success_at": t.isoformat()})
    sender = Sender()
    assert not check_stale(state, t + timedelta(minutes=29), ["10:00", "23:00"], sender)
    assert check_stale(state, t + timedelta(minutes=30), ["10:00", "23:00"], sender)
    state.save(tmp_path / "s.json")
    state = State.load(tmp_path / "s.json")
    assert not check_stale(state, t + timedelta(minutes=35), ["10:00", "23:00"], sender)
    assert len(sender.sent) == 1


def test_overnight_pause_does_not_warn_at_start():
    state = State(health={"last_success_at": "2026-11-02T22:55:00+08:00"})
    sender = Sender()
    assert not check_stale(state, cli._resolve_now("2026-11-03T10:00:00"), ["10:00", "23:00"], sender)
    assert sender.sent == []


def test_failed_health_alert_can_retry():
    state = State(health={"first_attempt_at": "2026-11-02T10:00:00+08:00"})
    class Broken(Sender):
        def send(self, *args):
            raise NotifyError("offline")
    t = cli._resolve_now("2026-11-02T10:30:00")
    assert not check_stale(state, t, ["10:00", "23:00"], Broken())
    assert not check_stale(state, t + timedelta(minutes=5), ["10:00", "23:00"], Sender())
    assert check_stale(state, t + timedelta(minutes=15), ["10:00", "23:00"], Sender())


def test_logging_sink_does_not_acknowledge_health_alert():
    state = State(health={"first_attempt_at": "2026-11-02T10:00:00+08:00"})
    assert not check_stale(state, cli._resolve_now("2026-11-02T11:00:00"), ["10:00", "23:00"], LoggingNotifier())
    assert state.health_alert_at is None


def test_report_distinguishes_query_and_delivery():
    state = State(health={"status": "ok", "notification": "failed", "fetched": 10})
    assert "ok" in report(state) and "failed" in report(state)
