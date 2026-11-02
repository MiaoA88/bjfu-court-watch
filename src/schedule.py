"""每周固定盯场表。

模型：把"周几"和"该周几要盯的时段"绑在一起，一条规则一项，长期保留、按周重复。

    schedule:
      - weekday: 1              # 周一
        time_windows: ["20:00-21:00"]
      - weekday: 6              # 周六
        time_windows: ["14:00-16:00"]

运行语义：
  * 只在"今天这个周几"出现在表里时才查询；否则一次请求都不发（不消耗额度）。
  * 查询的就是今天这一天的场地，命中即推送。
  * 表不会被"本周过去了"清空，下周同一天继续生效。

cron 只在这几个周几触发，因此 cron 的星期列表必须与表一致；
`cron_expression()` 负责推导，tests/test_workflow_consistency.py 负责守住两者不漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
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
        spans = "、".join(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in self.windows)
        return f"{WEEKDAY_NAMES[self.weekday]} {spans}"


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
        try:
            weekday = int(weekday)
        except (TypeError, ValueError):
            raise ValueError(f"schedule 第 {index} 项缺少合法的 weekday（1=周一 … 7=周日）") from None
        if not 1 <= weekday <= 7:
            raise ValueError(f"schedule 第 {index} 项 weekday={weekday} 越界，应为 1-7")

        raw_windows = item.get("time_windows", item.get("windows"))
        if not raw_windows:
            raise ValueError(f"schedule 第 {index} 项（{WEEKDAY_NAMES[weekday]}）没有配置 time_windows")
        windows = tuple(parse_windows(raw_windows))
        rules.append(Rule(weekday=weekday, windows=windows))
    return rules


def rules_for_day(rules: Iterable[Rule], day: datetime) -> list[Rule]:
    """取出"今天这个周几"对应的规则；空列表表示今天不盯场。"""
    return [rule for rule in rules if rule.weekday == day.isoweekday()]


def merged_windows(rules: Iterable[Rule]) -> list[tuple[time, time]]:
    """把同一天的多条规则合成一个时段列表（去重后按开始时间排序）。"""
    seen: set[tuple[time, time]] = set()
    merged: list[tuple[time, time]] = []
    for rule in rules:
        for window in rule.windows:
            if window not in seen:
                seen.add(window)
                merged.append(window)
    merged.sort(key=lambda w: (w[0], w[1]))
    return merged


def describe(rules: Iterable[Rule]) -> str:
    """给日志用：把整个表压成一行。"""
    items = [rule.label for rule in rules]
    return "；".join(items) if items else "（空）"


def weekday_label(day: datetime) -> str:
    return f"周{weekday_cn(day.date())}"


# --------------------------------------------------------------------- cron 推导
def cron_dow(rules: Iterable[Rule]) -> str:
    """把规则的周几集合变成 cron 的 day-of-week 字段，如 "1,6"。"""
    days = sorted({rule.weekday for rule in rules})
    if not days:
        return "*"
    # cron 的 0 和 7 都表示周日；这里统一用 7，和配置里的编号一致
    return ",".join(str(d) for d in days)


def cron_expression(
    rules: Iterable[Rule],
    active_hours: tuple[str, str] = ("12:00", "20:00"),
) -> str:
    """推导出与配置一致的 cron（北京时间换算成 UTC）。

    active_hours 是左闭右开，例如 12:00-20:00 覆盖 12..19 点，
    换算成 UTC 就是 4..11 点。
    """
    start, end = active_window(active_hours)
    start_utc = (start.hour - 8) % 24
    last_beijing_hour = (end.hour - 1) % 24
    end_utc = (last_beijing_hour - 8) % 24
    if start_utc <= end_utc:
        hours = f"{start_utc}-{end_utc}"
    else:  # 跨日窗口：cron 无法表达成一段，退化为全天并依赖代码闸门
        hours = "*"
    return f"*/5 {hours} * * {cron_dow(rules)}"
