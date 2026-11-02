"""命令行入口：watch（正式监控）/ probe（接口定型）/ smoke（连通性验证）。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .availability import FieldMap, diagnose, fetch_resources, fetch_slots, probe
from .ehall import (
    BASE_URL,
    ApiError,
    AuthExpired,
    EhallClient,
    EhallError,
    SchemaChanged,
    TransportError,
)
from .matcher import Hit, parse_windows, select
from .notify import ConsoleNotifier, NotifyError, build_notifier
from .state import State
from .timeutil import DEFAULT_ACTIVE_HOURS, in_active_window, now, resolve_dates, weekday_cn

log = logging.getLogger("court")

DEFAULT_CONFIG = "config.yaml"
FAILURE_ALERT_THRESHOLD = 6  # 连续 6 次失败（约 30 分钟）才打扰人


# --------------------------------------------------------------------- 配置
def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        raise SystemExit(f"找不到配置文件 {file}")
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"配置文件 {file} 顶层必须是映射")
    return data


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def build_client(cfg: dict[str, Any], *, require_cookie: bool = True) -> EhallClient | None:
    cookie = _env("EHALL_COOKIE")
    if not cookie:
        if require_cookie:
            raise SystemExit("缺少环境变量 EHALL_COOKIE（浏览器 Cookie）")
        return None
    http_cfg = cfg.get("http") or {}
    return EhallClient(
        cookie,
        user_agent=_env("EHALL_UA"),
        timeout=float(http_cfg.get("timeout_seconds", 15)),
        retries=int(http_cfg.get("retries", 2)),
    )


def _state_path(cfg: dict[str, Any]) -> Path:
    return Path((cfg.get("state") or {}).get("path", ".state/notified.json"))


# --------------------------------------------------------------------- watch
def cmd_watch(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    moment = _resolve_now(args.now)
    active_hours = cfg.get("active_hours") or list(DEFAULT_ACTIVE_HOURS)

    if not in_active_window(moment, active_hours):
        log.info(
            "北京时间 %s 不在查询窗口 %s-%s（系统关闭时段），本次不查询、不推送",
            moment.strftime("%Y-%m-%d %H:%M"),
            active_hours[0],
            active_hours[1],
        )
        return 0

    venue = cfg.get("venue") or {}
    hall_id = venue.get("hall_id", 54)
    targets = cfg.get("targets") or {}
    matching = cfg.get("matching") or {}
    notify_cfg = cfg.get("notify") or {}
    fields = FieldMap.from_config(cfg.get("fields"))

    days = resolve_dates(targets.get("days_ahead") or [], targets.get("dates") or [], moment.date())
    windows = parse_windows(targets.get("time_windows") or [])
    if not windows:
        raise SystemExit("targets.time_windows 为空，不知道该盯哪些时段")
    if not days:
        raise SystemExit("targets.days_ahead 与 targets.dates 都为空，不知道该盯哪些日期")

    state_path = _state_path(cfg)
    state = State.load(state_path)
    state.prune(moment.date())

    client = build_client(cfg)
    notifier = build_notifier(
        notify_cfg.get("provider", "wxpusher_spt"),
        _env("WXPUSHER_SPT"),
        dry_run=args.dry_run,
    )

    # 资源列表只用于日志与兜底，失败不致命
    if not state.resources.get(str(hall_id)):
        resources = fetch_resources(client, hall_id)
        if resources:
            state.resources[str(hall_id)] = resources
            log.info("场馆 %s 下发现 %d 个资源：%s", hall_id, len(resources),
                     "、".join(r["name"] for r in resources[:8]))

    endpoint = venue.get("margin_endpoint", "/site/reservation/resource-info-margin")
    method = venue.get("margin_method", "GET")

    try:
        slots = []
        for day in days:
            slots.extend(
                fetch_slots(
                    client,
                    endpoint=endpoint,
                    method=method,
                    hall_id=hall_id,
                    day=day,
                    fields=fields,
                    extra_params=venue.get("extra_params") or {},
                    param_names=venue.get("param_names") or {},
                )
            )
            if args.sleep_between:
                _sleep(args.sleep_between)
    except AuthExpired as exc:
        return _handle_error(state, state_path, notifier, moment, exc, kind="auth")
    except (ApiError, SchemaChanged, TransportError) as exc:
        return _handle_error(state, state_path, notifier, moment, exc, kind="other")

    log.info("共取到 %d 个时段，目标日期 %s", len(slots), ", ".join(d.isoformat() for d in days))

    hits = select(
        slots,
        target_dates=days,
        windows=windows,
        min_consecutive_slots=int(matching.get("min_consecutive_slots", 1)),
        include=venue.get("resource_name_include") or [],
        exclude=venue.get("resource_name_exclude") or [],
    )
    fresh = [h for h in hits if not state.already_notified(h.key(hall_id))]

    log.info("命中 %d 处，其中未推送过 %d 处", len(hits), len(fresh))

    if not fresh:
        state.record_success()
        if not args.dry_run:
            state.save(state_path)
        return 0

    title, body = render_message(fresh, hall_id, moment, notify_cfg.get("detail_url"))
    try:
        notifier.send(title, body, notify_cfg.get("detail_url"))
    except NotifyError as exc:
        log.error("推送失败：%s", exc)
        state.record_failure()
        if not args.dry_run:
            state.save(state_path)
        return 1

    state.record_success()
    if not args.dry_run:
        for hit in fresh:
            state.mark_notified(hit.key(hall_id), moment)
        state.save(state_path)
    return 0


def _handle_error(
    state: State,
    state_path: Path,
    notifier,
    moment: datetime,
    exc: Exception,
    *,
    kind: str,
) -> int:
    log.error("%s：%s", type(exc).__name__, exc)

    if kind == "auth":
        today = moment.date().isoformat()
        if state.auth_alert_on != today:
            try:
                notifier.send(
                    "⚠️ ehall 登录态失效",
                    "预约系统 Cookie 已过期，监控已暂停。\n\n"
                    "**恢复步骤**\n"
                    "1. 浏览器打开预约页并登录\n"
                    "2. F12 → Network → 任一 `site/reservation` 请求 → 复制 Cookie\n"
                    "3. 更新 GitHub Secret `EHALL_COOKIE`（UA 变了同时更新 `EHALL_UA`）\n\n"
                    f"失效时间：{moment.strftime('%Y-%m-%d %H:%M')}",
                    None,
                )
                state.auth_alert_on = today
            except NotifyError as notify_exc:
                log.error("登录态告警推送失败：%s", notify_exc)
    else:
        count = state.record_failure()
        if count == FAILURE_ALERT_THRESHOLD:
            try:
                notifier.send(
                    "⚠️ 场地监控连续失败",
                    f"已连续 {count} 次请求失败（约 {count * 5} 分钟），最后一次错误：\n\n"
                    f"`{type(exc).__name__}: {exc}`\n\n"
                    "可能是接口改版、校园网抖动或系统维护。",
                    None,
                )
            except NotifyError as notify_exc:
                log.error("失败告警推送失败：%s", notify_exc)

    if not isinstance(notifier, ConsoleNotifier):
        state.save(state_path)
    return 0


# --------------------------------------------------------------------- 渲染
def render_message(
    hits: list[Hit],
    hall_id: Any,
    moment: datetime,
    detail_url: str | None,
    *,
    max_lines: int = 40,
) -> tuple[str, str]:
    grouped: dict[str, list[Hit]] = {}
    for hit in hits:
        grouped.setdefault(hit.date.isoformat(), []).append(hit)

    lines = [f"### 🏸 发现 {len(hits)} 处空场", ""]
    shown = 0
    for day_key in sorted(grouped):
        day = grouped[day_key][0].date
        lines.append(f"**{day.strftime('%m-%d')} 周{weekday_cn(day)}**")
        for hit in grouped[day_key]:
            if shown >= max_lines:
                break
            lines.append(f"- {hit.label} · {hit.resource_name}（余 {hit.min_remaining}）")
            shown += 1
        lines.append("")
    if len(hits) > shown:
        lines.append(f"…另有 {len(hits) - shown} 处未列出")
        lines.append("")

    lines.append(f"_查询时间 {moment.strftime('%m-%d %H:%M')} · 场馆 {hall_id}_")
    body = "\n".join(lines).strip()

    first = hits[0]
    title = f"🏸 {first.date.strftime('%m-%d')} {first.label} 有空场"
    if len(hits) > 1:
        title = f"🏸 {len(hits)} 处空场（{first.date.strftime('%m-%d')} {first.label} 起）"
    return title[:100], body


# --------------------------------------------------------------------- probe
def cmd_probe(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    hall_id = args.hall or (cfg.get("venue") or {}).get("hall_id", 54)
    client = build_client(cfg, require_cookie=not args.anonymous)

    if client is None:
        print("！未提供 EHALL_COOKIE，仅做未登录探测（验证网络与 WAF）\n")
    else:
        print(f"已装载 Cookie（长度 {len(_env('EHALL_COOKIE') or '')}），开始探测端点\n")

    day = datetime.fromisoformat(args.date).date() if args.date else None
    summary = probe(client, hall_id, out_dir=args.out, day=day, sleep_between=args.sleep)

    ok = [r for r in summary if not r.get("error") and r.get("slots")]
    print(f"\n共探测 {len(summary)} 次，其中 {len(ok)} 次抽到了时段。")
    if ok:
        best = max(ok, key=lambda r: r["slots"])
        print(
            "建议写入 config.yaml：\n"
            f"  venue.margin_endpoint: {best['path']}\n"
            f"  venue.margin_method: {best['method']}\n"
            f"  # 抽到 {best['slots']} 个时段，参数 {json.dumps(best['params'], ensure_ascii=False)}"
        )
    return 0


# --------------------------------------------------------------------- smoke
def cmd_smoke(args: argparse.Namespace) -> int:
    """只验证连通性：不读配置、不推送，打印信封的 e 值。"""
    import requests

    cookie = _env("EHALL_COOKIE")
    headers = {"User-Agent": _env("EHALL_UA") or "curl/8"}
    if cookie:
        headers["Cookie"] = cookie

    url = f"{BASE_URL}/site/reservation/hall"
    print(f"GET {url}")
    print(f"Cookie: {'已提供（长度 %d）' % len(cookie) if cookie else '未提供（匿名）'}")
    try:
        response = requests.get(url, headers=headers, timeout=20, allow_redirects=False)
    except requests.RequestException as exc:
        print(f"\n结果：网络层失败 → {exc}")
        print("判定：runner 到 ehall 不通（可能被 WAF 拦或需要校内网络）。")
        return 1

    print(f"HTTP {response.status_code}  content-type={response.headers.get('Content-Type')}")
    head = (response.text or "")[:300].replace("\n", " ")
    print(f"响应片段：{head!r}\n")

    if response.status_code in (301, 302, 303, 307, 308):
        print("判定：被重定向到登录页 → 网络可达，Cookie 无效或已过期。")
        return 0
    try:
        payload = json.loads(response.text)
        code = str(payload.get("e"))
    except (json.JSONDecodeError, AttributeError):
        print("判定：返回的不是 JSON 信封 → 大概率被 WAF 拦截或需要校内网络。")
        return 1

    if code == "10042":
        print("判定：网络可达，仅缺登录态（e=10042）。方案可行，继续配 Cookie。")
        return 0
    if code in {"0", "1000"}:
        print("判定：Cookie 有效，接口直接返回数据。")
        return 0
    print(f"判定：返回了业务码 {code}，需要人工看一眼响应体。")
    return 0


# --------------------------------------------------------------------- 工具
def _resolve_now(value: str | None) -> datetime:
    if not value:
        return now()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        from .timeutil import TZ

        parsed = parsed.replace(tzinfo=TZ)
    return parsed


def _sleep(seconds: float) -> None:
    import time as _t

    _t.sleep(seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="court", description="北林羽毛球场捡漏监控")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    watch = sub.add_parser("watch", help="查询并推送（正式运行）")
    watch.add_argument("--dry-run", action="store_true", help="只打印不推送、不写状态")
    watch.add_argument("--now", help="伪造当前时间（ISO8601，测试用）")
    watch.add_argument("--sleep-between", type=float, default=1.0, help="多日期查询之间的间隔秒数")
    watch.set_defaults(func=cmd_watch)

    probe_cmd = sub.add_parser("probe", help="探测候选接口，定型字段映射")
    probe_cmd.add_argument("--hall", help="场馆 id，默认取 config 里的 hall_id")
    probe_cmd.add_argument("--date", help="探测用日期 YYYY-MM-DD")
    probe_cmd.add_argument("--out", default="probe-out", help="响应落盘目录")
    probe_cmd.add_argument("--sleep", type=float, default=0.6, help="每次探测之间的间隔")
    probe_cmd.add_argument("--anonymous", action="store_true", help="不带 Cookie，只验证网络")
    probe_cmd.set_defaults(func=cmd_probe)

    smoke = sub.add_parser("smoke", help="连通性验证（GitHub Actions 首次部署用）")
    smoke.set_defaults(func=cmd_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as exc:  # 兜底：把栈打全，便于在 Actions 日志里定位
        log.exception("未预期的异常：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
