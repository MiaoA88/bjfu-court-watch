"""每日启动和通知正文；复用扫描测试替身。"""
from datetime import date

import pytest

from src import cli
from src.ehall import TransportError
from test_cli import write_config as cli_config, make_args, slot, stub_pipeline


def write_config(tmp_path, **overrides):
    overrides.setdefault("notify", {"provider": "wxpusher_spt", "announce_schedule_changes": False})
    return cli_config(tmp_path, **overrides)


def test_start_notification_sent_once_per_day(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    recorder = stub_pipeline(monkeypatch, [slot(date(2026, 11, 2), 20, 21)])

    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00")) == 0
    starts = [t for t, _ in recorder.sent if "启动" in t]
    assert len(starts) == 1

    # 同日第二次：不再发
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:05:00")) == 0
    assert len([t for t, _ in recorder.sent if "启动" in t]) == 1

    assert cli.cmd_watch(make_args(cfg, "2026-11-03T15:00:00")) == 0
    assert len([t for t, _ in recorder.sent if "启动" in t]) == 2


def test_start_notification_body_shows_schedule(tmp_path, monkeypatch):
    cfg = write_config(
        tmp_path,
        schedule=[
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
            {"weekday": 6, "time_windows": ["14:00-22:00"]},
        ],
    )
    recorder = stub_pipeline(monkeypatch, [])
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00")) == 0

    body = next(b for t, b in recorder.sent if "启动" in t)
    assert "周一 20:00-21:00" in body
    assert "周六 14:00-22:00" in body
    assert "本次要查" in body


def test_start_notification_can_be_disabled(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, notify={"provider": "wxpusher_spt", "announce_start": False})
    recorder = stub_pipeline(monkeypatch, [])
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00")) == 0
    assert [t for t, _ in recorder.sent if "启动" in t] == []


def test_start_notification_still_sent_outside_window(tmp_path, monkeypatch):
    """窗口外也要发：这是判断"调度器到底有没有派活"的唯一信号。

    2026-11-04 全天没有任何消息，就是因为唯一一次 schedule 落在 04:31，
    被窗口闸门挡住后直接 return，连启动通知都没发。
    """
    cfg = write_config(tmp_path)
    recorder = stub_pipeline(monkeypatch, [])
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T23:00:00")) == 0

    starts = [t for t, _ in recorder.sent if "启动" in t]
    assert len(starts) == 1
    body = next(b for t, b in recorder.sent if "启动" in t)
    assert "扫描窗口" in body and "之外" in body, "应说明本次落在窗口外、不会查询"
    assert "不会查询场地" in body


def test_heartbeat_written_to_step_summary(tmp_path, monkeypatch):
    """每次运行都写 job summary，用于在 Actions 页面核对调度频次。"""
    cfg = write_config(tmp_path)
    stub_pipeline(monkeypatch, [])
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00"))
    cli.cmd_watch(make_args(cfg, "2026-11-02T23:30:00"))

    text = summary.read_text(encoding="utf-8")
    assert "窗口内" in text and "窗口外" in text
    assert text.count("- ") == 2


@pytest.mark.parametrize("moment,fail_query", [
    ("2026-11-04T15:00:00", False),  # 本周目标已过去
    ("2026-11-02T15:00:00", True),
])
def test_start_notice_even_without_targets_or_when_query_fails(tmp_path, monkeypatch, moment, fail_query):
    cfg = write_config(tmp_path)
    recorder = stub_pipeline(monkeypatch, [])
    def fetch(*args, **kwargs):
        if fail_query:
            raise TransportError("offline")
        pytest.fail("无目标日期不应查询")
    monkeypatch.setattr(cli, "fetch_slots", fetch)
    for _ in range(2):
        assert cli.cmd_watch(make_args(cfg, moment)) == 0
    assert len([t for t, _ in recorder.sent if "启动" in t]) == 1


def test_hit_body_contains_details_without_scan_appendix(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    recorder = stub_pipeline(monkeypatch, [slot(date(2026, 11, 2), 20, 21)])
    cli.cmd_watch(make_args(cfg, "2026-11-02T15:00:00"))
    body = next(b for t, b in recorder.sent if "空场" in t)
    assert "1号塑胶场地" in body and "20:00-21:00" in body
    assert "运行日志" not in body
