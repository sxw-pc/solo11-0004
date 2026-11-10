"""Read JSON/CSV task sheets into raw, source-located mappings."""
from __future__ import annotations
import csv
import json
from pathlib import Path
from typing import Any
from fillbot.errors import TaskValidationError
from fillbot.models import ValidationIssue
_TOP_LEVEL = {"task_id", "biz_key", "attachments", "payload"}
def _issue(path: Path, row: int, field: str, message: str) -> ValidationIssue:
    return ValidationIssue(str(path), row, field, message)
def _fail(message: str, issues: list[ValidationIssue]) -> TaskValidationError:
    return TaskValidationError(message, issues)
def _meta(path: Path, row: int) -> dict[str, Any]:
    return {"_source_file": str(path), "_source_row": row, "_base_dir": str(path.parent)}
def _json_documents(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise _fail(f"cannot read task sheet: {exc}", [_issue(path, 0, "<file>", str(exc))]) from exc
    except json.JSONDecodeError as exc:
        raise _fail("invalid JSON task sheet", [_issue(path, exc.lineno, "<json>", exc.msg)]) from exc
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise _fail(
            "JSON task sheet must be an object or an array of objects",
            [_issue(path, 1, "<root>", "expected object or array")],
        )
    docs = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise _fail("task entry must be an object", [_issue(path, index, "<task>", "expected object")])
        docs.append({**item, **_meta(path, index)})
    return docs
def _split_attachments(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace("|", ";").split(";") if part.strip()]
def _csv_documents(path: Path) -> list[dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise _fail(f"cannot read task sheet: {exc}", [_issue(path, 0, "<file>", str(exc))]) from exc
    with handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise _fail("CSV task sheet has no header row", [_issue(path, 1, "<header>", "missing header")])
        headers = [name.strip() for name in reader.fieldnames]
        missing = {"task_id", "biz_key"} - set(headers)
        if missing:
            raise _fail(
                "CSV task sheet is missing required columns",
                [_issue(path, 1, name, "missing required column") for name in sorted(missing)],
            )
        docs = []
        for row_number, row in enumerate(reader, 2):
            doc = _meta(path, row_number)
            payload: dict[str, Any] = {}
            for raw_key, raw_value in row.items():
                if raw_key is None:
                    continue
                key, value = raw_key.strip(), raw_value if raw_value is not None else ""
                if key == "attachments":
                    doc["attachments"] = _split_attachments(value)
                elif key in _TOP_LEVEL:
                    doc[key] = str(value).strip()
                else:
                    payload[key] = str(value).strip()
            doc["payload"] = payload
            docs.append(doc)
    return docs
def load_raw_tasks(path: str | Path) -> list[dict[str, Any]]:
    sheet = Path(path)
    if not sheet.is_file():
        raise _fail(f"task sheet not found: {sheet}", [_issue(sheet, 0, "<file>", "not found")])
    suffix = sheet.suffix.lower()
    if suffix == ".json":
        return _json_documents(sheet)
    if suffix == ".csv":
        return _csv_documents(sheet)
    raise _fail(f"unsupported task sheet type: {suffix}", [_issue(sheet, 0, "<file>", "expected .json or .csv")])
