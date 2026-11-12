"""DSL security block parsing and validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from fillbot.dsl.schema import validate_script
from fillbot.errors import ScriptError
from fillbot.security.levels import SecurityLevel
from fillbot.security.masking import MaskRule

_BASE_STEPS = [{"action": "open", "url": "http://127.0.0.1/"}]


def _script(security: object | None) -> dict:
    data = {"name": "s", "version": 1, "steps": _BASE_STEPS}
    if security is not None:
        data["security"] = security
    return data


def test_script_without_security_keeps_legacy_defaults() -> None:
    script = validate_script(_script(None))
    assert not script.security.enabled
    assert script.security.default_level is None and script.security.rules == {}


def test_security_block_parses_rules_fields_and_default() -> None:
    script = validate_script(
        _script(
            {
                "default_level": "L2",
                "rules": {
                    "L1": {"strategy": "partial", "keep_prefix": 1},
                    "L4": {"strategy": "redact"},
                },
                "fields": {"password": "L4", "contact_phone": 3},
            }
        )
    )
    assert script.security.enabled
    assert script.security.default_level is SecurityLevel.L2
    assert script.security.rules[SecurityLevel.L1].strategy == "partial"
    assert script.security.rules[SecurityLevel.L4] == MaskRule("redact")
    assert script.security.field_levels["password"] is SecurityLevel.L4
    assert script.security.field_levels["contact_phone"] is SecurityLevel.L3


def test_security_block_validation_errors() -> None:
    bad_blocks = [
        {"rules": []},
        {"rules": {"L9": {"strategy": "full"}}},
        {"rules": {"L1": {"strategy": "partial"}}},  # partial without keep counts
        {"fields": "nope"},
        {"fields": {"password": "L5"}},
        {"default_level": "secret"},
        {"unknown_key": 1},
        "not-a-mapping",
    ]
    for block in bad_blocks:
        with pytest.raises(ScriptError):
            validate_script(_script(block))


def test_unknown_top_level_key_still_rejected() -> None:
    with pytest.raises(ScriptError, match="unknown top-level"):
        validate_script({"name": "s", "version": 1, "steps": _BASE_STEPS, "bogus": 1})


def test_graded_example_script_loads() -> None:
    script_path = Path(__file__).parents[1] / "scripts" / "quarterly_report_graded.yaml"
    from fillbot.dsl.schema import load_script

    loaded = load_script(script_path)
    assert loaded.security.enabled
    assert loaded.security.field_levels["password"] is SecurityLevel.L4
    assert loaded.security.rules[SecurityLevel.L4].strategy == "redact"
    assert len(loaded.steps) == 28
