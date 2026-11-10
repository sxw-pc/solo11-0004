"""Server-side validation for the fictional reporting workflow."""
from __future__ import annotations
import re
from datetime import date
from typing import Any, Mapping
from fillbot.mocksite.seed import Account
PERIOD_RE = re.compile(r"^\d{4}-Q[1-4]$")
ALLOWED_SUFFIXES = {".pdf", ".png"}
MAX_ATTACHMENT_BYTES = 64 * 1024
Errors = dict[str, list[str]]
def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key, "")
    return value.strip() if isinstance(value, str) else str(value).strip()
def _add(errors: Errors, key: str, message: str) -> None:
    errors.setdefault(key, []).append(message)
def _number(
    errors: Errors,
    data: Mapping[str, Any],
    key: str,
    label: str,
    minimum: float,
    maximum: float,
    *,
    integer: bool = False,
) -> float | int | None:
    raw = _text(data, key)
    if not raw:
        _add(errors, key, f"{label}不能为空")
        return None
    try:
        value: float | int = int(raw) if integer else float(raw)
    except ValueError:
        _add(errors, key, f"{label}必须是{'整数' if integer else '数字'}")
        return None
    if value < minimum or value > maximum:
        _add(errors, key, f"{label}必须在 {minimum:g} 到 {maximum:g} 之间")
        return None
    return value
def validate_step1(data: Mapping[str, Any], account: Account) -> tuple[dict[str, str], Errors]:
    errors: Errors = {}
    company_id = _text(data, "company_id")
    if company_id != account.company_id:
        _add(errors, "company_id", "企业编号与登录账号不匹配")
    company_name = _text(data, "company_name")
    if not company_name:
        _add(errors, "company_name", "企业名称不能为空")
    elif len(company_name) > 80:
        _add(errors, "company_name", "企业名称不能超过 80 个字符")
    period = _text(data, "period")
    if not PERIOD_RE.fullmatch(period):
        _add(errors, "period", "报送季度格式应为 YYYY-QN")
    contact_name = _text(data, "contact_name")
    if not contact_name:
        _add(errors, "contact_name", "联系人不能为空")
    report_date = _text(data, "report_date")
    try:
        date.fromisoformat(report_date)
    except ValueError:
        _add(errors, "report_date", "报送日期格式应为 YYYY-MM-DD")
    values = {
        "company_id": company_id,
        "company_name": company_name,
        "period": period,
        "contact_name": contact_name,
        "report_date": report_date,
    }
    return values, errors
def validate_step2(data: Mapping[str, Any]) -> tuple[dict[str, Any], Errors]:
    errors: Errors = {}
    electricity = _number(errors, data, "electricity", "用电量", 0, 10_000_000)
    water = _number(errors, data, "water", "用水量", 0, 1_000_000)
    gas = _number(errors, data, "gas", "用气量", 0, 1_000_000)
    return {
        "electricity": electricity,
        "water": water,
        "gas": gas,
    }, errors
def validate_step3(data: Mapping[str, Any], upload: Any) -> tuple[dict[str, Any], Errors]:
    errors: Errors = {}
    employees = _number(errors, data, "employees", "期末从业人数", 0, 10_000, integer=True)
    hires = _number(errors, data, "hires", "本季新增人数", 0, 2_000, integer=True)
    if _text(data, "confirmed") not in {"on", "true", "1"}:
        _add(errors, "confirmed", "必须确认数据真实性")
    filename = str(getattr(upload, "filename", "") or "")
    if not filename:
        _add(errors, "attachment", "必须上传附件")
        size = 0
    else:
        suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix not in ALLOWED_SUFFIXES:
            _add(errors, "attachment", "附件仅支持 PDF 或 PNG")
        payload = upload.read()
        size = len(payload)
        if size == 0:
            _add(errors, "attachment", "附件不能为空")
        elif size > MAX_ATTACHMENT_BYTES:
            _add(errors, "attachment", "附件不能超过 64 KiB")
    return {
        "employees": employees,
        "hires": hires,
        "confirmed": True,
        "attachment_name": filename,
        "attachment_size": size,
    }, errors
