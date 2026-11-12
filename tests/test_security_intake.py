"""Task-sheet sensitive-field grading: JSON/CSV parsing, validation, errors."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fillbot.errors import TaskValidationError
from fillbot.intake.loader import load_raw_tasks, parse_field_security
from fillbot.intake.validate import validate_raw_tasks


def _doc(payload: object, *, field_security: object = None, row: int = 1) -> dict:
    doc = {
        "task_id": f"T-{row}",
        "biz_key": f"C002:2025-Q{row}",
        "payload": payload,
        "attachments": ["attachment.pdf"],
        "field_security": field_security,
        "_source_file": "memory.json",
        "_source_row": row,
        "_base_dir": str(Path(__file__).parent / "fixtures"),
    }
    if field_security is None:
        doc.pop("field_security")
    return doc


def _payload() -> dict:
    return {
        "username": "parkco-b",
        "password": "mock-only-beta",
        "company_id": "C002",
        "company_name": "青岚谷生态科技（虚构）",
        "period": "2025-Q4",
        "contact_name": "林小岚（虚构）",
        "report_date": "2025-11-10",
        "electricity": 12800.5,
        "water": 340.2,
        "gas": 88.1,
        "employees": 42,
        "hires": 3,
    }


def test_parse_field_security_shapes() -> None:
    assert parse_field_security(None) is None
    assert parse_field_security("") is None
    assert parse_field_security({"password": "L4"}) == {"password": "L4"}
    assert parse_field_security('{"password": "L4"}') == {"password": "L4"}
    assert parse_field_security("password=L4;contact_phone=L3") == {
        "password": "L4",
        "contact_phone": "L3",
    }
    assert parse_field_security("password=L4|contact_phone=L3")["contact_phone"] == "L3"
    with pytest.raises(ValueError):
        parse_field_security("password")
    with pytest.raises(ValueError):
        parse_field_security("[1, 2]")
    with pytest.raises(ValueError):
        parse_field_security('["not-object"]')


def test_graded_json_task_sheet_round_trip() -> None:
    doc = _doc(_payload(), field_security={"password": "L4", "contact_name": "L2"})
    record = validate_raw_tasks([doc])[0]
    assert record.field_security == {"password": "L4", "contact_name": "L2"}
    assert record.company_id == "C002"


def test_legacy_sheets_have_empty_grading() -> None:
    record = validate_raw_tasks([_doc(_payload())])[0]
    assert record.field_security == {}


def test_invalid_levels_and_unknown_fields_are_diagnosed() -> None:
    with pytest.raises(TaskValidationError) as raised:
        validate_raw_tasks([_doc(_payload(), field_security={"password": "L9", "ghost": "L1"})])
    fields = {issue.field for issue in raised.value.issues}
    assert "field_security.password" in fields
    assert "field_security.ghost" in fields


def test_bad_field_security_shape() -> None:
    with pytest.raises(TaskValidationError) as raised:
        validate_raw_tasks([_doc(_payload(), field_security="not-a-mapping")])
    assert any(issue.field == "field_security" for issue in raised.value.issues)


def test_csv_field_security_column(tmp_path: Path) -> None:
    attachment = Path(__file__).parent / "fixtures" / "attachment.pdf"
    header = (
        "task_id,biz_key,attachments,field_security,username,password,company_id,company_name,period,"
        "contact_name,report_date,electricity,water,gas,employees,hires\n"
    )
    row_template = (
        "T-1,C002:2025-Q4,{att},{grades},parkco-b,mock-only-beta,C002,Acme,2025-Q4,"
        "Lin,2025-11-10,1,2,3,4,5\n"
    )
    csv_path = tmp_path / "graded.csv"
    csv_path.write_text(
        header + row_template.format(att=attachment, grades="password=L4;contact_name=L2"),
        encoding="utf-8",
    )
    docs = load_raw_tasks(csv_path)
    assert docs[0]["field_security"] == {"password": "L4", "contact_name": "L2"}
    records = validate_raw_tasks(docs)
    assert records[0].field_security["password"] == "L4"

    # JSON-object form in a CSV cell also works.
    csv_path.write_text(
        header + row_template.format(att=attachment, grades='"{""password"": ""L3""}"'),
        encoding="utf-8",
    )
    records = validate_raw_tasks(load_raw_tasks(csv_path))
    assert records[0].field_security == {"password": "L3"}

    bad = tmp_path / "bad.csv"
    bad.write_text(
        "task_id,biz_key,field_security\nT-3,C002:2025-Q4,garbage-no-equals\n", encoding="utf-8"
    )
    with pytest.raises(TaskValidationError) as raised:
        load_raw_tasks(bad)
    assert raised.value.issues[0].field == "field_security"


def test_graded_json_file_loader(tmp_path: Path) -> None:
    path = tmp_path / "graded.json"
    path.write_text(
        json.dumps([_doc(_payload(), field_security={"password": "L4"})]),
        encoding="utf-8",
    )
    docs = load_raw_tasks(path)
    assert docs[0]["field_security"] == {"password": "L4"}
