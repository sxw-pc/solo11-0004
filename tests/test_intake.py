from __future__ import annotations
import json
from pathlib import Path
import pytest
from fillbot.errors import TaskValidationError
from fillbot.intake.loader import load_raw_tasks
from fillbot.intake.validate import load_and_validate_tasks, validate_raw_tasks
FIXTURES = Path(__file__).parent / "fixtures"
def _payload(**updates: object) -> dict[str, object]:
    data: dict[str, object] = {
        "username": "parkco-b",
        "password": "mock-only-beta",
        "company_id": "C002",
        "company_name": "青岚谷生态科技（虚构）",
        "period": "2025-Q4",
        "contact_name": "林小岚（虚构）",
        "report_date": "2025-11-10",
        "electricity": "12800.5",
        "water": 340,
        "gas": 88,
        "employees": 42,
        "hires": 3,
    }
    data.update(updates)
    return data
def _doc(payload: object, *, row: int = 1, attachments: object = None) -> dict:
    return {
        "task_id": f"T-{row}",
        "biz_key": f"C002:2025-Q{row}",
        "payload": payload,
        "attachments": ["attachment.pdf"] if attachments is None else attachments,
        "_source_file": "memory.json",
        "_source_row": row,
        "_base_dir": str(FIXTURES),
    }
def _issues(*docs: dict) -> list:
    with pytest.raises(TaskValidationError) as raised:
        validate_raw_tasks(list(docs))
    return raised.value.issues
def test_json_csv_and_normalization() -> None:
    task = load_and_validate_tasks(FIXTURES / "valid_task.json")[0]
    assert (task.task_id, task.payload["electricity"], task.payload["employees"]) == ("T-9001", 12800.5, 42)
    assert task.attachment == (FIXTURES / "attachment.pdf").resolve() and task.source_row == 1
    tasks = load_and_validate_tasks(FIXTURES / "valid_tasks.csv")
    assert [item.task_id for item in tasks] == ["T-9002", "T-9003"]
    assert tasks[0].payload["employees"] == 18 and tasks[1].biz_key == "C002:2025-Q2"
    assert validate_raw_tasks([_doc(_payload(contact_phone="13800000000"))])[0].payload["contact_phone"] == "13800000000"
def test_loader_shapes_and_attachments(tmp_path: Path) -> None:
    docs = load_raw_tasks(FIXTURES / "valid_task.json")
    assert docs[0]["_source_row"] == 1 and docs[0]["payload"]["company_id"] == "C002"
    array = tmp_path / "array.json"
    array.write_text(json.dumps(json.loads((FIXTURES / "invalid_tasks.json").read_text(encoding="utf-8"))))
    assert load_raw_tasks(array)[0]["_source_row"] == 1
    csv_path = tmp_path / "tasks.csv"
    csv_path.write_text(
        "task_id,biz_key,attachments,company_id,period\nT-1,C002:2025-Q4,a.pdf;b.pdf,C002,2025-Q4\nT-2,X, c.pdf|d.pdf,C003,2025-Q1\n"
    )
    rows = load_raw_tasks(csv_path)
    assert rows[0]["_source_row"] == 2 and rows[0]["attachments"] == ["a.pdf", "b.pdf"]
    assert rows[0]["payload"] == {"company_id": "C002", "period": "2025-Q4"}
    assert rows[1]["attachments"] == ["c.pdf", "d.pdf"]
def test_invalid_fixture_aggregates_locations_and_bad_attachments() -> None:
    with pytest.raises(TaskValidationError) as raised:
        load_and_validate_tasks(FIXTURES / "invalid_tasks.json")
    issues = raised.value.issues
    fields = {issue.field for issue in issues}
    assert {
        "task_id",
        "biz_key",
        "attachments[0]",
        "payload.username",
        "payload.company_id",
        "payload.report_date",
        "payload.electricity",
        "payload.water",
        "payload.employees",
    } <= fields
    assert all(issue.row == 1 for issue in issues)
    assert any("file not found" in issue.message for issue in issues)
    assert issues[0].render().startswith(f"{FIXTURES / 'invalid_tasks.json'}:row 1")
    for shape in ([], {"x": 1}, [42], "", [" ", "x"]):
        assert _issues(_doc(_payload(), attachments=shape))
    assert _issues(_doc(_payload(), attachments=[42]))[0].field == "attachments[0]"
def test_payload_duplicate_and_top_level_errors() -> None:
    messages = [issue.message for issue in _issues(_doc(_payload(username="", company_id="C999", report_date="nope")))]
    assert any("required" in item for item in messages)
    assert any("C001" in item for item in messages) and any("valid date" in item for item in messages)
    first = _doc(_payload())
    second = {**first, "_source_row": 2, "attachments": ["attachment.pdf", " "]}
    fields = [issue.field for issue in _issues(first, second)]
    assert fields.count("task_id") == 1 and fields.count("biz_key") == 1 and "attachments[1]" in fields
    assert any(issue.field == "payload" for issue in _issues(_doc("not-a-dict")))
    assert _issues(_doc(_payload(contact_phone="123")))[0].field == "payload.contact_phone"
def test_relative_attachment_and_json_array_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attachment = tmp_path / "fallback.pdf"
    attachment.write_bytes(b"%PDF-fallback")
    monkeypatch.chdir(tmp_path)
    doc = _doc(_payload(), attachments=["fallback.pdf"])
    doc["_base_dir"] = str(tmp_path / "elsewhere")
    assert validate_raw_tasks([doc])[0].attachment == attachment.resolve()
    path = tmp_path / "batch.json"
    path.write_text(json.dumps([{**_doc(_payload(employees="bad")), "_source_file": None}]))
    with pytest.raises(TaskValidationError) as raised:
        load_and_validate_tasks(path)
    assert raised.value.issues[0].source_file == str(path)
def test_loader_errors(tmp_path: Path) -> None:
    with pytest.raises(TaskValidationError, match="not found"):
        load_raw_tasks(tmp_path / "missing.json")
    unknown = tmp_path / "tasks.txt"
    unknown.write_text("x")
    with pytest.raises(TaskValidationError, match="unsupported"):
        load_raw_tasks(unknown)
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    with pytest.raises(TaskValidationError, match="no header"):
        load_raw_tasks(empty)
    bad = tmp_path / "bad.json"
    bad.write_text('{"task_id": ')
    with pytest.raises(TaskValidationError) as raised:
        load_raw_tasks(bad)
    assert raised.value.issues[0].field == "<json>" and raised.value.issues[0].row >= 1
    root, entry = tmp_path / "root.json", tmp_path / "entry.json"
    root.write_text("42")
    entry.write_text("[1]")
    with pytest.raises(TaskValidationError, match="object or an array"):
        load_raw_tasks(root)
    with pytest.raises(TaskValidationError, match="must be an object"):
        load_raw_tasks(entry)
    columns = tmp_path / "bad.csv"
    columns.write_text("id,key\n1,2\n")
    with pytest.raises(TaskValidationError) as raised:
        load_raw_tasks(columns)
    assert {issue.field for issue in raised.value.issues} == {"task_id", "biz_key"}
