"""SQLite ledger: runs, tasks and steps, with a global receipt uniqueness rule."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from fillbot.errors import ScriptError
from fillbot.models import RunRecord, StepResult, TaskRecord, TaskSummary
from fillbot.security.access import (
    WILDCARD,
    AccessDecision,
    AccessPolicy,
    AuditEvent,
    Principal,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  tasks_total INTEGER NOT NULL DEFAULT 0, succeeded INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0, task_source TEXT NOT NULL DEFAULT '', script_source TEXT NOT NULL DEFAULT '',
  dry_run INTEGER NOT NULL DEFAULT 0, base_url TEXT NOT NULL DEFAULT '', evidence_dir TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL, biz_key TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT, finished_at TEXT, duration_ms INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0, error_class TEXT, error_message TEXT, receipt_no TEXT,
  evidence_dir TEXT NOT NULL DEFAULT '', UNIQUE(run_id, task_id), FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL, step_index INTEGER NOT NULL,
  action TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  duration_ms INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error_class TEXT,
  error_message TEXT, snapshot_path TEXT, UNIQUE(run_id, task_id, step_index),
  FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_receipt_unique ON tasks(receipt_no)
  WHERE receipt_no IS NOT NULL AND receipt_no <> '';
CREATE INDEX IF NOT EXISTS idx_tasks_biz_key ON tasks(biz_key, status);
-- Role/company dimension of access control. Empty table keeps the legacy
-- open-default behaviour; any row switches guarded entry points to deny-by-default.
CREATE TABLE IF NOT EXISTS access_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, company_id TEXT NOT NULL DEFAULT '*',
  access_level TEXT NOT NULL DEFAULT 'read', created_at TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  UNIQUE(role, company_id, access_level));
CREATE INDEX IF NOT EXISTS idx_access_grants_role ON access_grants(role);
CREATE TABLE IF NOT EXISTS access_config (
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
  principal TEXT NOT NULL, resource TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_audit_principal ON audit_log(principal, at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, at);
"""
_TASK_COLUMNS = "run_id, task_id, biz_key, status, started_at, finished_at, duration_ms, attempts, error_class, error_message, receipt_no, evidence_dir"
_STEP_COLUMNS = "run_id, task_id, step_index, action, status, started_at, finished_at, duration_ms, attempts, error_class, error_message, snapshot_path"
_VALID_ACCESS_LEVELS = frozenset({"read", "admin"})
_DEFAULT_CONFIG = {"access_enabled": "0"}


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        with self.connect() as connection:
            connection.execute(sql, params)

    def _fetch(self, sql: str, params: tuple[Any, ...], *, one: bool = False) -> Any:
        with self.connect() as connection:
            cursor = connection.execute(sql, params)
            return cursor.fetchone() if one else cursor.fetchall()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_legacy_schema(connection)
            for key, value in _DEFAULT_CONFIG.items():
                connection.execute(
                    "INSERT OR IGNORE INTO access_config(key, value) VALUES (?, ?)", (key, value)
                )

    @staticmethod
    def _migrate_legacy_schema(connection: sqlite3.Connection) -> None:
        """Add columns/tables introduced after the first release, idempotently."""
        existing = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        if "company_id" not in existing:
            connection.execute("ALTER TABLE tasks ADD COLUMN company_id TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "UPDATE tasks SET company_id = substr(biz_key, 1, instr(biz_key, ':') - 1) "
                "WHERE instr(biz_key, ':') > 0"
            )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_company ON tasks(company_id)")

    # ----------------------------------------------------------- run lifecycle
    def create_run(self, run: RunRecord) -> None:
        self._write(
            "INSERT INTO runs (run_id, started_at, finished_at, status, tasks_total, succeeded, failed, skipped, task_source, script_source, dry_run, base_url, evidence_dir, options_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.started_at,
                run.finished_at,
                run.status.value,
                run.tasks_total,
                run.succeeded,
                run.failed,
                run.skipped,
                run.task_source,
                run.script_source,
                int(run.dry_run),
                run.base_url,
                run.evidence_dir,
                json.dumps(run.options, ensure_ascii=False, sort_keys=True),
            ),
        )
    def reopen_run(self, run_id: str) -> None:
        self._write("UPDATE runs SET status = 'running', finished_at = NULL WHERE run_id = ?", (run_id,))
    def finish_run(self, run: RunRecord) -> None:
        self._write(
            "UPDATE runs SET finished_at = ?, status = ?, tasks_total = ?, succeeded = ?, failed = ?, skipped = ?, evidence_dir = ? WHERE run_id = ?",
            (
                run.finished_at,
                run.status.value,
                run.tasks_total,
                run.succeeded,
                run.failed,
                run.skipped,
                run.evidence_dir,
                run.run_id,
            ),
        )
    def start_task(self, run_id: str, task: TaskRecord, started_at: str, evidence_dir: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO tasks (run_id, task_id, biz_key, company_id, status, started_at, evidence_dir) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?) "
                "ON CONFLICT(run_id, task_id) DO UPDATE SET status = 'running', started_at = excluded.started_at, finished_at = NULL, "
                "duration_ms = 0, attempts = 0, error_class = NULL, error_message = NULL, receipt_no = NULL, evidence_dir = excluded.evidence_dir, "
                "company_id = excluded.company_id",
                (run_id, task.task_id, task.biz_key, task.company_id, started_at, evidence_dir),
            )
            connection.execute("DELETE FROM steps WHERE run_id = ? AND task_id = ?", (run_id, task.task_id))
    def finish_task(
        self,
        run_id: str,
        task_id: str,
        *,
        status: str,
        finished_at: str,
        duration_ms: int,
        attempts: int,
        error_class: str | None = None,
        error_message: str | None = None,
        receipt_no: str | None = None,
        evidence_dir: str = "",
    ) -> None:
        if receipt_no:
            duplicate = self._fetch(
                "SELECT run_id, task_id FROM tasks WHERE receipt_no = ? AND NOT (run_id = ? AND task_id = ?)",
                (receipt_no, run_id, task_id),
                one=True,
            )
            if duplicate is not None:
                raise ScriptError(
                    f"duplicate receipt number {receipt_no} already used by {duplicate['run_id']}/{duplicate['task_id']}"
                )
        self._write(
            "UPDATE tasks SET status = ?, finished_at = ?, duration_ms = ?, attempts = ?, error_class = ?, error_message = ?, receipt_no = ?, evidence_dir = ? WHERE run_id = ? AND task_id = ?",
            (
                status,
                finished_at,
                duration_ms,
                attempts,
                error_class,
                error_message,
                receipt_no,
                evidence_dir,
                run_id,
                task_id,
            ),
        )
    def record_step(self, run_id: str, task_id: str, step: StepResult) -> None:
        self._write(
            f"INSERT INTO steps ({_STEP_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, task_id, step_index) DO UPDATE SET action = excluded.action, status = excluded.status, "
            "started_at = excluded.started_at, finished_at = excluded.finished_at, duration_ms = excluded.duration_ms, "
            "attempts = excluded.attempts, error_class = excluded.error_class, error_message = excluded.error_message, snapshot_path = excluded.snapshot_path",
            (
                run_id,
                task_id,
                step.step_index,
                step.action,
                step.status.value,
                step.started_at,
                step.finished_at,
                step.duration_ms,
                step.attempts,
                step.error_class,
                step.error_message,
                step.snapshot_path,
            ),
        )
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._fetch("SELECT * FROM runs WHERE run_id = ?", (run_id,), one=True)
        return dict(row) if row else None
    def latest_run_id(self) -> str | None:
        row = self._fetch("SELECT run_id FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1", (), one=True)
        return str(row["run_id"]) if row else None
    def list_task_rows(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._fetch("SELECT * FROM tasks WHERE run_id = ? ORDER BY rowid ASC", (run_id,))]
    def list_steps(self, run_id: str, task_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM steps WHERE run_id = ?", [run_id]
        if task_id is not None:
            sql, params = sql + " AND task_id = ?", params + [task_id]
        return [dict(row) for row in self._fetch(sql + " ORDER BY task_id ASC, step_index ASC", tuple(params))]
    def list_tasks(self, run_id: str) -> list[TaskSummary]:
        return [
            TaskSummary(
                str(row["task_id"]),
                str(row["biz_key"]),
                str(row["status"]),
                int(row["duration_ms"]),
                int(row["attempts"]),
                row["error_class"],
                row["error_message"],
                row["receipt_no"],
                str(row["evidence_dir"]),
            )
            for row in self.list_task_rows(run_id)
        ]
    def has_successful_biz_key(self, biz_key: str) -> bool:
        row = self._fetch(
            "SELECT 1 FROM tasks WHERE biz_key = ? AND status = 'success' AND receipt_no IS NOT NULL LIMIT 1",
            (biz_key,),
            one=True,
        )
        return row is not None
    def receipt_exists(self, receipt_no: str) -> bool:
        return self._fetch("SELECT 1 FROM tasks WHERE receipt_no = ? LIMIT 1", (receipt_no,), one=True) is not None
    def receipt_count(self) -> int:
        row = self._fetch(
            "SELECT COUNT(*) AS n FROM tasks WHERE receipt_no IS NOT NULL AND receipt_no <> ''", (), one=True
        )
        return int(row["n"])
    def task_status(self, run_id: str, task_id: str) -> str | None:
        row = self._fetch("SELECT status FROM tasks WHERE run_id = ? AND task_id = ?", (run_id, task_id), one=True)
        return str(row["status"]) if row else None

    def task_company(self, run_id: str, task_id: str) -> str:
        """Owning company of a task row; empty string for pre-migration data."""
        row = self._fetch(
            "SELECT company_id, biz_key FROM tasks WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
            one=True,
        )
        if row is None:
            return ""
        company = str(row["company_id"] or "")
        if company:
            return company
        biz_key = str(row["biz_key"] or "")
        return biz_key.split(":", 1)[0] if ":" in biz_key else ""

    # =========================================================== access control
    def access_enabled(self) -> bool:
        """Authorizer protocol: true once an operator switches enforcement on."""
        row = self._fetch("SELECT value FROM access_config WHERE key = 'access_enabled'", (), one=True)
        return bool(row and str(row["value"]).lower() in {"1", "true", "yes", "on"})

    def set_access_enabled(self, enabled: bool) -> None:
        self._write(
            "INSERT INTO access_config(key, value) VALUES ('access_enabled', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if enabled else "0",),
        )

    def upsert_grant(self, role: str, company_id: str, access_level: str, *, note: str = "") -> None:
        role, company_id, access_level = role.strip(), (company_id or WILDCARD).strip(), access_level.strip().lower()
        if not role:
            raise ScriptError("grant role must be a non-empty string")
        if access_level not in _VALID_ACCESS_LEVELS:
            raise ScriptError(f"grant access_level must be one of {', '.join(sorted(_VALID_ACCESS_LEVELS))}")
        from fillbot.models import iso_now

        self._write(
            "INSERT INTO access_grants(role, company_id, access_level, created_at, note) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(role, company_id, access_level) DO UPDATE SET note = excluded.note",
            (role, company_id or WILDCARD, access_level, iso_now(), note),
        )

    def revoke_grant(self, role: str, company_id: str, access_level: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM access_grants WHERE role = ? AND company_id = ? AND access_level = ?",
                (role.strip(), (company_id or WILDCARD).strip(), access_level.strip().lower()),
            )
            return int(cursor.rowcount)

    def list_grants(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._fetch(
            "SELECT role, company_id, access_level, created_at, note FROM access_grants "
            "ORDER BY role, company_id, access_level", ()
        )]

    def grants_for(self, principal: Principal) -> list[dict[str, Any]]:
        """Authorizer protocol: resolve grants for all roles of a principal."""
        roles = list(principal.roles) or [principal.identity]
        if not roles:
            return []
        placeholders = ",".join("?" for _ in roles)
        return [dict(row) for row in self._fetch(
            f"SELECT role, company_id, access_level FROM access_grants WHERE role IN ({placeholders})",
            tuple(roles),
        )]

    # =================================================================== audit
    def record_audit(self, event: AuditEvent) -> None:
        from fillbot.models import iso_now

        self._write(
            "INSERT INTO audit_log(at, action, principal, resource, decision, reason, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                iso_now(),
                event.action,
                event.principal,
                event.resource,
                event.decision,
                event.reason,
                json.dumps(dict(event.details), ensure_ascii=False, sort_keys=True),
            ),
        )

    def list_audit(
        self,
        *,
        principal: str | None = None,
        action: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM audit_log WHERE 1 = 1", []
        if principal:
            sql, params = sql + " AND principal = ?", params + [principal]
        if action:
            sql, params = sql + " AND action = ?", params + [action]
        rows = self._fetch(sql + " ORDER BY id DESC LIMIT ?", tuple(params + [max(1, int(limit))]))
        return [dict(row) for row in rows]

    def audit_count(self) -> int:
        row = self._fetch("SELECT COUNT(*) AS n FROM audit_log", (), one=True)
        return int(row["n"])

    # ========================================================== guarded queries
    def access_policy(self) -> AccessPolicy:
        return AccessPolicy(self)

    def _scope_sql(self, decision: AccessDecision) -> tuple[str, list[Any]]:
        if not decision.scoped:
            return "", []
        return " AND company_id IN ({})".format(",".join("?" for _ in decision.company_scope)), list(
            decision.company_scope
        )

    def guarded_task_rows(
        self, run_id: str, principal: Principal | None, *, policy: AccessPolicy | None = None
    ) -> list[dict[str, Any]]:
        """Ledger query entry point with role/company filtering and audit."""
        policy = policy or self.access_policy()
        decision = policy.authorize(principal, "ledger.query", details={"run_id": run_id})
        if not decision.allowed:
            return []
        scope_sql, params = self._scope_sql(decision)
        rows = self._fetch(
            f"SELECT * FROM tasks WHERE run_id = ?{scope_sql} ORDER BY rowid ASC",
            tuple([run_id] + params),
        )
        return [dict(row) for row in rows]

    def guarded_list_tasks(
        self, run_id: str, principal: Principal | None, *, policy: AccessPolicy | None = None
    ) -> list[TaskSummary]:
        return [
            TaskSummary(
                str(row["task_id"]),
                str(row["biz_key"]),
                str(row["status"]),
                int(row["duration_ms"]),
                int(row["attempts"]),
                row["error_class"],
                row["error_message"],
                row["receipt_no"],
                str(row["evidence_dir"]),
            )
            for row in self.guarded_task_rows(run_id, principal, policy=policy)
        ]

    def authorize_task(
        self,
        run_id: str,
        task_id: str,
        principal: Principal | None,
        resource: str,
        *,
        policy: AccessPolicy | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> AccessDecision:
        """Guard a single-task resource (evidence file, task row)."""
        policy = policy or self.access_policy()
        company = self.task_company(run_id, task_id)
        return policy.authorize(
            principal, resource, company=company or None, details={"run_id": run_id, "task_id": task_id, **(details or {})}
        )

    def guarded_steps(
        self,
        run_id: str,
        task_id: str,
        principal: Principal | None,
        *,
        policy: AccessPolicy | None = None,
    ) -> list[dict[str, Any]]:
        policy = policy or self.access_policy()
        decision = self.authorize_task(run_id, task_id, principal, "ledger.steps", policy=policy)
        if not decision.allowed:
            return []
        return [dict(row) for row in self._fetch(
            "SELECT * FROM steps WHERE run_id = ? AND task_id = ? ORDER BY step_index ASC",
            (run_id, task_id),
        )]
