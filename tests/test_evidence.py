from __future__ import annotations
import json
import os
import time
from pathlib import Path
from fillbot.browser.base import Page
from fillbot.evidence.store import EvidenceStore, safe_component, sanitize_html
from fillbot.ledger.db import Ledger
from fillbot.models import RunRecord, iso_now
HTML = """
<html><body><input name="password" value="hunter2"><input name="csrf_token" value="token-value">
<textarea name="secret_note">top-secret</textarea><input name="company" value="青岚谷（虚构）">
<p>extra sensitive-value</p></body></html>
"""
def test_masking_snapshot_actions_and_receipt(tmp_path: Path) -> None:
    clean = sanitize_html(HTML, ("password", "token", "secret"), ["sensitive-value"])
    assert 'name="password" value="***"' in clean and 'name="csrf_token" value="***"' in clean
    assert "top-secret" not in clean and "sensitive-value" not in clean and "***" in clean
    assert "青岚谷（虚构）" in clean and safe_component("..") == "unnamed" and safe_component("a/b") == "a_b"
    store = EvidenceStore(tmp_path / "evidence", mask_fields=("password", "token"))
    page = Page(200, "http://127.0.0.1/login", {"X-Test": "1"}, HTML, 12)
    snapshot = store.snapshot("run/1", "task:1", 2, page, secret_values=["hunter2"])
    assert snapshot.name == "step-02.html" and snapshot.parent.name == "task_1"
    text = snapshot.read_text(encoding="utf-8")
    assert "hunter2" not in text and "***" in text
    assert store.save_receipt("run/1", "task:1", page).name == "receipt.html"
    record = json.loads(store.append_action("run/1", "task:1", {"step": 1, "action": "fill"}).read_text(encoding="utf-8"))
    assert record["step"] == 1 and "timestamp" in record
def test_retention_and_keep_zero(tmp_path: Path) -> None:
    base = tmp_path / "evidence"
    old, new = base / "old-run" / "task", base / "new-run" / "task"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "step-01.html").write_text("old")
    (new / "step-01.html").write_text("new")
    old_time = time.time() - 1000
    os.utime(old.parent, (old_time, old_time))
    os.utime(old, (old_time, old_time))
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.create_run(RunRecord(run_id="old-run", started_at=iso_now()))
    ledger.finish_run(RunRecord(run_id="old-run", started_at=iso_now(), finished_at=iso_now()))
    assert EvidenceStore(base, keep_runs=1).cleanup() == ["old-run"]
    assert not old.parent.exists() and (new / "step-01.html").is_file() and ledger.get_run("old-run") is not None
    assert EvidenceStore(tmp_path / "none", keep_runs=0).cleanup() == []
    assert EvidenceStore(base, keep_runs=1).cleanup(0) == ["new-run"] and not list(base.iterdir())
def test_cleanup_protects_runs_awaiting_confirmation(tmp_path: Path) -> None:
    base = tmp_path / "evidence"
    old, waiting, new = (base / name / "task" for name in ("old-run", "waiting-run", "new-run"))
    for directory in (old, waiting, new):
        directory.mkdir(parents=True)
        (directory / "confirmation-snapshot.html").write_text("snapshot")
    old_time = time.time() - 1000
    os.utime(old.parent, (old_time, old_time))
    os.utime(old, (old_time, old_time))
    removed = EvidenceStore(base, keep_runs=1).cleanup(protect=["waiting-run"])
    assert removed == ["old-run"]
    assert (waiting / "confirmation-snapshot.html").is_file()
    assert not old.parent.exists() and new.parent.is_dir()
