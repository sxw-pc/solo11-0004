"""Load and validate YAML precheck rulesets with dynamic parameter support.

Rule configs may reference ``{{params.name}}``; values come from the file's
``params:`` defaults merged with CLI ``--param name=value`` overrides. A
whole-string reference preserves the parameter's type (numbers stay numbers,
lists stay lists); embedded references are stringified. All errors name the
rules file and the offending rule so misconfiguration is easy to locate.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
import yaml
from fillbot.errors import ConfigError
from fillbot.precheck.checks import RULE_TYPES

PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PARAM_REF_RE = re.compile(r"\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
TOP_LEVEL_KEYS = frozenset({"name", "version", "params", "rules"})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    type: str
    config: dict[str, Any]
    message: str | None = None


@dataclass(frozen=True)
class RuleSet:
    name: str
    rules: tuple[Rule, ...]
    params: dict[str, Any] = field(default_factory=dict)
    source: str = ""


def parse_param_overrides(items: Sequence[str] | None) -> dict[str, Any]:
    """Parse repeated ``--param name=value`` options; values are YAML scalars."""
    overrides: dict[str, Any] = {}
    for item in items or []:
        name, sep, raw = item.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ConfigError(f"--param must be NAME=VALUE, got {item!r}")
        if not PARAM_NAME_RE.fullmatch(name):
            raise ConfigError(f"--param name {name!r} must match {PARAM_NAME_RE.pattern}")
        try:
            overrides[name] = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"--param {name}: cannot parse value {raw!r}: {exc}") from exc
    return overrides


def _yaml_reason(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    if mark is not None and problem:
        return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"
    return str(exc)


def load_ruleset(path: str | Path, *, param_overrides: Mapping[str, Any] | None = None) -> RuleSet:
    rules_path = Path(path)
    if not rules_path.is_file():
        raise ConfigError(f"precheck rules file not found: {rules_path}")
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse precheck rules {rules_path}: {_yaml_reason(exc)}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read precheck rules {rules_path}: {exc}") from exc
    return validate_ruleset(raw, source=str(rules_path), param_overrides=param_overrides)


def _interpolate(node: Any, params: Mapping[str, Any], referenced: set[str], err) -> Any:
    if isinstance(node, str):
        whole = PARAM_REF_RE.fullmatch(node)
        if whole:
            return _param_value(whole.group(1), params, referenced, err)
        if "{{" in node:
            return PARAM_REF_RE.sub(lambda m: str(_param_value(m.group(1), params, referenced, err)), node)
        return node
    if isinstance(node, list):
        return [_interpolate(item, params, referenced, err) for item in node]
    if isinstance(node, dict):
        return {key: _interpolate(value, params, referenced, err) for key, value in node.items()}
    return node


def _param_value(name: str, params: Mapping[str, Any], referenced: set[str], err) -> Any:
    referenced.add(name)
    if name not in params:
        err(f"undefined param '{{{{params.{name}}}}}' (declare it under params: or pass --param {name}=...)")
    return params[name]


def _parse_rule(raw: Any, index: int, source: str, params: Mapping[str, Any], referenced: set[str], err) -> Rule:
    def rerr(message: str) -> None:
        label = raw.get("id") if isinstance(raw, dict) else None
        prefix = f"rule {label!r}" if isinstance(label, str) and label.strip() else f"rule #{index}"
        err(f"{prefix}: {message}")

    if not isinstance(raw, dict):
        rerr("must be a mapping")
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        rerr("id must be a non-empty string")
    rule_id = rule_id.strip()
    rule_type = raw.get("type")
    if rule_type not in RULE_TYPES:
        rerr(f"unknown type {rule_type!r}; available types: {', '.join(sorted(RULE_TYPES))}")
    spec = RULE_TYPES[rule_type]
    body = {key: value for key, value in raw.items() if key not in {"id", "type", "message"}}
    unknown = set(body) - spec.required - spec.optional
    if unknown:
        rerr(f"unknown key(s) for type {rule_type}: {', '.join(sorted(unknown))}")
    missing = spec.required - set(body)
    if missing:
        rerr(f"missing required key(s): {', '.join(sorted(missing))}")
    message = raw.get("message")
    if message is not None and not isinstance(message, str):
        rerr("message must be a string")
    interpolated = _interpolate(body, params, referenced, rerr)
    config = spec.validate(interpolated, rerr)
    custom = _interpolate(message, params, referenced, rerr) if message is not None else None
    return Rule(rule_id, rule_type, config, custom)


def validate_ruleset(
    raw: Any,
    *,
    source: str = "<memory>",
    param_overrides: Mapping[str, Any] | None = None,
) -> RuleSet:
    def err(message: str) -> None:
        raise ConfigError(f"precheck rules {source}: {message}")

    if not isinstance(raw, Mapping):
        err("top level must be a mapping")
    unknown = set(raw) - TOP_LEVEL_KEYS
    if unknown:
        err(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        err("name is required and must be a non-empty string")
    if raw.get("version", 1) != 1:
        err("only ruleset version 1 is supported")
    declared = raw.get("params", {})
    if declared is None:
        declared = {}
    if not isinstance(declared, Mapping):
        err("params must be a mapping of name -> default value")
    for key in declared:
        if not isinstance(key, str) or not PARAM_NAME_RE.fullmatch(key):
            err(f"param name {key!r} must match {PARAM_NAME_RE.pattern}")
    params = {**declared, **(param_overrides or {})}
    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        err("rules must be a non-empty list")
    referenced: set[str] = set()
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_rules, 1):
        rule = _parse_rule(item, index, source, params, referenced, err)
        if rule.rule_id in seen_ids:
            err(f"duplicate rule id {rule.rule_id!r}")
        seen_ids.add(rule.rule_id)
        rules.append(rule)
    unused = set(param_overrides or {}) - set(declared) - referenced
    if unused:
        err(f"param override(s) not declared or referenced by any rule: {', '.join(sorted(unused))}")
    return RuleSet(name.strip(), tuple(rules), dict(params), source)
