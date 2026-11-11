"""Cross-field rule check implementations for the offline precheck engine.

Each rule type provides a ``validate`` hook (run once at load time, raising via
the supplied ``err`` callback) and a ``check``/``check_batch`` hook (run per
task or per batch, returning human-readable violation messages). Everything is
pure in-memory computation: no I/O, no network.
"""
from __future__ import annotations
import operator
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Sequence
from fillbot.models import TaskRecord

PERIOD_RE = re.compile(r"^(\d{4})-Q([1-4])$")
OPS = {
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    ">": operator.gt,
}
ORDERING_OPS = frozenset({"<", "<=", ">", ">="})
CONDITION_OPS = frozenset({"==", "!=", ">", ">=", "<", "<=", "in", "not_in", "matches", "blank", "not_blank"})
_NO_VALUE_OPS = frozenset({"blank", "not_blank"})

ErrFn = Callable[[str], None]


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _num(value: Any) -> float | None:
    """Coerce to float; booleans and non-numeric values yield None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _fmt(value: float) -> str:
    return f"{value:g}"


def _values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)):
        coerced = _num(actual)
        return coerced is not None and coerced == expected
    if actual is None or expected is None:
        return actual is expected
    return str(actual) == str(expected)


def _compare(actual: Any, op: str, expected: Any) -> bool:
    if op in ("==", "!="):
        equal = _values_equal(actual, expected)
        return equal if op == "==" else not equal
    left, right = _num(actual), _num(expected)
    if left is None or right is None:
        return False
    return bool(OPS[op](left, right))


def _condition_desc(cond: dict[str, Any]) -> str:
    if cond["op"] in _NO_VALUE_OPS:
        return f"{cond['field']} {cond['op']}"
    return f"{cond['field']} {cond['op']} {cond.get('value')!r}"


def validate_condition(raw: Any, err: ErrFn, *, label: str = "condition") -> dict[str, Any]:
    if not isinstance(raw, dict):
        err(f"{label} must be a mapping")
    unknown = set(raw) - {"field", "op", "value"}
    if unknown:
        err(f"{label} has unknown key(s): {', '.join(sorted(unknown))}")
    field_name = raw.get("field")
    if not isinstance(field_name, str) or not field_name.strip():
        err(f"{label}.field must be a non-empty string")
    op = raw.get("op")
    if op not in CONDITION_OPS:
        err(f"{label}.op must be one of {', '.join(sorted(CONDITION_OPS))}")
    if op in _NO_VALUE_OPS:
        if "value" in raw:
            err(f"{label}.value is not allowed for op {op}")
    elif "value" not in raw:
        err(f"{label}.value is required for op {op}")
    value = raw.get("value")
    if op in ORDERING_OPS and _num(value) is None:
        err(f"{label}.value must be numeric for op {op}")
    if op in {"in", "not_in"} and (not isinstance(value, list) or not value):
        err(f"{label}.value must be a non-empty list for op {op}")
    normalized = {"field": field_name.strip(), "op": op, "value": value}
    if op == "matches":
        if not isinstance(value, str) or not value:
            err(f"{label}.value must be a non-empty regex string for op matches")
        try:
            normalized["_pattern"] = re.compile(value)
        except re.error as exc:
            err(f"{label}.value is not a valid regex: {exc}")
    normalized["_desc"] = _condition_desc(normalized)
    return normalized


def eval_condition(cond: dict[str, Any], payload: dict[str, Any]) -> bool:
    actual = payload.get(cond["field"])
    op = cond["op"]
    if op == "blank":
        return _blank(actual)
    if op == "not_blank":
        return not _blank(actual)
    if op in OPS:
        return _compare(actual, op, cond.get("value"))
    if op in ("in", "not_in"):
        found = any(_values_equal(actual, item) for item in cond["value"])
        return not found if op == "not_in" else found
    if op == "matches":
        return actual is not None and bool(cond["_pattern"].fullmatch(str(actual)))
    return False  # pragma: no cover - ops are validated at load time


def _require_str(value: Any, name: str, err: ErrFn) -> str:
    if not isinstance(value, str) or not value.strip():
        err(f"{name} must be a non-empty string")
    return value.strip()


def _require_number(value: Any, name: str, err: ErrFn) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        err(f"{name} must be a number")
    return float(value)


def _require_str_list(value: Any, name: str, err: ErrFn) -> list[str]:
    if not isinstance(value, list) or not value:
        err(f"{name} must be a non-empty list of field names")
    return [_require_str(item, f"{name}[{index}]", err) for index, item in enumerate(value)]


def _check_bounds(cfg: dict[str, Any], err: ErrFn) -> None:
    if "min" not in cfg and "max" not in cfg:
        err("at least one of min/max is required")
    for key in ("min", "max"):
        if key in cfg:
            cfg[key] = _require_number(cfg[key], key, err)
    if "min" in cfg and "max" in cfg and cfg["min"] > cfg["max"]:
        err("min cannot exceed max")


def _field_value(task: TaskRecord, field: str) -> tuple[float | None, str | None]:
    value = _num(task.payload.get(field))
    if value is None:
        return None, f"field '{field}' is missing or not numeric (got {task.payload.get(field)!r})"
    return value, None


# --- numeric_threshold -----------------------------------------------------
def _validate_numeric_threshold(cfg: dict[str, Any], err: ErrFn) -> dict[str, Any]:
    out = {"field": _require_str(cfg.get("field"), "field", err)}
    _check_bounds(cfg, err)
    out.update({key: cfg[key] for key in ("min", "max") if key in cfg})
    return out


def _check_numeric_threshold(task: TaskRecord, cfg: dict[str, Any]) -> list[str]:
    value, problem = _field_value(task, cfg["field"])
    if problem:
        return [problem]
    issues = []
    if "min" in cfg and value < cfg["min"]:
        issues.append(f"{cfg['field']}={_fmt(value)} is below minimum {_fmt(cfg['min'])}")
    if "max" in cfg and value > cfg["max"]:
        issues.append(f"{cfg['field']}={_fmt(value)} exceeds maximum {_fmt(cfg['max'])}")
    return issues


# --- field_relation --------------------------------------------------------
def _validate_field_relation(cfg: dict[str, Any], err: ErrFn) -> dict[str, Any]:
    out = {"left": _require_str(cfg.get("left"), "left", err)}
    op = cfg.get("op")
    if op not in OPS:
        err(f"op must be one of {', '.join(OPS)}")
    out["op"] = op
    has_field, has_value = "right_field" in cfg, "right_value" in cfg
    if has_field == has_value:
        err("exactly one of right_field/right_value is required")
    if has_field:
        out["right_field"] = _require_str(cfg["right_field"], "right_field", err)
    else:
        out["right_value"] = _require_number(cfg["right_value"], "right_value", err)
    return out


def _check_field_relation(task: TaskRecord, cfg: dict[str, Any]) -> list[str]:
    left, problem = _field_value(task, cfg["left"])
    if problem:
        return [problem]
    if "right_field" in cfg:
        right, problem = _field_value(task, cfg["right_field"])
        if problem:
            return [problem]
        desc = cfg["right_field"]
    else:
        right, desc = cfg["right_value"], "value"
    if not OPS[cfg["op"]](left, right):
        return [f"{cfg['left']}={_fmt(left)} must be {cfg['op']} {desc}={_fmt(right)}"]
    return []


# --- logical_association ---------------------------------------------------
def _validate_logical_association(cfg: dict[str, Any], err: ErrFn) -> dict[str, Any]:
    when = validate_condition(cfg.get("when"), err, label="when")
    raw_then = cfg.get("then")
    if not isinstance(raw_then, list) or not raw_then:
        err("then must be a non-empty list")
    then = []
    for index, item in enumerate(raw_then):
        if not isinstance(item, dict):
            err(f"then[{index}] must be a mapping")
        if "require" in item:
            if set(item) != {"require"}:
                err(f"then[{index}] with 'require' accepts no other keys")
            then.append({"require": _require_str(item["require"], f"then[{index}].require", err)})
        else:
            then.append(validate_condition(item, err, label=f"then[{index}]"))
    return {"when": when, "then": then}


def _check_logical_association(task: TaskRecord, cfg: dict[str, Any]) -> list[str]:
    when = cfg["when"]
    if not eval_condition(when, task.payload):
        return []
    issues = []
    for req in cfg["then"]:
        if "require" in req:
            if _blank(task.payload.get(req["require"])):
                issues.append(f"'{req['require']}' is required when {when['_desc']}")
        elif not eval_condition(req, task.payload):
            actual = task.payload.get(req["field"])
            issues.append(f"expected {req['_desc']} when {when['_desc']}, got {actual!r}")
    return issues


# --- period_match ----------------------------------------------------------
def _period_index(text: str) -> int:
    match = PERIOD_RE.fullmatch(text)
    return int(match.group(1)) * 4 + int(match.group(2))  # type: ignore[union-attr]


def _validate_period(cfg: Any, name: str, err: ErrFn) -> str:
    text = _require_str(cfg, name, err)
    if not PERIOD_RE.fullmatch(text):
        err(f"{name} must be a period like 2025-Q4, got {text!r}")
    return text


def _validate_period_match(cfg: dict[str, Any], err: ErrFn) -> dict[str, Any]:
    out = {"period_field": _require_str(cfg.get("period_field"), "period_field", err)}
    selectors = ("date_field", "biz_key_suffix", "allowed", "not_before", "not_after")
    if not any(key in cfg for key in selectors):
        err(f"at least one of {', '.join(selectors)} is required")
    if "date_field" in cfg:
        out["date_field"] = _require_str(cfg["date_field"], "date_field", err)
    if "biz_key_suffix" in cfg:
        if not isinstance(cfg["biz_key_suffix"], bool):
            err("biz_key_suffix must be a boolean")
        out["biz_key_suffix"] = cfg["biz_key_suffix"]
    if "allowed" in cfg:
        allowed = cfg["allowed"]
        if not isinstance(allowed, list) or not allowed:
            err("allowed must be a non-empty list of periods")
        out["allowed"] = [_validate_period(item, f"allowed[{index}]", err) for index, item in enumerate(allowed)]
    for key in ("not_before", "not_after"):
        if key in cfg:
            out[key] = _validate_period(cfg[key], key, err)
    if "not_before" in out and "not_after" in out and _period_index(out["not_before"]) > _period_index(out["not_after"]):
        err("not_before cannot be after not_after")
    return out


def _check_period_match(task: TaskRecord, cfg: dict[str, Any]) -> list[str]:
    field = cfg["period_field"]
    raw = task.payload.get(field)
    match = PERIOD_RE.fullmatch(str(raw).strip()) if raw is not None else None
    if match is None:
        return [f"{field}={raw!r} is not a valid period (expected YYYY-Qn)"]
    period = f"{match.group(1)}-Q{match.group(2)}"
    index = _period_index(period)
    issues = []
    if "allowed" in cfg and period not in cfg["allowed"]:
        issues.append(f"{field}={period} is not in allowed periods {', '.join(cfg['allowed'])}")
    if "not_before" in cfg and index < _period_index(cfg["not_before"]):
        issues.append(f"{field}={period} is before {cfg['not_before']}")
    if "not_after" in cfg and index > _period_index(cfg["not_after"]):
        issues.append(f"{field}={period} is after {cfg['not_after']}")
    if "date_field" in cfg:
        date_field = cfg["date_field"]
        raw_date = task.payload.get(date_field)
        try:
            parsed = date.fromisoformat(str(raw_date).strip())
        except (TypeError, ValueError):
            issues.append(f"{date_field}={raw_date!r} is not a valid date (YYYY-MM-DD)")
        else:
            quarter = f"{parsed.year}-Q{(parsed.month - 1) // 3 + 1}"
            if quarter != period:
                issues.append(f"{date_field}={parsed.isoformat()} falls in {quarter}, not {field}={period}")
    if cfg.get("biz_key_suffix") and not task.biz_key.endswith(f":{period}"):
        issues.append(f"biz_key={task.biz_key!r} does not end with ':{period}'")
    return issues


# --- value_mapping ---------------------------------------------------------
def _validate_value_mapping(cfg: dict[str, Any], err: ErrFn) -> dict[str, Any]:
    out = {"field": _require_str(cfg.get("field"), "field", err)}
    raw_map = cfg.get("map")
    if not isinstance(raw_map, dict) or not raw_map:
        err("map must be a non-empty mapping of source value -> {field: [allowed, ...]}")
    mapping: dict[str, dict[str, frozenset[str]]] = {}
    for source, targets in raw_map.items():
        if not isinstance(targets, dict) or not targets:
            err(f"map[{source!r}] must be a non-empty mapping of field -> allowed values")
        entry = {}
        for target, allowed in targets.items():
            target_name = _require_str(target, f"map[{source!r}] key", err)
            if not isinstance(allowed, list) or not allowed:
                err(f"map[{source!r}].{target_name} must be a non-empty list")
            entry[target_name] = frozenset(str(item) for item in allowed)
        mapping[str(source)] = entry
    out["map"] = mapping
    on_unmapped = cfg.get("on_unmapped", "error")
    if on_unmapped not in {"error", "ignore"}:
        err("on_unmapped must be error or ignore")
    out["on_unmapped"] = on_unmapped
    return out


def _check_value_mapping(task: TaskRecord, cfg: dict[str, Any]) -> list[str]:
    field = cfg["field"]
    actual = task.payload.get(field)
    key = str(actual).strip() if actual is not None else ""
    entry = cfg["map"].get(key)
    if entry is None:
        if cfg["on_unmapped"] == "ignore":
            return []
        return [f"{field}={actual!r} has no mapping defined"]
    issues = []
    for target, allowed in entry.items():
        value = task.payload.get(target)
        if str(value) not in allowed:
            issues.append(f"{target}={value!r} is not allowed for {field}={key!r}; expected one of {', '.join(sorted(allowed))}")
    return issues


# --- aggregate_sum ---------------------------------------------------------
def _validate_aggregate_sum(cfg: dict[str, Any], err: ErrFn) -> dict[str, Any]:
    out = {"fields": _require_str_list(cfg.get("fields"), "fields", err)}
    _check_bounds(cfg, err)
    out.update({key: cfg[key] for key in ("min", "max") if key in cfg})
    return out


def _check_aggregate_sum(task: TaskRecord, cfg: dict[str, Any]) -> list[str]:
    total, missing = 0.0, []
    for field in cfg["fields"]:
        value = _num(task.payload.get(field))
        if value is None:
            missing.append(field)
        else:
            total += value
    if missing:
        return [f"field(s) {', '.join(missing)} are missing or not numeric for sum"]
    label = f"sum({'+'.join(cfg['fields'])})={_fmt(total)}"
    issues = []
    if "min" in cfg and total < cfg["min"]:
        issues.append(f"{label} is below minimum {_fmt(cfg['min'])}")
    if "max" in cfg and total > cfg["max"]:
        issues.append(f"{label} exceeds maximum {_fmt(cfg['max'])}")
    return issues


# --- batch_unique ----------------------------------------------------------
def _validate_batch_unique(cfg: dict[str, Any], err: ErrFn) -> dict[str, Any]:
    return {"fields": _require_str_list(cfg.get("fields"), "fields", err)}


def _check_batch_unique(tasks: Sequence[TaskRecord], cfg: dict[str, Any]) -> list[tuple[TaskRecord, str]]:
    groups: dict[tuple[str, ...], list[TaskRecord]] = {}
    for task in tasks:
        key = tuple(str(task.payload.get(field)) for field in cfg["fields"])
        groups.setdefault(key, []).append(task)
    findings = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        ids = [member.task_id for member in members]
        label = "=".join(key)
        for member in members:
            others = ", ".join(other for other in ids if other != member.task_id)
            findings.append((member, f"duplicate {'+'.join(cfg['fields'])} combination {label!r} also used by {others}"))
    return findings


@dataclass(frozen=True)
class RuleType:
    name: str
    required: frozenset[str]
    optional: frozenset[str]
    validate: Callable[[dict[str, Any], ErrFn], dict[str, Any]]
    check: Callable[..., Any]
    batch: bool = False


RULE_TYPES: dict[str, RuleType] = {
    spec.name: spec
    for spec in (
        RuleType(
            "numeric_threshold",
            frozenset({"field"}),
            frozenset({"min", "max"}),
            _validate_numeric_threshold,
            _check_numeric_threshold,
        ),
        RuleType(
            "field_relation",
            frozenset({"left", "op"}),
            frozenset({"right_field", "right_value"}),
            _validate_field_relation,
            _check_field_relation,
        ),
        RuleType(
            "logical_association",
            frozenset({"when", "then"}),
            frozenset(),
            _validate_logical_association,
            _check_logical_association,
        ),
        RuleType(
            "period_match",
            frozenset({"period_field"}),
            frozenset({"date_field", "biz_key_suffix", "allowed", "not_before", "not_after"}),
            _validate_period_match,
            _check_period_match,
        ),
        RuleType(
            "value_mapping",
            frozenset({"field", "map"}),
            frozenset({"on_unmapped"}),
            _validate_value_mapping,
            _check_value_mapping,
        ),
        RuleType(
            "aggregate_sum",
            frozenset({"fields"}),
            frozenset({"min", "max"}),
            _validate_aggregate_sum,
            _check_aggregate_sum,
        ),
        RuleType(
            "batch_unique",
            frozenset({"fields"}),
            frozenset(),
            _validate_batch_unique,
            _check_batch_unique,
            batch=True,
        ),
    )
}
