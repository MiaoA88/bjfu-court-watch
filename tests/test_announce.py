"""announce 子命令：push 配置后立即把生效的盯场表推到手机。"""

import argparse

import pytest
import yaml

from src import cli


class Recorder:
    delivers = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, url=None):
        self.sent.append((title, body, url))


def write_config(tmp_path, **overrides):
    cfg = {
        "venue": {"hall_id": 54, "resource_ids": [156]},
        "schedule": [
            {"weekday": 1, "time_windows": ["19:00-22:00"]},
            {"weekday": 6, "time_windows": ["13:00-22:00"]},
        ],
        "active_hours": ["12:00", "22:00"],
        "scan_interval_minutes": 5,
        "notify": {"provider": "wxpusher_spt", "detail_url": "https://example.test/x"},
        "state": {"path": str(tmp_path / "state.json")},
    }
    cfg.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def test_announce_pushes_effective_schedule(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: recorder)
    monkeypatch.setenv("WXPUSHER_SPT", "SPT_test")
    cfg = write_config(tmp_path)

    # 2026-11-02 是周一
    rc = cli.cmd_announce(
        argparse.Namespace(config=str(cfg), now="2026-11-02T12:05:00", dry_run=False, verbose=False)
    )
    assert rc == 0
    title, body, url = recorder.sent[0]
    assert "盯场表已生效" in title
    assert "12:00-22:00" in body and "每 5 分钟" in body
    assert "周一 19:00-22:00" in body
    assert "周六 13:00-22:00" in body
    assert url == "https://example.test/x"


def test_announce_reports_bad_yaml(tmp_path):
    """YAML 坏了应该给出可读提示（SystemExit + 中文说明），不是 traceback。"""
    bad = tmp_path / "config.yaml"
    bad.write_text("schedule:\n  - weekday: 5\n    time_windows: ['19:00-22:00']\n    - weekday: 6\n")
    with pytest.raises(SystemExit) as info:
        cli.cmd_announce(
            argparse.Namespace(config=str(bad), now=None, dry_run=True, verbose=False)
        )
    assert "不是合法的 YAML" in str(info.value)


def test_announce_reports_empty_schedule(tmp_path, capsys):
    cfg = write_config(tmp_path, schedule=[])
    rc = cli.cmd_announce(
        argparse.Namespace(config=str(cfg), now=None, dry_run=True, verbose=False)
    )
    assert rc == 1
    assert "schedule 为空" in capsys.readouterr().out


def test_announce_dry_run_does_not_send(tmp_path, monkeypatch):
    """--dry-run 走 ConsoleNotifier：只打印，不发网络请求。"""
    console = cli.ConsoleNotifier()
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: console)
    monkeypatch.setenv("WXPUSHER_SPT", "SPT_test")
    cfg = write_config(tmp_path)

    rc = cli.cmd_announce(
        argparse.Namespace(config=str(cfg), now="2026-11-02T12:05:00", dry_run=True, verbose=False)
    )
    assert rc == 0
    assert len(console.sent) == 1
    assert "盯场表已生效" in console.sent[0][0]


def test_announce_missing_spt_returns_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("WXPUSHER_SPT", raising=False)
    cfg = write_config(tmp_path)
    assert cli.cmd_announce(argparse.Namespace(config=str(cfg), now=None, dry_run=False)) == 1
