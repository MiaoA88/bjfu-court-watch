import argparse
from datetime import date, time

import pytest
import yaml

from src import cli
from src.matcher import Hit, Slot

MONDAY = "2026-11-02"      # 周一
WEDNESDAY = "2026-11-04"   # 周三


class Recorder:
    """替身通知器：记录下来，不发网络请求。"""

    delivers = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, url=None):
        self.sent.append((title, body))


class SilentRecorder(Recorder):
    delivers = False


def write_config(tmp_path, **overrides):
    cfg = {
        "venue": {"hall_id": 54, "resource_ids": [156]},
        "schedule": [{"weekday": 1, "time_windows": ["20:00-21:00"]}],
        "active_hours": ["14:00", "22:00"],
        "notify": {"provider": "wxpusher_spt", "announce_schedule_changes": True},
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


def slot(day, start_hour, end_hour, name="1号塑胶场地", status=1, remaining=1):
    return Slot(
        date=day,
        start=time(start_hour, 0),
        end=time(end_hour, 0),
        resource_id=f"156#{name}",
        resource_name=name,
        remaining=remaining,
        status=status,
    )


def stub_pipeline(monkeypatch, slots, recorder=None):
    """把网络与推送全部打桩，只留编排逻辑。"""
    recorder = recorder or Recorder()
    monkeypatch.setattr(cli, "build_client", lambda cfg: object())
    monkeypatch.setattr(cli, "fetch_resources", lambda client, hall_id: [])
    monkeypatch.setattr(cli, "fetch_slots", lambda *a, **k: list(slots))
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: recorder)
    return recorder


# ------------------------------------------------------------------ 闸门：扫描窗口
def test_outside_active_hours_skips_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("EHALL_COOKIE", raising=False)
    cfg = write_config(tmp_path)
    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T22:00:00")) == 0  # 22:00 右开边界
    assert not (tmp_path / "state.json").exists(), "窗口外不应写状态"


def test_empty_schedule_is_fatal(tmp_path):
    cfg = write_config(tmp_path, schedule=[])
    with pytest.raises(SystemExit):
        cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00"))


def test_invalid_schedule_is_fatal(tmp_path):
    cfg = write_config(tmp_path, schedule=[{"weekday": 9, "time_windows": ["20:00-21:00"]}])
    with pytest.raises(SystemExit):
        cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00"))


# ------------------------------------------------------------------ 每天都跑，查下一次
def test_runs_on_non_matching_weekday_and_queries_remaining_week(tmp_path, monkeypatch):
    """周三也要跑，但只查本周剩下的周六；周一那条已过去，不该再查。"""
    cfg = write_config(
        tmp_path,
        schedule=[
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
            {"weekday": 6, "time_windows": ["14:00-15:00"]},
        ],
    )
    seen = []

    def fake_fetch_slots(client, *, hall_id, day, page_size, available_status):
        seen.append(day)
        return [slot(day, 14, 15)]

    recorder = Recorder()
    monkeypatch.setattr(cli, "build_client", lambda cfg: object())
    monkeypatch.setattr(cli, "fetch_resources", lambda client, hall_id: [])
    monkeypatch.setattr(cli, "fetch_slots", fake_fetch_slots)
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: recorder)

    assert cli.cmd_watch(make_args(cfg, f"{WEDNESDAY}T15:00:00")) == 0
    assert seen == [date(2026, 11, 7)], "周三只该查本周六"
    assert any("空场" in title for title, _ in recorder.sent)


def test_no_remaining_weekday_exits_without_requests(tmp_path, monkeypatch):
    """周日跑、只配了周一周六：本周已无目标，零请求退出。"""
    cfg = write_config(
        tmp_path,
        schedule=[
            {"weekday": 1, "time_windows": ["20:00-21:00"]},
            {"weekday": 6, "time_windows": ["14:00-15:00"]},
        ],
    )
    monkeypatch.delenv("EHALL_COOKIE", raising=False)
    assert cli.cmd_watch(make_args(cfg, "2026-11-08T15:00:00")) == 0
    assert not (tmp_path / "state.json").exists()


# ------------------------------------------------------------------ 盯场表变更通知
def test_announces_schedule_on_first_run_only(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    recorder = stub_pipeline(monkeypatch, [slot(date(2026, 11, 2), 20, 21)])

    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00")) == 0
    announces = [t for t, _ in recorder.sent if "盯场表" in t]
    assert len(announces) == 1
    body = next(b for t, b in recorder.sent if "盯场表" in t)
    assert "周一 20:00-21:00" in body

    # 同配置再跑：不再公告
    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:05:00")) == 0
    assert len([t for t, _ in recorder.sent if "盯场表" in t]) == 1


def test_announces_again_when_schedule_changes(tmp_path, monkeypatch):
    recorder = stub_pipeline(monkeypatch, [slot(date(2026, 11, 2), 20, 21)])
    cfg = write_config(tmp_path)
    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00")) == 0

    cfg2 = write_config(
        tmp_path,
        schedule=[{"weekday": 1, "time_windows": ["19:00-21:00"]}],
    )
    assert cli.cmd_watch(make_args(cfg2, f"{MONDAY}T15:05:00")) == 0
    bodies = [b for t, b in recorder.sent if "盯场表" in t]
    assert len(bodies) == 2
    assert "19:00-21:00" in bodies[-1]


def test_announce_can_be_disabled(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, notify={"provider": "wxpusher_spt", "announce_schedule_changes": False})
    recorder = stub_pipeline(monkeypatch, [slot(date(2026, 11, 2), 20, 21)])
    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00")) == 0
    assert [t for t, _ in recorder.sent if "盯场表" in t] == []


def test_silent_channel_does_not_record_fingerprint(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    silent = SilentRecorder()
    stub_pipeline(monkeypatch, [slot(date(2026, 11, 2), 20, 21)], silent)
    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00")) == 0

    # 换回会投递的通道：应当补发一次公告
    recorder = Recorder()
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: recorder)
    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:05:00")) == 0
    assert len([t for t, _ in recorder.sent if "盯场表" in t]) == 1


# ------------------------------------------------------------------ 去重
def test_second_run_does_not_repeat_the_same_slot(tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    recorder = stub_pipeline(monkeypatch, [slot(date(2026, 11, 2), 20, 21)])

    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00")) == 0
    first_hits = [t for t, _ in recorder.sent if "空场" in t]
    assert len(first_hits) == 1

    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:05:00")) == 0
    assert len([t for t, _ in recorder.sent if "空场" in t]) == 1, "同一空场不应重复推送"


# ------------------------------------------------------------------ 可用性判定
def test_unopened_slot_is_not_pushed(tmp_path, monkeypatch):
    """status=5（未开放）即使 margin=1 也不能推。"""
    cfg = write_config(tmp_path)
    recorder = stub_pipeline(monkeypatch, [slot(date(2026, 11, 2), 20, 21, status=5)])
    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00")) == 0
    assert [t for t, _ in recorder.sent if "空场" in t] == []


# ------------------------------------------------------------------ 缺配置
def test_missing_cookie_alerts_once_per_day(tmp_path, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: recorder)
    monkeypatch.delenv("EHALL_COOKIE", raising=False)
    cfg = write_config(tmp_path)

    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:00:00")) == 0
    assert len([t for t, _ in recorder.sent if "配置" in t]) == 1

    assert cli.cmd_watch(make_args(cfg, f"{MONDAY}T15:05:00")) == 0
    assert len([t for t, _ in recorder.sent if "配置" in t]) == 1


# ------------------------------------------------------------------ 本地自检
def test_check_passes_on_valid_config(tmp_path):
    cfg = write_config(tmp_path)
    args = argparse.Namespace(config=str(cfg), now=f"{MONDAY}T15:00:00", verbose=False)
    assert cli.cmd_check(args) == 0


def test_check_rejects_leftover_schedule(tmp_path, capsys):
    """watch.yml 里残留 cron 时应报错：调度已交给 cron-job.org，两套定时器会打架。"""
    cfg = write_config(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / "watch.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text('on:\n  schedule:\n    - cron: "*/5 0-5 * * *"\n', encoding="utf-8")
    args = argparse.Namespace(config=str(cfg), now=f"{MONDAY}T15:00:00", verbose=False)

    assert cli.cmd_check(args) == 1
    assert "cron-job.org" in capsys.readouterr().out


def test_check_accepts_dispatch_only_workflow(tmp_path, capsys):
    """只有 workflow_dispatch 的 watch.yml 才是正确状态。"""
    cfg = write_config(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / "watch.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on:\n  workflow_dispatch:\n", encoding="utf-8")
    args = argparse.Namespace(config=str(cfg), now=f"{MONDAY}T15:00:00", verbose=False)

    assert cli.cmd_check(args) == 0
    out = capsys.readouterr().out
    assert "cron-job.org" in out
    # 建议计划用北京时间（cron-job.org 可设 Asia/Shanghai），14:00-22:00 → 14-21
    assert "*/5 14-21 * * *" in out, "应给出 cron-job.org 的建议计划"


def test_check_reports_bad_yaml_instead_of_traceback(tmp_path, capsys):
    """复现线上那次事故：列表项缩进多一层。"""
    bad = tmp_path / "config.yaml"
    bad.write_text(
        "schedule:\n"
        "  - weekday: 5\n"
        "    time_windows: ['19:00-22:00']\n"
        "    - weekday: 6\n"
        "    time_windows: ['14:00-22:00']\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(config=str(bad), now=f"{MONDAY}T15:00:00", verbose=False)
    assert cli.cmd_check(args) == 1
    assert "不是合法的 YAML" in capsys.readouterr().out


def test_check_rejects_schedule_with_nonstandard_formatting(tmp_path):
    cfg = write_config(tmp_path)
    workflow = tmp_path / ".github/workflows/watch.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text('on: {workflow_dispatch: null, schedule: [{cron: "*/5 * * * *"}]}', encoding="utf-8")
    assert cli.cmd_check(argparse.Namespace(config=str(cfg), now=None)) == 1


def test_now_with_offset_is_normalized_to_beijing():
    moment = cli._resolve_now("2026-11-08T18:00:00Z")
    assert moment.date() == date(2026, 11, 9)
    assert moment.hour == 2


def test_check_flags_empty_schedule(tmp_path):
    cfg = write_config(tmp_path, schedule=[])
    args = argparse.Namespace(config=str(cfg), now=f"{MONDAY}T15:00:00", verbose=False)
    assert cli.cmd_check(args) == 1


# ------------------------------------------------------------------ 消息渲染
def make_hit(day, hour, name="7号场", rid="156#7号场", minutes=60):
    start = time(hour, 0)
    end = time((hour + minutes // 60) % 24, 0)
    slot_obj = Slot(
        date=day, start=start, end=end, resource_id=rid, resource_name=name, remaining=1, status=1
    )
    return Hit(date=day, start=start, end=end, resource_id=rid, resource_name=name, slots=(slot_obj,))


def test_render_merges_courts_of_same_span():
    day = date(2026, 11, 5)
    hits = [make_hit(day, 18, name="1号场", rid="156#1"), make_hit(day, 18, name="2号场", rid="156#2")]
    title, body = cli.render_message(hits, 54, cli.now(), None)

    assert "1 个时段有空场" in title
    assert "18:00-19:00 · 2 块（1号场、2号场）" in body
    assert "场馆 54" in body


def test_render_sorts_courts_numerically():
    day = date(2026, 11, 5)
    hits = [
        make_hit(day, 18, name="10号场", rid="156#10"),
        make_hit(day, 18, name="2号场", rid="156#2"),
    ]
    _, body = cli.render_message(hits, 54, cli.now(), None)
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
