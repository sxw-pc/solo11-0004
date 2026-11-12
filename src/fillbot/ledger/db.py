"""SQLite ledger: runs, tasks, steps and evidence refs, with a global receipt uniqueness rule."""
from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from fillbot.errors import ScriptError
from fillbot.models import RunRecord, StepResult, TaskRecord, TaskSummary, iso_now
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
CREATE TABLE IF NOT EXISTS evidence_refs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
  biz_key TEXT NOT NULL DEFAULT '', evidence_type TEXT NOT NULL, path TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'run', created_at TEXT NOT NULL, released_at TEXT,
  UNIQUE(run_id, evidence_type, path, source));
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_receipt_unique ON tasks(receipt_no)
  WHERE receipt_no IS NOT NULL AND receipt_no <> '';
CREATE INDEX IF NOT EXISTS idx_tasks_biz_key ON tasks(biz_key, status);
CREATE INDEX IF NOT EXISTS idx_evidence_refs_path ON evidence_refs(path);
CREATE INDEX IF NOT EXISTS idx_evidence_refs_run ON evidence_refs(run_id, evidence_type);
CREATE INDEX IF NOT EXISTS idx_evidence_refs_biz_key ON evidence_refs(biz_key);
"""
_TASK_COLUMNS = "run_id, task_id, biz_key, status, started_at, finished_at, duration_ms, attempts, error_class, error_message, receipt_no, evidence_dir"
_STEP_COLUMNS = "run_id, task_id, step_index, action, status, started_at, finished_at, duration_ms, attempts, error_class, error_message, snapshot_path"
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
                "INSERT INTO tasks (run_id, task_id, biz_key, status, started_at, evidence_dir) VALUES (?, ?, ?, 'running', ?, ?) "
                "ON CONFLICT(run_id, task_id) DO UPDATE SET status = 'running', started_at = excluded.started_at, finished_at = NULL, "
                "duration_ms = 0, attempts = 0, error_class = NULL, error_message = NULL, receipt_no = NULL, evidence_dir = excluded.evidence_dir",
                (run_id, task.task_id, task.biz_key, started_at, evidence_dir),
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
    def list_runs(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._fetch("SELECT * FROM runs ORDER BY started_at ASC, rowid ASC", ())]
    def register_evidence_ref(
        self,
        run_id: str,
        task_id: str,
        biz_key: str,
        evidence_type: str,
        path: str,
        *,
        source: str = "run",
        created_at: str | None = None,
    ) -> None:
        """Register (or re-activate) one evidence reference; idempotent per (run, type, path, source)."""
        self._write(
            "INSERT INTO evidence_refs (run_id, task_id, biz_key, evidence_type, path, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, evidence_type, path, source) DO UPDATE SET released_at = NULL, "
            "task_id = excluded.task_id, biz_key = excluded.biz_key",
            (run_id, task_id, biz_key, evidence_type, path, source, created_at or iso_now()),
        )
    def list_evidence_refs(
        self,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        biz_key: str | None = None,
        evidence_type: str | None = None,
        source: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        for column, value in (
            ("run_id", run_id),
            ("task_id", task_id),
            ("biz_key", biz_key),
            ("evidence_type", evidence_type),
            ("source", source),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if active_only:
            clauses.append("released_at IS NULL")
        sql = f"SELECT * FROM evidence_refs WHERE {' AND '.join(clauses)} ORDER BY run_id ASC, evidence_type ASC, path ASC, source ASC"
        return [dict(row) for row in self._fetch(sql, tuple(params))]
    def release_evidence_refs(
        self,
        *,
        run_id: str | None = None,
        source: str | None = None,
        paths: list[str] | None = None,
        ids: list[int] | None = None,
        released_at: str | None = None,
    ) -> int:
        """Mark matching refs released; returns how many refs transitioned."""
        clauses, params = ["released_at IS NULL"], []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if paths is not None:
            if not paths:
                return 0
            clauses.append("path IN (" + ",".join("?" * len(paths)) + ")")
            params.extend(paths)
        if ids is not None:
            if not ids:
                return 0
            clauses.append("id IN (" + ",".join("?" * len(ids)) + ")")
            params.extend(ids)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE evidence_refs SET released_at = ? WHERE {' AND '.join(clauses)}",
                (released_at or iso_now(), *params),
            )
            return cursor.rowcount
    def run_has_evidence_refs(self, run_id: str) -> bool:
        return self._fetch("SELECT 1 FROM evidence_refs WHERE run_id = ? LIMIT 1", (run_id,), one=True) is not None
    @staticmethod
    def _insert_ref(
        connection: sqlite3.Connection,
        run_id: str,
        task_id: str,
        biz_key: str,
        evidence_type: str,
        path: str,
        created_at: str,
    ) -> int:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO evidence_refs (run_id, task_id, biz_key, evidence_type, path, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'run', ?)",
            (run_id, task_id, biz_key, evidence_type, path, created_at),
        )
        return cursor.rowcount
    def backfill_evidence_refs(self, *, now: str | None = None) -> int:
        """Register refs for runs that predate the registry (legacy data); returns refs inserted.

        Runs with no derivable references are left untouched so cleanup keeps
        them conservatively; any database error aborts the caller before any
        evidence is deleted.
        """
        stamp = now or iso_now()
        inserted = 0
        with self.connect() as connection:
            run_ids = [str(row["run_id"]) for row in connection.execute("SELECT run_id FROM runs")]
            for run_id in run_ids:
                if connection.execute("SELECT 1 FROM evidence_refs WHERE run_id = ? LIMIT 1", (run_id,)).fetchone():
                    continue
                tasks = connection.execute(
                    "SELECT task_id, biz_key, receipt_no, evidence_dir FROM tasks WHERE run_id = ?", (run_id,)
                ).fetchall()
                biz_keys = {str(task["task_id"]): str(task["biz_key"]) for task in tasks}
                for task in tasks:
                    evidence_dir = str(task["evidence_dir"] or "")
                    if not evidence_dir:
                        continue
                    task_id = str(task["task_id"])
                    inserted += self._insert_ref(
                        connection, run_id, task_id, biz_keys[task_id], "actions", str(Path(evidence_dir) / "actions.jsonl"), stamp
                    )
                    if task["receipt_no"]:
                        inserted += self._insert_ref(
                            connection, run_id, task_id, biz_keys[task_id], "receipt", str(Path(evidence_dir) / "receipt.html"), stamp
                        )
                steps = connection.execute(
                    "SELECT DISTINCT task_id, snapshot_path FROM steps WHERE run_id = ? AND snapshot_path IS NOT NULL AND snapshot_path <> ''",
                    (run_id,),
                ).fetchall()
                for step in steps:
                    task_id = str(step["task_id"])
                    inserted += self._insert_ref(
                        connection, run_id, task_id, biz_keys.get(task_id, ""), "snapshot", str(step["snapshot_path"]), stamp
                    )
        return inserted
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
