"""Unit tests for graded masking rules, level parsing and fail-closed paths."""
from __future__ import annotations

import pytest

from fillbot.errors import ScriptError
from fillbot.security.levels import FAIL_CLOSED_LEVEL, SecurityLevel
from fillbot.security.masking import (
    DEFAULT_RULES,
    LEGACY_LEVEL,
    REDACTION,
    MaskingPolicy,
    MaskRule,
    apply_secret_redactions,
    mask_value,
)


def test_level_parsing_accepts_labels_ints_and_enum() -> None:
    assert SecurityLevel.parse("L1") is SecurityLevel.L1
    assert SecurityLevel.parse("l4") is SecurityLevel.L4
    assert SecurityLevel.parse(3) is SecurityLevel.L3
    assert SecurityLevel.parse(SecurityLevel.L2) is SecurityLevel.L2
    for bad in ("L5", "0", "secret", None, True, 1.5):
        with pytest.raises(ValueError):
            SecurityLevel.parse(bad)


def test_builtin_rules_apply_strategies_deterministically() -> None:
    assert DEFAULT_RULES[SecurityLevel.L1].apply("parkco-b") == "p*******"
    assert DEFAULT_RULES[SecurityLevel.L2].apply("13800001234") == "*******1234"
    assert DEFAULT_RULES[SecurityLevel.L3].apply("anything") == "***"
    assert DEFAULT_RULES[SecurityLevel.L4].apply("anything") == REDACTION
    rule = MaskRule("hash")
    assert rule.apply("secret").startswith("sha256:") and rule.apply("secret") == rule.apply("secret")
    assert MaskRule("full", replacement="##").apply("x") == "##"
    # Partial rule never reveals more than the source contains, and kept
    # prefix/suffix ranges never overlap onto the same character.
    tiny = MaskRule("partial", keep_prefix=4, keep_suffix=4)
    assert tiny.apply("ab") == "a*"
    assert tiny.apply("abcde") == "abcd*"  # keep at least one masked char
    assert MaskRule("partial", keep_prefix=1).apply("") == "*"


def test_mask_rule_rejects_bad_configuration() -> None:
    with pytest.raises(ScriptError):
        MaskRule.from_dict({"strategy": "bogus"})
    with pytest.raises(ScriptError):
        MaskRule.from_dict({"strategy": "partial"})
    with pytest.raises(ScriptError):
        MaskRule.from_dict({"strategy": "partial", "keep_prefix": -1})
    with pytest.raises(ScriptError):
        MaskRule.from_dict({"strategy": "full", "mask_char": "**"})
    with pytest.raises(ScriptError):
        MaskRule.from_dict("not-a-mapping")
    with pytest.raises(ScriptError):
        MaskRule.from_dict({"strategy": "full", "replacement": ""})


def test_policy_create_validates_dsl_payloads() -> None:
    with pytest.raises(ScriptError):
        MaskingPolicy.create(rules={"L9": {"strategy": "full"}})
    with pytest.raises(ScriptError):
        MaskingPolicy.create(field_levels={"password": "L5"})
    with pytest.raises(ScriptError):
        MaskingPolicy.create(field_levels={"": "L1"})
    with pytest.raises(ScriptError):
        MaskingPolicy.create(default_level="nope")
    policy = MaskingPolicy.create(
        rules={"L4": {"strategy": "redact"}},
        field_levels={"password": "L4", "contact_phone": "L3"},
        default_level="L2",
        mask_fields=("password", "token"),
    )
    assert not policy.legacy
    assert policy.level_for("password") is SecurityLevel.L4
    assert policy.level_for("contact_phone") is SecurityLevel.L3
    assert policy.level_for("company_name") is SecurityLevel.L2  # default level
    assert policy.level_for("username") is SecurityLevel.L2


def test_unknown_field_without_default_falls_back_legacy_then_fail_closed() -> None:
    policy = MaskingPolicy.create(field_levels={"password": "L4"}, mask_fields=("password",))
    assert policy.level_for("company_name") is LEGACY_LEVEL
    # Sensitive-by-name fields without an explicit grade fail closed at L4.
    assert policy.level_for("password_reset") is FAIL_CLOSED_LEVEL
    assert policy.rule_for(FAIL_CLOSED_LEVEL).apply("x") == REDACTION
    # A rule table gap never yields plaintext.
    broken = MaskingPolicy(rules={}, field_levels={"a": SecurityLevel.L2})
    assert broken.rule_for(SecurityLevel.L2).apply("plaintext") == REDACTION


def test_mask_value_never_raises_and_never_leaks() -> None:
    class BrokenRule(MaskRule):
        def apply(self, text: str) -> str:  # type: ignore[override]
            raise RuntimeError("boom")

    assert mask_value("plaintext", BrokenRule("full")) == REDACTION
    assert mask_value(None, None) in {"***", REDACTION}


def test_graded_html_sanitization_masks_per_level() -> None:
    html = (
        '<input name="password" value="hunter2secret">'
        '<input name="contact_phone" value="13800001234">'
        '<input name="company_name" value="Acme Corp">'
        '<input name="period" value="2025-Q4">'
        "<p>raw hunter2secret appears here</p>"
    )
    policy = MaskingPolicy.create(
        field_levels={"password": "L4", "contact_phone": "L3", "company_name": "L1"},
        mask_fields=("password",),
    )
    clean = policy.sanitize_html(html, secret_values=["hunter2secret"])
    assert "hunter2secret" not in clean
    assert REDACTION in clean  # L4 input + free-text redaction
    assert 'name="contact_phone" value="***"' in clean  # L3 full mask
    assert 'name="company_name" value="A********"' in clean  # L1 keep prefix
    # Unmarked, non-sensitive control stays intact.
    assert 'name="period" value="2025-Q4"' in clean


def test_legacy_policy_is_byte_identical_to_fixed_rules() -> None:
    from fillbot.browser.snapshot import sanitize_html

    html = '<input name="password" value="abc"><input name="company" value="kept">'
    policy = MaskingPolicy.legacy_policy(("password",))
    assert policy.legacy
    assert policy.sanitize_html(html, ["abc"]) == sanitize_html(html, ("password",), ["abc"])
    # Secret redaction helper mirrors the legacy *** replacement style.
    out = apply_secret_redactions("<p>abcdef</p>", ["abcdef"], MaskRule("full"))
    assert "abcdef" not in out and "***" in out


def test_partial_rule_unicode_aware() -> None:
    rule = MaskRule("partial", keep_prefix=1, keep_suffix=1)
    assert rule.apply("青岚谷科技") == "青***技"
