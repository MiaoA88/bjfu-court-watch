"""跨运行去重状态。

GitHub Actions 的 runner 是一次性的，状态必须外置。这里用一个小 JSON 文件，
由 workflow 的 actions/cache 负责跨运行传递（见 .github/workflows/watch.yml）。

状态丢失时仍可发现当前空场，但可能重复通知。
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
    """保存成功通知的格子、通知标记和当天扫描计数。"""

    notified: dict[str, str] = field(default_factory=dict)
    auth_alert_sent: bool = False
    schedule_fingerprint: str | None = None
    started_on: str | None = None
    scan_on: str | None = None
    scan_count: int = 0
    schema_version: int = SCHEMA_VERSION

    def mark_notified(self, key: str, when: datetime) -> None:
        self.notified[key] = when.isoformat(timespec="seconds")

    def prune(self, today: date) -> int:
        """清理过去日期及无效键。"""
        removed = 0
        for key in list(self.notified):
            try:
                target = date.fromisoformat(key.split("|")[1])
            except (IndexError, ValueError):
                target = date.min
            if target < today:
                self.notified.pop(key)
                removed += 1
        return removed

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
        notified = {str(k): str(v) for k, v in notified.items()} if isinstance(notified, dict) else {}
        # 旧版占用/待发送格子不能继续视为已通知；只需一次简单迁移。
        for field_name in ("occupied", "pending"):
            legacy = raw.get(field_name)
            if isinstance(legacy, dict):
                for key in legacy:
                    notified.pop(key, None)
        return cls(
            notified=notified,
            auth_alert_sent=raw.get("auth_alert_sent", bool(raw.get("auth_alert_on"))) is True,
            schedule_fingerprint=raw.get("schedule_fingerprint") or None,
            started_on=raw.get("started_on") or None,
            scan_on=raw.get("scan_on") or None,
            scan_count=raw.get("scan_count", 0) if type(raw.get("scan_count", 0)) is int else 0,
        )

    def save(self, path: str | Path) -> None:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "notified": self.notified,
            "auth_alert_sent": self.auth_alert_sent,
            "schedule_fingerprint": self.schedule_fingerprint,
            "started_on": self.started_on,
            "scan_on": self.scan_on,
            "scan_count": self.scan_count,
        }
        tmp = file.with_suffix(file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(file)
