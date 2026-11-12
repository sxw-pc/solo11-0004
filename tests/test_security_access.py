"""Role/company access control, audit trail and fail-closed configuration tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from fillbot.errors import ScriptError
from fillbot.ledger.db import Ledger
from fillbot.models import RunRecord, TaskRecord, iso_now
from fillbot.security.access import AccessPolicy, Principal


def _task(task_id: str, company: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        biz_key=f"{company}:2025-Q4",
        payload={"company_id": company},
        source_row=1,
    )


def _seed(ledger: Ledger) -> None:
    ledger.create_run(RunRecord(run_id="R-1", started_at=iso_now()))
    for task_id, company in (("T-1", "C001"), ("T-2", "C002"), ("T-3", "C003")):
        ledger.start_task("R-1", _task(task_id, company), iso_now(), f"e/{task_id}")
        ledger.finish_task(
            "R-1", task_id, status="success", finished_at=iso_now(), duration_ms=5, attempts=1
        )


def test_principal_parsing() -> None:
    assert Principal.parse(None) is None
    principal = Principal.parse("alice:auditor,reader@C001,C002")
    assert principal.identity == "alice"
    assert principal.roles == ("auditor", "reader")
    assert principal.companies == ("C001", "C002")
    assert Principal.parse("bob") == Principal("bob")
    with pytest.raises(ValueError):
        Principal.parse(":role@C001")
    assert Principal.parse({"identity": "carol", "roles": ["admin"], "companies": ["*"]}).roles == ("admin",)


def test_default_open_mode_keeps_legacy_behaviour(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    _seed(ledger)
    assert ledger.access_enabled() is False
    # Anonymous / no-principal calls see everything, exactly like the old API.
    assert {row["task_id"] for row in ledger.guarded_task_rows("R-1", None)} == {"T-1", "T-2", "T-3"}
    assert len(ledger.guarded_list_tasks("R-1", None)) == 3
    # Open-mode access is still audited.
    events = ledger.list_audit(action="ledger.query")
    assert events and all(row["decision"] == "allow" for row in events)


def test_grants_filter_by_company_and_deny_by_default(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    _seed(ledger)
    ledger.upsert_grant("auditor", "C001", "read")
    ledger.upsert_grant("auditor", "C002", "read")
    ledger.set_access_enabled(True)
    principal = Principal("alice", ("auditor",))

    rows = ledger.guarded_task_rows("R-1", principal)
    assert {row["task_id"] for row in rows} == {"T-1", "T-2"}

    # Single-task authorization: same-company allowed, other company denied.
    allowed = ledger.authorize_task("R-1", "T-1", principal, "evidence.read")
    denied = ledger.authorize_task("R-1", "T-3", principal, "evidence.read")
    assert allowed.allowed and not denied.allowed and denied.reason.startswith("company")

    # A principal with no grants gets nothing (deny by default).
    stranger = Principal("mallory", ("nobody",))
    assert ledger.guarded_task_rows("R-1", stranger) == []
    decision = ledger.authorize_task("R-1", "T-1", stranger, "evidence.read")
    assert not decision.allowed and decision.reason == "no-grant-configured"


def test_wildcard_company_grant_and_admin_level(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    _seed(ledger)
    ledger.upsert_grant("admin", "*", "admin")
    ledger.set_access_enabled(True)
    principal = Principal("root", ("admin",))
    rows = ledger.guarded_task_rows("R-1", principal)
    assert {row["task_id"] for row in rows} == {"T-1", "T-2", "T-3"}
    decision = ledger.authorize_task("R-1", "T-3", principal, "evidence.read")
    assert decision.allowed and not decision.scoped


def test_enforcement_toggle_returns_to_open_default(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    _seed(ledger)
    ledger.upsert_grant("auditor", "C001", "read")
    ledger.set_access_enabled(True)
    assert len(ledger.guarded_task_rows("R-1", Principal("x", ("auditor",)))) == 1
    ledger.set_access_enabled(False)
    assert len(ledger.guarded_task_rows("R-1", Principal("x", ("nobody",)))) == 3


def test_grant_validation_and_crud(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    with pytest.raises(ScriptError):
        ledger.upsert_grant(" ", "C001", "read")
    with pytest.raises(ScriptError):
        ledger.upsert_grant("auditor", "C001", "superuser")
    ledger.upsert_grant("auditor", "C001", "read", note="Q4 review")
    # Upsert is idempotent on the uniqueness key.
    ledger.upsert_grant("auditor", "C001", "read", note="updated")
    grants = ledger.list_grants()
    assert len(grants) == 1 and grants[0]["note"] == "updated"
    assert ledger.revoke_grant("auditor", "C001", "read") == 1
    assert ledger.revoke_grant("auditor", "C001", "read") == 0


def test_audit_trail_records_every_decision(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    _seed(ledger)
    ledger.upsert_grant("auditor", "C001", "read")
    ledger.set_access_enabled(True)
    principal = Principal("alice", ("auditor",))
    ledger.authorize_task("R-1", "T-1", principal, "evidence.read")
    ledger.authorize_task("R-1", "T-2", principal, "evidence.read")
    ledger.guarded_steps("R-1", "T-1", principal)
    ledger.guarded_steps("R-1", "T-2", principal)
    assert ledger.audit_count() >= 4
    denies = ledger.list_audit(principal="alice", action="evidence.read")
    assert {row["decision"] for row in denies} == {"allow", "deny"}
    assert all(row["principal"] == "alice" for row in denies)
    # Audit rows never embed payload data.
    raw = " ".join(row["details_json"] + row["resource"] for row in ledger.list_audit(limit=100))
    assert "hunter" not in raw and "mock-only" not in raw


def test_policy_without_authorizer_runs_open_and_keeps_events_in_memory() -> None:
    policy = AccessPolicy(None)
    decision = policy.authorize(None, "evidence.read", company="C001")
    assert decision.allowed and decision.open_mode
    assert policy.pending_events() and policy.audit_failures() == []


def test_legacy_biz_key_backfills_company_for_old_databases(tmp_path: Path) -> None:
    # Simulate an old-schema database without the company_id column.
    import sqlite3

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, tasks_total INTEGER DEFAULT 0, succeeded INTEGER DEFAULT 0,
          failed INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, task_source TEXT DEFAULT '',
          script_source TEXT DEFAULT '', dry_run INTEGER DEFAULT 0, base_url TEXT DEFAULT '',
          evidence_dir TEXT DEFAULT '', options_json TEXT DEFAULT '{}');
        CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          task_id TEXT NOT NULL, biz_key TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT,
          finished_at TEXT, duration_ms INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
          error_class TEXT, error_message TEXT, receipt_no TEXT, evidence_dir TEXT DEFAULT '',
          UNIQUE(run_id, task_id));
        CREATE TABLE steps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          task_id TEXT NOT NULL, step_index INTEGER NOT NULL, action TEXT NOT NULL,
          status TEXT NOT NULL, started_at TEXT, finished_at TEXT, duration_ms INTEGER DEFAULT 0,
          attempts INTEGER DEFAULT 0, error_class TEXT, error_message TEXT, snapshot_path TEXT,
          UNIQUE(run_id, task_id, step_index));
        """
    )
    connection.execute("INSERT INTO runs(run_id, started_at, status) VALUES ('old', ?, 'success')", (iso_now(),))
    connection.execute(
        "INSERT INTO tasks(run_id, task_id, biz_key, status) VALUES ('old', 'T-old', 'C009:2024-Q1', 'success')"
    )
    connection.commit()
    connection.close()

    ledger = Ledger(db_path)  # migration runs on open
    assert ledger.task_company("old", "T-old") == "C009"
    rows = ledger.list_task_rows("old")
    assert rows[0]["company_id"] == "C009"
    # Old data is fully visible under the open default.
    assert ledger.guarded_task_rows("old", None)[0]["task_id"] == "T-old"
