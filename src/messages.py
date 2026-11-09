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

    cells = {slot.key(hall_id): slot for hit in hits for slot in hit.slots}
    first = min(cells.values(), key=lambda slot: (slot.date, slot.start, slot.resource_name))
    suffix = "等有多个空场" if len(cells) > 1 else "有空场"
    title = (
        f"🏸 周{weekday_cn(first.date)}的"
        f"{first.start:%H:%M}-{first.end:%H:%M}{suffix}"
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
