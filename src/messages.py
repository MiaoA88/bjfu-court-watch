"""Render notifications without network or persistence side effects."""
from datetime import date, datetime
from typing import Any
from .matcher import Hit
from .timeutil import weekday_cn

def render_message(
    hits: list[Hit],
    hall_id: Any,
    moment: datetime,
    detail_url: str | None,
    *,
    max_items: int = 30,
) -> tuple[str, str]:
    """按「日期 → 时段」聚合，用场地数代替逐条罗列，避免一条消息几十行。"""
    if not hits:
        body = f"目标时段暂无可约场地。\n\n_查询时间 {moment.strftime('%m-%d %H:%M')} · 场馆 {hall_id}_"
        return "🏸 暂无空场", body

    by_day: dict[date, dict[tuple, list[str]]] = {}
    for hit in hits:
        by_day.setdefault(hit.date, {}).setdefault((hit.start, hit.end), []).append(hit.resource_name)

    total_spans = sum(len(spans) for spans in by_day.values())
    lines = [f"### 🏸 {len(by_day)} 天共 {total_spans} 个时段有空场", ""]

    shown = 0
    for day in sorted(by_day):
        lines.append(f"**{day.strftime('%m-%d')} 周{weekday_cn(day)}**")
        for (start, end), names in sorted(by_day[day].items()):
            if shown >= max_items:
                break
            ordered = sorted(names, key=_court_key)
            preview = "、".join(ordered[:4]) + ("…" if len(ordered) > 4 else "")
            lines.append(f"- {start.strftime('%H:%M')}-{end.strftime('%H:%M')} · {len(ordered)} 块（{preview}）")
            shown += 1
        lines.append("")

    if total_spans > shown:
        lines.append(f"…另有 {total_spans - shown} 个时段未列出")
        lines.append("")

    lines.append(f"_查询时间 {moment.strftime('%m-%d %H:%M')} · 场馆 {hall_id}_")
    body = "\n".join(lines).strip()

    # 标题直接给出"最近哪个时段能打"，比"从哪天起有几个时段"更好扫读
    first_day = min(by_day)
    first_start, first_end = min(by_day[first_day])
    title = (
        f"🏸 周{weekday_cn(first_day)}起"
        f"{first_start.strftime('%H:%M')}-{first_end.strftime('%H:%M')}有空场"
    )
    return title[:100], body


def _court_key(name: str) -> tuple[int, str]:
    """让 1号、2号、10号 按数字排，而不是字典序。"""
    digits = ""
    for char in name:
        if char.isdigit():
            digits += char
        else:
            break
    return (int(digits) if digits else 9999, name)


class CycleLog:
    """收集一轮运行的关键数字，作为推送附录，便于事后定位"为什么没报"。"""

    def __init__(self, moment: datetime) -> None:
        self.started = moment
        self.days: list[dict[str, object]] = []
        self.notes: list[str] = []

    def add_day(self, target, fetched: int, kept: int, hits: int) -> None:
        self.days.append(
            {"label": target.label, "fetched": fetched, "kept": kept, "hits": hits}
        )

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def total_fetched(self) -> int:
        return sum(int(d["fetched"]) for d in self.days)

    @property
    def total_hits(self) -> int:
        return sum(int(d["hits"]) for d in self.days)

    def appendix(self) -> str:
        """极简表格：每天取到多少格、留下多少、命中多少。"""
        lines = [
            "",
            "---",
            f"**运行日志** {self.started.strftime('%m-%d %H:%M')}",
        ]
        for day in self.days:
            lines.append(
                f"- {day['label']}：取到 {day['fetched']} 格 → 保留 {day['kept']} → 命中 {day['hits']}"
            )
        for note in self.notes:
            lines.append(f"- {note}")
        return "\n".join(lines)
