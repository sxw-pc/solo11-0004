from __future__ import annotations
import csv
import json
import re
from pathlib import Path
from typing import Any, Callable
import pytest
import yaml
from flask import Flask
from fillbot.cli import main
from fillbot.config import BotConfig, load_config
from fillbot.dsl.schema import load_script
from fillbot.errors import ConfigError
from fillbot.intake import load_and_validate_tasks
from fillbot.ledger.db import Ledger
from fillbot.mocksite.chaos import ChaosSettings
from fillbot.runner.scheduler import Runner
ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "quarterly_report.yaml"
VALID_TASK = ROOT / "tests" / "fixtures" / "valid_task.json"
ATTACHMENT = ROOT / "tests" / "fixtures" / "attachment.pdf"
RECEIPT_RE = re.compile(r"^RCP-\d{8}-\d{4}$")
def _load() -> tuple[list[Any], Any]:
    return load_and_validate_tasks(ROOT / "tasks" / "example_batch.json"), load_script(SCRIPT)
def _write_config(tmp_path: Path, base_url: str) -> Path:
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
def test_mixed_batch_evidence_idempotency_and_summary(serve: Callable[..., Any], make_config: Callable[..., Any]) -> None:
    live = serve(chaos=ChaosSettings(enabled=True, flaky_submit=True, flaky_company="C003"))
    config = make_config(live.base_url)
    tasks, script = _load()
    runner = Runner(config)
    run = runner.run(tasks, script, task_source="tasks/example_batch.json", script_source="scripts/quarterly_report.yaml")
    assert (run.status.value, run.succeeded, run.failed, run.skipped) == ("failed", 2, 1, 0)
    assert len(live.receipts) == 3
    rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(run.run_id)}
    assert rows["T-1002"]["error_class"] == "CLIENT" and RECEIPT_RE.fullmatch(rows["T-1001"]["receipt_no"])
    assert rows["T-1001"]["receipt_no"] != rows["T-1003"]["receipt_no"]
    final = next(item for item in runner.ledger.list_steps(run.run_id, "T-1003") if item["step_index"] == 26)
    assert final["status"] == "ok" and final["attempts"] == 2
    bad_step = next(item for item in runner.ledger.list_steps(run.run_id, "T-1002") if item["status"] == "failed")
    assert bad_step["error_class"] == "CLIENT"
    summary_dir = config.runs_dir / run.run_id
    data = json.loads((summary_dir / "summary.json").read_text(encoding="utf-8"))
    assert (data["tasks_total"], data["succeeded"], data["failed"], data["skipped"]) == (3, 2, 1, 0)
    assert data["failure_reasons"] == {"CLIENT": 1}
    with (summary_dir / "summary.csv").open(encoding="utf-8", newline="") as handle:
        csv_values = dict(csv.reader(handle))
    assert csv_values["failure_reason:CLIENT"] == "1" and (summary_dir / "run.log").is_file()
    evidence = config.evidence_dir / run.run_id
    assert (evidence / "T-1001" / "receipt.html").is_file() and not (evidence / "T-1002" / "receipt.html").exists()
    combined = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in evidence.rglob("*") if path.is_file())
    assert "mock-only-beta" not in combined and 'value="***"' in combined
    second = runner.run(tasks, script, task_source="tasks/example_batch.json", script_source="scripts/quarterly_report.yaml")
    second_rows = {row["task_id"]: row for row in runner.ledger.list_task_rows(second.run_id)}
    assert [second_rows[key]["status"] for key in ("T-1001", "T-1002", "T-1003")] == ["skipped", "failed", "skipped"]
    assert len(live.receipts) == 3
def test_dry_run_stops_before_final_submit(tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any]) -> None:
    live = serve()
    config = make_config(
        live.base_url,
        ledger_path=str(tmp_path / "dry" / "ledger.db"),
        evidence_dir=str(tmp_path / "dry" / "evidence"),
        runs_dir=str(tmp_path / "dry" / "runs"),
    )
    loaded, script = _load()
    tasks = [loaded[0], loaded[2]]
    run = Runner(config).run(tasks, script, dry_run=True)
    assert run.status.value == "dry_run" and live.receipts == ["RCP-20250115-0001"]
    evidence = config.evidence_dir / run.run_id
    for task in tasks:
        assert (evidence / task.task_id / "step-26.html").is_file() and not (evidence / task.task_id / "receipt.html").exists()
def test_cli_validate_run_and_report(tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "--tasks", str(VALID_TASK), "--script", str(SCRIPT)]) == 0
    assert "no network requests" in capsys.readouterr().out
    assert main(["validate", "--tasks", str(ROOT / "tests" / "fixtures" / "invalid_tasks.json"), "--script", str(SCRIPT)]) == 2
    assert "task validation error" in capsys.readouterr().err
    assert main(["validate", "--tasks", "/missing/tasks.json", "--script", str(SCRIPT)]) == 2
    assert main(["validate", "--tasks", str(VALID_TASK), "--script", "/missing/script.yaml"]) == 2
    live = serve()
    config = _write_config(tmp_path, live.base_url)
    code = main(["run", "--tasks", str(VALID_TASK), "--script", str(SCRIPT), "--config", str(config), "--rate-limit", "6000"])
    assert code == 0 and "T-9001" in capsys.readouterr().out
    ledger = Ledger(tmp_path / "data" / "ledger.db")
    run_id = ledger.latest_run_id()
    assert run_id is not None and ledger.get_run(run_id)["status"] == "success"
    assert main(["report", run_id, "--config", str(config)]) == 0 and "succeeded" in capsys.readouterr().out
    assert main(["report", "missing", "--config", str(config)]) == 2 and "run not found" in capsys.readouterr().err
    assert main(["run", "--tasks", str(VALID_TASK), "--script", str(SCRIPT), "--config", str(config), "--rate-limit", "0"]) == 2
def test_cli_resume_after_interruption(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path, serve().base_url)
    source = json.loads((ROOT / "tasks" / "example_batch.json").read_text(encoding="utf-8"))
    selected = [source[0], source[2]]
    for task in selected:
        task["attachments"] = [str(ATTACHMENT)]
    task_file = tmp_path / "two_tasks.json"
    task_file.write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
    original, calls = Runner._execute_task, []
    def crash_after_one(self: Runner, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        calls.append(args[1].task_id)
        if len(calls) == 1:
            raise SystemExit(23)
        return result
    monkeypatch.setattr(Runner, "_execute_task", crash_after_one)
    args = ["run", "--tasks", str(task_file), "--script", str(SCRIPT), "--config", str(config)]
    with pytest.raises(SystemExit):
        main(args)
    monkeypatch.setattr(Runner, "_execute_task", original)
    ledger, run_id = Ledger(tmp_path / "data" / "ledger.db"), None
    run_id = ledger.latest_run_id()
    assert run_id is not None and ledger.get_run(run_id)["status"] == "interrupted"
    assert main(["resume", run_id, "--config", str(config)]) == 0 and "succeeded" in capsys.readouterr().out
    assert ledger.get_run(run_id)["status"] == "success"
    assert len(set(ledger.list_task_rows(run_id)[i]["receipt_no"] for i in range(2))) == 2
def test_mock_serve_loopback_and_bad_port(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    called: dict[str, Any] = {}
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: called.update(kwargs))
    assert main(["mock-serve", "--port", "5099", "--chaos", "--chaos-seed", "2", "--delay-ms", "1"]) == 0
    assert called["host"] == "127.0.0.1" and called["port"] == 5099 and "127.0.0.1" in capsys.readouterr().out
    assert main(["mock-serve", "--port", "0"]) == 2
def test_config_precedence_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("base_url: http://127.0.0.1:1/\ntimeout_seconds: 3\njitter: 'no'\nmask_fields: [password]\n")
    config = load_config(path, overrides={"base_url": "http://127.0.0.1:2", "jitter": "yes"}, environ={"FILLBOT_RATE_LIMIT_PER_MINUTE": "30"})
    assert config.base_url == "http://127.0.0.1:2" and config.rate_limit_per_minute == 30 and config.jitter
    assert config.requests_per_second == 0.5 and config.public_dict()["ledger_path"] == str(config.ledger_path)
    assert config.with_overrides(jitter=None) is config and config.with_overrides(max_retries=1).max_retries == 1
    assert BotConfig().timeout_seconds == 10.0
    for text in ("x", "[1]", "base_url: ftp://x", "timeout_seconds: -1", "jitter: maybe", "unknown: 1", "mask_fields: x"):
        path.write_text(text if "\n" in text else f"{text if ':' in text else 'base_url: ' + text}\n")
        with pytest.raises(ConfigError):
            load_config(path, environ={})
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "none.yaml", environ={})
    with pytest.raises(ConfigError, match="unknown config override"):
        load_config(overrides={"nope": 1}, environ={})
    with pytest.raises(ConfigError, match="cannot exceed"):
        load_config(environ={"FILLBOT_BACKOFF_BASE_SECONDS": "2", "FILLBOT_BACKOFF_MAX_SECONDS": "1"})
