"""余量接口适配层。

实测结论（2026-11-02，hallView?id=54）：

  端点：GET /site/reservation/list-page
  参数：hall_id=54 & time=YYYY-MM-DD & p=1 & page_size=20   （注意是 snake_case）
  响应：{"e":0,"d":{"total":2,"list":[
            {"id":156,"name":"羽毛球综合场馆（收费）",
             "table":{"595":[ {cell}, ... ]},          // 场地 × 时段 的网格
             ...}]}}

  单个 cell：
    {"abscissa":"1号塑胶场地",        // 场地
     "yaxis":"18:00-19:00",          // 时段
     "row":{"status":1,"total":1,"margin":1,"price":0},
     "date":"2026-11-03"}

**为什么不能只看 margin**：status=5 表示"未来日期尚未放场"，此时 margin 也返回 1。
只判 margin>0 会把未来整月都当成空场。可用性必须看 status==1。

  status=1 → 可预约（margin 必为 1）
  status=3 → 已约满
  status=4 → 不可预约（当日已过 / 该日停用）
  status=5 → 未开放
  status=0 → 已过期（当天已过去的时段）
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from .ehall import EhallClient, EhallError, SchemaChanged
from .matcher import Slot
from .timeutil import TZ, now

log = logging.getLogger(__name__)

LIST_PAGE = "/site/reservation/list-page"

# 可用性判定：只有 status==1 才是真的能约
AVAILABLE_STATUS = 1
STATUS_LABELS = {
    0: "已过期",
    1: "可预约",
    2: "未知2",
    3: "已约满",
    4: "不可预约",
    5: "未开放",
}

_SLOT_SPAN_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2})")
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})")


# --------------------------------------------------------------------- 时间解析
def parse_clock(value: Any) -> time | None:
    """把各种时间表示统一成 time；解析不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
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
    if text.isdigit() and len(text) >= 10:
        return parse_clock(int(text))
    return None


def parse_span(value: Any) -> tuple[time, time] | None:
    """把 "18:00-19:00" 解析成 (start, end)。"""
    if value is None:
        return None
    match = _SLOT_SPAN_RE.search(str(value))
    if not match:
        return None
    sh, sm, eh, em = (int(match.group(i)) for i in range(1, 5))
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return None
    if (eh, em) <= (sh, sm):
        return None
    return time(sh, sm), time(eh, em)


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
        if len(str(number)) == 8:
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
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    digits = re.search(r"-?\d+", text)
    return int(digits.group()) if digits else None


# --------------------------------------------------------------------- 专用解析
def _integer_field(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SchemaChanged(f"{label} 必须是整数")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value.strip()):
        raise SchemaChanged(f"{label} 必须是非负整数")
    number = int(value)
    if number < 0:
        raise SchemaChanged(f"{label} 不能为负数")
    return number


def _page_items(payload: Any) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
        raise SchemaChanged("list-page 缺少合法的 list 数组")
    items = payload["list"]
    if "total" in payload:
        total = _integer_field(payload["total"], "total")
        if total != len(items):
            raise SchemaChanged("list-page 数据不完整，请检查分页或增大 venue.page_size")
    if any(not isinstance(item, dict) for item in items):
        raise SchemaChanged("list-page 的资源必须是对象")
    return items


def parse_list_page(
    payload: Any,
    day: date,
    *,
    available_status: int = AVAILABLE_STATUS,
) -> list[Slot]:
    """Validate the complete page before accepting any availability observation."""
    slots: list[Slot] = []
    seen: set[str] = set()
    for item in _page_items(payload):
        resource_pk = _integer_field(item.get("id"), "resource.id")
        table = item.get("table")
        if not isinstance(table, dict):
            raise SchemaChanged(f"资源 {resource_pk} 缺少合法 table 网格")
        for rows in table.values():
            if not isinstance(rows, list):
                raise SchemaChanged("table 每一列必须是数组")
            for cell in rows:
                if not isinstance(cell, dict):
                    raise SchemaChanged("场次必须是对象")
                span = parse_span(cell.get("yaxis"))
                row = cell.get("row")
                court = cell.get("abscissa")
                if span is None or not isinstance(row, dict):
                    raise SchemaChanged("场次缺少合法 yaxis 或 row")
                if not isinstance(court, str) or not court.strip():
                    raise SchemaChanged("场次缺少场地名称 abscissa")
                margin = _integer_field(row.get("margin"), "row.margin")
                status = _integer_field(row.get("status"), "row.status")
                try:
                    cell_day = parse_day(cell.get("date"))
                except (ValueError, OverflowError, OSError) as exc:
                    raise SchemaChanged("场次 date 无效") from exc
                if cell_day is None:
                    raise SchemaChanged("场次缺少合法 date")
                slot = Slot(date=cell_day, start=span[0], end=span[1],
                            resource_id=f"{resource_pk}#{court.strip()}",
                            resource_name=court.strip(), remaining=margin,
                            capacity=_integer_field(row["total"], "row.total") if "total" in row else None,
                            status=status, available_status=available_status)
                key = slot.key("")
                if key in seen:
                    raise SchemaChanged("接口返回重复的场次格子")
                seen.add(key)
                slots.append(slot)
    return slots


def summarize_table(payload: Any) -> dict[str, Any]:
    """给日志/自检用：每个资源的场地数、时段数、状态分布。"""
    report: dict[str, Any] = {}
    items = _page_items(payload)
    for item in items:
        if not isinstance(item, dict):
            continue
        cells = [c for rows in (item.get("table") or {}).values() if isinstance(rows, list) for c in rows]
        spans = {c.get("yaxis") for c in cells if isinstance(c, dict)}
        courts = {c.get("abscissa") for c in cells if isinstance(c, dict)}
        statuses: dict[int, int] = {}
        for cell in cells:
            status = parse_remaining((cell.get("row") or {}).get("status"))
            if status is not None:
                statuses[status] = statuses.get(status, 0) + 1
        report[str(item.get("name"))] = {
            "courts": len(courts),
            "spans": len(spans),
            "cells": len(cells),
            "status": {STATUS_LABELS.get(k, k): v for k, v in sorted(statuses.items())},
        }
    return report


# --------------------------------------------------------------------- 抓取
def fetch_slots(
    client: EhallClient,
    *,
    hall_id: str | int,
    day: date,
    page_size: int = 20,
    available_status: int = AVAILABLE_STATUS,
) -> list[Slot]:
    payload = client.data(
        "GET", LIST_PAGE, params={"hall_id": hall_id, "time": day.isoformat(), "p": 1, "page_size": page_size}
    )
    slots = parse_list_page(payload, day, available_status=available_status)
    if any(slot.date != day for slot in slots):
        raise SchemaChanged("接口返回的场次日期与请求日期不符")
    report = summarize_table(payload)
    log.info("%s 取到 %d 格；%s", day, len(slots), json.dumps(report, ensure_ascii=False))
    return slots


# --------------------------------------------------------------------- 自检
def probe(
    client: EhallClient | None,
    hall_id: str | int,
    *,
    out_dir: str | Path = "probe-out",
    days: int = 8,
    sleep_between: float = 0.6,
    sleep=None,
) -> list[dict[str, Any]]:
    """逐日拉 list-page，报告可约/各状态数量，并把首日原始响应落盘。

    用途有二：首次定型；接口改版后用 `python -m src.cli probe` 复查。
    """
    pause = sleep or _time_sleep
    base_day = now().date()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for offset in range(days):
        day = date.fromordinal(base_day.toordinal() + offset)
        record: dict[str, Any] = {"date": day.isoformat(), "offset": offset}
        try:
            if client is None:
                import requests

                response = requests.get(
                    f"https://ehall.bjfu.edu.cn{LIST_PAGE}",
                    params={"hall_id": hall_id, "time": day.isoformat(), "p": 1, "page_size": 20},
                    timeout=20,
                    allow_redirects=False,
                )
                record["http"] = response.status_code
                record["head"] = (response.text or "")[:120]
                results.append(record)
                continue

            payload = client.data(
                "GET", LIST_PAGE, params={"hall_id": hall_id, "time": day.isoformat(), "p": 1, "page_size": 20}
            )
            slots = parse_list_page(payload, day)
            record["cells"] = len(slots)
            record["available"] = sum(1 for s in slots if s.available)
            record["status"] = summarize_table(payload)
            if offset == 0:
                file = out / f"list-page-{day.isoformat()}.json"
                file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                record["saved"] = str(file)
        except EhallError as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # 探测工具：不中断
            record["error"] = f"{type(exc).__name__}: {exc}"

        results.append(record)
        print(_format_record(record))
        if sleep_between:
            pause(sleep_between)
    return results


def _format_record(record: dict[str, Any]) -> str:
    head = f"{record['date']}  (+{record['offset']}d)"
    if "error" in record:
        return f"{head}  ! {record['error']}"
    if "http" in record:
        return f"{head}  http={record['http']} {record.get('head', '')!r}"
    line = f"{head}  格子={record.get('cells', 0):<4} 可约={record.get('available', 0):<4}"
    for name, info in (record.get("status") or {}).items():
        line += f"\n    {name}: 场地{info['courts']}×时段{info['spans']}={info['cells']} 格 {info['status']}"
    if record.get("saved"):
        line += f"\n    saved → {record['saved']}"
    return line


def _time_sleep(seconds: float) -> None:
    import time as _t

    _t.sleep(seconds)
