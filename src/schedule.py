"""每周固定盯场表。

模型：把"周几"和"该周几要盯的时段"绑在一起，一条规则一项，长期保留、按周重复。

    schedule:
      - weekday: 1              # 周一
        time_windows: ["20:00-21:00"]
      - weekday: 6              # 周六
        time_windows: ["14:00-22:00"]     # 一个窗口跨多个场次也可以

运行语义（只查本周还没过去的那些周几）：
  * 当前产品策略只查本周，不假设学校未来一定按同一放场规则运行。
  * 因此每条规则映射到"本周的那一天"（周一起算），且必须不早于今天；
  * 周一运行会一次查到本周所有配置的周几，周中只查剩下的，周日之后自然空转；
  * 表不会被"本周过去了"清空，下周一自动重新生效。

外部 cron 每天触发，可生成北京时间或 UTC 的小时范围；
`cron_expression()` 负责推导，tests/test_workflow_consistency.py 负责守漂移。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from .matcher import parse_windows
from .timeutil import active_window, weekday_cn

WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


@dataclass(frozen=True)
class Rule:
    """一条规则：某个周几 + 要盯的若干时段。"""

    weekday: int
    windows: tuple[tuple[time, time], ...]

    @property
    def label(self) -> str:
        return f"{WEEKDAY_NAMES[self.weekday]} {format_windows(self.windows)}"


@dataclass(frozen=True)
class Target:
    """一次实际查询：某一天 + 该天要看的时段。"""

    date: date
    windows: tuple[tuple[time, time], ...]

    @property
    def label(self) -> str:
        return f"{self.date.strftime('%m-%d')} 周{weekday_cn(self.date)} {format_windows(self.windows)}"


def format_windows(windows: Iterable[tuple[time, time]]) -> str:
    return "、".join(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in windows)


def _dedupe_sorted(windows: Iterable[tuple[time, time]]) -> tuple[tuple[time, time], ...]:
    unique = sorted(set(windows), key=lambda w: (w[0], w[1]))
    return tuple(unique)


def parse_schedule(raw: Any) -> list[Rule]:
    """解析 config 里的 schedule 段；任何不合法的写法都直接报错，不静默忽略。"""
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise ValueError("schedule 必须是列表，形如 [{weekday: 1, time_windows: ['20:00-21:00']}]")

    rules: list[Rule] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"schedule 第 {index} 项必须是映射，收到 {type(item).__name__}")
        weekday = item.get("weekday", item.get("day"))
        if isinstance(weekday, bool) or not isinstance(weekday, (str, int)):
            raise ValueError(f"schedule 第 {index} 项 weekday 必须是整数")
        try:
            weekday = int(weekday)
        except (TypeError, ValueError):
            raise ValueError(f"schedule 第 {index} 项缺少合法的 weekday（1=周一 … 7=周日）") from None
        if not 1 <= weekday <= 7:
            raise ValueError(f"schedule 第 {index} 项 weekday={weekday} 越界，应为 1-7")

        raw_windows = item.get("time_windows", item.get("windows"))
        if not raw_windows:
            raise ValueError(f"schedule 第 {index} 项（{WEEKDAY_NAMES[weekday]}）没有配置 time_windows")
        windows = _dedupe_sorted(parse_windows(raw_windows))
        rules.append(Rule(weekday=weekday, windows=windows))
    return rules


def plan_targets(rules: Iterable[Rule], moment: datetime) -> list[Target]:
    """把盯场表映射成本次真正要查的日期。

    按本项目的本周查询策略，在本周（周一起算）里取：
    该周几对应本周的那一天，且不早于今天；本周已经过去的周几直接跳过，
    因为那天的场次已经开场、也约不上了。
    """
    from .timeutil import TZ
    today = moment.astimezone(TZ).date()
    week_start = today - timedelta(days=today.isoweekday() - 1)
    merged: dict[date, list[tuple[time, time]]] = {}
    for rule in rules:
        day = week_start + timedelta(days=rule.weekday - 1)
        if day < today:
            continue
        merged.setdefault(day, []).extend(rule.windows)
    return [Target(date=day, windows=_dedupe_sorted(windows)) for day, windows in sorted(merged.items())]


def describes_targets(targets: Iterable[Target]) -> str:
    return "；".join(target.label for target in targets) if targets else "（空）"


def describe(rules: Iterable[Rule]) -> str:
    """给日志/推送用：把整个表压成一行。"""
    items = [rule.label for rule in rules]
    return "；".join(items) if items else "（空）"


def fingerprint(rules: Iterable[Rule]) -> str:
    """盯场表指纹，用于"表变了就推送一条最新时段"。"""
    payload = [
        [rule.weekday, [[s.strftime("%H:%M"), e.strftime("%H:%M")] for s, e in rule.windows]]
        for rule in sorted(rules, key=lambda r: r.weekday)
    ]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


# --------------------------------------------------------------------- cron 推导
#
# 计费口径（重要）：GitHub 按 **job** 向上取整到整分钟，所以我们每次 14–33 秒的运行
# 都记 1 分钟。额度约束因此是"每月运行次数 ≤ 2000"，与单次跑多久基本无关。
# 下面几个函数就是为了在改窗口/间隔前先把这笔账算清楚。

FREE_MINUTES_PER_MONTH = 2000


def runs_per_hour(interval: int) -> int:
    """cron 的 */N 在每小时实际触发几次（N 不整除 60 时不是 60/N）。"""
    if interval < 1:
        raise ValueError(f"间隔必须 ≥ 1 分钟，收到 {interval}")
    if interval > 59:
        raise ValueError(f"间隔最大 59 分钟，收到 {interval}")
    return len(range(0, 60, interval))


def window_hours(active_hours: tuple[str, str] = ("09:00", "22:00")) -> int:
    start, end = active_window(active_hours)
    if end > start:
        return end.hour - start.hour
    return (24 - start.hour) + end.hour


def daily_runs(active_hours: tuple[str, str] = ("09:00", "22:00"), interval: int = 15) -> int:
    return runs_per_hour(interval) * window_hours(active_hours)


def monthly_minutes(
    active_hours: tuple[str, str] = ("09:00", "22:00"),
    interval: int = 15,
    *,
    days: int = 31,
    extra_runs: int = 0,
) -> int:
    """按 job 取整口径估算每月消耗的分钟数。days 默认取最长的 31 天。"""
    return daily_runs(active_hours, interval) * days + extra_runs


def cron_expression(
    active_hours: tuple[str, str] = ("10:00", "23:00"),
    interval: int = 5,
    *,
    utc: bool = True,
) -> str:
    """Generate an exact hour window, including windows crossing midnight."""
    start, end = active_window(active_hours)
    if start == end or start.minute or end.minute:
        raise ValueError("cron 扫描窗口必须为不同的整点")
    runs_per_hour(interval)
    offset = 8 if utc else 0
    hours = sorted((start.hour + i - offset) % 24 for i in range(window_hours(active_hours)))
    spans = []
    first = last = hours[0]
    for hour in hours[1:]:
        if hour == last + 1:
            last = hour
        else:
            spans.append(str(first) if first == last else f"{first}-{last}")
            first = last = hour
    spans.append(str(first) if first == last else f"{first}-{last}")
    step = "*" if interval == 1 else f"*/{interval}"
    return f"{step} {','.join(spans)} * * *"


def remaining_runs(moment: datetime, active_hours, interval: int) -> int:
    """今日当前分钟之后的计划触发数，按 cron */N 每小时重新计数。"""
    from .timeutil import TZ, in_active_window
    local = moment.astimezone(TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return sum(
        in_active_window(midnight + timedelta(minutes=minute), active_hours)
        and (minute % 60) % interval == 0
        for minute in range(local.hour * 60 + local.minute + 1, 24 * 60)
    )
