"""通知附录与每日启动通知。"""

import argparse
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
import yaml

from src import cli
from src.matcher import Slot
from src.schedule import Target

BJ = ZoneInfo("Asia/Shanghai")


class Recorder:
    delivers = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, url=None):
        self.sent.append((title, body))


def write_config(tmp_path, **overrides):
    cfg = {
        "venue": {"hall_id": 54, "resource_ids": [156]},
        "schedule": [{"weekday": 1, "time_windows": ["20:00-21:00"]}],
        "active_hours": ["14:00", "22:00"],
        "notify": {
            "provider": "wxpusher_spt",
            "announce_start": True,
            "announce_schedule_changes": False,
        },
        "state": {"path": str(tmp_path / "state.json")},
    }
    cfg.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def make_args(config, now):
    return argparse.Namespace(config=str(config), now=now, dry_run=False, sleep_between=0, verbose=False)


def slot(day, start_hour, end_hour, status=1, name="1号塑胶场地"):
    return Slot(
        date=day,
        start=time(start_hour, 0),
        end=time(end_hour, 0),
        resource_id=f"156#{name}",
        resource_name=name,
        remaining=1,
        status=status,
    )


def stub(monkeypatch, slots, recorder=None):
    recorder = recorder or Recorder()
    monkeypatch.setattr(cli, "build_client", lambda cfg: object())
    monkeypatch.setattr(cli, "fetch_resources", lambda client, hall_id: [])
    monkeypatch.setattr(cli, "fetch_slots", lambda *a, **k: list(slots))
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: recorder)
    return recorder


# ------------------------------------------------------------------ 附录
def test_cycle_log_appendix_lists_each_day():
    moment = datetime(2026, 11, 4, 15, 0, tzinfo=BJ)
    cycle = cli.CycleLog(moment)
    cycle.add_day(Target(date=date(2026, 11, 4), windows=((time(20, 0), time(21, 0)),)), 252, 84, 0)
    cycle.add_day(Target(date=date(2026, 11, 7), windows=((time(14, 0), time(22, 0)),)), 252, 90, 2)

    text = cycle.appendix()
    assert "**运行日志** 11-04 15:00" in text
    assert "11-04 周三 20:00-21:00：取到 252 格 → 可用 84 → 命中 0" in text
    assert "11-07 周六 14:00-22:00：取到 252 格 → 可用 90 → 命中 2" in text
    assert cycle.total_fetched == 504
    assert cycle.total_hits == 2


def test_cycle_log_notes():
    cycle = cli.CycleLog(datetime(2026, 11, 4, 15, 0, tzinfo=BJ))
    cycle.note("接口返回 10042")
    assert "接口返回 10042" in cycle.appendix()


def test_hit_notification_contains_appendix(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    recorder = stub(monkeypatch, [slot(date(2026, 11, 2), 20, 21)])
    # 2026-11-02 周一 → 查本周一 20:00-21:00
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00")) == 0

    hit_msgs = [body for title, body in recorder.sent if "空场" in title]
    assert len(hit_msgs) == 1
    body = hit_msgs[0]
    assert "运行日志" in body
    assert "取到 1 格 → 可用 1 → 命中 1" in body


def test_no_hit_means_no_notification(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    recorder = stub(monkeypatch, [slot(date(2026, 11, 2), 20, 21, status=3)])
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00")) == 0
    assert [t for t, _ in recorder.sent if "空场" in t] == []


def test_no_targets_weekday_means_no_notification(tmp_path, monkeypatch):
    """周三查周一规则：本周一已过，无目标日期 → 不发任何通知。"""
    cfg = write_config(tmp_path)
    recorder = stub(monkeypatch, [slot(date(2026, 11, 9), 20, 21)])
    assert cli.cmd_watch(make_args(cfg, "2026-11-04T15:00:00")) == 0
    assert recorder.sent == []


# ------------------------------------------------------------------ 启动通知
def test_start_notification_sent_once_per_day(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    recorder = stub(monkeypatch, [slot(date(2026, 11, 2), 20, 21)])

    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00")) == 0
    starts = [t for t, _ in recorder.sent if "启动" in t]
    assert len(starts) == 1

    # 同日第二次：不再发
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:05:00")) == 0
    assert len([t for t, _ in recorder.sent if "启动" in t]) == 1


def test_start_notification_body_shows_schedule(tmp_path, monkeypatch):
    cfg = write_config(
        tmp_path,
        schedule=[
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
            {"weekday": 6, "time_windows": ["14:00-22:00"]},
        ],
    )
    recorder = stub(monkeypatch, [])
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00")) == 0

    body = next(b for t, b in recorder.sent if "启动" in t)
    assert "周一 20:00-21:00" in body
    assert "周六 14:00-22:00" in body
    assert "本次要查" in body


def test_start_notification_can_be_disabled(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, notify={"provider": "wxpusher_spt", "announce_start": False})
    recorder = stub(monkeypatch, [])
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00")) == 0
    assert [t for t, _ in recorder.sent if "启动" in t] == []


def test_start_notification_still_sent_outside_window(tmp_path, monkeypatch):
    """窗口外也要发：这是判断"调度器到底有没有派活"的唯一信号。

    2026-11-04 全天没有任何消息，就是因为唯一一次 schedule 落在 04:31，
    被窗口闸门挡住后直接 return，连启动通知都没发。
    """
    cfg = write_config(tmp_path)
    recorder = stub(monkeypatch, [])
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T23:00:00")) == 0

    starts = [t for t, _ in recorder.sent if "启动" in t]
    assert len(starts) == 1
    body = next(b for t, b in recorder.sent if "启动" in t)
    assert "扫描窗口" in body and "之外" in body, "应说明本次落在窗口外、不会查询"
    assert "不会查询场地" in body


def test_heartbeat_written_to_step_summary(tmp_path, monkeypatch):
    """每次运行都写 job summary，用于在 Actions 页面核对调度频次。"""
    cfg = write_config(tmp_path)
    stub(monkeypatch, [])
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00"))
    cli.cmd_watch(make_args(cfg, "2026-11-02T23:30:00"))

    text = summary.read_text(encoding="utf-8")
    assert "窗口内" in text and "窗口外" in text
    assert text.count("- ") == 2
