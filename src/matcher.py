"""把原始余量归一化成 Slot，再按目标日期/时段筛出可推送的命中。

这一层完全纯函数、不碰网络，便于用 fixture 做回归测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .timeutil import parse_hhmm


@dataclass(frozen=True)
class Slot:
    """一个"某场地某日某时段"的可预约单元。

    status 是学校的原始状态码：只有 status==available_status（默认 1）才是真的能约。
    仅看 remaining 会把 status=5「未开放」的格子误判成空场——那时 margin 也返回 1。
    """

    date: date
    start: time
    end: time
    resource_id: str
    resource_name: str
    remaining: int
    capacity: int | None = None
    status: int | None = None
    available_status: int = 1

    @property
    def available(self) -> bool:
        if self.remaining <= 0:
            return False
        if self.status is None:  # 无状态码的来源（如测试/其他接口）退化为只看余量
            return True
        return self.status == self.available_status

    @property
    def minutes(self) -> int:
        return _minutes_between(self.start, self.end)

    def overlaps(self, window: tuple[time, time]) -> bool:
        w_start, w_end = window
        return self.start < w_end and self.end > w_start

    def key(self, hall_id: str | int) -> str:
        """单个场次格子的稳定身份：馆|日期|开始|结束|场地。

        按格子记账，而不是按"合并后的连续区间"——后者会随邻段增减而漂移，
        导致同一批格子被反复当成新命中。
        """
        return "|".join(
            [
                str(hall_id),
                self.date.isoformat(),
                self.start.strftime("%H:%M"),
                self.end.strftime("%H:%M"),
                self.resource_id,
            ]
        )


@dataclass(frozen=True)
class Hit:
    """连续若干个 Slot 组成的一次命中。"""

    date: date
    start: time
    end: time
    resource_id: str
    resource_name: str
    slots: tuple[Slot, ...]

    @property
    def min_remaining(self) -> int:
        return min(s.remaining for s in self.slots)

    @property
    def label(self) -> str:
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


def cell_keys(hits: list[Hit], hall_id: str | int) -> list[str]:
    """把命中展开成"格子键"清单（一段连续区间可能包含多个 1 小时格子）。"""
    keys: list[str] = []
    for hit in hits:
        for slot in hit.slots:
            keys.append(slot.key(hall_id))
    return keys


def _minutes_between(start: time, end: time) -> int:
    anchor = date(2000, 1, 1)
    return int((_dt(anchor, end) - _dt(anchor, start)).total_seconds() // 60)


def _dt(day: date, clock: time):
    from datetime import datetime

    return datetime.combine(day, clock)


def parse_windows(values) -> list[tuple[time, time]]:
    """接受多种写法：单个字符串、字符串列表、[start, end] 二元组。

    一个窗口可以跨任意多个场次（"14:00-22:00" 会命中其中的每一个 1 小时场次）。
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    windows: list[tuple[time, time]] = []
    for raw in values:
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            windows.append((parse_hhmm(raw[0]), parse_hhmm(raw[1])))
            continue
        text = str(raw).strip().replace("–", "-").replace("—", "-").replace("~", "-")
        if "-" not in text:
            raise ValueError(f"时间窗口格式应为 HH:MM-HH:MM，收到 {raw!r}")
        left, right = text.split("-", 1)
        start, end = parse_hhmm(left), parse_hhmm(right)
        if start >= end:
            raise ValueError(f"时间窗口起点必须早于终点：{raw!r}")
        windows.append((start, end))
    return windows


def drop_started(slots: list[Slot], moment: datetime) -> list[Slot]:
    """剔除已经开始的时段。

    今天的 19:00-20:00 场在 19:20 已经无法预约，但学校接口仍可能返回 status=1。
    必须在合并连续区间之前过滤，否则会连带丢掉后面还能约的整段。
    """
    kept: list[Slot] = []
    for slot in slots:
        start = datetime.combine(slot.date, slot.start, moment.tzinfo)
        if start > moment:
            kept.append(slot)
    return kept


def select(
    slots: list[Slot],
    *,
    target_dates,
    windows: list[tuple[time, time]],
    min_consecutive_slots: int = 1,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Hit]:
    """筛选可用时段并合并成连续区间（用于展示）。

    去重不在这里做——见 cli 里基于格子键的 _pick_fresh。

    规则：
      1. 日期在目标集合内、剩场 > 0；
      2. 与任一目标窗口有交集；
      3. 场地名命中 include（若配置）且不命中 exclude；
      4. 同日期同场地内，首尾相接的 Slot 视为连续；连续段数 ≥ min_consecutive_slots 才算命中。
    """
    wanted = {d if isinstance(d, date) else date.fromisoformat(str(d)) for d in target_dates}
    includes = [s.strip() for s in (include or []) if str(s).strip()]
    excludes = [s.strip() for s in (exclude or []) if str(s).strip()]
    minimum = max(1, int(min_consecutive_slots))

    buckets: dict[tuple[date, str], list[Slot]] = {}
    for slot in slots:
        if slot.date not in wanted or not slot.available:
            continue
        if not any(slot.overlaps(w) for w in windows):
            continue
        if includes and not any(token in slot.resource_name for token in includes):
            continue
        if excludes and any(token in slot.resource_name for token in excludes):
            continue
        buckets.setdefault((slot.date, slot.resource_id), []).append(slot)

    hits: list[Hit] = []
    for (day, resource_id), group in buckets.items():
        ordered = sorted(group, key=lambda s: (s.start, s.end))
        run: list[Slot] = [ordered[0]]
        for nxt in ordered[1:]:
            prev = run[-1]
            if _contiguous(prev, nxt):
                run.append(nxt)
            else:
                _emit(hits, run, minimum, day, resource_id)
                run = [nxt]
        _emit(hits, run, minimum, day, resource_id)

    hits.sort(key=lambda h: (h.date, h.start, h.resource_name))
    return hits


def _contiguous(prev: Slot, nxt: Slot) -> bool:
    """上一段结束即下一段开始（容忍 0 分钟以外的相等判断）。"""
    return prev.end == nxt.start


def _emit(hits: list[Hit], run: list[Slot], minimum: int, day: date, resource_id: str) -> None:
    if len(run) < minimum:
        return
    hits.append(
        Hit(
            date=day,
            start=run[0].start,
            end=run[-1].end,
            resource_id=resource_id,
            resource_name=run[0].resource_name,
            slots=tuple(run),
        )
    )
