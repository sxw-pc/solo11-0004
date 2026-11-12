"""Evidence store: graded writes, guarded reads, manifests and legacy files."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fillbot.browser.base import Page
from fillbot.evidence.store import EvidenceAccessDenied, EvidenceStore, MANIFEST_NAME
from fillbot.ledger.db import Ledger
from fillbot.models import RunRecord, TaskRecord, iso_now
from fillbot.security.access import Principal
from fillbot.security.levels import SecurityLevel
from fillbot.security.masking import REDACTION, MaskingPolicy

HTML = """
<html><body>
<input name="password" value="hunter2secret">
<input name="contact_phone" value="13800001234">
<input name="company_name" value="Acme Corp">
<input name="period" value="2025-Q4">
<p>free-text hunter2secret</p>
</body></html>
"""


def _task(task_id: str, company: str) -> TaskRecord:
    return TaskRecord(task_id, f"{company}:2025-Q4", {"company_id": company}, source_row=1)


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.create_run(RunRecord(run_id="R-1", started_at=iso_now()))
    ledger.start_task("R-1", _task("T-1", "C001"), iso_now(), str(tmp_path / "e"))
    ledger.finish_task("R-1", "T-1", status="success", finished_at=iso_now(), duration_ms=1, attempts=1)
    return ledger


def _graded_store(tmp_path: Path, ledger: Ledger) -> EvidenceStore:
    policy = MaskingPolicy.create(
        field_levels={"password": "L4", "contact_phone": "L3", "company_name": "L1"},
        mask_fields=("password", "token"),
    )
    store = EvidenceStore(tmp_path / "evidence", policy=policy)
    store.bind_ledger(ledger)
    return store


def test_graded_write_masks_by_level_and_writes_manifest(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = _graded_store(tmp_path, ledger)
    page = Page(200, "http://127.0.0.1/x", {}, HTML, 12)
    path = store.save_receipt("R-1", "T-1", page, secret_values=["hunter2secret"])
    text = path.read_text(encoding="utf-8")
    assert "hunter2secret" not in text and "13800001234" not in text
    assert REDACTION in text  # L4 password + free-text redaction
    assert 'name="contact_phone" value="***"' in text  # L3 (full rule on empty/value)
    assert 'name="company_name" value="A********"' in text  # L1 keep first char
    assert 'name="period" value="2025-Q4"' in text  # ungraded control untouched
    manifest = json.loads((path.parent / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["graded"] is True and manifest["field_levels"]["password"] == "L4"
    # Every write produced an audit row.
    assert ledger.list_audit(action="evidence.mask")


def test_legacy_write_is_unchanged(tmp_path: Path) -> None:
    from fillbot.browser.snapshot import sanitize_html

    ledger = _ledger(tmp_path)
    store = EvidenceStore(tmp_path / "evidence", mask_fields=("password", "token"))
    store.bind_ledger(ledger)
    page = Page(200, "u", {}, HTML, 1)
    path = store.snapshot("R-1", "T-1", 0, page, secret_values=["hunter2secret"])
    assert path.read_text(encoding="utf-8") == sanitize_html(HTML, ("password", "token"), ["hunter2secret"])
    assert not (path.parent / MANIFEST_NAME).exists()


def test_guarded_read_allow_deny_and_audit(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = _graded_store(tmp_path, ledger)
    page = Page(200, "u", {}, HTML, 1)
    store.snapshot("R-1", "T-1", 0, page, secret_values=["hunter2secret"])

    # Open mode: anonymous reads succeed and are audited.
    content = store.read_evidence("R-1", "T-1", "step-00.html", None, company_resolver=ledger.task_company)
    assert "hunter2secret" not in content

    ledger.upsert_grant("auditor", "C001", "read")
    ledger.set_access_enabled(True)
    allowed = Principal("alice", ("auditor",))
    assert store.read_evidence("R-1", "T-1", "step-00.html", allowed, company_resolver=ledger.task_company)

    other = Principal("bob", ("auditor",))
    # Give Bob a grant on a different company only.
    ledger.upsert_grant("auditor-c2", "C002", "read")
    bob = Principal("bob", ("auditor-c2",))
    with pytest.raises(EvidenceAccessDenied):
        store.read_evidence("R-1", "T-1", "step-00.html", bob, company_resolver=ledger.task_company)

    events = [
        row
        for row in ledger.list_audit(action="evidence.read", limit=500)
        if row["decision"] == "allow"
    ]
    assert events and all(row["decision"] == "allow" for row in events)
    denies = [
        row for row in ledger.list_audit(action="evidence.read", limit=500) if row["decision"] == "deny"
    ]
    assert denies and denies[0]["decision"] == "deny"
    assert store.list_evidence("R-1", "T-1", allowed, company_resolver=ledger.task_company) == ["step-00.html"]
    with pytest.raises(EvidenceAccessDenied):
        store.list_evidence("R-1", "T-1", bob, company_resolver=ledger.task_company)


def test_read_path_traversal_and_missing_file_blocked(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = _graded_store(tmp_path, ledger)
    with pytest.raises(Exception):
        store.read_evidence("R-1", "T-1", "../escape.html", None, company_resolver=ledger.task_company)
    with pytest.raises(Exception):
        store.read_evidence("R-1", "T-1", "/etc/passwd", None, company_resolver=ledger.task_company)
    with pytest.raises(Exception):
        store.read_evidence("R-1", "T-1", "missing.html", None, company_resolver=ledger.task_company)


def test_legacy_evidence_directory_reads_under_open_default(tmp_path: Path) -> None:
    # A directory produced by an older release has no manifest at all.
    ledger = _ledger(tmp_path)
    store = EvidenceStore(tmp_path / "evidence", mask_fields=("password",))
    store.bind_ledger(ledger)
    target = store.task_dir("R-1", "T-1")
    (target / "receipt.html").write_text("<html>legacy</html>", encoding="utf-8")
    content = store.read_evidence("R-1", "T-1", "receipt.html", None, company_resolver=ledger.task_company)
    assert content == "<html>legacy</html>"
    assert ledger.list_audit(action="evidence.read")[0]["reason"] == "open-default"


def test_broken_masking_rule_degrades_to_redaction(tmp_path: Path) -> None:
    # Rule table with a gap at L2 must fall back to the strongest rule.
    policy = MaskingPolicy(
        rules={}, field_levels={"password": SecurityLevel.L2}
    )
    store = EvidenceStore(tmp_path / "evidence", policy=policy)
    page = Page(200, "u", {}, '<input name="password" value="plaintext">', 1)
    path = store.snapshot("R-1", "T-1", 0, page)
    assert "plaintext" not in path.read_text(encoding="utf-8")
    assert REDACTION in path.read_text(encoding="utf-8")
