"""守住 config.yaml 与 watch.yml 的一致性。

cron 的星期列表和小时范围是从 config 推导出来的；一旦有人只改了一边，
线上就会"该跑的时候没跑"或"不该跑的时候空跑"，而且不会有任何告警。
"""

from pathlib import Path

import yaml

from src.schedule import cron_expression, parse_schedule
from src.timeutil import DEFAULT_ACTIVE_HOURS

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "watch.yml"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def trigger_block(workflow: dict) -> dict:
    # PyYAML 按 YAML 1.1 把裸 `on:` 解析成布尔 True
    return workflow.get("on") or workflow.get(True)


def test_cron_matches_config_schedule_and_active_hours():
    config = load_yaml(CONFIG)
    rules = parse_schedule(config["schedule"])
    active_hours = tuple(config.get("active_hours") or DEFAULT_ACTIVE_HOURS)
    expected = cron_expression(rules, active_hours)

    workflow = load_yaml(WORKFLOW)
    crons = [entry["cron"] for entry in trigger_block(workflow)["schedule"]]

    assert crons == [expected], (
        f"watch.yml 的 cron {crons} 与 config.yaml 推导出的 {expected} 不一致；"
        "改盯场表时记得同步 cron"
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
    rules = parse_schedule(config["schedule"])
    assert rules, "schedule 为空会导致监控永不运行"
