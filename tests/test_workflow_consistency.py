"""守住 config.yaml 与 watch.yml 的一致性。

cron 的小时范围是从 config 的 active_hours 推导出来的；一旦有人只改了一边，
线上就会"该跑的时候没跑"或"不该跑的时候空跑"，而且不会有任何告警。
"""

from pathlib import Path

import yaml

from src.schedule import FREE_MINUTES_PER_MONTH, cron_expression, monthly_minutes, parse_schedule
from src.timeutil import DEFAULT_ACTIVE_HOURS

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "watch.yml"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def trigger_block(workflow: dict) -> dict:
    # PyYAML 按 YAML 1.1 把裸 `on:` 解析成布尔 True
    return workflow.get("on") or workflow.get(True)


def test_cron_matches_config_active_hours():
    config = load_yaml(CONFIG)
    active_hours = tuple(config.get("active_hours") or DEFAULT_ACTIVE_HOURS)
    interval = int(config.get("scan_interval_minutes", 15))
    expected = cron_expression(active_hours, interval)

    workflow = load_yaml(WORKFLOW)
    crons = [entry["cron"] for entry in trigger_block(workflow)["schedule"]]

    assert crons == [expected], (
        f"watch.yml 的 cron {crons} 与 config.yaml 推导出的 {expected} 不一致；"
        "改扫描窗口或间隔时记得同步 cron"
    )


def test_config_stays_within_actions_quota():
    """public 仓库不计分钟，额度不再是硬约束；这里只在私有仓库口径下做提示。"""
    config = load_yaml(CONFIG)
    active_hours = tuple(config.get("active_hours") or DEFAULT_ACTIVE_HOURS)
    interval = int(config.get("scan_interval_minutes", 5))
    per_month = monthly_minutes(active_hours, interval)
    if per_month > FREE_MINUTES_PER_MONTH:
        print(
            f"\n[提示] 当前配置约 {per_month} 分钟/月，私有仓库会超限；"
            "public 仓库不计分钟，无需处理。"
        )


def test_workflow_has_manual_dispatch():
    workflow = load_yaml(WORKFLOW)
    assert "workflow_dispatch" in trigger_block(workflow)


def test_workflow_passes_all_three_secrets():
    workflow = load_yaml(WORKFLOW)
    steps = workflow["jobs"]["watch"]["steps"]
    env = next(step["env"] for step in steps if step.get("name") == "Watch")
    for name in ("EHALL_COOKIE", "EHALL_UA", "WXPUSHER_SPT"):
        assert f"${{{{ secrets.{name} }}}}" == env[name]


def test_workflow_comments_are_ascii_only():
    """GitHub 会静默丢弃含中文注释的 workflow，这条守着别再写回去。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    bad = [(i, line) for i, line in enumerate(text.splitlines(), 1) if any(ord(c) > 127 for c in line)]
    assert bad == [], f"watch.yml 第 {bad[0][0]} 行含非 ASCII 字符：{bad[0][1]!r}"


def test_config_schedule_is_not_empty():
    config = load_yaml(CONFIG)
    assert parse_schedule(config["schedule"]), "schedule 为空会导致监控永不运行"
