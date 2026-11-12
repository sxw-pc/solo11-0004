"""SQLite persistence for dependency workflows and batch states.

Kept in a *separate* database from the task ledger so the dependency module is
fully additive and the existing ledger schema is untouched.  Every scheduling
decision (status change, run id, blocking reason, timeout deadline) is persisted
before the in-memory loop acts on it, which is what makes a restart recoverable.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fillbot.deps.models import (
    BatchState,
    BatchStatus,
    WorkflowRecord,
    WorkflowStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
  workflow_id TEXT PRIMARY KEY,
  plan_name TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  default_timeout_seconds INTEGER,
  poll_interval_seconds REAL NOT NULL DEFAULT 1.0,
  dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS batch_states (
  workflow_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  depends_on_json TEXT NOT NULL DEFAULT '[]',
  run_id TEXT,
  timeout_deadline TEXT,
  reason TEXT,
  blocked_by_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workflow_id, name),
  FOREIGN KEY(workflow_id) REFERENCES workflows(workflow_id)
);
CREATE INDEX IF NOT EXISTS idx_batch_status ON batch_states(workflow_id, status);
"""


class DependencyStore:
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

    # -- workflows ---------------------------------------------------------
    def create_workflow(self, record: WorkflowRecord) -> None:
        self._write(
            "INSERT INTO workflows (workflow_id, plan_name, status, source, started_at, finished_at, "
            "default_timeout_seconds, poll_interval_seconds, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.workflow_id,
                record.plan_name,
                record.status.value,
                record.source,
                record.started_at,
                record.finished_at,
                record.default_timeout_seconds,
                record.poll_interval_seconds,
                int(record.dry_run),
            ),
        )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        row = self._fetch("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,), one=True)
        return dict(row) if row else None

    def list_workflows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._fetch("SELECT * FROM workflows ORDER BY started_at ASC", ())]

    def latest_workflow_id(self) -> str | None:
        row = self._fetch(
            "SELECT workflow_id FROM workflows ORDER BY started_at DESC, rowid DESC LIMIT 1", (), one=True
        )
        return str(row["workflow_id"]) if row else None

    def update_workflow_status(
        self, workflow_id: str, status: WorkflowStatus, finished_at: str | None = None
    ) -> None:
        self._write(
            "UPDATE workflows SET status = ?, finished_at = COALESCE(?, finished_at) WHERE workflow_id = ?",
            (status.value, finished_at, workflow_id),
        )

    # -- batch states ------------------------------------------------------
    def upsert_batch(self, state: BatchState) -> None:
        self._write(
            "INSERT INTO batch_states (workflow_id, name, status, depends_on_json, run_id, timeout_deadline, "
            "reason, blocked_by_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workflow_id, name) DO UPDATE SET status = excluded.status, "
            "depends_on_json = excluded.depends_on_json, run_id = excluded.run_id, "
            "timeout_deadline = excluded.timeout_deadline, reason = excluded.reason, "
            "blocked_by_json = excluded.blocked_by_json, updated_at = excluded.updated_at",
            (
                state.workflow_id,
                state.name,
                state.status.value,
                json.dumps(list(state.depends_on), ensure_ascii=False),
                state.run_id,
                state.timeout_deadline,
                state.reason,
                json.dumps(list(state.blocked_by), ensure_ascii=False),
                state.updated_at,
            ),
        )

    def get_batch(self, workflow_id: str, name: str) -> BatchState | None:
        row = self._fetch(
            "SELECT * FROM batch_states WHERE workflow_id = ? AND name = ?",
            (workflow_id, name),
            one=True,
        )
        return self._hydrate(row) if row else None

    def list_batches(self, workflow_id: str) -> list[BatchState]:
        rows = self._fetch(
            "SELECT * FROM batch_states WHERE workflow_id = ? ORDER BY rowid ASC", (workflow_id,)
        )
        return [self._hydrate(row) for row in rows]

    def status_map(self, workflow_id: str) -> dict[str, BatchStatus]:
        return {state.name: state.status for state in self.list_batches(workflow_id)}

    @staticmethod
    def _hydrate(row: sqlite3.Row) -> BatchState:
        return BatchState(
            workflow_id=str(row["workflow_id"]),
            name=str(row["name"]),
            status=BatchStatus(str(row["status"])),
            depends_on=tuple(json.loads(row["depends_on_json"])),
            run_id=row["run_id"],
            timeout_deadline=row["timeout_deadline"],
            reason=row["reason"],
            blocked_by=tuple(json.loads(row["blocked_by_json"])),
            updated_at=str(row["updated_at"]),
        )
