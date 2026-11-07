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
CONFIG_CHECK = ROOT / ".github" / "workflows" / "config-check.yml"


def all_workflows() -> list[Path]:
    return sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def trigger_block(workflow: dict) -> dict:
    # PyYAML 按 YAML 1.1 把裸 `on:` 解析成布尔 True
    return workflow.get("on") or workflow.get(True)


def test_watch_has_no_github_schedule():
    """调度已交给 cron-job.org，watch.yml 不应再保留 schedule。

    实测 GitHub 免费 runner 对 */5 cron 三天只触发 3 次、单次延迟 6 小时以上，
    因此改用外部定时器调 workflow_dispatch。留着 schedule 会形成两套定时器打架。
    """
    workflow = load_yaml(WORKFLOW)
    trigger = trigger_block(workflow)
    assert "schedule" not in trigger, "watch.yml 不应再有 schedule 触发"
    assert "workflow_dispatch" in trigger, "必须保留 workflow_dispatch 供外部调用"


def test_scan_interval_is_sane_for_external_cron():
    """config 的间隔要能被 cron-job.org 的 cron 表达（1-59 分钟）。"""
    config = load_yaml(CONFIG)
    interval = int(config.get("scan_interval_minutes", 5))
    assert 1 <= interval <= 59
    assert cron_expression(tuple(config["active_hours"]), interval, utc=False)


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


def test_workflows_are_valid_yaml():
    for path in all_workflows():
        workflow = load_yaml(path)
        assert isinstance(workflow, dict)
        assert trigger_block(workflow)
        assert workflow.get("jobs")


def test_config_check_has_no_repository_write_side_effects():
    workflow = load_yaml(CONFIG_CHECK)
    assert workflow["permissions"]["contents"] == "read"
    runs = " ".join(str(s.get("run", "")) for s in workflow["jobs"]["announce"]["steps"])
    assert "git push" not in runs
    assert "git commit" not in runs


def test_state_is_saved_on_failed_watch_and_reruns():
    steps = load_yaml(WORKFLOW)["jobs"]["watch"]["steps"]
    restore = next(s for s in steps if s.get("uses") == "actions/cache/restore@v4")
    save = next(s for s in steps if s.get("uses") == "actions/cache/save@v4")
    assert "always()" in save["if"]
    assert "hashFiles" in save["if"]
    assert "github.run_attempt" in save["with"]["key"]
    assert restore["with"]["key"] == save["with"]["key"]


def test_config_check_triggers_on_config_push():
    """config.yaml 一推送就该立即跑一次，不必等定时任务。"""
    workflow = load_yaml(CONFIG_CHECK)
    trigger = trigger_block(workflow)
    assert "push" in trigger, "缺少 push 触发"
    assert workflow["jobs"] and "config.yaml" in " ".join(
        str(p) for p in trigger["push"]["paths"]
    )
    assert "workflow_dispatch" in trigger, "保留手动触发便于排查"


def test_config_check_pushes_announcement():
    workflow = load_yaml(CONFIG_CHECK)
    steps = workflow["jobs"]["announce"]["steps"]
    runs = " ".join(str(s.get("run", "")) for s in steps)
    assert "src.cli check" in runs, "应先校验配置"
    assert "src.cli announce" in runs, "应推送当前盯场表"
    env = next(s["env"] for s in steps if s.get("name") == "Announce effective schedule")
    assert env["WXPUSHER_SPT"] == "${{ secrets.WXPUSHER_SPT }}"


def test_config_schedule_is_not_empty():
    config = load_yaml(CONFIG)
    assert parse_schedule(config["schedule"]), "schedule 为空会导致监控永不运行"
