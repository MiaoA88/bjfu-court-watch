"""CLI commands and watch orchestration; domain logic lives in dedicated modules."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from .config import DEFAULT_CONFIG, load_config
from .dedup import pick_fresh as _pick_fresh
from .alerts import FAILURE_ALERT_THRESHOLD, handle_error as _handle_error
from .messages import CycleLog, render_message, _court_key
from .health import check_stale, report as health_report, write_summary
from .availability import AVAILABLE_STATUS, fetch_resources, fetch_slots, probe
from .ehall import (
    BASE_URL,
    ApiError,
    AuthExpired,
    EhallClient,
    SchemaChanged,
    TransportError,
)
from .matcher import Hit, Slot, drop_started, select
from .notify import ConsoleNotifier, NotifyError, build_notifier
from .schedule import (
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
from .timeutil import DEFAULT_ACTIVE_HOURS, TZ, in_active_window, now, weekday_cn

log = logging.getLogger("court")


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
    notify_cfg = cfg.get("notify") or {}

    # 每次运行都留一条记录，即使落在窗口外——否则"任务跑了但没发消息"
    # 和"任务根本没跑"无法区分，排查时只能靠翻 Actions 日志。
    if not args.dry_run:
        _heartbeat(moment, in_active_window(moment, active_hours), active_hours)

    if not in_active_window(moment, active_hours):
        log.info(
            "北京时间 %s 不在扫描窗口 %s-%s，本次不查询场地",
            moment.strftime("%Y-%m-%d %H:%M"),
            active_hours[0],
            active_hours[1],
        )
        # 窗口外也要发一次"今日启动"，说明调度器至少派活过来了
        _start_notice_if_needed(cfg, notify_cfg, moment, args)
        return 0

    venue = cfg.get("venue") or {}
    hall_id = venue.get("hall_id", 54)
    matching = cfg.get("matching") or {}

    # 盯场表映射到本周尚未过去的日期。
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
        delivered = _announce_schedule(notifier, rules, moment)
        if delivered and not args.dry_run:
            state.schedule_fingerprint = current_fingerprint
    else:
        log.warning("盯场表有变化，但当前通道不投递，先不记账")

    if not args.dry_run:
        check_stale(state, moment, active_hours, notifier)
    state.health.setdefault("first_attempt_at", moment.isoformat(timespec="seconds"))
    state.health.update(last_attempt_at=moment.isoformat(timespec="seconds"), status="running",
                        notification="not_attempted", days=0, fetched=0, hits=0)
    try:
        client = build_client(cfg)
    except SystemExit as exc:
        # 缺少 Secret 通过状态和告警反馈，避免每轮都触发失败邮件。
        if isinstance(notifier, ConsoleNotifier):
            raise
        message = str(exc)
        return _handle_error(state, state_path, notifier, moment, RuntimeError(message), kind="config")

    # 每天第一次运行时发一条"开始运行"，方便确认监控活着
    _start_notice_if_needed(cfg, notify_cfg, moment, args, state)

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
            state.health.update(days=index + 1, fetched=fetched, hits=len(hits))
            if args.sleep_between and index < len(targets) - 1:
                _sleep(args.sleep_between)
    except AuthExpired as exc:
        return _handle_error(state, state_path, notifier, moment, exc, kind="auth")
    except (ApiError, SchemaChanged, TransportError) as exc:
        return _handle_error(state, state_path, notifier, moment, exc, kind="other")

    hits.sort(key=lambda hit: (hit.date, hit.start, hit.resource_name))
    log.info("共取到 %d 格（%d 天），命中 %d 处", fetched, len(targets), len(hits))

    state.record_success()
    state.auth_alert_on = None
    state.health_alert_at = None
    state.health.update(status="ok", last_success_at=moment.isoformat(timespec="seconds"),
                        days=len(targets), fetched=fetched, hits=len(hits), last_error=None)
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
            state.health["notification"] = "not_needed" if getattr(notifier, "delivers", False) else "unconfigured"
            state.save(state_path)
            write_summary(state)
        return 0

    title, body = render_message(fresh, hall_id, moment, notify_cfg.get("detail_url"))
    body = body + cycle.appendix()
    try:
        notifier.send(title, body, notify_cfg.get("detail_url"))
    except NotifyError as exc:
        log.error("推送失败：%s", exc)
        state.health.update(notification="failed", notification_error=str(exc))
        if not args.dry_run:
            state.save(state_path)
            write_summary(state)
        return 1

    state.record_success()
    if not args.dry_run:
        if getattr(notifier, "delivers", False):
            state.health.update(notification="sent", notification_error=None, last_notification_at=moment.isoformat(timespec="seconds"))
            for hit in fresh:
                for slot in hit.slots:
                    state.mark_notified(slot.key(hall_id), moment)
        else:
            # 没配 SPT / dry-run：消息没真正投递，不能记账，否则配好后会漏推
            state.health["notification"] = "unconfigured"
            log.warning("本轮未真正投递，不记录已推送状态，下次仍会重试这批空场")
        state.save(state_path)
        write_summary(state)
    return 0


# --------------------------------------------------------------------- announce
def cmd_announce(args: argparse.Namespace) -> int:
    """配置一推送就把"当前生效的盯场表"发到手机。

    独立于 watch；这条通知只确认仓库配置，不验证外部调度器的计划。
    """
    cfg = load_config(args.config)
    try:
        rules = parse_schedule(cfg.get("schedule"))
    except ValueError as exc:
        print(f"✗ schedule 配置有误：{exc}")
        return 1
    if not rules:
        print("✗ schedule 为空")
        return 1

    moment = _resolve_now(args.now)
    active_hours = tuple(cfg.get("active_hours") or DEFAULT_ACTIVE_HOURS)
    interval = int(cfg.get("scan_interval_minutes", 5))
    targets = plan_targets(rules, moment)

    lines = [
        "### ⚙️ 盯场表已生效",
        "",
        f"**扫描窗口** {active_hours[0]}-{active_hours[1]} · 每 {interval} 分钟",
        "",
        "**每周盯场**",
    ]
    for rule in rules:
        lines.append(f"- {rule.label}")
    lines += ["", f"**本次要查**：{describes_targets(targets) if targets else '（本周已无目标）'}", ""]
    lines.append(f"_更新于 {moment.strftime('%m-%d %H:%M')}_")

    notifier = build_notifier(
        (cfg.get("notify") or {}).get("provider", "wxpusher_spt"),
        _env("WXPUSHER_SPT"),
        dry_run=args.dry_run,
    )
    detail_url = (cfg.get("notify") or {}).get("detail_url")
    try:
        notifier.send("⚙️ 盯场表已生效", "\n".join(lines), detail_url)
    except NotifyError as exc:
        print(f"✗ 推送失败：{exc}")
        return 1
    if not args.dry_run and not getattr(notifier, "delivers", False):
        print("✗ 未实际推送：缺少 WXPUSHER_SPT")
        return 1
    print("✓ 已预览当前盯场表" if args.dry_run else "✓ 已推送当前盯场表")
    return 0


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
    """Validate config and print the intended external schedule without network access."""
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
    interval = int(cfg.get("scan_interval_minutes", 5))
    moment = _resolve_now(args.now)

    print(f"✓ 配置可解析：{config_path}")
    print(f"  盯场表：{describe(rules)}")
    print(f"  扫描窗口：{active_hours[0]}-{active_hours[1]}（北京时间）")

    targets = plan_targets(rules, moment)
    today = f"{moment:%Y-%m-%d} 周{weekday_cn(moment.date())}"
    print(f"  {today} 会查：{describes_targets(targets) if targets else '（本周已无目标，不会发请求）'}")

    per_day = daily_runs(active_hours, interval)
    per_month = monthly_minutes(active_hours, interval)
    print(f"  查询间隔：{interval} 分钟 → {per_day} 次/天")
    print(f"  cron-job.org 计划：{cron_expression(active_hours, interval, utc=False)}（时区 Asia/Shanghai）")
    print("  请人工核对外部调度器；check 不读取或修改 cron-job.org 配置。")
    print(f"  运行规模：{per_day} 次/天，按 31 天最多 {per_month} 次/月（不含额外手动运行）")
    print("  此处估算运行次数；实际费用取决于仓库方案、runner 类型和作业时长。")

    workflow = config_path.parent / ".github" / "workflows" / "watch.yml"
    if not workflow.exists():
        return 0

    import yaml
    try:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("workflow 顶层必须是映射")
        triggers = document.get("on", document.get(True))
        if not isinstance(triggers, dict) or "workflow_dispatch" not in triggers:
            raise ValueError("watch.yml 必须配置 workflow_dispatch")
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"✗ workflow 配置有误：{exc}")
        return 1
    if "schedule" in triggers:
        # 调度已迁到 cron-job.org；watch.yml 里残留 schedule 会形成两套定时器。
        print("✗ watch.yml 仍有 cron 行：调度已交给 cron-job.org，请删除 schedule 触发")
        return 1

    print("✓ watch.yml 无 GitHub 侧调度（由 cron-job.org 触发 workflow_dispatch）")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    state = State.load(_state_path(cfg))
    print(health_report(state))
    return 0


# --------------------------------------------------------------------- 工具
def _heartbeat(moment: datetime, in_window: bool, active_hours) -> None:
    """把每次运行写进 GitHub Actions 的 job summary，不推送、不耗额度。

    Actions 页面能直接看到"今天几点跑过几次"，用来判断调度是否正常。
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    line = (
        f"- {moment.strftime('%Y-%m-%d %H:%M')} "
        f"{'✅ 窗口内' if in_window else '⏸ 窗口外'} "
        f"({active_hours[0]}-{active_hours[1]})\n"
    )
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def _start_notice_if_needed(
    cfg: dict[str, Any],
    notify_cfg: dict[str, Any],
    moment: datetime,
    args,
    state: "State | None" = None,
) -> None:
    """每天首次运行发一条启动通知；窗口外也发，用来证明调度活着。

    传 state 进来时复用它（调用方负责 save），否则自己加载并保存。
    这样同一次运行里即使调用两次也不会重复推送。
    """
    if not notify_cfg.get("announce_start", True):
        return
    owns_state = state is None
    if owns_state:
        state = State.load(_state_path(cfg))
    today_key = moment.date().isoformat()
    if state.started_on == today_key:
        return
    try:
        rules = parse_schedule(cfg.get("schedule"))
    except ValueError:
        return
    if not rules:
        return

    notifier = build_notifier(
        notify_cfg.get("provider", "wxpusher_spt"),
        _env("WXPUSHER_SPT"),
        dry_run=args.dry_run,
    )
    active_hours = cfg.get("active_hours") or list(DEFAULT_ACTIVE_HOURS)
    in_window = in_active_window(moment, active_hours)
    targets = plan_targets(rules, moment)

    lines = ["### ▶️ 今日首次运行", ""]
    if in_window:
        lines.append(f"**扫描窗口** {active_hours[0]}-{active_hours[1]}，本次开始查询。")
    else:
        lines.append(
            f"**注意**：本次运行于 {moment.strftime('%H:%M')}，"
            f"在扫描窗口 {active_hours[0]}-{active_hours[1]} 之外，因此不会查询场地。"
        )
    lines += ["", "**本次要查**：" + (describes_targets(targets) if targets else "（本周已无目标）")]
    lines += ["", f"_启动于 {moment.strftime('%m-%d %H:%M')}_"]

    try:
        notifier.send("▶️ 场地监控已启动", "\n".join(lines), notify_cfg.get("detail_url"))
        if getattr(notifier, "delivers", False) and not args.dry_run:
            state.started_on = today_key
            if owns_state:
                state.save(_state_path(cfg))
    except NotifyError as exc:
        log.error("启动通知推送失败：%s", exc)


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


def _announce_schedule(notifier, rules, moment: datetime) -> bool:
    """盯场表变化时推一条，让人一眼看到线上到底在盯什么。"""
    body = "\n".join(f"- {rule.label}" for rule in rules)
    message = (
        "盯场表已生效，之后每天按以下时段持续监控：\n\n"
        f"{body}\n\n"
        f"_更新于 {moment.strftime('%m-%d %H:%M')}_"
    )
    try:
        notifier.send("⚙️ 盯场表已更新", message, None)
        return bool(getattr(notifier, "delivers", False))
    except NotifyError as exc:
        log.error("盯场表通知推送失败：%s", exc)
        return False


def _resolve_now(value: str | None) -> datetime:
    if not value:
        return now()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


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

    check = sub.add_parser("check", help="校验配置并打印外部调度计划（不自动同步）")
    check.add_argument("--now", help="伪造当前时间（ISO8601）")
    check.set_defaults(func=cmd_check)

    announce = sub.add_parser("announce", help="把当前生效的盯场表推送到手机")
    announce.add_argument("--now", help="伪造当前时间（ISO8601）")
    announce.add_argument("--dry-run", action="store_true", help="只打印不推送")
    announce.set_defaults(func=cmd_announce)
    status = sub.add_parser("status", help="查看本地状态文件中的最近扫描结果")
    status.set_defaults(func=cmd_status)
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
