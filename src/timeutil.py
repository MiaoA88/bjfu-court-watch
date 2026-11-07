"""时间与查询窗口相关的纯函数。

全程使用 Asia/Shanghai：GitHub runner 的本地时区是 UTC，
任何"今天/明天"的判断都必须显式换算，不能依赖系统时区。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# 兜底默认值，config.yaml 的 active_hours 会覆盖它
DEFAULT_ACTIVE_HOURS = ("10:00", "23:00")


def now() -> datetime:
    """当前北京时间。"""
    return datetime.now(TZ)


def parse_hhmm(value: str) -> time:
    """把 "09:00" 解析为 time；容忍前后空格与单个数字的小时。"""
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，收到 {value!r}")
    return time(int(parts[0]), int(parts[1]))


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(str(value).strip())


def active_window(active_hours=DEFAULT_ACTIVE_HOURS) -> tuple[time, time]:
    start, end = active_hours
    return parse_hhmm(start), parse_hhmm(end)


def in_active_window(moment: datetime, active_hours=DEFAULT_ACTIVE_HOURS) -> bool:
    """左闭右开：09:00 开工，20:00 准时收工。

    支持跨零点窗口（如 22:00-06:00），虽然本项目用不到。
    """
    start, end = active_window(active_hours)
    local = moment.astimezone(TZ).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def resolve_dates(
    days_ahead=(),
    dates=(),
    today: date | None = None,
) -> list[date]:
    """合并"相对今天往后 N 天"与"绝对日期"，去重升序。"""
    base = today or now().date()
    picked: set[date] = set()
    for offset in days_ahead:
        picked.add(base + timedelta(days=int(offset)))
    for value in dates:
        picked.add(parse_iso_date(value))
    return sorted(picked)


def weekday_cn(value: date) -> str:
    return "一二三四五六日"[value.weekday()]
