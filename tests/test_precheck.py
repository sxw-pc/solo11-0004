"""Tests for the offline cross-field precheck module (ruleset, engine, CLI)."""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any, Callable
import pytest
import yaml
from fillbot.cli import main
from fillbot.errors import ConfigError
from fillbot.ledger.db import Ledger
from fillbot.models import TaskRecord
from fillbot.precheck import load_ruleset, parse_param_overrides, run_precheck, validate_ruleset

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "quarterly_report.yaml"
EXAMPLE_RULES = ROOT / "configs" / "precheck.example.yaml"
EXAMPLE_TASKS = ROOT / "tasks" / "example_batch.json"
VALID_TASK = ROOT / "tests" / "fixtures" / "valid_task.json"
ATTACHMENT = ROOT / "tests" / "fixtures" / "attachment.pdf"


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": "parkco-b",
        "password": "mock-only-beta",
        "company_id": "C002",
        "company_name": "青岚谷生态科技（虚构）",
        "period": "2025-Q4",
        "contact_name": "林小岚（虚构）",
        "report_date": "2025-11-10",
        "electricity": 12800.5,
        "water": 340.2,
        "gas": 88.1,
        "employees": 42,
        "hires": 3,
    }
    payload.update(overrides)
    return payload


def make_task(task_id: str = "T-1", biz_key: str = "C002:2025-Q4", **overrides: Any) -> TaskRecord:
    return TaskRecord(task_id, biz_key, _payload(**overrides))


def ruleset(*rules: dict[str, Any], params: dict[str, Any] | None = None, overrides: dict[str, Any] | None = None):
    data: dict[str, Any] = {"name": "test-rules", "version": 1, "rules": list(rules)}
    if params is not None:
        data["params"] = params
    return validate_ruleset(data, param_overrides=overrides)


def write_rules(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def write_tasks(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path


def task_entry(task_id: str, biz_key: str, **overrides: Any) -> dict[str, Any]:
    return {"task_id": task_id, "biz_key": biz_key, "attachments": [str(ATTACHMENT)], "payload": _payload(**overrides)}


def write_run_config(tmp_path: Path, base_url: str) -> Path:
    path = tmp_path / "bot.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "base_url": base_url,
                "rate_limit_per_minute": 6000,
                "timeout_seconds": 5,
                "backoff_base_seconds": 0.01,
                "backoff_max_seconds": 0.05,
                "evidence_dir": str(tmp_path / "evidence"),
                "ledger_path": str(tmp_path / "data" / "ledger.db"),
                "runs_dir": str(tmp_path / "runs"),
            }
        )
    )
    return path


# --- rule type behaviour ----------------------------------------------------
def test_numeric_threshold_bounds_and_bad_values() -> None:
    rs = ruleset({"id": "cap", "type": "numeric_threshold", "field": "electricity", "min": 100, "max": 20000})
    assert run_precheck([make_task()], rs).ok
    high = run_precheck([make_task(electricity=20000.5)], rs)
    assert [i.message for i in high.issues] == ["electricity=20000.5 exceeds maximum 20000"]
    low = run_precheck([make_task(electricity="50")], rs)
    assert "below minimum 100" in low.issues[0].message
    for bad in (None, "abc", True):
        report = run_precheck([make_task(electricity=bad)], rs)
        assert len(report.issues) == 1 and "missing or not numeric" in report.issues[0].message


def test_field_relation_field_and_constant() -> None:
    rs = ruleset({"id": "rel", "type": "field_relation", "left": "hires", "op": "<=", "right_field": "employees"})
    assert run_precheck([make_task()], rs).ok
    report = run_precheck([make_task(hires=50, employees=10)], rs)
    assert [i.message for i in report.issues] == ["hires=50 must be <= employees=10"]
    constant = ruleset({"id": "rel2", "type": "field_relation", "left": "employees", "op": ">", "right_value": 0})
    assert run_precheck([make_task()], constant).ok
    assert not run_precheck([make_task(employees=0)], constant).ok
    missing = run_precheck([make_task(employees=None)], rs)
    assert "employees" in missing.issues[0].message and "not numeric" in missing.issues[0].message


def test_logical_association_require_and_expect() -> None:
    rs = ruleset(
        {
            "id": "assoc",
            "type": "logical_association",
            "when": {"field": "employees", "op": ">", "value": 100},
            "then": [{"require": "contact_phone"}, {"field": "hires", "op": "<=", "value": 10}],
        }
    )
    assert run_precheck([make_task()], rs).ok  # condition false -> no requirements
    assert run_precheck([make_task(employees=500, contact_phone="13800000000", hires=5)], rs).ok
    report = run_precheck([make_task(employees=500, hires=50)], rs)
    assert len(report.issues) == 2
    assert "'contact_phone' is required when employees > 100" in report.issues[0].message
    assert "expected hires <= 10 when employees > 100, got 50" in report.issues[1].message


def test_period_match_all_selectors() -> None:
    rs = ruleset(
        {
            "id": "period",
            "type": "period_match",
            "period_field": "period",
            "date_field": "report_date",
            "biz_key_suffix": True,
            "allowed": ["2025-Q3", "2025-Q4"],
            "not_before": "2025-Q1",
            "not_after": "2025-Q4",
        }
    )
    assert run_precheck([make_task()], rs).ok
    wrong_quarter = run_precheck([make_task(report_date="2025-10-01", period="2025-Q3")], rs)
    assert any("falls in 2025-Q4, not period=2025-Q3" in i.message for i in wrong_quarter.issues)
    bad_key = run_precheck([make_task(biz_key="C002:2025-Q3")], rs)
    assert any("does not end with ':2025-Q4'" in i.message for i in bad_key.issues)
    not_allowed = run_precheck([make_task(period="2024-Q4", report_date="2024-11-01", biz_key="C002:2024-Q4")], rs)
    assert any("not in allowed periods" in i.message for i in not_allowed.issues)
    too_late = run_precheck([make_task(period="2026-Q1", report_date="2026-02-01", biz_key="C002:2026-Q1")], rs)
    assert any("is after 2025-Q4" in i.message for i in too_late.issues)
    invalid = run_precheck([make_task(period="Q4-2025")], rs)
    assert "not a valid period" in invalid.issues[0].message
    bad_date = run_precheck([make_task(report_date="11/10/2025")], rs)
    assert any("not a valid date" in i.message for i in bad_date.issues)


def test_value_mapping_and_unmapped_policy() -> None:
    rule = {
        "id": "mapping",
        "type": "value_mapping",
        "field": "company_id",
        "map": {"C002": {"username": ["parkco-b"]}},
    }
    assert run_precheck([make_task()], ruleset(rule)).ok
    report = run_precheck([make_task(username="parkco-c")], ruleset(rule))
    assert "username='parkco-c' is not allowed for company_id='C002'" in report.issues[0].message
    unmapped = run_precheck([make_task(company_id="C009")], ruleset(rule))
    assert "no mapping defined" in unmapped.issues[0].message
    ignored = ruleset({**rule, "on_unmapped": "ignore"})
    assert run_precheck([make_task(company_id="C009")], ignored).ok


def test_aggregate_sum_bounds() -> None:
    rs = ruleset({"id": "sum", "type": "aggregate_sum", "fields": ["electricity", "water", "gas"], "max": 14000})
    assert run_precheck([make_task()], rs).ok  # 13228.8
    report = run_precheck([make_task(electricity=13900)], rs)
    assert "sum(electricity+water+gas)=14328.3 exceeds maximum 14000" in report.issues[0].message
    missing = run_precheck([make_task(water=None)], rs)
    assert "water" in missing.issues[0].message and "for sum" in missing.issues[0].message


def test_batch_unique_flags_all_members() -> None:
    rs = ruleset({"id": "uniq", "type": "batch_unique", "fields": ["company_id", "period"]})
    tasks = [make_task("T-1"), make_task("T-2"), make_task("T-3", company_id="C003")]
    report = run_precheck(tasks, rs)
    assert report.failed_task_ids == {"T-1", "T-2"}
    assert "also used by T-2" in report.issues[0].message and "also used by T-1" in report.issues[1].message
    assert run_precheck([make_task("T-1"), make_task("T-2", period="2025-Q3")], rs).ok


def test_logical_association_condition_operators() -> None:
    def assoc(**when: Any) -> Any:
        return ruleset(
            {"id": "c", "type": "logical_association", "when": when, "then": [{"require": "contact_phone"}]}
        )

    assert run_precheck([make_task()], assoc(field="contact_name", op="blank")).ok  # not blank -> no fire
    assert run_precheck([make_task()], assoc(field="contact_phone", op="blank")).issues  # missing -> fires
    assert run_precheck([make_task()], assoc(field="company_id", op="in", value=["C001", "C002"])).issues
    assert run_precheck([make_task()], assoc(field="company_id", op="not_in", value=["C001", "C002"])).ok
    assert run_precheck([make_task()], assoc(field="period", op="matches", value=r"2025-Q[24]")).issues
    assert run_precheck([make_task()], assoc(field="period", op="matches", value=r"2024-.*")).ok
    assert run_precheck([make_task()], assoc(field="employees", op="!=", value=42)).ok
    assert run_precheck([make_task()], assoc(field="employees", op="!=", value=41)).issues
    # ordering against a non-numeric actual never fires
    assert run_precheck([make_task(company_name=None)], assoc(field="company_name", op=">", value=3)).ok


def test_custom_message_wraps_detail() -> None:
    rs = ruleset({"id": "cap", "type": "numeric_threshold", "field": "electricity", "max": 10, "message": "用电量超限"})
    report = run_precheck([make_task()], rs)
    assert report.issues[0].message == "用电量超限 (electricity=12800.5 exceeds maximum 10)"


# --- aggregation, identity and performance ----------------------------------
def test_all_violations_aggregated_without_early_stop() -> None:
    rs = ruleset(
        {"id": "r1", "type": "numeric_threshold", "field": "electricity", "max": 100},
        {"id": "r2", "type": "field_relation", "left": "hires", "op": "<=", "right_field": "employees"},
        {"id": "r3", "type": "aggregate_sum", "fields": ["water", "gas"], "max": 10},
    )
    tasks = [make_task(f"T-{i}", hires=99, employees=1) for i in range(1, 4)]
    report = run_precheck(tasks, rs)
    assert len(report.issues) == 9  # 3 tasks x 3 failing rules, nothing dropped
    assert report.failed_task_ids == {"T-1", "T-2", "T-3"}
    for issue in report.issues:
        assert issue.task_id and issue.biz_key == "C002:2025-Q4" and issue.rule_id in {"r1", "r2", "r3"}
    assert report.as_dict()["violations"] == 9 and not report.ok


def test_example_ruleset_flags_known_bad_row() -> None:
    from fillbot.intake import load_and_validate_tasks

    rs = load_ruleset(EXAMPLE_RULES)
    assert len(rs.rules) == 8 and rs.name == "quarterly-precheck"
    report = run_precheck(load_and_validate_tasks(EXAMPLE_TASKS), rs)
    assert report.failed_task_ids == {"T-1002"}
    assert {issue.rule_id for issue in report.issues} == {"large-employer-needs-phone", "period-consistency"}


def test_performance_1000_tasks_under_3_seconds() -> None:
    rs = ruleset(
        {"id": "r1", "type": "numeric_threshold", "field": "electricity", "max": 50000},
        {"id": "r2", "type": "field_relation", "left": "hires", "op": "<=", "right_field": "employees"},
        {
            "id": "r3",
            "type": "logical_association",
            "when": {"field": "employees", "op": ">", "value": 100},
            "then": [{"require": "contact_phone"}],
        },
        {"id": "r4", "type": "period_match", "period_field": "period", "date_field": "report_date"},
        {"id": "r5", "type": "value_mapping", "field": "company_id", "map": {"C002": {"username": ["parkco-b"]}}},
        {"id": "r6", "type": "aggregate_sum", "fields": ["electricity", "water", "gas"], "max": 60000},
        {"id": "r7", "type": "batch_unique", "fields": ["serial"]},
    )
    tasks = [make_task(f"T-{i}", serial=f"S-{i}") for i in range(1000)]
    report = run_precheck(tasks, rs)
    assert report.ok and report.tasks_total == 1000 and report.rules_total == 7
    assert report.duration_ms < 3000


# --- dynamic params ---------------------------------------------------------
def test_params_defaults_overrides_and_types() -> None:
    data = {
        "name": "p",
        "params": {"cap": 100, "periods": ["2025-Q4"]},
        "rules": [
            {"id": "cap", "type": "numeric_threshold", "field": "electricity", "max": "{{params.cap}}"},
            {"id": "period", "type": "period_match", "period_field": "period", "allowed": "{{params.periods}}"},
            {"id": "msg", "type": "numeric_threshold", "field": "water", "max": 1, "message": "上限{{params.cap}}吨"},
        ],
    }
    rs = validate_ruleset(data)
    assert rs.rules[0].config["max"] == 100.0 and isinstance(rs.rules[0].config["max"], float)
    assert rs.rules[1].config["allowed"] == ["2025-Q4"]
    assert rs.rules[2].message == "上限100吨"
    overridden = validate_ruleset(data, param_overrides={"cap": 5})
    assert overridden.rules[0].config["max"] == 5.0
    report = run_precheck([make_task()], overridden)
    assert any(issue.rule_id == "cap" for issue in report.issues)


def test_undefined_param_and_unused_override_errors() -> None:
    with pytest.raises(ConfigError, match=r"rule 'cap'.*undefined param.*params\.cap"):
        validate_ruleset({"name": "x", "rules": [{"id": "cap", "type": "numeric_threshold", "field": "a", "max": "{{params.cap}}"}]})
    with pytest.raises(ConfigError, match="not declared or referenced"):
        validate_ruleset(
            {"name": "x", "rules": [{"id": "cap", "type": "numeric_threshold", "field": "a", "max": 1}]},
            param_overrides={"typo_param": 1},
        )


def test_parse_param_overrides() -> None:
    assert parse_param_overrides(["cap=50000", "periods=[2025-Q3, 2025-Q4]", "name=text"]) == {
        "cap": 50000,
        "periods": ["2025-Q3", "2025-Q4"],
        "name": "text",
    }
    with pytest.raises(ConfigError, match="NAME=VALUE"):
        parse_param_overrides(["no-separator"])
    with pytest.raises(ConfigError, match="must match"):
        parse_param_overrides(["1bad=1"])


# --- ruleset loading and error reporting ------------------------------------
def test_yaml_syntax_error_reports_file_and_line(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: x\nrules:\n  - id: a\n    type: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_ruleset(path)
    message = str(excinfo.value)
    assert str(path) in message and "line" in message and "column" in message


def test_ruleset_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="precheck rules file not found"):
        load_ruleset(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "data,fragment",
    [
        ([1, 2], "top level must be a mapping"),
        ({"rules": [{"id": "a", "type": "numeric_threshold", "field": "x", "max": 1}]}, "name is required"),
        ({"name": "x", "version": 2, "rules": [{"id": "a", "type": "numeric_threshold", "field": "x", "max": 1}]}, "version 1"),
        ({"name": "x", "bogus": 1, "rules": [{"id": "a", "type": "numeric_threshold", "field": "x", "max": 1}]}, "unknown top-level"),
        ({"name": "x", "params": [1], "rules": [{"id": "a", "type": "numeric_threshold", "field": "x", "max": 1}]}, "params must be a mapping"),
        ({"name": "x", "rules": []}, "non-empty list"),
        ({"name": "x", "rules": [["not-a-mapping"]]}, "must be a mapping"),
        ({"name": "x", "rules": [{"type": "numeric_threshold", "field": "x", "max": 1}]}, "id must be a non-empty string"),
        ({"name": "x", "rules": [{"id": "a", "type": "nope"}]}, "unknown type 'nope'.*available types"),
        ({"name": "x", "rules": [{"id": "a", "type": "numeric_threshold", "field": "x", "max": 1, "zz": 2}]}, "unknown key"),
        ({"name": "x", "rules": [{"id": "a", "type": "numeric_threshold", "max": 1}]}, "missing required key.*field"),
        ({"name": "x", "rules": [{"id": "a", "type": "numeric_threshold", "field": "x"}]}, "min/max"),
        ({"name": "x", "rules": [{"id": "a", "type": "numeric_threshold", "field": "x", "min": 5, "max": 1}]}, "min cannot exceed max"),
        ({"name": "x", "rules": [{"id": "a", "type": "field_relation", "left": "x", "op": "~~", "right_value": 1}]}, "op must be one of"),
        ({"name": "x", "rules": [{"id": "a", "type": "field_relation", "left": "x", "op": "<", "right_field": "y", "right_value": 1}]}, "exactly one"),
        ({"name": "x", "rules": [{"id": "a", "type": "logical_association", "when": {"field": "x", "op": "??", "value": 1}, "then": [{"require": "y"}]}]}, "op must be one of"),
        ({"name": "x", "rules": [{"id": "a", "type": "logical_association", "when": {"field": "x", "op": "==", "value": 1}, "then": []}]}, "then must be a non-empty list"),
        ({"name": "x", "rules": [{"id": "a", "type": "period_match", "period_field": "period"}]}, "at least one of"),
        ({"name": "x", "rules": [{"id": "a", "type": "period_match", "period_field": "period", "allowed": ["Q4"]}]}, "must be a period"),
        ({"name": "x", "rules": [{"id": "a", "type": "value_mapping", "field": "x", "map": {}}]}, "non-empty mapping"),
        ({"name": "x", "rules": [{"id": "a", "type": "value_mapping", "field": "x", "map": {"A": {"y": []}}}]}, "non-empty list"),
        ({"name": "x", "rules": [{"id": "a", "type": "aggregate_sum", "fields": [], "max": 1}]}, "non-empty list"),
        ({"name": "x", "rules": [{"id": "a", "type": "batch_unique", "fields": "x"}]}, "non-empty list"),
        (
            {
                "name": "x",
                "rules": [
                    {"id": "a", "type": "numeric_threshold", "field": "x", "max": 1},
                    {"id": "a", "type": "numeric_threshold", "field": "y", "max": 1},
                ],
            },
            "duplicate rule id",
        ),
    ],
)
def test_ruleset_schema_errors(data: Any, fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment):
        validate_ruleset(data)


def test_schema_error_names_source_file(tmp_path: Path) -> None:
    path = write_rules(tmp_path, {"name": "x", "rules": [{"id": "a", "type": "nope"}]})
    with pytest.raises(ConfigError, match=rf"precheck rules {path}.*rule 'a'.*unknown type"):
        load_ruleset(path)


# --- CLI: standalone precheck command ---------------------------------------
def test_cli_precheck_pass_fail_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rules = write_rules(
        tmp_path,
        {
            "name": "cli-rules",
            "params": {"cap": 20000},
            "rules": [
                {"id": "cap", "type": "numeric_threshold", "field": "electricity", "max": "{{params.cap}}"},
                {"id": "rel", "type": "field_relation", "left": "hires", "op": "<=", "right_field": "employees"},
            ],
        },
    )
    assert main(["precheck", "--tasks", str(VALID_TASK), "--rules", str(rules)]) == 0
    out = capsys.readouterr().out
    assert "precheck OK" in out and "no network requests" in out

    bad = write_tasks(tmp_path, [task_entry("T-1", "C002:2025-Q4", hires=99, employees=1)])
    assert main(["precheck", "--tasks", str(bad), "--rules", str(rules)]) == 1
    out = capsys.readouterr().out
    assert "precheck FAILED" in out and "T-1 (biz_key=C002:2025-Q4) [rel]" in out

    assert main(["precheck", "--tasks", str(bad), "--rules", str(rules), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["violations"] == 1 and report["tasks_failed"] == 1
    issue = report["issues"][0]
    assert issue["task_id"] == "T-1" and issue["biz_key"] == "C002:2025-Q4" and issue["rule_id"] == "rel"

    assert main(["precheck", "--tasks", str(bad), "--rules", str(rules), "--param", "cap=1"]) == 1
    out = capsys.readouterr().out
    assert "[cap]" in out and "[rel]" in out  # param override took effect, both rules fired


def test_cli_precheck_error_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["precheck", "--tasks", str(VALID_TASK), "--rules", str(tmp_path / "none.yaml")]) == 2
    assert "not found" in capsys.readouterr().err
    broken = tmp_path / "broken.yaml"
    broken.write_text("rules: [unclosed\n", encoding="utf-8")
    assert main(["precheck", "--tasks", str(VALID_TASK), "--rules", str(broken)]) == 2
    err = capsys.readouterr().err
    assert str(broken) in err and "line" in err
    rules = write_rules(tmp_path, {"name": "x", "rules": [{"id": "a", "type": "numeric_threshold", "field": "x", "max": 1}]})
    assert main(["precheck", "--tasks", str(tmp_path / "missing.json"), "--rules", str(rules)]) == 2
    assert main(["precheck", "--tasks", str(VALID_TASK), "--rules", str(rules), "--param", "oops"]) == 2
    assert "NAME=VALUE" in capsys.readouterr().err


def test_cli_precheck_end_to_end_performance(tmp_path: Path) -> None:
    entries = [task_entry(f"T-{i}", f"C002:2025-Q4-{i:04d}", serial=f"S-{i}") for i in range(1000)]
    tasks = write_tasks(tmp_path, entries)
    rules = write_rules(
        tmp_path,
        {
            "name": "perf",
            "rules": [
                {"id": "r1", "type": "numeric_threshold", "field": "electricity", "max": 50000},
                {"id": "r2", "type": "field_relation", "left": "hires", "op": "<=", "right_field": "employees"},
                {
                    "id": "r3",
                    "type": "logical_association",
                    "when": {"field": "employees", "op": ">", "value": 100},
                    "then": [{"require": "contact_phone"}],
                },
                {"id": "r4", "type": "period_match", "period_field": "period", "date_field": "report_date"},
                {"id": "r5", "type": "value_mapping", "field": "company_id", "map": {"C002": {"username": ["parkco-b"]}}},
                {"id": "r6", "type": "aggregate_sum", "fields": ["electricity", "water", "gas"], "max": 60000},
                {"id": "r7", "type": "batch_unique", "fields": ["serial"]},
            ],
        },
    )
    started = time.monotonic()
    code = main(["precheck", "--tasks", str(tasks), "--rules", str(rules)])
    elapsed = time.monotonic() - started
    assert code == 0 and elapsed < 3.0


# --- CLI: run integration (blocked by default, warn-only opt-out) -----------
def test_run_precheck_blocks_failing_tasks(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_run_config(tmp_path, serve().base_url)
    tasks = write_tasks(
        tmp_path,
        [
            task_entry("T-1", "C002:2025-Q4"),
            task_entry("T-2", "C003:2025-Q4", username="parkco-c", password="mock-only-gamma", hires=99, employees=1),
        ],
    )
    rules = write_rules(
        tmp_path,
        {"name": "gate", "rules": [{"id": "rel", "type": "field_relation", "left": "hires", "op": "<=", "right_field": "employees"}]},
    )
    code = main(["run", "--tasks", str(tasks), "--script", str(SCRIPT), "--config", str(config), "--precheck", str(rules)])
    assert code == 0
    out = capsys.readouterr().out
    assert "precheck blocked 1 task(s); 1 task(s) proceed" in out
    ledger = Ledger(tmp_path / "data" / "ledger.db")
    run_id = ledger.latest_run_id()
    rows = {row["task_id"]: row for row in ledger.list_task_rows(run_id)}
    assert set(rows) == {"T-1"} and rows["T-1"]["status"] == "success"  # T-2 never entered filling


def test_run_precheck_warn_only_and_all_blocked(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_run_config(tmp_path, serve().base_url)
    bad = task_entry("T-2", "C003:2025-Q4", username="parkco-c", password="mock-only-gamma", hires=99, employees=1)
    tasks = write_tasks(tmp_path, [bad])
    rules = write_rules(
        tmp_path,
        {"name": "gate", "rules": [{"id": "rel", "type": "field_relation", "left": "hires", "op": "<=", "right_field": "employees"}]},
    )
    base = ["run", "--tasks", str(tasks), "--script", str(SCRIPT), "--config", str(config)]
    assert main(base + ["--precheck", str(rules)]) == 2  # every task blocked -> nothing to run
    assert "nothing to run" in capsys.readouterr().err
    assert main(base + ["--precheck", str(rules), "--precheck-warn-only"]) == 0
    out = capsys.readouterr().out
    assert "ignored (--precheck-warn-only)" in out
    ledger = Ledger(tmp_path / "data" / "ledger.db")
    rows = ledger.list_task_rows(ledger.latest_run_id())
    assert [row["status"] for row in rows] == ["success"]  # warn-only let T-2 through


def test_run_param_requires_precheck(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_run_config(tmp_path, "http://127.0.0.1:1")
    tasks = write_tasks(tmp_path, [task_entry("T-1", "C002:2025-Q4")])
    base = ["run", "--tasks", str(tasks), "--script", str(SCRIPT), "--config", str(config)]
    assert main(base + ["--param", "cap=1"]) == 2
    assert "require --precheck" in capsys.readouterr().err
    assert main(base + ["--precheck-warn-only"]) == 2
    assert "require --precheck" in capsys.readouterr().err
