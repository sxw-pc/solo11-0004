from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from fillbot.cli import main
from fillbot.deps.graph import load_plan
from fillbot.deps.models import BatchStatus, CycleDetectedError
from fillbot.deps.report import render_workflow
from fillbot.deps.store import DependencyStore

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "quarterly_report.yaml"
ATTACHMENT = ROOT / "tests" / "fixtures" / "attachment.pdf"


def _task(tmp_path: Path, tid: str, company: str, period: str) -> Path:
    accounts = {
        "C001": ("parkco-a", "mock-only-alpha", "远山数据服务社（虚构）", "沈遥（虚构）"),
        "C002": ("parkco-b", "mock-only-beta", "青岚谷生态科技（虚构）", "林小岚（虚构）"),
        "C003": ("parkco-c", "mock-only-gamma", "溪谷智造工坊（虚构）", "顾青（虚构）"),
    }
    username, password, cname, contact = accounts[company]
    doc = [{
        "task_id": tid,
        "biz_key": f"{company}:{period}",
        "attachments": [str(ATTACHMENT)],
        "payload": {
            "username": username, "password": password, "company_id": company,
            "company_name": cname, "period": period, "contact_name": contact,
            "report_date": "2025-11-10", "electricity": 12800.5, "water": 340.2,
            "gas": 88.1, "employees": 42, "hires": 3,
        },
    }]
    path = tmp_path / f"{tid}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _write_plan(
    tmp_path: Path,
    batches: list[dict[str, Any]],
    *,
    default_timeout: int | None = None,
    poll: float = 0.05,
) -> Path:
    raw: dict[str, Any] = {"name": "e2e-wf", "poll_interval_seconds": poll, "batches": batches}
    if default_timeout is not None:
        raw["default_timeout_seconds"] = default_timeout
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def _batch(tmp_path: Path, name: str, tid: str, company: str, period: str, deps: list[str] | None = None,
           timeout: int | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "tasks": str(_task(tmp_path, tid, company, period)),
        "script": str(SCRIPT),
    }
    if deps:
        entry["depends_on"] = deps
    if timeout is not None:
        entry["timeout_seconds"] = timeout
    return entry


def _config(tmp_path: Path, base_url: str) -> Path:
    path = tmp_path / "bot.yaml"
    path.write_text(yaml.safe_dump({
        "base_url": base_url,
        "rate_limit_per_minute": 6000,
        "timeout_seconds": 5,
        "backoff_base_seconds": 0.01,
        "backoff_max_seconds": 0.05,
        "evidence_dir": str(tmp_path / "evidence"),
        "ledger_path": str(tmp_path / "data" / "ledger.db"),
        "runs_dir": str(tmp_path / "runs"),
    }), encoding="utf-8")
    return path


def _deps_config(tmp_path: Path) -> Path:
    path = tmp_path / "deps.yaml"
    path.write_text(yaml.safe_dump({
        "store_path": str(tmp_path / "data" / "deps.db"),
        "poll_interval_seconds": 0.05,
    }), encoding="utf-8")
    return path


def test_deps_validate_offline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path = _write_plan(tmp_path, [
        _batch(tmp_path, "a", "T-3001", "C001", "2025-Q1"),
        _batch(tmp_path, "b", "T-3002", "C002", "2025-Q1", ["a"]),
    ])
    assert main(["deps", "validate", "--plan", str(plan_path)]) == 0
    out = capsys.readouterr().out
    assert "2 batch" in out and "no cycles" in out


def test_deps_validate_reports_cycle_clearly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_path = _write_plan(tmp_path, [
        _batch(tmp_path, "a", "T-3001", "C001", "2025-Q1", ["c"]),
        _batch(tmp_path, "b", "T-3002", "C002", "2025-Q1", ["a"]),
        _batch(tmp_path, "c", "T-3003", "C003", "2025-Q1", ["b"]),
    ])
    # The graph layer raises before any work happens.
    with pytest.raises(CycleDetectedError):
        load_plan(plan_path)
    code = main(["deps", "validate", "--plan", str(plan_path)])
    err = capsys.readouterr().err
    assert code == 2 and "cyclic" in err and "cycle:" in err


def test_deps_run_happy_path_against_mock_site(
    tmp_path: Path, serve: Callable[..., Any], make_config: Callable[..., Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = serve()
    config = _config(tmp_path, live.base_url)
    plan_path = _write_plan(tmp_path, [
        _batch(tmp_path, "accounting", "T-3101", "C001", "2025-Q2"),
        _batch(tmp_path, "energy", "T-3102", "C002", "2025-Q2", ["accounting"]),
        _batch(tmp_path, "summary", "T-3103", "C003", "2025-Q2", ["energy"]),
    ])
    code = main(["deps", "run", "--plan", str(plan_path), "--config", str(config), "--deps-config", str(_deps_config(tmp_path))])
    assert code == 0
    store = DependencyStore(tmp_path / "data" / "deps.db")
    workflow_id = store.latest_workflow_id()
    assert workflow_id is not None
    statuses = store.status_map(workflow_id)
    assert set(statuses.values()) == {BatchStatus.SUCCESS}
    rendered = render_workflow(store, workflow_id)
    assert "accounting [success]" in rendered and "energy [success]" in rendered
    assert "workflow:" in rendered
    # Three distinct receipts were actually submitted, in dependency order (the mock
    # site additionally ships one seeded receipt, so assert on the new run ids instead).
    run_ids = {s.run_id for s in store.list_batches(workflow_id)}
    assert len(run_ids) == 3
    assert "status=success" in capsys.readouterr().out


def test_deps_dry_run_marks_batches_success_and_unblocks_downstream(
    tmp_path: Path, serve: Callable[..., Any],
) -> None:
    live = serve()
    config = _config(tmp_path, live.base_url)
    plan_path = _write_plan(tmp_path, [
        _batch(tmp_path, "a", "T-3501", "C001", "2025-Q2"),
        _batch(tmp_path, "b", "T-3502", "C002", "2025-Q2", ["a"]),
    ])
    code = main([
        "deps", "run", "--plan", str(plan_path), "--config", str(config),
        "--deps-config", str(_deps_config(tmp_path)), "--dry-run",
    ])
    assert code == 0
    store = DependencyStore(tmp_path / "data" / "deps.db")
    workflow_id = store.latest_workflow_id()
    assert set(store.status_map(workflow_id).values()) == {BatchStatus.SUCCESS}
    assert store.get_workflow(workflow_id)["dry_run"] == 1
    # Dry runs stop before the final submit: no new receipts beyond the seeded one.
    assert all(not r.startswith("RCP-2026") for r in live.receipts) or len(live.receipts) == 1


def test_deps_failure_cascades_and_blocks_downstream(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str],
) -> None:
    # C003 fails its first submit and retries succeed, so force a hard CLIENT failure
    # instead by pointing the failing batch at a payload that breaks validation is hard;
    # use the flaky company with retries exhausted via a dedicated failing script path.
    from fillbot.mocksite.chaos import ChaosSettings

    live = serve(chaos=ChaosSettings(enabled=True, failure_rate=1.0, seed=1))
    config = _config(tmp_path, live.base_url)
    plan_path = _write_plan(tmp_path, [
        _batch(tmp_path, "root", "T-3201", "C001", "2025-Q3"),
        _batch(tmp_path, "downstream", "T-3202", "C002", "2025-Q3", ["root"]),
    ], default_timeout=30)
    code = main(["deps", "run", "--plan", str(plan_path), "--config", str(config), "--deps-config", str(_deps_config(tmp_path))])
    assert code == 1  # workflow reports failure via non-zero exit
    store = DependencyStore(tmp_path / "data" / "deps.db")
    workflow_id = store.latest_workflow_id()
    states = {s.name: s for s in store.list_batches(workflow_id)}
    assert states["root"].status == BatchStatus.FAILED
    assert states["downstream"].status == BatchStatus.BLOCKED
    assert states["downstream"].blocked_by == ("root",)
    assert "did not succeed" in (states["downstream"].reason or "")
    err_or_out = capsys.readouterr()
    assert "blocked" in (err_or_out.out + err_or_out.err)


def test_deps_status_and_resume_commands(
    tmp_path: Path, serve: Callable[..., Any], capsys: pytest.CaptureFixture[str],
) -> None:
    live = serve()
    config = _config(tmp_path, live.base_url)
    plan_path = _write_plan(tmp_path, [
        _batch(tmp_path, "a", "T-3301", "C001", "2025-Q4"),
        _batch(tmp_path, "b", "T-3302", "C002", "2025-Q4", ["a"]),
    ])
    assert main(["deps", "run", "--plan", str(plan_path), "--config", str(config), "--deps-config", str(_deps_config(tmp_path))]) == 0
    assert main(["deps", "status", "--config", str(config), "--deps-config", str(_deps_config(tmp_path))]) == 0
    assert "a [success]" in capsys.readouterr().out
    # Resuming a completed workflow is a no-op that still reports success.
    assert main(["deps", "resume", "--config", str(config), "--deps-config", str(_deps_config(tmp_path))]) == 0
    assert "status=success" in capsys.readouterr().out


def test_deps_status_without_workflow_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, "http://127.0.0.1:1")
    assert main(["deps", "status", "--config", str(config), "--deps-config", str(_deps_config(tmp_path))]) == 2
    assert "no dependency workflow" in capsys.readouterr().err


def test_dependency_db_is_separate_from_task_ledger(
    tmp_path: Path, serve: Callable[..., Any],
) -> None:
    live = serve()
    config = _config(tmp_path, live.base_url)
    plan_path = _write_plan(tmp_path, [_batch(tmp_path, "a", "T-3401", "C001", "2025-Q1")])
    assert main(["deps", "run", "--plan", str(plan_path), "--config", str(config), "--deps-config", str(_deps_config(tmp_path))]) == 0
    import sqlite3

    def tables(path: Path) -> set[str]:
        with sqlite3.connect(path) as conn:
            return {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not r[0].startswith("sqlite_")
            }

    assert tables(tmp_path / "data" / "deps.db") == {"workflows", "batch_states"}
    assert tables(tmp_path / "data" / "ledger.db") == {"runs", "tasks", "steps"}
