"""Failure alerts with delivery acknowledgement and retry cooldown."""
import logging
from datetime import datetime
from pathlib import Path

from .health import retry_due, write_summary
from .notify import ConsoleNotifier, NotifyError
from .state import State

log = logging.getLogger(__name__)
FAILURE_ALERT_THRESHOLD = 6


def handle_error(state: State, state_path: Path, notifier, moment: datetime,
                 exc: Exception, *, kind: str) -> int:
    log.error("%s：%s", type(exc).__name__, exc)
    state.health.update(status=f"{kind}_error", last_attempt_at=moment.isoformat(timespec="seconds"),
                        last_error=f"{type(exc).__name__}: {exc}", notification="not_attempted")
    if kind in {"auth", "config"}:
        if state.auth_alert_on != moment.date().isoformat():
            title = "⚠️ ehall 登录态失效" if kind == "auth" else "⚠️ 监控缺少配置"
            body = ("监控无法查询，请在浏览器重新登录预约页，复制 Cookie，更新 EHALL_COOKIE；"
                    "若浏览器变化，同时更新 EHALL_UA。\n\n"
                    f"检测时间：{moment:%Y-%m-%d %H:%M}")
            try:
                notifier.send(title, body, None)
                if getattr(notifier, "delivers", False):
                    state.auth_alert_on = moment.date().isoformat()
                    state.health["notification"] = "alert_sent"
            except NotifyError as notify_exc:
                state.health["notification"] = "alert_failed"
                log.error("告警推送失败：%s", notify_exc)
    else:
        count = state.record_failure()
        if count >= FAILURE_ALERT_THRESHOLD and not state.failure_alert_sent and retry_due(state.failure_alert_attempt_at, moment):
            if getattr(notifier, "delivers", False):
                state.failure_alert_attempt_at = moment.isoformat(timespec="seconds")
            try:
                notifier.send("⚠️ 场地监控连续失败",
                              f"已连续 {count} 次查询失败。最后一次错误：\n\n{type(exc).__name__}: {exc}", None)
                if getattr(notifier, "delivers", False):
                    state.failure_alert_sent = True
                    state.health["notification"] = "alert_sent"
            except NotifyError as notify_exc:
                state.health["notification"] = "alert_failed"
                log.error("失败告警推送失败：%s", notify_exc)
    if not isinstance(notifier, ConsoleNotifier):
        state.save(state_path)
        write_summary(state)
    # Recoverable upstream errors are recorded, not turned into mail on every run.
    return 0
