"""Action registry and structural validation for YAML scripts."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
import yaml
from fillbot.browser.base import Locator
from fillbot.errors import ScriptError, UnregisteredActionError
from fillbot.runner.retry import RetrySpec, parse_retry_spec
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
@dataclass(frozen=True)
class ActionSpec:
    required: frozenset[str]
    optional: frozenset[str] = frozenset()
    locator: bool = False
    optional_locator: bool = False
    @property
    def params(self) -> frozenset[str]:
        return self.required | self.optional
ACTION_REGISTRY = {
    "open": ActionSpec(frozenset({"url"})),
    "fill": ActionSpec(frozenset({"value"}), locator=True),
    "select": ActionSpec(frozenset({"value"}), locator=True),
    "check": ActionSpec(frozenset(), frozenset({"checked"}), locator=True),
    "upload": ActionSpec(frozenset({"path"}), locator=True),
    "click": ActionSpec(frozenset(), locator=True),
    "wait": ActionSpec(frozenset({"seconds"})),
    "assert_text": ActionSpec(frozenset({"text"}), frozenset({"attr"}), optional_locator=True),
    "assert_status": ActionSpec(frozenset({"status"})),
    "extract": ActionSpec(frozenset({"save_as"}), frozenset({"attr"}), locator=True),
    "set_var": ActionSpec(frozenset({"name", "value"})),
}
COMMON_KEYS = frozenset({"on_error", "retry", "timeout", "final"})
ON_ERROR_VALUES = frozenset({"fail", "skip", "continue"})
@dataclass(frozen=True)
class Step:
    index: int
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    locator: Locator | None = None
    on_error: str = "fail"
    retry: RetrySpec = field(default_factory=RetrySpec)
    retry_explicit: bool = False
    timeout: float | None = None
    final: bool = False
@dataclass(frozen=True)
class Script:
    name: str
    version: int
    steps: tuple[Step, ...]
    description: str = ""
def available_actions() -> list[str]:
    return sorted(ACTION_REGISTRY)
def _locator_for(action: str, raw: dict[str, Any], step_no: int) -> Locator | None:
    spec, has_css, has_label = ACTION_REGISTRY[action], "selector" in raw, "label" in raw
    if spec.locator and has_css == has_label:
        raise ScriptError(f"step {step_no}: {action} requires exactly one of selector/label")
    if spec.optional_locator and has_css and has_label:
        raise ScriptError(f"step {step_no}: {action} accepts only one of selector/label")
    if not (spec.locator or spec.optional_locator) and (has_css or has_label):
        raise ScriptError(f"step {step_no}: {action} does not accept a locator")
    key = "selector" if has_css else "label" if has_label else None
    if key is None:
        return None
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ScriptError(f"step {step_no}: {key} must be a non-empty string")
    return Locator(**{("css" if key == "selector" else "label"): value.strip()})
def _parse_retry(raw: Any, step_no: int) -> RetrySpec:
    # Validation lives in the shared retry module so YAML parsing, config
    # defaults and runtime policies all speak the same retry vocabulary.
    try:
        return parse_retry_spec(raw, step_no)
    except ValueError as exc:
        raise ScriptError(str(exc)) from exc
def _check_param_types(action: str, params: dict[str, Any], step_no: int) -> None:
    def fail(message: str) -> None:
        raise ScriptError(f"step {step_no}: {message}")
    if action == "wait":
        seconds = params.get("seconds")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            fail("wait.seconds must be a number")
        if seconds < 0:
            fail("wait.seconds must be >= 0")
    elif action == "assert_status":
        status = params.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            fail("assert_status.status must be an integer 100..599")
    elif action == "check" and "checked" in params and not isinstance(params["checked"], bool):
        fail("check.checked must be a boolean")
    elif action == "extract" and not _valid_identifier(params.get("save_as")):
        fail("extract.save_as must be a valid identifier")
    elif action == "set_var" and not _valid_identifier(params.get("name")):
        fail("set_var.name must be a valid identifier")
    elif action == "open" and (not isinstance(params.get("url"), str) or not params["url"].strip()):
        fail("open.url must be a non-empty string")
    if "attr" in params and (not isinstance(params["attr"], str) or not params["attr"].strip()):
        fail("attr must be a non-empty string")
def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))
def _parse_step(raw: Any, step_no: int) -> Step:
    if not isinstance(raw, dict):
        raise ScriptError(f"step {step_no}: must be a mapping")
    action = raw.get("action")
    if not isinstance(action, str) or not action:
        raise ScriptError(f"step {step_no}: action is required")
    if action not in ACTION_REGISTRY:
        raise UnregisteredActionError(
            f"step {step_no}: unknown action {action!r}; available actions: {', '.join(available_actions())}"
        )
    spec = ACTION_REGISTRY[action]
    locator = _locator_for(action, raw, step_no)
    unknown = set(raw) - (spec.params | COMMON_KEYS | {"selector", "label", "action"})
    if unknown:
        raise ScriptError(f"step {step_no}: unsupported parameter(s) for {action}: {', '.join(sorted(unknown))}")
    missing = set() if action == "extract" else spec.required - set(raw)
    if missing:
        raise ScriptError(f"step {step_no}: {action} missing required parameter(s): {', '.join(sorted(missing))}")
    params = {key: value for key, value in raw.items() if key not in COMMON_KEYS | {"selector", "label", "action"}}
    _check_param_types(action, params, step_no)
    on_error = raw.get("on_error", "fail")
    if on_error not in ON_ERROR_VALUES:
        raise ScriptError(f"step {step_no}: on_error must be fail, skip or continue")
    timeout = raw.get("timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0):
        raise ScriptError(f"step {step_no}: timeout must be > 0")
    final = raw.get("final", False)
    if not isinstance(final, bool):
        raise ScriptError(f"step {step_no}: final must be a boolean")
    if final and action != "click":
        raise ScriptError(f"step {step_no}: final is only valid on click")
    return Step(
        step_no,
        action,
        params,
        locator,
        on_error,
        _parse_retry(raw.get("retry"), step_no),
        "retry" in raw,
        float(timeout) if timeout is not None else None,
        final,
    )
def validate_script(data: Mapping[str, Any]) -> Script:
    if not isinstance(data, Mapping):
        raise ScriptError("script must be a YAML mapping")
    unknown = set(data) - {"name", "version", "steps", "description"}
    if unknown:
        raise ScriptError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScriptError("script.name is required")
    if data.get("version", 1) != 1:
        raise ScriptError("only script version 1 is supported")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScriptError("script.steps must be a non-empty list")
    steps = tuple(_parse_step(item, index) for index, item in enumerate(raw_steps, 1))
    return Script(name.strip(), 1, steps, str(data.get("description", "")))
def load_script(path: str | Path) -> Script:
    script_path = Path(path)
    if not script_path.is_file():
        raise ScriptError(f"script file not found: {script_path}")
    try:
        raw = yaml.safe_load(script_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScriptError(f"cannot read script {script_path}: {exc}") from exc
    if raw is None:
        raise ScriptError(f"script is empty: {script_path}")
    return validate_script(raw)
