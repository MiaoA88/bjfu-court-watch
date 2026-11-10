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
    max_items: int | None = None,
    all_hits: list[Hit] | None = None,
    scan_log: str | None = None,
) -> tuple[str, str]:
    """标题只计新增格子；正文列出全部匹配空场并加粗新增场地。"""
    if not hits:
        body = f"目标时段暂无可约场地。\n\n_查询时间 {moment.strftime('%m-%d %H:%M')} · 场馆 {hall_id}_"
        return "🏸 暂无空场", body

    fresh_keys = {slot.key(hall_id) for hit in hits for slot in hit.slots}
    current = {slot.key(hall_id): slot for hit in (all_hits if all_hits is not None else hits) for slot in hit.slots}
    by_day: dict[date, dict[tuple, list]] = {}
    for slot in current.values():
        by_day.setdefault(slot.date, {}).setdefault((slot.start, slot.end), []).append(slot)
    total_spans = sum(len(spans) for spans in by_day.values())
    lines = [f"### 🏸 本次新增 {len(fresh_keys)} 个空场", "",
             f"目前的空场有：{len(by_day)} 天共 {total_spans} 个时段、{len(current)} 个场地时段。",
             "**加粗场地为本轮新增空场**（含此前发送失败的补发）。", ""]
    shown = 0
    for day in sorted(by_day):
        lines.append(f"**{day:%m-%d} 周{weekday_cn(day)}**")
        for (start, end), slots in sorted(by_day[day].items()):
            if max_items is not None and shown >= max_items:
                break
            names = [f"**{slot.resource_name}**" if slot.key(hall_id) in fresh_keys else slot.resource_name
                     for slot in sorted(slots, key=lambda slot: _court_key(slot.resource_name))]
            lines.append(f"- {start:%H:%M}-{end:%H:%M} · {len(names)} 块（{'、'.join(names)}）")
            shown += 1
        lines.append("")
    if total_spans > shown:
        lines.extend([f"…另有 {total_spans - shown} 个时段未列出", ""])
    if scan_log:
        lines.extend(["---", "**扫描记录**", scan_log, ""])
    lines.append(f"_查询时间 {moment:%m-%d %H:%M} · 场馆 {hall_id}_")
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
