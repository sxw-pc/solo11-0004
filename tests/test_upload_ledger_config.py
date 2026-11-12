from __future__ import annotations
import sqlite3
from pathlib import Path
import pytest
from fillbot.config import BotConfig, load_config
from fillbot.errors import ConfigError
from fillbot.ledger.db import Ledger
from fillbot.models import TaskRecord, iso_now


def _task(task_id: str = "T-1") -> TaskRecord:
    return TaskRecord(task_id=task_id, biz_key="C1:q", payload={}, source_row=1)


def test_upload_state_and_attachment_hash_dao_roundtrip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    from fillbot.models import RunRecord

    ledger.create_run(RunRecord(run_id="R-1", started_at=iso_now(), base_url="http://x"))
    hashes = {"/tmp/a.pdf": "a" * 64, "/tmp/b.pdf": "b" * 64}
    ledger.start_task("R-1", _task(), iso_now(), "/evidence/T-1", attachment_hashes=hashes)
    assert ledger.get_attachment_hashes("R-1", "T-1") == hashes
    assert ledger.all_attachment_hashes("R-1") == {"T-1": hashes}

    state = {
        "version": 1,
        "temp_dir": "/tmp/fillbot-upload-x",
        "files": {
            "/tmp/a.pdf": {
                "upload_id": "a" * 64,
                "path": "/tmp/a.pdf",
                "received_chunks": [0, 1],
                "total_chunks": 3,
                "complete": False,
            }
        },
    }
    ledger.save_upload_state("R-1", "T-1", state)
    assert ledger.get_upload_state("R-1", "T-1") == state
    # Restarting the task (retry/resume) must preserve the upload progress.
    ledger.start_task("R-1", _task(), iso_now(), "/evidence/T-1", attachment_hashes=hashes)
    assert ledger.get_upload_state("R-1", "T-1") == state
    assert ledger.get_upload_state("R-1", "missing") is None
    # Finishing the task keeps the checkpoint for post-mortem inspection.
    ledger.finish_task("R-1", "T-1", status="failed", finished_at=iso_now(),
                       duration_ms=5, attempts=2, error_class="NETWORK")
    assert ledger.get_upload_state("R-1", "T-1") == state


def test_legacy_ledger_schema_migrates_additively(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    # Build a ledger with the pre-feature schema (no new columns).
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
              status TEXT NOT NULL, tasks_total INTEGER NOT NULL DEFAULT 0, succeeded INTEGER NOT NULL DEFAULT 0,
              failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0, task_source TEXT NOT NULL DEFAULT '',
              script_source TEXT NOT NULL DEFAULT '', dry_run INTEGER NOT NULL DEFAULT 0, base_url TEXT NOT NULL DEFAULT '',
              evidence_dir TEXT NOT NULL DEFAULT '', options_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
              biz_key TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, finished_at TEXT,
              duration_ms INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error_class TEXT,
              error_message TEXT, receipt_no TEXT, evidence_dir TEXT NOT NULL DEFAULT '',
              UNIQUE(run_id, task_id));
            CREATE TABLE steps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
              step_index INTEGER NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT,
              finished_at TEXT, duration_ms INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
              error_class TEXT, error_message TEXT, snapshot_path TEXT,
              UNIQUE(run_id, task_id, step_index));
            INSERT INTO runs (run_id, started_at, status) VALUES ('R-9', '2026-01-01T00:00:00+00:00', 'interrupted');
            INSERT INTO tasks (run_id, task_id, biz_key, status, evidence_dir)
              VALUES ('R-9', 'T-9', 'C1:q', 'running', '/old');
            """
        )
    # Opening the legacy DB migrates it without losing existing rows.
    ledger = Ledger(db_path)
    run = ledger.get_run("R-9")
    assert run is not None and run["status"] == "interrupted"
    assert ledger.get_upload_state("R-9", "T-9") is None
    assert ledger.get_attachment_hashes("R-9", "T-9") == {}
    ledger.save_upload_state("R-9", "T-9", {"version": 1, "files": {}})
    assert ledger.get_upload_state("R-9", "T-9") == {"version": 1, "files": {}}
    # Migration is idempotent: reopening must not error.
    Ledger(db_path)


def test_chunk_config_defaults_env_and_validation(tmp_path: Path) -> None:
    default = BotConfig()
    assert default.upload_chunk_size == 32 * 1024
    assert default.upload_chunk_threshold == 32 * 1024
    configured = load_config(
        overrides={"upload_chunk_size": 4096, "upload_chunk_threshold": 2048},
        environ={},
    )
    assert configured.upload_chunk_size == 4096 and configured.upload_chunk_threshold == 2048
    env_config = load_config(
        environ={
            "FILLBOT_UPLOAD_CHUNK_SIZE": "8192",
            "FILLBOT_UPLOAD_CHUNK_THRESHOLD": "4096",
        }
    )
    assert env_config.upload_chunk_size == 8192 and env_config.upload_chunk_threshold == 4096
    with pytest.raises(ConfigError, match="threshold cannot exceed"):
        load_config(overrides={"upload_chunk_size": 1024, "upload_chunk_threshold": 2048}, environ={})
    for bad in (0, -5):
        with pytest.raises(ConfigError):
            load_config(overrides={"upload_chunk_size": bad}, environ={})
    # public_dict surfaces the new knobs for diagnostics.
    assert configured.public_dict()["upload_chunk_size"] == 4096
