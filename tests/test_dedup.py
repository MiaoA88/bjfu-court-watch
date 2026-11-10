"""去重语义：只在"占用 → 空闲"或"首次发现"时通知。

用户提的两个关键场景：
  A. 先报 21:00-22:00 @6号场，下一轮 20:00-21:00 也空了 → 应该通知
  B. 先报 21:00-22:00 @6号场，下一轮 21:00-22:00 @5号场 也空了 → 应该通知
  C. 已报的格子被抢走、之后又释放 → 应该再通知
  D. 一直空着没被抢的格子 → 不应每轮重复通知
  E. 连续段边界漂移（20:00-22:00 变成只剩 21:00）→ 不应误报
"""

import argparse
from datetime import date, time

import pytest
import yaml

from src import cli
from src.matcher import Slot

DAY = date(2026, 11, 2)  # 周一


class Recorder:
    delivers = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, url=None):
        self.sent.append((title, body))

    @property
    def hit_titles(self):
        return [t for t, _ in self.sent if "空场" in t]


def write_config(tmp_path):
    cfg = {
        "venue": {"hall_id": 54, "resource_ids": [156]},
        "schedule": [{"weekday": 1, "time_windows": ["14:00-22:00"]}],
        "active_hours": ["14:00", "22:00"],
        "notify": {"provider": "wxpusher_spt", "announce_start": False,
                   "announce_schedule_changes": False},
        "state": {"path": str(tmp_path / "state.json")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def cell(hour: int, name: str = "6号塑胶场地", status: int = 1, day=DAY) -> Slot:
    return Slot(
        date=day,
        start=time(hour, 0),
        end=time(hour + 1, 0),
        resource_id=f"156#{name}",
        resource_name=name,
        remaining=1 if status == 1 else 0,
        status=status,
    )


class Harness:
    """同一个 state 文件上反复跑 cmd_watch，模拟连续多轮检测。"""

    def __init__(self, tmp_path, monkeypatch):
        self.cfg = write_config(tmp_path)
        self.recorder = Recorder()
        self.slots: list[Slot] = []
        monkeypatch.setattr(cli, "build_client", lambda cfg, **kwargs: object())
        monkeypatch.setattr(cli, "fetch_slots", lambda *a, **k: list(self.slots))
        monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: self.recorder)
        self.monkeypatch = monkeypatch

    def run(self, slots: list[Slot], now: str = "2026-11-02T15:00:00") -> int:
        self.slots = slots
        args = argparse.Namespace(
            config=str(self.cfg), now=now, dry_run=False, sleep_between=0, verbose=False
        )
        before = len(self.recorder.hit_titles)
        rc = cli.cmd_watch(args)
        return rc, len(self.recorder.hit_titles) - before


def test_scenario_a_new_earlier_slot_is_notified(tmp_path, monkeypatch):
    """已报 21:00@6号，下一轮 20:00@6号 变空 → 要通知。

    标题只计新增的 20:00 格子，正文同时列出新增与原有的空场。
    """
    h = Harness(tmp_path, monkeypatch)

    h.run([cell(21)])
    assert len(h.recorder.hit_titles) == 1, "首次应通知 21:00"
    assert "21:00-22:00" in h.recorder.sent[-1][1]

    # 再加一个 20:00 的格子（21:00 仍空着）
    _, new = h.run([cell(20), cell(21)])
    assert new == 1, "新释放的 20:00 应该再通知一次"
    assert "20:00-21:00" in h.recorder.sent[-1][1]
    assert "21:00-22:00" in h.recorder.sent[-1][1]  # 旧空场也列入当前全部空场


def test_scenario_b_another_court_same_span_is_notified(tmp_path, monkeypatch):
    """已报 21:00@6号，下一轮 21:00@5号 也空 → 要通知。

    两段互不相连，所以新消息只包含 5 号那块。
    """
    h = Harness(tmp_path, monkeypatch)

    h.run([cell(21, "6号塑胶场地")])
    assert len(h.recorder.hit_titles) == 1

    _, new = h.run([cell(21, "6号塑胶场地"), cell(21, "5号塑胶场地")])
    assert new == 1, "同段另一块场地变空，应通知"
    body = h.recorder.sent[-1][1]
    assert "5号塑胶场地" in body


def test_scenario_c_released_again_is_notified(tmp_path, monkeypatch):
    """格子被抢走、之后又释放 → 应再次通知（捡漏两次算两次）。"""
    h = Harness(tmp_path, monkeypatch)

    h.run([cell(21)])
    assert len(h.recorder.hit_titles) == 1

    # 被抢：status=3 已约满
    _, new = h.run([cell(21, status=3)])
    assert new == 0, "被占用不该通知"

    # 又释放
    _, new = h.run([cell(21)])
    assert new == 1, "重新释放应再次通知"


def test_scenario_d_steady_free_cell_is_silent(tmp_path, monkeypatch):
    """一直空着且已报过 → 不重复通知。"""
    h = Harness(tmp_path, monkeypatch)

    h.run([cell(21)])
    assert len(h.recorder.hit_titles) == 1

    for _ in range(3):
        _, new = h.run([cell(21)])
        assert new == 0, "常态空闲不应刷屏"
    assert len(h.recorder.hit_titles) == 1


def test_scenario_e_span_boundary_drift_no_false_alarm(tmp_path, monkeypatch):
    """20:00-22:00 连续段因邻段被占缩成 21:00-22:00，不应误报。"""
    h = Harness(tmp_path, monkeypatch)

    h.run([cell(20), cell(21)])
    assert len(h.recorder.hit_titles) == 1

    # 20:00 被抢走，只剩 21:00
    _, new = h.run([cell(20, status=3), cell(21)])
    assert new == 0, "只是区间边界变化，不是新空场"


def test_first_detection_of_multiple_slots_sends_one_message(tmp_path, monkeypatch):
    """同一轮里多个新格子合并成一条消息，不刷屏。"""
    h = Harness(tmp_path, monkeypatch)
    _, new = h.run([cell(20), cell(21, "5号塑胶场地"), cell(20, "4号塑胶场地")])
    assert new == 1


def test_unconstrained_window_still_requires_new_cell(tmp_path, monkeypatch):
    """窗口放宽后，白天格子同样按"新释放"判定。"""
    h = Harness(tmp_path, monkeypatch)
    # 用 16:00 起（15:00 运行时 14:00 已开场会被剔除）
    h.run([cell(16)])
    assert len(h.recorder.hit_titles) == 1
    _, new = h.run([cell(16), cell(17)])
    assert new == 1


@pytest.mark.parametrize("slots,expected", [
    ([cell(19)], "🏸 周一的19:00-20:00有空场"),
    ([cell(19), cell(19, "5号塑胶场地")], "🏸 周一的19:00-20:00等有多个空场"),
    ([cell(20), cell(19)], "🏸 周一的19:00-20:00等有多个空场"),
])
def test_title_counts_independent_cells(tmp_path, monkeypatch, slots, expected):
    h = Harness(tmp_path, monkeypatch)
    assert h.run(slots) == (0, 1)
    assert h.recorder.hit_titles == [expected]


def test_new_cell_uses_full_run_for_minimum_but_only_notifies_new_cell(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    cfg = yaml.safe_load(h.cfg.read_text(encoding="utf-8"))
    cfg["matching"] = {"min_consecutive_slots": 2}
    h.cfg.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert h.run([cell(19), cell(20)])[1] == 1
    assert h.run([cell(19), cell(20), cell(21)])[1] == 1
    assert h.recorder.hit_titles[-1] == "🏸 周一的21:00-22:00有空场"
    assert h.run([cell(19), cell(20), cell(21)])[1] == 0
