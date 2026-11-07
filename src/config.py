"""Load and validate configuration once for every command."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

from .schedule import parse_schedule, runs_per_hour
from .timeutil import DEFAULT_ACTIVE_HOURS, active_window

DEFAULT_CONFIG = "config.yaml"


def _integer(value: Any, field: str, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} 必须是 ≥ {minimum} 的整数")


def validate_config(cfg: dict[str, Any]) -> None:
    for section in ("venue", "matching", "notify", "http", "state"):
        if section in cfg and not isinstance(cfg[section], dict):
            raise ValueError(f"{section} 必须是映射")
    # Commands requiring targets reject an empty schedule themselves.
    parse_schedule(cfg.get("schedule"))
    hours = cfg.get("active_hours", list(DEFAULT_ACTIVE_HOURS))
    if not isinstance(hours, (list, tuple)) or len(hours) != 2:
        raise ValueError("active_hours 必须包含开始、结束两个时间")
    start, end = active_window(hours)
    if start == end or start.minute or end.minute:
        raise ValueError("active_hours 起止必须不同且为整点（支持跨午夜）")
    interval = cfg.get("scan_interval_minutes", 5)
    _integer(interval, "scan_interval_minutes")
    runs_per_hour(interval)
    venue = cfg.get("venue", {})
    for field, default, minimum in (("hall_id", 54, 1), ("page_size", 20, 1), ("available_status", 1, 0)):
        _integer(venue.get(field, default), f"venue.{field}", minimum)
    ids = venue.get("resource_ids", [])
    if not isinstance(ids, list):
        raise ValueError("venue.resource_ids 必须是整数列表")
    for value in ids:
        _integer(value, "venue.resource_ids")
    for field in ("resource_name_include", "resource_name_exclude"):
        values = venue.get(field, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"venue.{field} 必须是非空字符串列表")
    _integer(cfg.get("matching", {}).get("min_consecutive_slots", 1), "matching.min_consecutive_slots")
    http = cfg.get("http", {})
    timeout = http.get("timeout_seconds", 15)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("http.timeout_seconds 必须是有限正数")
    _integer(http.get("retries", 2), "http.retries", 0)
    notify = cfg.get("notify", {})
    provider = notify.get("provider", "wxpusher_spt")
    if not isinstance(provider, str) or provider.strip().lower() not in {"wxpusher_spt", "wxpusher", "spt"}:
        raise ValueError("notify.provider 仅支持 wxpusher_spt / wxpusher / spt")
    for field in ("announce_start", "announce_schedule_changes"):
        if field in notify and not isinstance(notify[field], bool):
            raise ValueError(f"notify.{field} 必须是布尔值")
    url = notify.get("detail_url")
    if url is not None and (not isinstance(url, str) or not url.startswith(("https://", "http://"))):
        raise ValueError("notify.detail_url 必须是 HTTP(S) 地址")
    state_path = cfg.get("state", {}).get("path", ".state/notified.json")
    if not isinstance(state_path, str) or not state_path.strip():
        raise ValueError("state.path 必须是非空路径")


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    file = Path(path)
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"无法读取配置文件 {file}：{exc}") from exc
    except (yaml.YAMLError, UnicodeError) as exc:
        raise SystemExit(f"{file} 不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"配置文件 {file} 顶层必须是映射")
    try:
        validate_config(data)
    except (ValueError, TypeError) as exc:
        raise SystemExit(f"配置有误：{exc}") from exc
    return data
