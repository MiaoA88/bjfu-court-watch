"""命令行入口：watch（正式监控）/ probe（接口定型）/ smoke（连通性验证）。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .availability import AVAILABLE_STATUS, fetch_resources, fetch_slots, probe
from .ehall import (
    BASE_URL,
    ApiError,
    AuthExpired,
    EhallClient,
    EhallError,
    SchemaChanged,
    TransportError,
)
from .matcher import Hit, Slot, drop_started, select
from .notify import ConsoleNotifier, NotifyError, build_notifier
from .schedule import (
    FREE_MINUTES_PER_MONTH,
    cron_expression,
    daily_runs,
    describe,
    describes_targets,
    fingerprint,
    monthly_minutes,
    parse_schedule,
    plan_targets,
)
from .state import State
from .timeutil import DEFAULT_ACTIVE_HOURS, in_active_window, now, weekday_cn

log = logging.getLogger("court")

DEFAULT_CONFIG = "config.yaml"
FAILURE_ALERT_THRESHOLD = 6  # 连续 6 次失败（约 30 分钟）才打扰人


# --------------------------------------------------------------------- 配置
def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        raise SystemExit(f"找不到配置文件 {file}")
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        # 缩进错/引号没闭合是最常见的低级错，给出可读信息而不是 traceback
        raise SystemExit(f"{file} 不是合法的 YAML（多半是缩进或引号）：{exc}") from exc
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
    matching = cfg.get("matching") or {}
    notify_cfg = cfg.get("notify") or {}

    # 盯场表：周几 + 该周几的时段。每天都跑，但只查"该周几的下一次"
    try:
        rules = parse_schedule(cfg.get("schedule"))
    except ValueError as exc:
        raise SystemExit(f"schedule 配置有误：{exc}") from exc
    if not rules:
        raise SystemExit("schedule 为空：请在 config.yaml 里配置要盯的星期与时段")

    targets = plan_targets(rules, moment)
    log.info("盯场表：%s", describe(rules))
    if not targets:
        log.info("本周剩下的日期里没有配置的周几，本次不查询、不推送")
        return 0
    log.info("本次要查：%s", describes_targets(targets))

    state_path = _state_path(cfg)
    state = State.load(state_path)
    state.prune(moment.date())

    notifier = build_notifier(
        notify_cfg.get("provider", "wxpusher_spt"),
        _env("WXPUSHER_SPT"),
        dry_run=args.dry_run,
    )

    # 盯场表变了就推一条"当前最新时段"，省得靠人肉确认改动生效没有
    current_fingerprint = fingerprint(rules)
    announce = bool(notify_cfg.get("announce_schedule_changes", True))
    if state.schedule_fingerprint == current_fingerprint:
        log.info("盯场表未变化（指纹 %s）", current_fingerprint)
    elif not announce:
        state.schedule_fingerprint = current_fingerprint
    elif getattr(notifier, "delivers", False) or args.dry_run:
        _announce_schedule(notifier, rules, moment)
        if getattr(notifier, "delivers", False) and not args.dry_run:
            state.schedule_fingerprint = current_fingerprint
    else:
        log.warning("盯场表有变化，但当前通道不投递，先不记账")

    try:
        client = build_client(cfg)
    except SystemExit as exc:
        # 少配一个 Secret 不该让任务每 5 分钟失败一次（一天 144 封失败邮件）
        if isinstance(notifier, ConsoleNotifier):
            raise
        message = str(exc)
        return _handle_error(state, state_path, notifier, moment, RuntimeError(message), kind="config")

    # 每天第一次运行时发一条"开始运行"，方便确认监控活着
    today_key = moment.date().isoformat()
    if state.started_on != today_key and notify_cfg.get("announce_start", True):
        if getattr(notifier, "delivers", False) or args.dry_run:
            _announce_start(notifier, rules, targets, moment, notify_cfg.get("detail_url"))
            if getattr(notifier, "delivers", False) and not args.dry_run:
                state.started_on = today_key

    # 资源列表只用于日志，失败不致命
    if not state.resources.get(str(hall_id)):
        resources = fetch_resources(client, hall_id)
        if resources:
            state.resources[str(hall_id)] = resources
            log.info("场馆 %s 下发现 %d 个资源：%s", hall_id, len(resources),
                     "、".join(r["name"] for r in resources[:8]))

    available_status = int(venue.get("available_status", AVAILABLE_STATUS))
    page_size = int(venue.get("page_size", 20))
    resource_ids = [str(x) for x in (venue.get("resource_ids") or [])]
    minimum = int(matching.get("min_consecutive_slots", 1))
    include = venue.get("resource_name_include") or []
    exclude = venue.get("resource_name_exclude") or []

    hits: list[Hit] = []
    all_slots: list[Slot] = []
    fetched = 0
    cycle = CycleLog(moment)
    try:
        for index, target in enumerate(targets):
            slots = fetch_slots(
                client,
                hall_id=hall_id,
                day=target.date,
                page_size=page_size,
                available_status=available_status,
            )
            fetched += len(slots)
            kept = _filter_slots(slots, resource_ids, moment)
            all_slots.extend(kept)
            day_hits = select(
                kept,
                target_dates=[target.date],
                windows=list(target.windows),
                min_consecutive_slots=minimum,
                include=include,
                exclude=exclude,
            )
            cycle.add_day(target, len(slots), len(kept), len(day_hits))
            hits.extend(day_hits)
            if args.sleep_between and index < len(targets) - 1:
                _sleep(args.sleep_between)
    except AuthExpired as exc:
        return _handle_error(state, state_path, notifier, moment, exc, kind="auth")
    except (ApiError, SchemaChanged, TransportError) as exc:
        return _handle_error(state, state_path, notifier, moment, exc, kind="other")

    hits.sort(key=lambda hit: (hit.date, hit.start, hit.resource_name))
    log.info("共取到 %d 格（%d 天），命中 %d 处", fetched, len(targets), len(hits))

    fresh, released = _pick_fresh(hits, all_slots, state, hall_id, moment)
    log.info(
        "命中 %d 处；其中新释放的格子 %d 个、需推送的段 %d 处",
        len(hits),
        released,
        len(fresh),
    )

    if not fresh:
        state.record_success()
        if not args.dry_run:
            state.save(state_path)
        return 0

    title, body = render_message(fresh, hall_id, moment, notify_cfg.get("detail_url"))
    body = body + cycle.appendix()
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
        if getattr(notifier, "delivers", False):
            for hit in fresh:
                for slot in hit.slots:
                    state.mark_notified(slot.key(hall_id), moment)
        else:
            # 没配 SPT / dry-run：消息没真正投递，不能记账，否则配好后会漏推
            log.warning("本轮未真正投递，不记录已推送状态，下次仍会重试这批空场")
        state.save(state_path)
    return 0


def _pick_fresh(
    hits: list[Hit],
    all_slots: list[Slot],
    state: State,
    hall_id,
    moment: datetime,
) -> tuple[list[Hit], int]:
    """按**单个场次格子**判定新鲜度，并更新占用状态。

    对应你的要求：「设定时间内，有占用的场变为空场，就通知」。

      规则 A（捡漏）：上次观察到占用/不可约，这次可用 → 必定通知，
                     即使这个格子以前推过（被抢走又被释放，算新机会）。
      规则 B（首次）：一直可用、但从未推送过 → 通知。
      规则 C（常态）：一直可用、且已推过 → 不通知，避免每 5 分钟刷屏。
      规则 D（占回）：本轮不可用 → 记入占用集合，等它释放时触发规则 A。

    必须先读旧状态再更新，否则 mark_free 会提前抹掉"上次是占用"这个事实。
    """
    # ---- 阶段一：读旧状态，判定哪些格子新鲜
    was_occupied: dict[str, bool] = {}
    notified: dict[str, bool] = {}
    for slot in all_slots:
        key = slot.key(hall_id)
        was_occupied[key] = state.was_occupied(key)
        notified[key] = state.already_notified(key)

    # ---- 阶段二：更新状态（占用/空闲），供下一轮使用
    released = 0
    for slot in all_slots:
        key = slot.key(hall_id)
        if slot.available:
            if was_occupied[key]:
                released += 1
            state.mark_free(key)
        else:
            state.mark_occupied(key, moment)

    # ---- 阶段三：挑出需要推送的格子
    def is_new(key: str) -> bool:
        if was_occupied[key]:
            return True          # 规则 A
        return not notified[key]  # 规则 B

    fresh_hits = [
        hit for hit in hits if any(is_new(s.key(hall_id)) for s in hit.slots)
    ]
    return fresh_hits, released


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

    if kind in {"auth", "config"}:
        today = moment.date().isoformat()
        if state.auth_alert_on != today:  # 每天最多打扰一次
            if kind == "auth":
                title = "⚠️ ehall 登录态失效"
                body = (
                    "预约系统 Cookie 已过期，监控已暂停。\n\n"
                    "**恢复步骤**\n"
                    "1. 浏览器打开预约页并登录\n"
                    "2. F12 → Network → 任一 `site/reservation` 请求 → 复制 Cookie\n"
                    "3. 更新 GitHub Secret `EHALL_COOKIE`（UA 变了同时更新 `EHALL_UA`）\n\n"
                    f"失效时间：{moment.strftime('%Y-%m-%d %H:%M')}"
                )
            else:
                title = "⚠️ 监控缺少配置"
                body = (
                    "缺少环境变量 `EHALL_COOKIE`，监控没有启动。\n\n"
                    "**修复**\n"
                    "仓库 Settings → Secrets and variables → Actions → 添加：\n"
                    "- `EHALL_COOKIE`：浏览器 Cookie\n"
                    "- `EHALL_UA`：同一浏览器的 User-Agent\n\n"
                    f"时间：{moment.strftime('%Y-%m-%d %H:%M')}"
                )
            try:
                notifier.send(title, body, None)
                state.auth_alert_on = today
            except NotifyError as notify_exc:
                log.error("告警推送失败：%s", notify_exc)
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
    max_items: int = 30,
) -> tuple[str, str]:
    """按「日期 → 时段」聚合，用场地数代替逐条罗列，避免一条消息几十行。"""
    if not hits:
        body = f"目标时段暂无可约场地。\n\n_查询时间 {moment.strftime('%m-%d %H:%M')} · 场馆 {hall_id}_"
        return "🏸 暂无空场", body

    by_day: dict[date, dict[tuple, list[str]]] = {}
    for hit in hits:
        by_day.setdefault(hit.date, {}).setdefault((hit.start, hit.end), []).append(hit.resource_name)

    total_spans = sum(len(spans) for spans in by_day.values())
    lines = [f"### 🏸 {len(by_day)} 天共 {total_spans} 个时段有空场", ""]

    shown = 0
    for day in sorted(by_day):
        lines.append(f"**{day.strftime('%m-%d')} 周{weekday_cn(day)}**")
        for (start, end), names in sorted(by_day[day].items()):
            if shown >= max_items:
                break
            ordered = sorted(names, key=_court_key)
            preview = "、".join(ordered[:4]) + ("…" if len(ordered) > 4 else "")
            lines.append(f"- {start.strftime('%H:%M')}-{end.strftime('%H:%M')} · {len(ordered)} 块（{preview}）")
            shown += 1
        lines.append("")

    if total_spans > shown:
        lines.append(f"…另有 {total_spans - shown} 个时段未列出")
        lines.append("")

    lines.append(f"_查询时间 {moment.strftime('%m-%d %H:%M')} · 场馆 {hall_id}_")
    body = "\n".join(lines).strip()

    first_day = min(by_day)
    title = f"🏸 {first_day.strftime('%m-%d')} 起 {total_spans} 个时段有空场"
    return title[:100], body


def _court_key(name: str) -> tuple[int, str]:
    """让 1号、2号、10号 按数字排，而不是字典序。"""
    digits = ""
    for char in name:
        if char.isdigit():
            digits += char
        else:
            break
    return (int(digits) if digits else 9999, name)


# --------------------------------------------------------------------- probe
def cmd_probe(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    hall_id = args.hall or (cfg.get("venue") or {}).get("hall_id", 54)
    client = build_client(cfg, require_cookie=not args.anonymous)

    if client is None:
        print("！未提供 EHALL_COOKIE，仅做未登录探测（验证网络与 WAF）\n")
    else:
        print(f"已装载 Cookie（长度 {len(_env('EHALL_COOKIE') or '')}），逐日拉取 list-page\n")

    results = probe(client, hall_id, out_dir=args.out, days=args.days, sleep_between=args.sleep)

    usable = [r for r in results if r.get("available")]
    print(f"\n共探测 {len(results)} 天，其中 {len(usable)} 天存在可约格子。")
    if usable:
        print("\n可约概况：")
        for record in usable:
            print(f"  {record['date']}  可约 {record['available']} 格")
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


# --------------------------------------------------------------------- check
def cmd_check(args: argparse.Namespace) -> int:
    """本地自检：配置能不能解析、今天会查什么、cron 是否跟扫描窗口同步。

    push 之前跑一遍，缩进错、时窗写反、cron 忘改都能当场发现。
    """
    config_path = Path(args.config)
    try:
        cfg = load_config(config_path)
    except SystemExit as exc:
        print(f"✗ {exc}")
        return 1
    try:
        rules = parse_schedule(cfg.get("schedule"))
    except ValueError as exc:
        print(f"✗ schedule 配置有误：{exc}")
        return 1
    if not rules:
        print("✗ schedule 为空：请配置要盯的星期与时段")
        return 1

    active_hours = tuple(cfg.get("active_hours") or DEFAULT_ACTIVE_HOURS)
    interval = int(cfg.get("scan_interval_minutes", 15))
    moment = _resolve_now(args.now)

    print(f"✓ 配置可解析：{config_path}")
    print(f"  盯场表：{describe(rules)}")
    print(f"  扫描窗口：{active_hours[0]}-{active_hours[1]}（北京时间）")

    targets = plan_targets(rules, moment)
    today = f"{moment:%Y-%m-%d} 周{weekday_cn(moment.date())}"
    print(f"  {today} 会查：{describes_targets(targets) if targets else '（本周已无目标，不会发请求）'}")

    expected = cron_expression(active_hours, interval)
    per_day = daily_runs(active_hours, interval)
    per_month = monthly_minutes(active_hours, interval)
    print(f"  查询间隔：{interval} 分钟 → {per_day} 次/天")
    print(f"  额度估算：约 {per_month} 分钟/月（按 31 天；GitHub 按 job 取整，每次 1 分钟）")
    print(f"  cron 应为：{expected}")

    if per_month > FREE_MINUTES_PER_MONTH:
        print(
            f"  ⚠ 约 {per_month} 分钟/月，超过私有仓库额度；"
            "仓库为 public 时不计分钟，可忽略"
        )
    else:
        print(f"  ✓ 额度余量 {FREE_MINUTES_PER_MONTH - per_month} 分钟（私有仓库口径）")

    workflow = config_path.parent / ".github" / "workflows" / "watch.yml"
    if workflow.exists():
        crons = [
            line.split('"')[1]
            for line in workflow.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- cron:")
        ]
        if crons and crons[0] != expected:
            print(f"✗ watch.yml 里写的是 {crons[0]}，与上面推导值不一致，请同步修改")
            return 1
        print("✓ watch.yml 的 cron 与扫描窗口一致")
    return 0


# --------------------------------------------------------------------- 工具
def _filter_slots(slots: list[Slot], resource_ids: list[str], moment: datetime) -> list[Slot]:
    """按资源下钻 + 剔除已经开始的时段。"""
    kept = slots
    if resource_ids:
        kept = [s for s in kept if s.resource_id.split("#", 1)[0] in resource_ids]
        log.info("资源过滤 %s：%d → %d 格", resource_ids, len(slots), len(kept))
    before = len(kept)
    kept = drop_started(kept, moment)
    if before != len(kept):
        log.info("剔除已开始的时段：%d → %d 格", before, len(kept))
    return kept


class CycleLog:
    """收集一轮运行的关键数字，作为推送附录，便于事后定位"为什么没报"。"""

    def __init__(self, moment: datetime) -> None:
        self.started = moment
        self.days: list[dict[str, object]] = []
        self.notes: list[str] = []

    def add_day(self, target, fetched: int, kept: int, hits: int) -> None:
        self.days.append(
            {"label": target.label, "fetched": fetched, "kept": kept, "hits": hits}
        )

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def total_fetched(self) -> int:
        return sum(int(d["fetched"]) for d in self.days)

    @property
    def total_hits(self) -> int:
        return sum(int(d["hits"]) for d in self.days)

    def appendix(self) -> str:
        """极简表格：每天取到多少格、留下多少、命中多少。"""
        lines = [
            "",
            "---",
            f"**运行日志** {self.started.strftime('%m-%d %H:%M')}",
        ]
        for day in self.days:
            lines.append(
                f"- {day['label']}：取到 {day['fetched']} 格 → 可用 {day['kept']} → 命中 {day['hits']}"
            )
        for note in self.notes:
            lines.append(f"- {note}")
        return "\n".join(lines)


def _announce_start(notifier, rules, targets, moment: datetime, detail_url: str | None) -> None:
    """每天第一次运行发一条，证明监控确实在跑。"""
    lines = [
        f"### ▶️ 今日开始监控",
        "",
        f"**扫描窗口** {moment.strftime('%H:%M')} 起，盯场表：",
        "",
    ]
    for rule in rules:
        lines.append(f"- {rule.label}")
    lines.append("")
    lines.append(f"**本次要查**：{describes_targets(targets)}")
    lines.append("")
    lines.append(f"_启动于 {moment.strftime('%m-%d %H:%M')}_")
    try:
        notifier.send("▶️ 场地监控已启动", "\n".join(lines), detail_url)
    except NotifyError as exc:
        log.error("启动通知推送失败：%s", exc)


def _announce_schedule(notifier, rules, moment: datetime) -> None:
    """盯场表变化时推一条，让人一眼看到线上到底在盯什么。"""
    body = "\n".join(f"- {rule.label}" for rule in rules)
    message = (
        "盯场表已生效，之后每天按以下时段持续监控：\n\n"
        f"{body}\n\n"
        f"_更新于 {moment.strftime('%m-%d %H:%M')}_"
    )
    try:
        notifier.send("⚙️ 盯场表已更新", message, None)
    except NotifyError as exc:
        log.error("盯场表通知推送失败：%s", exc)


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

    probe_cmd = sub.add_parser("probe", help="逐日拉取余量，检查接口是否可用")
    probe_cmd.add_argument("--hall", help="场馆 id，默认取 config 里的 hall_id")
    probe_cmd.add_argument("--days", type=int, default=8, help="从今天起连续查几天")
    probe_cmd.add_argument("--out", default="probe-out", help="原始响应落盘目录")
    probe_cmd.add_argument("--sleep", type=float, default=0.6, help="每次请求之间的间隔")
    probe_cmd.add_argument("--anonymous", action="store_true", help="不带 Cookie，只验证网络")
    probe_cmd.set_defaults(func=cmd_probe)

    smoke = sub.add_parser("smoke", help="连通性验证（GitHub Actions 首次部署用）")
    smoke.set_defaults(func=cmd_smoke)

    check = sub.add_parser("check", help="本地自检：配置能否解析、cron 是否同步")
    check.add_argument("--now", help="伪造当前时间（ISO8601）")
    check.set_defaults(func=cmd_check)
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
