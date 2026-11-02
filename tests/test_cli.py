import argparse

import pytest
import yaml

from src import cli


class Recorder:
    """替身通知器：记录下来，不发网络请求。"""

    def __init__(self):
        self.sent = []

    def send(self, title, body, url=None):
        self.sent.append((title, body))


def write_config(tmp_path, **overrides):
    cfg = {
        "venue": {"hall_id": 54},
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


def test_outside_window_skips_without_touching_anything(tmp_path, monkeypatch):
    monkeypatch.delenv("EHALL_COOKIE", raising=False)
    cfg = write_config(tmp_path)
    # 21:00 已过 20:00 关门时间
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
    cfg = write_config(tmp_path, targets={"days_ahead": [], "dates": [], "time_windows": ["18:00-19:00"]})
    with pytest.raises(SystemExit):
        cli.cmd_watch(make_args(cfg, "2026-11-02T10:00:00"))


def test_render_message_groups_by_day_and_caps_lines():
    from datetime import date, time

    from src.matcher import Hit, Slot

    def hit(day, hour, rid="7", name="7号场", remaining=1):
        slot = Slot(
            date=day,
            start=time(hour, 0),
            end=time(hour + 1, 0),
            resource_id=rid,
            resource_name=name,
            remaining=remaining,
        )
        return Hit(
            date=day,
            start=slot.start,
            end=slot.end,
            resource_id=rid,
            resource_name=name,
            slots=(slot,),
        )

    moment = cli.now()
    title, body = cli.render_message([hit(date(2026, 11, 5), 18), hit(date(2026, 11, 5), 19)], 54, moment, None)
    assert "2 处空场" in title
    assert "11-05" in body and "18:00-19:00" in body
    assert "场馆 54" in body

    many = [hit(date(2026, 11, 5), h % 23, rid=str(h)) for h in range(50)]
    _, capped = cli.render_message(many, 54, moment, None, max_lines=10)
    assert "另有 40 处未列出" in capped


def test_render_message_without_detail_url_is_fine():
    moment = cli.now()
    title, body = cli.render_message([], 54, moment, None)
    assert "无空场" in title
    assert "场馆 54" in body
