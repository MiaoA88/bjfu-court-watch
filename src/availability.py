"""余量接口适配层。

学校系统的字段命名无法在未登录时确定，所以这里不做"猜死"的硬编码，而是：

  1. 递归遍历响应 JSON，找到"同时带时间字段和余量字段"的字典节点；
  2. 字段名从 config.yaml 的 fields 里按优先级取第一个存在的键；
  3. 日期可以从祖先节点继承（很多实现把 date 放在外层，slot 里只有时分）。

probe 子命令会把候选端点的真实响应落盘，据此把 fields 调准即可，无需改代码。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from .ehall import EhallClient, EhallError
from .matcher import Slot
from .timeutil import TZ, now

log = logging.getLogger(__name__)

# 现场探测出来的端点清单；顺序即优先级
CANDIDATE_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("resource_info_margin", "GET", "/site/reservation/resource-info-margin"),
    ("any_resource_info_margin", "GET", "/site/reservation/any-resource-info-margin"),
    ("batch_info_margin", "GET", "/site/reservation/batch-info-margin"),
    ("resource_info", "GET", "/site/reservation/resource-info"),
    ("any_resource_info_time", "GET", "/site/reservation/any-resource-info-time"),
    ("hall", "GET", "/site/reservation/hall"),
)

_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})")


@dataclass
class FieldMap:
    """字段候选名。命中顺序即优先级。"""

    date: list[str] = field(
        default_factory=lambda: ["date", "reserveDate", "day", "useDate", "bizDate", "timeDate"]
    )
    start: list[str] = field(
        default_factory=lambda: [
            "startTime", "beginTime", "start", "startStr", "timeStart", "begin", "startDate", "fromTime",
        ]
    )
    end: list[str] = field(
        default_factory=lambda: ["endTime", "finish", "end", "endStr", "timeEnd", "toTime"]
    )
    remaining: list[str] = field(
        default_factory=lambda: [
            "surplus", "remain", "remaining", "leftNum", "left", "available", "stock", "canReserve", "isFree",
        ]
    )
    capacity: list[str] = field(default_factory=lambda: ["capacity", "total", "totalNum", "maxNum"])
    resource_name: list[str] = field(
        default_factory=lambda: ["resourceName", "name", "hallName", "resourceTitle", "title", "placeName"]
    )
    # 注意：故意不含 hallId —— 场馆 id 不是场地 id，混用会让所有场地塌缩成一个。
    resource_id: list[str] = field(
        default_factory=lambda: ["resourceId", "resourceCode", "id", "code"]
    )

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "FieldMap":
        base = cls()
        if not raw:
            return base
        for key, value in raw.items():
            if key in base.__dataclass_fields__ and isinstance(value, list) and value:
                setattr(base, key, [str(v) for v in value])
        return base


# --------------------------------------------------------------------- 取值工具
def _first(node: dict[str, Any], keys: Iterable[str]) -> tuple[str | None, Any]:
    for key in keys:
        if key in node and node[key] not in (None, ""):
            return key, node[key]
    return None, None


def parse_clock(value: Any) -> time | None:
    """把各种时间表示统一成 time；解析不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = int(value)
        if number > 10**11:  # 毫秒时间戳
            return datetime.fromtimestamp(number / 1000, TZ).time().replace(microsecond=0)
        if number > 10**9:  # 秒时间戳
            return datetime.fromtimestamp(number, TZ).time().replace(microsecond=0)
        if 0 <= number <= 2359:
            return _from_hhmm(str(number))
        return None

    text = str(value).strip()
    if not text:
        return None
    match = _TIME_RE.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
        return None
    if text.isdigit() and len(text) in (3, 4):
        return _from_hhmm(text)
    if text.isdigit() and len(text) >= 10:  # 字符串时间戳
        return parse_clock(int(text))
    return None


def _from_hhmm(text: str) -> time | None:
    padded = text.zfill(4) if len(text) <= 4 else text
    hour, minute = int(padded[:-2]), int(padded[-2:])
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return time(hour, minute)
    return None


def parse_day(value: Any) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        if number > 10**11:
            return datetime.fromtimestamp(number / 1000, TZ).date()
        if number > 10**9:
            return datetime.fromtimestamp(number, TZ).date()
        if len(str(number)) == 8:  # 20261105
            return _from_ymd(str(number))
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        return _from_ymd(text)
    match = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if text.isdigit() and len(text) >= 10:
        return parse_day(int(text))
    return None


def _from_ymd(text: str) -> date | None:
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def parse_remaining(value: Any) -> int | None:
    """余量：数字、布尔、'有/无'、'剩余2人' 都要能认。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text in {"有", "可约", "空闲", "true", "True", "TRUE", "yes"}:
        return 1
    if text in {"无", "约满", "已满", "false", "False", "FALSE", "no"}:
        return 0
    digits = re.search(r"-?\d+", text)
    return int(digits.group()) if digits else None


# --------------------------------------------------------------------- 抽取
def _walk(
    node: Any,
    inherited: dict[str, Any],
    fields: FieldMap,
    depth: int = 0,
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """深度优先遍历，把祖先里出现过的日期键下传给子节点。"""
    if depth > 8:
        return
    if isinstance(node, dict):
        context = dict(inherited)
        for key in fields.date:
            if key in node:
                parsed = parse_day(node[key])
                if parsed:
                    context["__date__"] = parsed
                    break
        # 场地信息常在"资源节点"上，时段节点只带时分，因此也要往下继承
        for key in fields.resource_id:
            if key in node and node[key] not in (None, ""):
                context["__rid__"] = str(node[key])
                break
        for key in fields.resource_name:
            if key in node and node[key] not in (None, ""):
                context["__rname__"] = str(node[key])
                break
        yield node, context
        for value in node.values():
            yield from _walk(value, context, fields, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, inherited, fields, depth + 1)


def extract_slots(
    payload: Any,
    fields: FieldMap | None = None,
    *,
    default_day: date | None = None,
) -> list[Slot]:
    """从任意响应结构里抽出 Slot 列表。找不到就返回空列表。"""
    fm = fields or FieldMap()
    slots: list[Slot] = []
    seen: set[tuple] = set()

    for node, context in _walk(payload, {}, fm):
        start_key, raw_start = _first(node, fm.start)
        if start_key is None:
            continue
        remain_key, raw_remaining = _first(node, fm.remaining)
        if remain_key is None:
            continue

        start = parse_clock(raw_start)
        if start is None:
            continue

        end_key, raw_end = _first(node, fm.end)
        end = parse_clock(raw_end) if end_key else None
        if end is None or end <= start:
            # 没有结束时间时按下一条时段推不出来，先跳过，交给 diagnose 报告
            continue

        day = context.get("__date__")
        if day is None:
            _, raw_day = _first(node, fm.date)
            day = parse_day(raw_day) if raw_day is not None else None
        day = day or default_day
        if day is None:
            continue

        remaining = parse_remaining(raw_remaining)
        if remaining is None:
            continue

        _, name = _first(node, fm.resource_name)
        _, rid = _first(node, fm.resource_id)
        _, cap = _first(node, fm.capacity)
        if rid is None:
            rid = context.get("__rid__")
        if name is None:
            name = context.get("__rname__")

        if rid is not None:
            resource_id = str(rid)
        elif name is not None:
            resource_id = str(name)
        else:
            resource_id = "unknown"
        resource_name = str(name) if name is not None else resource_id

        signature = (day, start, end, resource_id)
        if signature in seen:
            continue
        seen.add(signature)

        slots.append(
            Slot(
                date=day,
                start=start,
                end=end,
                resource_id=resource_id,
                resource_name=resource_name,
                remaining=remaining,
                capacity=int(cap) if isinstance(cap, (int, float)) else None,
            )
        )
    return slots


def diagnose(payload: Any, fields: FieldMap | None = None) -> dict[str, Any]:
    """probe 用：报告"看起来像时段"的节点及其键名，便于调 fields。"""
    fm = fields or FieldMap()
    candidates: list[dict[str, Any]] = []
    total_nodes = 0
    for node, context in _walk(payload, {}, fm):
        total_nodes += 1
        if not isinstance(node, dict):
            continue
        keys = set(node)
        has_time = bool(keys & set(fm.start)) or any(parse_clock(v) for v in node.values())
        has_count = bool(keys & set(fm.remaining))
        if has_time and (has_count or keys & set(fm.end)):
            candidates.append({"keys": sorted(keys), "inherited_date": str(context.get("__date__"))})
        if len(candidates) >= 8:
            break
    return {"node_count": total_nodes, "slot_like": candidates}


# --------------------------------------------------------------------- 抓取
def fetch_resources(client: EhallClient, hall_id: str | int) -> list[dict[str, str]]:
    """取场馆下的资源（场地）列表；解析不出来就返回空，让上层走兜底。"""
    try:
        payload = client.data("GET", "/site/reservation/hall", params={"hallId": hall_id})
    except EhallError as exc:
        log.warning("取场馆资源失败：%s", exc)
        return []

    fm = FieldMap()
    found: list[dict[str, str]] = []
    for node, _ in _walk(payload, {}, fm):
        _, rid = _first(node, fm.resource_id)
        _, name = _first(node, fm.resource_name)
        if rid is not None and name is not None:
            item = {"id": str(rid), "name": str(name)}
            if item not in found:
                found.append(item)
    return found


def fetch_slots(
    client: EhallClient,
    *,
    endpoint: str,
    method: str = "GET",
    hall_id: str | int,
    day: date,
    fields: FieldMap | None = None,
    extra_params: dict[str, Any] | None = None,
    param_names: dict[str, str] | None = None,
) -> list[Slot]:
    """按配置调用余量接口，抽成 Slot 列表。"""
    names = {
        "hall": "hallId",
        "date": "date",
        "resource": "resourceId",
        **(param_names or {}),
    }
    params: dict[str, Any] = {
        names["hall"]: hall_id,
        names["date"]: day.isoformat(),
        **(extra_params or {}),
    }

    if method.upper() == "GET":
        payload = client.data("GET", endpoint, params=params)
    else:
        payload = client.data("POST", endpoint, json_body=params)

    slots = extract_slots(payload, fields, default_day=day)
    log.info("%s %s %s → %d 个时段", method, endpoint, day, len(slots))
    return slots


# --------------------------------------------------------------------- probe
def probe(
    client: EhallClient | None,
    hall_id: str | int,
    *,
    out_dir: str | Path = "probe-out",
    day: date | None = None,
    sleep_between: float = 0.6,
    sleep=None,
) -> list[dict[str, Any]]:
    """遍历候选端点 × 参数组合，把结果落盘并返回摘要表。

    client 为 None 时只做未登录探测（验证网络与 WAF）。
    """
    pause = sleep or _time_sleep
    target_day = day or now().date()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    param_sets: list[dict[str, Any]] = [
        {"hallId": hall_id},
        {"id": hall_id},
        {"resourceId": hall_id},
    ]
    dated_sets: list[dict[str, Any]] = []
    for base in param_sets:
        dated_sets.append({**base, "date": target_day.isoformat()})

    summary: list[dict[str, Any]] = []
    for name, method, path in CANDIDATE_ENDPOINTS:
        for index, params in enumerate(param_sets + dated_sets):
            record: dict[str, Any] = {"name": name, "method": method, "path": path, "params": params}
            try:
                if client is None:
                    import requests

                    response = requests.get(
                        "https://ehall.bjfu.edu.cn" + path,
                        params=params,
                        timeout=15,
                        allow_redirects=False,
                    )
                    record["http"] = response.status_code
                    body = response.text
                else:
                    envelope = client.request(method, path, params=params)
                    record["http"] = 200
                    record["code"] = envelope.code
                    record["message"] = envelope.message
                    body = json.dumps(envelope.data, ensure_ascii=False)
                    record["slots"] = len(extract_slots(envelope.data, default_day=target_day))
                    file = out / f"{name}_{index}.json"
                    file.write_text(
                        json.dumps(envelope.data, ensure_ascii=False, indent=1), encoding="utf-8"
                    )
                    record["saved"] = str(file)

                record["bytes"] = len(body or "")
                record["head"] = (body or "")[:160].replace("\n", " ")
                if client is None:
                    record["code"] = _peek_code(body)
            except EhallError as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # 探测工具：任何异常都只记录，不中断
                record["error"] = f"{type(exc).__name__}: {exc}"

            summary.append(record)
            print(_format_record(record))
            if sleep_between:
                pause(sleep_between)

    return summary


def _peek_code(body: str) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return str(payload.get("e")) if isinstance(payload, dict) and "e" in payload else None


def _format_record(record: dict[str, Any]) -> str:
    head = f"[{record['name']:<26}] {record['method']} {record['path']} {record['params']}"
    if "error" in record:
        return f"{head}\n    ! {record['error']}"
    detail = f"    http={record.get('http')} code={record.get('code')} bytes={record.get('bytes')}"
    if record.get("slots") is not None:
        detail += f" slots={record['slots']}"
    if record.get("message"):
        detail += f" msg={record['message']!r}"
    if record.get("saved"):
        detail += f"\n    saved → {record['saved']}"
    return f"{head}\n{detail}\n    head: {record.get('head')!r}"


def _time_sleep(seconds: float) -> None:
    import time as _time_module

    _time_module.sleep(seconds)
