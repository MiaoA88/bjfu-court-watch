"""跨运行去重状态。

GitHub Actions 的 runner 是一次性的，状态必须外置。这里用一个小 JSON 文件，
由 workflow 的 actions/cache 负责跨运行传递（见 .github/workflows/watch.yml）。

状态丢失时仍可发现当前空场，但可能重复通知；历史健康数据也会丢失。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class State:
    """状态仓库。

    notified: 已推送过的**单个场次格子**（馆|日期|开始|结束|场地）→ 首次推送时间。
        按格子而不是按"合并后的连续区间"记账，因为区间会随邻段变化漂移：
        已报 20:00-22:00 后，20:00 被占只剩 21:00 时，若按区间记账会被当成新命中。
    occupied: 上一次观察时**处于占用/不可约**的格子 → 观察时间。
        "占用→空闲"就是捡漏成功，此时才推送；一直空闲的格子只报一次。
    """

    notified: dict[str, str] = field(default_factory=dict)
    occupied: dict[str, str] = field(default_factory=dict)
    pending: dict[str, str] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    failure_alert_sent: bool = False
    failure_alert_attempt_at: str | None = None
    health_alert_at: str | None = None
    # 连续请求失败次数，用于"失败 6 次才告警"
    consecutive_failures: int = 0
    # 登录态告警的日期（yyyy-mm-dd），用于"每天最多提醒一次"
    auth_alert_on: str | None = None
    # 场馆下的资源列表缓存：hall_id -> [{"id","name"}]
    resources: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    # 盯场表指纹：变了就推一条"当前最新时段"
    schedule_fingerprint: str | None = None
    # 已发过"开始运行"通知的日期（yyyy-mm-dd），每天只发一次
    started_on: str | None = None
    schema_version: int = SCHEMA_VERSION

    # ---------------------------------------------------------------- 去重
    def already_notified(self, key: str) -> bool:
        return key in self.notified

    def mark_notified(self, key: str, when: datetime) -> None:
        self.notified[key] = when.isoformat(timespec="seconds")
        self.pending.pop(key, None)

    def was_occupied(self, key: str) -> bool:
        return key in self.occupied

    def mark_occupied(self, key: str, when: datetime) -> None:
        self.occupied[key] = when.isoformat(timespec="seconds")

    def mark_free(self, key: str) -> None:
        """格子恢复可约：从占用集合里移除，下一次再被占又会重新计入。"""
        self.occupied.pop(key, None)

    def prune(self, today: date) -> int:
        """丢弃目标日期早于今天的键；今天未开场的记录仍保留。

        键格式约定为 hall|date|start|end|resource，第二段是 yyyy-mm-dd。
        occupied 用同一套键格式，一并剪除。
        """
        removed = []
        for store in (self.notified, self.occupied, self.pending):
            for key in list(store):
                parts = key.split("|")
                if len(parts) < 2:
                    removed.append((store, key))
                    continue
                try:
                    target = date.fromisoformat(parts[1])
                except ValueError:
                    removed.append((store, key))
                    continue
                if target < today:
                    removed.append((store, key))
        for store, key in removed:
            store.pop(key, None)
        return len(removed)

    # ---------------------------------------------------------------- 失败计数
    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.failure_alert_sent = False
        self.failure_alert_attempt_at = None

    def record_failure(self) -> int:
        self.consecutive_failures += 1
        return self.consecutive_failures

    # ---------------------------------------------------------------- 持久化
    @classmethod
    def load(cls, path: str | Path) -> "State":
        file = Path(path)
        if not file.exists():
            return cls()
        try:
            raw: Any = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("状态文件 %s 读取失败（%s），按空状态继续", file, exc)
            return cls()
        if not isinstance(raw, dict):
            log.warning("状态文件 %s 结构异常，按空状态继续", file)
            return cls()

        version = raw.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            log.warning("状态文件版本 %s 与当前 %s 不符，按空状态继续", version, SCHEMA_VERSION)
            return cls()

        notified = raw.get("notified") or {}
        occupied = raw.get("occupied") or {}
        resources = raw.get("resources") or {}
        pending = raw.get("pending") or {}
        health = raw.get("health") or {}
        try:
            failures = max(0, int(raw.get("consecutive_failures") or 0))
        except (ValueError, TypeError, OverflowError):
            failures = 0
        return cls(
            notified={str(k): str(v) for k, v in notified.items()} if isinstance(notified, dict) else {},
            occupied={str(k): str(v) for k, v in occupied.items()} if isinstance(occupied, dict) else {},
            pending={str(k): str(v) for k, v in pending.items()} if isinstance(pending, dict) else {},
            health=health if isinstance(health, dict) else {},
            failure_alert_sent=raw.get("failure_alert_sent") is True,
            failure_alert_attempt_at=raw.get("failure_alert_attempt_at"),
            health_alert_at=raw.get("health_alert_at"),
            consecutive_failures=failures,
            auth_alert_on=raw.get("auth_alert_on") or None,
            resources=resources if isinstance(resources, dict) else {},
            schedule_fingerprint=raw.get("schedule_fingerprint") or None,
            started_on=raw.get("started_on") or None,
            schema_version=SCHEMA_VERSION,
        )

    def save(self, path: str | Path) -> None:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "notified": self.notified,
            "occupied": self.occupied,
            "pending": self.pending,
            "health": self.health,
            "failure_alert_sent": self.failure_alert_sent,
            "failure_alert_attempt_at": self.failure_alert_attempt_at,
            "health_alert_at": self.health_alert_at,
            "consecutive_failures": self.consecutive_failures,
            "auth_alert_on": self.auth_alert_on,
            "resources": self.resources,
            "schedule_fingerprint": self.schedule_fingerprint,
            "started_on": self.started_on,
        }
        tmp = file.with_suffix(file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(file)
