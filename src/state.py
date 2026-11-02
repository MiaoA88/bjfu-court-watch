"""跨运行去重状态。

GitHub Actions 的 runner 是一次性的，状态必须外置。这里用一个小 JSON 文件，
由 workflow 的 actions/cache 负责跨运行传递（见 .github/workflows/watch.yml）。

状态丢失（缓存被驱逐）的后果只是"同一条空场再推一次"，不会漏推，因此可以接受。
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
    """notified: 已经推送过的空场键 → 推送时间（ISO8601）。"""

    notified: dict[str, str] = field(default_factory=dict)
    # 连续请求失败次数，用于"失败 6 次才告警"
    consecutive_failures: int = 0
    # 登录态告警的日期（yyyy-mm-dd），用于"每天最多提醒一次"
    auth_alert_on: str | None = None
    # 场馆下的资源列表缓存：hall_id -> [{"id","name"}]
    resources: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # ---------------------------------------------------------------- 去重
    def already_notified(self, key: str) -> bool:
        return key in self.notified

    def mark_notified(self, key: str, when: datetime) -> None:
        self.notified[key] = when.isoformat(timespec="seconds")

    def prune(self, today: date) -> int:
        """丢弃目标日期已过（含今天）的键：过了就永远不会再命中。

        键格式约定为 hall|date|start|end|resource，第二段是 yyyy-mm-dd。
        """
        removed = []
        for key in list(self.notified):
            parts = key.split("|")
            if len(parts) < 2:
                removed.append(key)
                continue
            try:
                target = date.fromisoformat(parts[1])
            except ValueError:
                removed.append(key)
                continue
            if target < today:
                removed.append(key)
        for key in removed:
            self.notified.pop(key, None)
        return len(removed)

    # ---------------------------------------------------------------- 失败计数
    def record_success(self) -> None:
        self.consecutive_failures = 0

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
        resources = raw.get("resources") or {}
        return cls(
            notified={str(k): str(v) for k, v in notified.items()} if isinstance(notified, dict) else {},
            consecutive_failures=int(raw.get("consecutive_failures") or 0),
            auth_alert_on=raw.get("auth_alert_on") or None,
            resources=resources if isinstance(resources, dict) else {},
            schema_version=SCHEMA_VERSION,
        )

    def save(self, path: str | Path) -> None:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "notified": self.notified,
            "consecutive_failures": self.consecutive_failures,
            "auth_alert_on": self.auth_alert_on,
            "resources": self.resources,
        }
        tmp = file.with_suffix(file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(file)
