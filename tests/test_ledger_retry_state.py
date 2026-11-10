from __future__ import annotations
import csv
import io
import json
import random
import sqlite3
from pathlib import Path
import pytest
from fillbot.errors import BotError, ClientError, ScriptError, ServerError
from fillbot.ledger.db import Ledger
from fillbot.ledger.report import build_summary, export_summary, print_summary, render_summary
from fillbot.models import RunRecord, StepResult, StepStatus, TaskRecord, TaskStatus, iso_now
from fillbot.runner.retry import RetryPolicy, run_with_retry
from fillbot.runner.state import RunStateMachine, TaskStateMachine, plan_resume
def _run(run_id: str = "R-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        started_at=iso_now(),
        tasks_total=3,
        task_source="/tmp/tasks.json",
        script_source="/tmp/script.yaml",
        base_url="http://127.0.0.1:5000",
    )
def _task(task_id: str, biz_key: str) -> TaskRecord:
    return TaskRecord(task_id=task_id, biz_key=biz_key, payload={}, source_row=1)
def _finish(ledger: Ledger, task: str, status: str, **values: object) -> None:
    ledger.finish_task("R-1", task, status=status, finished_at=iso_now(), duration_ms=1, attempts=1, **values)
def test_ledger_dao_and_duplicate_receipt(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    with sqlite3.connect(ledger.path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables == {"runs", "tasks", "steps"}
    ledger.create_run(_run())
    assert ledger.latest_run_id() == "R-1" and ledger.get_run("missing") is None
    ledger.start_task("R-1", _task("T-1", "C1:q"), iso_now(), "/evidence/T-1")
    step = StepResult(1, "open", StepStatus.OK, iso_now(), iso_now(), 3)
    ledger.record_step("R-1", "T-1", step)
    ledger.record_step("R-1", "T-1", step)
    assert len(ledger.list_steps("R-1", "T-1")) == 1
    _finish(ledger, "T-1", "success", receipt_no="RCP-20260101-0001")
    assert ledger.has_successful_biz_key("C1:q") and not ledger.has_successful_biz_key("C1:other")
    assert ledger.receipt_exists("RCP-20260101-0001") and ledger.receipt_count() == 1
    assert ledger.task_status("R-1", "T-1") == "success"
    for task_id, key, status in (("T-2", "C2:q", "skipped"), ("T-3", "C3:q", "failed")):
        ledger.start_task("R-1", _task(task_id, key), iso_now(), f"/evidence/{task_id}")
        _finish(ledger, task_id, status, error_class="CLIENT" if status == "failed" else None, error_message="400" if status == "failed" else None)
    run = _run()
    run.status, run.finished_at, run.succeeded, run.failed, run.skipped = type(run.status)("failed"), iso_now(), 1, 1, 1
    ledger.finish_run(run)
    assert [row.status for row in ledger.list_tasks("R-1")] == ["success", "skipped", "failed"]
    assert len(ledger.list_steps("R-1")) == 1 and ledger.get_run("R-1")["status"] == "failed"
    ledger.start_task("R-1", _task("T-4", "C4:q"), iso_now(), "/evidence/T-4")
    with pytest.raises(ScriptError, match="duplicate receipt"):
        _finish(ledger, "T-4", "success", receipt_no="RCP-20260101-0001")
    ledger.start_task("R-1", _task("T-1", "C1:q"), iso_now(), "e")
    assert ledger.list_steps("R-1", "T-1") == []
def test_summary_export_and_print(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.create_run(_run())
    entries = [("T-1", "success", "RCP-20260101-0001", None), ("T-2", "skipped", None, None), ("T-3", "failed", None, "ASSERT")]
    for index, (task_id, status, receipt, error_class) in enumerate(entries, start=1):
        ledger.start_task("R-1", _task(task_id, f"C{index}:q"), iso_now(), f"e/{task_id}")
        ledger.finish_task(
            "R-1",
            task_id,
            status=status,
            finished_at=iso_now(),
            duration_ms=index * 10,
            attempts=1,
            error_class=error_class,
            error_message="failed" if error_class else None,
            receipt_no=receipt,
        )
    ledger.finish_run(_run())
    summary = build_summary(ledger, "R-1")
    assert (summary.tasks_total, summary.succeeded, summary.failed, summary.skipped) == (3, 1, 1, 1)
    assert summary.failure_reasons == {"ASSERT": 1} and summary.average_duration_ms == 20.0
    assert summary.receipts == ["RCP-20260101-0001"]
    json_path, csv_path = export_summary(ledger, "R-1", tmp_path / "out")
    assert json.loads(json_path.read_text(encoding="utf-8"))["failure_reasons"] == {"ASSERT": 1}
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = dict(csv.reader(handle))
    assert rows["succeeded"] == "1" and rows["failure_reason:ASSERT"] == "1"
    assert "T-1 [success]" in render_summary(ledger, "R-1")
    stream = io.StringIO()
    print_summary(ledger, "R-1", stream)
    assert "receipts:" in stream.getvalue()
    with pytest.raises(ScriptError, match="not found"):
        build_summary(ledger, "missing")
def test_retry_policy_paths() -> None:
    sleeps: list[float] = []
    policy = RetryPolicy(3, 1.0, 4.0, True, rng=random.Random(1), sleeper=sleeps.append)
    assert 0.75 <= policy.compute_delay(1) <= 1.25 and 1.5 <= policy.compute_delay(2) <= 2.5
    assert policy.compute_delay(9) <= 4.0 and policy.compute_delay(1, ServerError("x", retry_after=7.5)) == 7.5
    assert RetryPolicy(base_seconds=1, max_seconds=4, jitter=False).compute_delay(3) == 4.0
    attempts, slept, hook = [], [], []
    def operation() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise ServerError("first 500", status=500)
        return "ok"
    outcome = run_with_retry(operation, RetryPolicy(2, 0.1, 1, jitter=False, sleeper=slept.append), on_retry=lambda *args: hook.append((args[0], args[1].category.value, args[2])))
    assert (outcome.value, outcome.attempts, slept, hook) == ("ok", 2, [0.1], [(1, "SERVER", 0.1)])
    calls = [0]
    def client_failure() -> None:
        calls[0] += 1
        raise ClientError("bad", status=400)
    with pytest.raises(ClientError):
        run_with_retry(client_failure, RetryPolicy(3, 0.1, sleeper=slept.append))
    assert calls[0] == 1
    with pytest.raises(ValueError):
        run_with_retry(lambda: (_ for _ in ()).throw(ValueError("bug")), RetryPolicy(3))
    limited = [0]
    def always_limited() -> None:
        limited[0] += 1
        raise ServerError("limited", status=429, retry_after=3.0)
    with pytest.raises(BotError):
        run_with_retry(always_limited, RetryPolicy(3, 5, 10, jitter=False, sleeper=sleeps.append))
    assert limited[0] == 3 and sleeps[-2:] == [3.0, 3.0]
    for arguments in ({"max_attempts": 0}, {"base_seconds": -1}, {"base_seconds": 2, "max_seconds": 1}):
        with pytest.raises(ValueError):
            RetryPolicy(**arguments)
def test_state_machines_and_resume_plan(tmp_path: Path) -> None:
    machine = TaskStateMachine()
    for status in (TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.RUNNING, TaskStatus.SUCCESS):
        machine.transition(status)
    assert machine.history[-1] == TaskStatus.SUCCESS
    with pytest.raises(ScriptError, match="invalid task transition"):
        machine.transition(TaskStatus.RUNNING)
    run = RunStateMachine()
    for status in ("interrupted", "running", "failed", "running", "success"):
        run.transition(status)
    with pytest.raises(ScriptError, match="invalid run transition"):
        run.transition("running")
    with pytest.raises(ScriptError, match="invalid initial state"):
        RunStateMachine("nonsense")
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.create_run(_run("R1"))
    tasks = [_task("T-1", "C1:q"), _task("T-2", "C2:q"), _task("T-3", "C3:q")]
    for index, (task, status) in enumerate(zip(tasks, ["success", "skipped", "failed"]), start=1):
        ledger.start_task("R1", task, iso_now(), "evidence")
        ledger.finish_task("R1", task.task_id, status=status, finished_at=iso_now(), duration_ms=1, attempts=1, receipt_no=f"RCP-2026010{index}-000{index}" if status == "success" else None)
    plan = plan_resume(ledger, "R1", tasks)
    assert plan.completed_task_ids == ["T-1", "T-2"] and [task.task_id for task in plan.remaining] == ["T-3"]
    assert plan.run_id == "R1" and not plan.is_complete and plan_resume(ledger, "R1", []).is_complete
    with pytest.raises(ScriptError, match="not found"):
        plan_resume(ledger, "missing", tasks)
