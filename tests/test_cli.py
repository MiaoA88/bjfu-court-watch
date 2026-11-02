import argparse
from datetime import date, time

import pytest
import yaml

from src import cli
from src.matcher import Hit, Slot


class Recorder:
    """替身通知器：记录下来，不发网络请求。"""

    def __init__(self):
        self.sent = []

    def send(self, title, body, url=None):
        self.sent.append((title, body))


def write_config(tmp_path, **overrides):
    cfg = {
        "venue": {"hall_id": 54, "resource_ids": [156]},
        "targets": {"days_ahead": [1], "time_windows": ["18:00-19:00"]},
        "active_hours": ["09:00", "20:00"],
        "notify": {"provider": "wxpusher_spt"},
        "state": {"path": str(tmp_path / "state.json")},
    }
    cfg.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def make_args(config, now, dry_run=False):
    return argparse.Namespace(
        config=str(config), now=now, dry_run=dry_run, sleep_between=0, verbose=False
    )


def make_hit(day, hour, name="7号场", rid="156#7号场", minutes=60):
    start = time(hour, 0)
    end = time((hour + minutes // 60) % 24, 0)
    slot = Slot(
        date=day,
        start=start,
        end=end,
        resource_id=rid,
        resource_name=name,
        remaining=1,
        status=1,
    )
    return Hit(date=day, start=start, end=end, resource_id=rid, resource_name=name, slots=(slot,))


# ------------------------------------------------------------------ 窗口闸门
def test_outside_window_skips_without_touching_anything(tmp_path, monkeypatch):
    monkeypatch.delenv("EHALL_COOKIE", raising=False)
    cfg = write_config(tmp_path)
    # 21:00 已过扫描窗口 20:00
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T21:00:00")) == 0
    assert not (tmp_path / "state.json").exists(), "窗口外不应写状态"


def test_missing_cookie_alerts_once_per_day(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: recorder)
    monkeypatch.delenv("EHALL_COOKIE", raising=False)
    cfg = write_config(tmp_path)

    assert cli.cmd_watch(make_args(cfg, "2026-11-02T10:00:00")) == 0
    assert len(recorder.sent) == 1
    assert "配置" in recorder.sent[0][0]

    # 同一天再跑：不重复打扰
    assert cli.cmd_watch(make_args(cfg, "2026-11-02T10:05:00")) == 0
    assert len(recorder.sent) == 1

    # 次日恢复提醒
    assert cli.cmd_watch(make_args(cfg, "2026-11-03T10:00:00")) == 0
    assert len(recorder.sent) == 2


def test_empty_time_windows_is_fatal(tmp_path):
    cfg = write_config(tmp_path, targets={"days_ahead": [1], "time_windows": []})
    with pytest.raises(SystemExit):
        cli.cmd_watch(make_args(cfg, "2026-11-02T10:00:00"))


def test_empty_targets_is_fatal(tmp_path):
    cfg = write_config(
        tmp_path, targets={"days_ahead": [], "dates": [], "time_windows": ["18:00-19:00"]}
    )
    with pytest.raises(SystemExit):
        cli.cmd_watch(make_args(cfg, "2026-11-02T10:00:00"))


# ------------------------------------------------------------------ 消息渲染
def test_render_merges_courts_of_same_span():
    day = date(2026, 11, 5)
    hits = [make_hit(day, 18, name="1号场", rid="156#1"), make_hit(day, 18, name="2号场", rid="156#2")]
    title, body = cli.render_message(hits, 54, cli.now(), None)

    assert "1 个时段有空场" in title
    assert "11-05" in body
    assert "18:00-19:00 · 2 块（1号场、2号场）" in body
    assert "场馆 54" in body


def test_render_sorts_courts_numerically():
    day = date(2026, 11, 5)
    hits = [
        make_hit(day, 18, name="10号场", rid="156#10"),
        make_hit(day, 18, name="2号场", rid="156#2"),
    ]
    _, body = cli.render_message(hits, 54, cli.now(), None)
    assert "2 号场、10号场" not in body  # 断言排序：2 在 10 前
    assert "2号场、10号场" in body


def test_render_caps_items_and_reports_remainder():
    day = date(2026, 11, 5)
    hits = [make_hit(day, hour, name=f"{hour}号场", rid=f"156#{hour}") for hour in range(12)]
    title, body = cli.render_message(hits, 54, cli.now(), None, max_items=5)

    assert "12 个时段有空场" in title
    assert "另有 7 个时段未列出" in body


def test_render_empty_hits():
    title, body = cli.render_message([], 54, cli.now(), None)
    assert "暂无空场" in title
    assert "场馆 54" in body


def test_court_key_sorts_by_leading_number():
    names = ["10号场", "2号场", "1号场", "未知场地"]
    assert sorted(names, key=cli._court_key) == ["1号场", "2号场", "10号场", "未知场地"]
