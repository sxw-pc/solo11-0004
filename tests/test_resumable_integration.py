"""End-to-end resumable-upload and integrity-check tests on the mock site."""
from __future__ import annotations
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
import pytest
from fillbot.config import load_config
from fillbot.dsl.schema import load_script
from fillbot.errors import TaskValidationError
from fillbot.hashing import sha256_file
from fillbot.intake import load_and_validate_tasks, verify_attachment_hashes
from fillbot.ledger.db import Ledger
from fillbot.mocksite.chaos import ChaosSettings
from fillbot.runner.scheduler import Runner

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "quarterly_report.yaml"
ATTACHMENT = ROOT / "tests" / "fixtures" / "attachment.pdf"
RECEIPT_RE = __import__("re").compile(r"^RCP-\d{8}-\d{4}$")


def _config(tmp_path: Path, base_url: str, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "base_url": base_url,
        "evidence_dir": str(tmp_path / "evidence"),
        "ledger_path": str(tmp_path / "data" / "ledger.db"),
        "runs_dir": str(tmp_path / "runs"),
        "rate_limit_per_minute": 6000,
        "timeout_seconds": 5,
        "backoff_base_seconds": 0.01,
        "backoff_max_seconds": 0.05,
        "evidence_keep_runs": 10,
    }
    values.update(overrides)
    return load_config(overrides=values)


def _batch(tmp_path: Path, attachment: Path, *, task_id: str = "T-2001", biz_key: str = "C002:2025-Q2") -> Path:
    source = json.loads((ROOT / "tasks" / "example_batch.json").read_text(encoding="utf-8"))[0]
    source["task_id"], source["biz_key"] = task_id, biz_key
    source["attachments"] = [str(attachment)]
    path = tmp_path / "batch.json"
    path.write_text(json.dumps([source], ensure_ascii=False), encoding="utf-8")
    return path


def test_intake_computes_attachment_hashes_on_load(tmp_path: Path) -> None:
    batch = _batch(tmp_path, ATTACHMENT)
    tasks = load_and_validate_tasks(batch)
    assert len(tasks[0].attachment_hashes) == 1
    assert tasks[0].attachment_hashes[0] == sha256_file(ATTACHMENT)
    # Tampering with the file flips its computed hash.
    tampered = tmp_path / "tampered.pdf"
    tampered.write_bytes(ATTACHMENT.read_bytes() + b"corrupt")
    tampered_batch = _batch(tmp_path, tampered, task_id="T-2002")
    tampered_hash = load_and_validate_tasks(tampered_batch)[0].attachment_hashes[0]
    assert tampered_hash != tasks[0].attachment_hashes[0]


def test_resume_blocks_when_attachment_hash_drifts_from_ledger(
    tmp_path: Path, serve: Callable[..., Any]
) -> None:
    # Use a writable copy so the committed fixture is never mutated.
    attachment = tmp_path / "attachment.pdf"
    attachment.write_bytes(ATTACHMENT.read_bytes())
    live = serve()
    config = _config(tmp_path, live.base_url)
    batch = _batch(tmp_path, attachment)
    tasks, script = load_and_validate_tasks(batch), load_script(SCRIPT)
    runner = Runner(config)
    run = runner.run(tasks, script, task_source=str(batch), script_source=str(SCRIPT))
    run_id = run.run_id
    assert run.status.value == "success"

    # Corrupt the attachment in place (same path, changed bytes): resume must
    # refuse before touching the network.
    original_bytes = attachment.read_bytes()
    try:
        attachment.write_bytes(original_bytes + b"corrupt-tail")
        tampered_tasks = load_and_validate_tasks(batch)
        ledger_hashes = runner.ledger.all_attachment_hashes(run_id)
        with pytest.raises(TaskValidationError, match="hash mismatch") as raised:
            verify_attachment_hashes(tampered_tasks, ledger_hashes)
        assert any("hash mismatch" in issue.message for issue in raised.value.issues)

        # And the Runner.resume path itself blocks before any task executes.
        fresh = Runner(config)
        with pytest.raises(TaskValidationError, match="hash mismatch"):
            fresh.resume(run_id, tampered_tasks, script, task_source=str(batch), script_source=str(SCRIPT))
    finally:
        attachment.write_bytes(original_bytes)


def test_upload_state_persisted_in_sqlite_and_recovers_on_resume(
    tmp_path: Path, serve: Callable[..., Any]
) -> None:
    # 48 KiB attachment with 16 KiB chunks => 3 chunks, exercising the chunked path.
    big = tmp_path / "big.pdf"
    big.write_bytes(os.urandom(48 * 1024))
    live = serve()
    config = _config(tmp_path, live.base_url, upload_chunk_size=16 * 1024, upload_chunk_threshold=16 * 1024)
    batch = _batch(tmp_path, big, biz_key="C002:2025-Q1")
    tasks, script = load_and_validate_tasks(batch), load_script(SCRIPT)
    runner = Runner(config)

    # Kill the process (simulate kill -9) immediately after the checkpoint for
    # chunk index 1 is persisted: chunks 0 and 1 landed, chunk 2 never started.
    from fillbot.browser.chunking import ChunkUploadManager

    real_save = ChunkUploadManager._save_checkpoint
    crash = {"fired": False}

    def crashing_save(self: ChunkUploadManager, key: str, entry: dict[str, Any]) -> None:
        real_save(self, key, entry)
        if not crash["fired"] and entry.get("received_chunks") == [0, 1]:
            crash["fired"] = True
            raise SystemExit(9)

    ChunkUploadManager._save_checkpoint = crashing_save  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit):
            runner.run(tasks, script, task_source=str(batch), script_source=str(SCRIPT))
    finally:
        ChunkUploadManager._save_checkpoint = real_save  # type: ignore[method-assign]

    # The ledger must hold the resumable checkpoint and attachment hashes.
    ledger = Ledger(config.ledger_path)
    run_id = ledger.latest_run_id()
    assert run_id is not None and ledger.get_run(run_id)["status"] == "interrupted"
    state = ledger.get_upload_state(run_id, "T-2001")
    assert state is not None
    entry = next(iter(state["files"].values()))
    assert entry["received_chunks"] == [0, 1] and entry["total_chunks"] == 3
    assert entry["sha256"] == sha256_file(big)
    hashes = ledger.get_attachment_hashes(run_id, "T-2001")
    assert hashes == {str(big.resolve()): sha256_file(big)}
    # The columns really live in the SQLite tasks table.
    with sqlite3.connect(ledger.path) as connection:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    assert {"attachment_hashes_json", "upload_state_json"} <= cols

    # Resume in a brand-new Runner (fresh process) and confirm completion.
    runner2 = Runner(config)
    run2 = runner2.resume(run_id, load_and_validate_tasks(batch), script,
                          task_source=str(batch), script_source=str(SCRIPT))
    assert run2.status.value == "success"
    final_state = ledger.get_upload_state(run_id, "T-2001")
    final_entry = next(iter(final_state["files"].values()))
    assert final_entry["received_chunks"] == [0, 1, 2] and final_entry["complete"] is True
    # Exactly one new receipt beyond the store's seeded RCP-20250115-0001.
    new_receipts = [item for item in live.receipts if item != "RCP-20250115-0001"]
    assert len(new_receipts) == 1 and RECEIPT_RE.fullmatch(new_receipts[0])


def test_temp_chunk_files_cleaned_after_task_completion(
    tmp_path: Path, serve: Callable[..., Any]
) -> None:
    big = tmp_path / "clean.pdf"
    big.write_bytes(os.urandom(40 * 1024))
    live = serve()
    config = _config(tmp_path, live.base_url, upload_chunk_size=16 * 1024, upload_chunk_threshold=16 * 1024)
    batch = _batch(tmp_path, big, task_id="T-2003", biz_key="C003:2025-Q2")
    runner = Runner(config)
    run = runner.run(load_and_validate_tasks(batch), load_script(SCRIPT),
                     task_source=str(batch), script_source=str(SCRIPT))
    assert run.status.value == "success"
    leftovers = [p for p in Path(__import__("tempfile").gettempdir()).glob("fillbot-upload-*") if p.exists()]
    assert leftovers == []


def test_small_attachments_keep_single_request_efficiency(
    tmp_path: Path, serve: Callable[..., Any]
) -> None:
    # Default threshold (32 KiB): the 133-byte fixture must never hit chunk routes.
    live = serve()
    config = _config(tmp_path, live.base_url)
    batch = _batch(tmp_path, ATTACHMENT, task_id="T-2004", biz_key="C002:2025-Q3")
    ledger = Ledger(config.ledger_path)
    runner = Runner(config, ledger=ledger)
    run = runner.run(load_and_validate_tasks(batch), load_script(SCRIPT),
                     task_source=str(batch), script_source=str(SCRIPT))
    assert run.status.value == "success"
    assert ledger.get_upload_state(run.run_id, "T-2004") is None


def test_server_side_hash_verification_runs_for_chunked_uploads(
    tmp_path: Path, serve: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "verify.pdf"
    big.write_bytes(os.urandom(40 * 1024))
    live = serve(chaos=ChaosSettings(enabled=False))
    config = _config(tmp_path, live.base_url, upload_chunk_size=16 * 1024, upload_chunk_threshold=16 * 1024)
    batch = _batch(tmp_path, big, task_id="T-2005", biz_key="C001:2025-Q2")
    runner = Runner(config)

    # Sabotage the final confirmation so the server "returns" a wrong digest.
    from fillbot.browser import chunking as chunking_mod

    original_get = chunking_mod.requests.Session.get

    class _FakeResponse:
        status_code = 200

        def json(self_inner) -> dict[str, Any]:
            return {"complete": True, "sha256": "f" * 64, "received_chunks": [0, 1, 2],
                    "total_chunks": 3, "upload_id": "x", "filename": "verify.pdf"}

    def sabotaged_get(self: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        response = original_get(self, url, *args, **kwargs)
        # Only rewrite the chunk-session status confirmation, never page GETs.
        if "/__mock/uploads/" in url and "/chunks/" not in url and response.status_code == 200:
            try:
                if response.json().get("complete"):
                    return _FakeResponse()
            except ValueError:
                pass
        return response

    monkeypatch.setattr(chunking_mod.requests.Session, "get", sabotaged_get)
    run = runner.run(load_and_validate_tasks(batch), load_script(SCRIPT),
                     task_source=str(batch), script_source=str(SCRIPT))
    # The runner records the adapter's hash-verification ClientError on the task
    # rather than letting it escape the batch boundary.
    assert run.status.value == "failed"
    row = runner.ledger.list_task_rows(run.run_id)[0]
    assert row["status"] == "failed" and row["error_class"] == "CLIENT"
    assert "hash verification failed" in (row["error_message"] or "")


def test_hash_computation_keeps_main_flow_under_two_seconds(tmp_path: Path) -> None:
    # A multi-MiB file hashes well within the 2s budget; the bounded helper
    # offloads the work and never blocks the main flow longer than its budget.
    big = tmp_path / "large.pdf"
    big.write_bytes(os.urandom(8 * 1024 * 1024))
    from fillbot.hashing import sha256_file_bounded

    start = time.monotonic()
    result = sha256_file_bounded(big, timeout_seconds=2.0)
    elapsed = time.monotonic() - start
    assert result == sha256_file(big)
    assert elapsed < 2.0, elapsed


def test_chunk_retry_recovers_from_live_server_faults_without_duplicate_submission(
    tmp_path: Path, serve: Callable[..., Any]
) -> None:
    big = tmp_path / "faulty.pdf"
    big.write_bytes(os.urandom(48 * 1024))  # 3 chunks at 16 KiB
    live = serve()
    # Two transient 500s on chunk PUTs, consumed automatically by retries.
    live.app.extensions["fillbot_chunk_uploads"].reset_faults([500, 500])
    config = _config(
        tmp_path,
        live.base_url,
        upload_chunk_size=16 * 1024,
        upload_chunk_threshold=16 * 1024,
        max_retries=4,
    )
    batch = _batch(tmp_path, big, task_id="T-2006", biz_key="C002:2025-Q4")
    runner = Runner(config)
    run = runner.run(
        load_and_validate_tasks(batch),
        load_script(SCRIPT),
        task_source=str(batch),
        script_source=str(SCRIPT),
    )
    assert run.status.value == "success"
    entry = next(iter(runner.ledger.get_upload_state(run.run_id, "T-2006")["files"].values()))
    assert entry["complete"] is True and entry["received_chunks"] == [0, 1, 2]
    # Exactly one new receipt: retries never produced a duplicate submission.
    assert [r for r in live.receipts if r != "RCP-20250115-0001"] == [live.receipts[-1]]
    assert live.app.extensions["fillbot_chunk_uploads"].faults == []
