"""Unit tests for the human confirmation gateway and gate state machine."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fillbot.browser.base import Page
from fillbot.errors import ConfirmationError
from fillbot.evidence.store import EvidenceStore
from fillbot.ledger.db import Ledger
from fillbot.models import RunRecord, TaskStatus, iso_now
from fillbot.runner.gateway import ConfirmationGateway, generate_token, hash_token, new_confirmation_id
from fillbot.runner.state import RunStateMachine, TaskStateMachine, plan_resume

HTML = """
<html><body><h1>请确认报送内容</h1>
<input name="password" value="mock-only-beta">
<span id="review-period">2025-Q4</span>
<form id="confirm-form"><button type="submit">确认并提交</button></form>
</body></html>
"""


def _gateway(tmp_path: Path) -> tuple[ConfirmationGateway, Ledger, EvidenceStore]:
    ledger = Ledger(tmp_path / "ledger.db")
    evidence = EvidenceStore(tmp_path / "evidence", mask_fields=("password", "token"))
    ledger.create_run(RunRecord(run_id="R-1", started_at=iso_now()))
    return ConfirmationGateway(ledger, evidence), ledger, evidence


def _pause(gateway: ConfirmationGateway) -> tuple[object, str]:
    page = Page(200, "http://127.0.0.1/report/confirm", {}, HTML, 5)
    paused = gateway.pause(
        run_id="R-1",
        task_id="T-1",
        biz_key="C002:2025-Q4",
        gate_step_index=26,
        page=page,
        filled_fields={"company_name": True, "electricity": True},
        mask_fields=("password",),
        secret_values=["mock-only-beta"],
    )
    return paused, paused.token


def test_token_helpers_are_unpredictable_and_stable_hash() -> None:
    token_a, token_b = generate_token(), generate_token()
    assert token_a != token_b and token_a.startswith("fbc-") and len(token_a) > 30
    assert hash_token(token_a) == hash_token(token_a) and hash_token(token_a) != hash_token(token_b)
    assert new_confirmation_id().startswith("CFM-")


def test_pause_persists_masked_snapshot_and_token_hash_only(tmp_path: Path) -> None:
    gateway, ledger, _ = _gateway(tmp_path)
    paused, token = _pause(gateway)
    record = paused.confirmation
    assert record.status == "awaiting_confirmation" and record.gate_step_index == 26
    # The plaintext token never reaches the ledger; only its hash and a short hint.
    with sqlite3.connect(ledger.path) as connection:
        rows = connection.execute("SELECT token_hash, token_hint FROM confirmations").fetchall()
    assert rows and rows[0][0] == hash_token(token) and token not in rows[0][0]
    assert token not in str(rows)
    snapshot = paused.snapshot_path.read_text(encoding="utf-8")
    assert "mock-only-beta" not in snapshot and "***" in snapshot and "2025-Q4" in snapshot
    meta = (paused.snapshot_path.parent / "confirmation.json").read_text(encoding="utf-8")
    assert '"filled_fields"' in meta and "company_name" in meta and "password" not in meta
    events = ledger.list_confirmation_events("R-1", "T-1")
    assert [event["event"] for event in events] == ["paused"]
    assert ledger.list_confirmations(status="awaiting_confirmation")[0]["task_id"] == "T-1"


def test_approve_consumes_token_once_and_audits(tmp_path: Path) -> None:
    gateway, ledger, _ = _gateway(tmp_path)
    _, token = _pause(gateway)
    record = gateway.decide(token, approve=True, actor="alice", note="looks good")
    assert record.status == "approved" and record.decided_by == "alice"
    with pytest.raises(ConfirmationError, match="already used"):
        gateway.decide(token, approve=True, actor="alice")
    events = ledger.list_confirmation_events("R-1", "T-1")
    assert [event["event"] for event in events] == ["paused", "approved", "spent_token"]
    assert events[-1]["actor"] == "alice"
    assert gateway.list_pending() == []


def test_reject_records_abandonment_decision(tmp_path: Path) -> None:
    gateway, ledger, _ = _gateway(tmp_path)
    _, token = _pause(gateway)
    record = gateway.decide(token, approve=False, actor="bob", note="wrong quarter")
    assert record.status == "rejected" and record.decision_note == "wrong quarter"
    events = ledger.list_confirmation_events("R-1", "T-1")
    assert [event["event"] for event in events] == ["paused", "rejected"]
    with pytest.raises(ConfirmationError, match="already used"):
        gateway.decide(token, approve=False, actor="bob")


def test_unknown_token_is_rejected_and_audited_without_leaking(tmp_path: Path) -> None:
    gateway, ledger, _ = _gateway(tmp_path)
    _, real = _pause(gateway)
    with pytest.raises(ConfirmationError, match="unknown confirmation token"):
        gateway.decide("fbc-totally-made-up", approve=True, actor="mallory")
    events = ledger.list_confirmation_events()
    assert events[-1]["event"] == "invalid_token" and real not in events[-1]["detail"]
    # The genuine token still works after a failed attempt.
    assert gateway.decide(real, approve=True, actor="alice").status == "approved"


def test_gate_state_machine_transitions() -> None:
    machine = TaskStateMachine()
    for target in (
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_CONFIRMATION,
        TaskStatus.RUNNING,
        TaskStatus.SUCCESS,
    ):
        machine.transition(target)
    assert machine.history[-1] == TaskStatus.SUCCESS
    awaiting = TaskStateMachine(TaskStatus.AWAITING_CONFIRMATION)
    awaiting.transition(TaskStatus.ABANDONED)
    with pytest.raises(Exception):
        awaiting.transition(TaskStatus.RUNNING)  # abandoned is terminal
    run = RunStateMachine()
    for target in ("awaiting_confirmation", "running", "success"):
        run.transition(target)
    assert run.status == "success"


def test_resume_plan_keeps_awaiting_and_abandoned_out_of_the_batch(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.create_run(RunRecord(run_id="R1", started_at=iso_now()))
    from fillbot.models import TaskRecord

    tasks = [
        TaskRecord(task_id="T-1", biz_key="C1:q", payload={}),
        TaskRecord(task_id="T-2", biz_key="C2:q", payload={}),
        TaskRecord(task_id="T-3", biz_key="C3:q", payload={}),
        TaskRecord(task_id="T-4", biz_key="C4:q", payload={}),
    ]
    statuses = {
        "T-1": "success",
        "T-2": "awaiting_confirmation",
        "T-3": "abandoned",
        "T-4": "failed",
    }
    for task in tasks:
        ledger.start_task("R1", task, iso_now(), "evidence")
        ledger.finish_task(
            "R1",
            task.task_id,
            status=statuses[task.task_id],
            finished_at=iso_now(),
            duration_ms=1,
            attempts=1,
            error_message="x" if task.task_id == "T-4" else None,
        )
    plan = plan_resume(ledger, "R1", tasks)
    # Only the plain failure is re-runnable; awaiting waits for a token and
    # abandoned is a terminal human decision.
    assert [task.task_id for task in plan.remaining] == ["T-2", "T-4"]
    assert plan.completed_task_ids == ["T-1", "T-3"]
