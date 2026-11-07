from copy import deepcopy
import argparse

import pytest
import yaml

from src import cli
from src.config import validate_config


BASE = {"schedule": [{"weekday": 1, "time_windows": ["19:00-22:00"]}]}


@pytest.mark.parametrize("section,field,value", [
    ("notify", "provider", "typo"),
    ("notify", "announce_start", "false"),
    ("notify", "detail_url", 4),
    ("http", "timeout_seconds", -5),
    ("http", "timeout_seconds", float("nan")),
    ("http", "retries", 1.5),
    ("venue", "page_size", 0),
    ("venue", "hall_id", True),
    ("venue", "resource_ids", "156"),
    ("venue", "resource_name_include", [12]),
    ("matching", "min_consecutive_slots", 0),
    ("state", "path", ""),
])
def test_check_rejects_invalid_runtime_settings(tmp_path, section, field, value):
    cfg = deepcopy(BASE)
    cfg[section] = {field: value}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    args = argparse.Namespace(config=str(path), now="2026-11-02T15:00:00", dry_run=True)
    assert cli.cmd_check(args) == 1
    with pytest.raises(SystemExit):
        cli.cmd_watch(args)
    with pytest.raises(SystemExit):
        cli.cmd_announce(args)


@pytest.mark.parametrize("value", [[], ["10:00"], ["10:00", "10:00"], ["10:30", "23:00"], ["25:00", "23:00"]])
def test_invalid_active_hours(value):
    with pytest.raises(ValueError):
        validate_config({**BASE, "active_hours": value})


@pytest.mark.parametrize("value", [0, 60, True, "5", 1.5])
def test_invalid_interval(value):
    with pytest.raises(ValueError):
        validate_config({**BASE, "scan_interval_minutes": value})


def test_cross_midnight_and_minimal_config_are_valid():
    validate_config(BASE)
    validate_config({**BASE, "active_hours": ["22:00", "06:00"]})
