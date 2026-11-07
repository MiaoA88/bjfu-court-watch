"""Persist scan outcomes and render concise operational summaries."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from .notify import NotifyError
from .state import State
from .timeutil import TZ, in_active_window

log = logging.getLogger(__name__)
ALERT_COOLDOWN = timedelta(minutes=15)
STALE_AFTER = timedelta(minutes=30)


def parse_timestamp(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def retry_due(previous, moment: datetime) -> bool:
    stamp = parse_timestamp(previous)
    return stamp is None or moment - stamp >= ALERT_COOLDOWN


def report(state: State) -> str:
    h = state.health
    return (f"查询状态：{h.get('status', 'unknown')}；最近尝试：{h.get('last_attempt_at', '无记录')}；"
            f"最近成功：{h.get('last_success_at', '无记录')}；"
            f"日期数：{h.get('days', 0)}；格子数：{h.get('fetched', 0)}；"
            f"命中：{h.get('hits', 0)}；通知：{h.get('notification', 'unknown')}；"
            f"待通知格子：{len(state.pending)}；连续查询失败：{state.consecutive_failures}")


def write_summary(state: State) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write("\n" + report(state) + "\n")
        except OSError as exc:
            log.warning("写健康摘要失败：%s", exc)


def check_stale(state: State, moment: datetime, active_hours, notifier) -> bool:
    """Warn on the next run; a stopped scheduler needs an independent observer."""
    baseline = parse_timestamp(state.health.get("last_success_at"))
    baseline = baseline or parse_timestamp(state.health.get("first_attempt_at"))
    if baseline is None or moment - baseline < STALE_AFTER:
        return False
    # Require at least 30 active minutes; normal overnight pauses do not alert.
    active_minutes = sum(
        in_active_window(moment - timedelta(minutes=i), active_hours)
        for i in range(30)
    )
    if active_minutes != 30 or not retry_due(state.health_alert_at, moment):
        return False
    if not getattr(notifier, "delivers", False):
        return False
    state.health_alert_at = moment.isoformat(timespec="seconds")
    try:
        notifier.send("⚠️ 场地监控长时间没有成功扫描",
                      f"最近成功扫描：{state.health.get('last_success_at', '尚无记录')}。\n"
                      "扫描窗口内至少 30 分钟没有成功记录，请检查调度、Cookie 和运行日志。", None)
        return True
    except NotifyError as exc:
        log.error("健康告警推送失败：%s", exc)
        return False
