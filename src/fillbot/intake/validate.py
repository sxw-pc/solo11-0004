"""Field-level task validation with complete, source-located diagnostics."""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from fillbot.errors import TaskValidationError
from fillbot.intake.loader import load_raw_tasks
from fillbot.models import TaskRecord, ValidationIssue
from fillbot.security.levels import SecurityLevel
TASK_ID_RE = re.compile(r"^T-[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
BIZ_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$")
PERIOD_RE = re.compile(r"^\d{4}-Q[1-4]$")
PHONE_RE = re.compile(r"^1\d{10}$")
@dataclass(frozen=True)
class FieldSpec:
    kind: str
    required: bool = False
    regex: re.Pattern[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    enum: tuple[str, ...] = ()
    min_length: int | None = None
    max_length: int | None = None
PAYLOAD_SCHEMA = {
    "username": FieldSpec("string", True, re.compile(r"^[a-z][a-z0-9-]{2,31}$"), max_length=32),
    "password": FieldSpec("string", True, min_length=8, max_length=128),
    "company_id": FieldSpec("enum", True, enum=("C001", "C002", "C003")),
    "company_name": FieldSpec("string", True, min_length=2, max_length=80),
    "period": FieldSpec("string", True, regex=PERIOD_RE),
    "contact_name": FieldSpec("string", True, min_length=2, max_length=40),
    "contact_phone": FieldSpec("string", False, regex=PHONE_RE, min_length=11, max_length=11),
    "report_date": FieldSpec("date", True),
    "electricity": FieldSpec("number", True, minimum=0, maximum=10_000_000),
    "water": FieldSpec("number", True, minimum=0, maximum=1_000_000),
    "gas": FieldSpec("number", True, minimum=0, maximum=1_000_000),
    "employees": FieldSpec("integer", True, minimum=0, maximum=100_000),
    "hires": FieldSpec("integer", True, minimum=0, maximum=10_000),
}
def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())
def _number(value: Any, integer: bool) -> int | float:
    if isinstance(value, bool):
        raise ValueError("boolean")
    if integer:
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            return int(value.strip())
        raise ValueError("integer")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value.strip())
    raise ValueError("number")
def _validate_field(spec: FieldSpec, value: Any) -> tuple[Any, str | None]:
    if _blank(value):
        return None, "is required" if spec.required else None
    if spec.kind == "string":
        if not isinstance(value, (str, int, float)):
            return None, "must be a string"
        text = value if isinstance(value, str) else str(value)
        if spec.min_length is not None and len(text) < spec.min_length:
            return None, f"length must be >= {spec.min_length}"
        if spec.max_length is not None and len(text) > spec.max_length:
            return None, f"length must be <= {spec.max_length}"
        if spec.regex and not spec.regex.fullmatch(text):
            return None, f"does not match {spec.regex.pattern}"
        return text, None
    if spec.kind == "enum":
        candidate = str(value).strip() if isinstance(value, (str, int)) else ""
        return (candidate, None) if candidate in spec.enum else (None, f"must be one of {', '.join(spec.enum)}")
    if spec.kind == "date":
        if not isinstance(value, str):
            return None, "must be a date string (YYYY-MM-DD)"
        try:
            date.fromisoformat(value.strip())
        except ValueError:
            return None, "must be a valid date (YYYY-MM-DD)"
        return value.strip(), None
    if spec.kind in {"number", "integer"}:
        try:
            parsed: Any = _number(value, spec.kind == "integer")
        except (TypeError, ValueError):
            return None, f"must be a {'integer' if spec.kind == 'integer' else 'number'}"
        if spec.minimum is not None and parsed < spec.minimum:
            return None, f"must be >= {spec.minimum:g}"
        if spec.maximum is not None and parsed > spec.maximum:
            return None, f"must be <= {spec.maximum:g}"
        return parsed, None
    return None, f"unsupported schema type {spec.kind}"
def _resolve_attachment(raw: str, base_dir: Path) -> Path | None:
    candidate = Path(raw)
    options = [candidate] if candidate.is_absolute() else [base_dir / candidate, Path.cwd() / candidate]
    for option in options:
        try:
            if option.is_file():
                return option.resolve()
        except OSError:
            continue
    return None
def _attachments(doc: dict[str, Any], source: str, row: int) -> tuple[list[Path], list[ValidationIssue]]:
    raw = doc.get("attachments")
    if raw is None or raw == "" or raw == []:
        return [], [ValidationIssue(source, row, "attachments", "is required")]
    if isinstance(raw, str):
        values: list[Any] = [part.strip() for part in raw.replace("|", ";").split(";") if part.strip()]
    elif isinstance(raw, list):
        values = raw
    else:
        return [], [ValidationIssue(source, row, "attachments", "must be a list of paths")]
    base_dir = Path(str(doc.get("_base_dir", ".")))
    resolved: list[Path] = []
    issues: list[ValidationIssue] = []
    for index, item in enumerate(values):
        field = f"attachments[{index}]"
        if not isinstance(item, str) or not item.strip():
            issues.append(ValidationIssue(source, row, field, "must be a path string"))
            continue
        found = _resolve_attachment(item.strip(), base_dir)
        if found is None:
            issues.append(ValidationIssue(source, row, field, f"file not found: {item.strip()}"))
        else:
            resolved.append(found)
    return resolved, issues
def _key_issue(
    source: str, row: int, field: str, value: Any, pattern: re.Pattern[str], seen: dict[str, int]
) -> tuple[str, list[ValidationIssue]]:
    if _blank(value):
        return "", [ValidationIssue(source, row, field, "is required")]
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        return "", [ValidationIssue(source, row, field, f"must match {pattern.pattern}")]
    text = value.strip()
    if text in seen:
        return text, [ValidationIssue(source, row, field, f"duplicate {field} (first seen at row {seen[text]})")]
    seen[text] = row
    return text, []
def _field_security(
    doc: dict[str, Any], source: str, row: int, known_fields: set[str]
) -> tuple[dict[str, str], list[ValidationIssue]]:
    """Validate the optional per-task sensitive-field grading declaration."""
    raw = doc.get("field_security")
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [ValidationIssue(source, row, "field_security", "must be an object of field=LEVEL")]
    issues: list[ValidationIssue] = []
    resolved: dict[str, str] = {}
    for name, raw_level in raw.items():
        field_name = f"field_security.{name}"
        if not isinstance(name, str) or not name.strip():
            issues.append(ValidationIssue(source, row, "field_security", "field names must be non-empty strings"))
            continue
        try:
            level = SecurityLevel.parse(raw_level)
        except ValueError as exc:
            issues.append(ValidationIssue(source, row, field_name, str(exc)))
            continue
        if name not in known_fields:
            issues.append(
                ValidationIssue(source, row, field_name, f"unknown payload field {name!r}; no grading applied")
            )
            continue
        resolved[name] = level.label
    return resolved, issues


def validate_raw_tasks(docs: list[dict[str, Any]]) -> list[TaskRecord]:
    issues: list[ValidationIssue] = []
    records: list[TaskRecord] = []
    seen_ids: dict[str, int] = {}
    seen_keys: dict[str, int] = {}
    for doc in docs:
        source = str(doc.get("_source_file", "<memory>"))
        row = int(doc.get("_source_row", 0))
        task_id, id_issues = _key_issue(source, row, "task_id", doc.get("task_id"), TASK_ID_RE, seen_ids)
        biz_key, key_issues = _key_issue(source, row, "biz_key", doc.get("biz_key"), BIZ_KEY_RE, seen_keys)
        issues.extend(id_issues + key_issues)
        raw_payload = doc.get("payload", {})
        if not isinstance(raw_payload, dict):
            issues.append(ValidationIssue(source, row, "payload", "must be an object"))
            raw_payload = {}
        normalized_payload = dict(raw_payload)
        for name, spec in PAYLOAD_SCHEMA.items():
            normalized, error = _validate_field(spec, raw_payload.get(name))
            if error:
                issues.append(ValidationIssue(source, row, f"payload.{name}", error))
            elif normalized is not None:
                normalized_payload[name] = normalized
        attachments, attachment_issues = _attachments(doc, source, row)
        issues.extend(attachment_issues)
        field_security, security_issues = _field_security(doc, source, row, set(PAYLOAD_SCHEMA))
        issues.extend(security_issues)
        if task_id and biz_key:
            records.append(
                TaskRecord(
                    task_id,
                    biz_key,
                    normalized_payload,
                    tuple(attachments),
                    row,
                    source,
                    dict(field_security),
                )
            )
    if issues:
        raise TaskValidationError(f"{len(issues)} task validation error(s) across {len(docs)} row(s)", issues)
    return records
def load_and_validate_tasks(path: str | Path) -> list[TaskRecord]:
    return validate_raw_tasks(load_raw_tasks(path))
