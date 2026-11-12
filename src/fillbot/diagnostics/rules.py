"""Predefined root-cause rules with explicit priority resolution.

Rules are pure data: no I/O and no exceptions. Conflicting matches are resolved
by ``(priority, rule id)`` so the outcome is deterministic and documented.
"""
from __future__ import annotations

import re
from typing import Iterable

from fillbot.diagnostics.models import ErrorFeatures, RootCause, Rule

# Stage labels used by the instrumentation points.
STAGE_INTAKE = "intake"
STAGE_INTERACTION = "interaction"
STAGE_SUBMIT = "submit"
STAGE_RECEIPT = "receipt"
STAGE_STEP = "step"
STAGE_RUNTIME = "runtime"


def _rules() -> tuple[Rule, ...]:
    # Priority bands (higher wins on conflicts):
    #   100  explicit protocol/status signals (429, 403, receipt contract...)
    #    90  stage-specific semantic signals (login page, attachment limits...)
    #    80  server-side field validation / state machine errors
    #    60  generic transport and assertion buckets
    #    40  script/input contract errors
    return (
        Rule(
            "rate-limited-429",
            RootCause.RATE_LIMITED,
            100,
            error_classes=frozenset({"SERVER"}),
            statuses=frozenset({429}),
        ),
        Rule(
            "permission-denied-403",
            RootCause.PERMISSION_DENIED,
            100,
            statuses=frozenset({403}),
        ),
        Rule(
            "duplicate-submission-409",
            RootCause.DUPLICATE_SUBMISSION,
            100,
            statuses=frozenset({409}),
            message_regex=("重复报送", "已报送", "duplicate submission"),
        ),
        Rule(
            "receipt-format",
            RootCause.RECEIPT_INVALID,
            100,
            stages=frozenset({STAGE_RECEIPT}),
            message_regex=("malformed receipt", "without extracting receipt", "receipt number", "回执"),
        ),
        Rule(
            "login-page-midflow",
            RootCause.SESSION_EXPIRED,
            95,
            stages=frozenset({STAGE_INTERACTION, STAGE_SUBMIT, STAGE_STEP}),
            statuses=frozenset({200, 302, 303, 307}),
            excluded_statuses=frozenset({401}),
            requires_login_page=True,
        ),
        Rule(
            "login-failed",
            RootCause.LOGIN_FAILED,
            90,
            stages=frozenset({STAGE_INTERACTION, STAGE_SUBMIT}),
            message_regex=("账号或口令", "账号或密码", "口令不正确", "登录失败", "unauthorized"),
        ),
        Rule(
            "login-failed-status",
            RootCause.LOGIN_FAILED,
            89,
            stages=frozenset({STAGE_INTERACTION, STAGE_SUBMIT}),
            statuses=frozenset({401}),
        ),
        Rule(
            "csrf-invalid",
            RootCause.CSRF_INVALID,
            90,
            message_regex=("csrf", "令牌校验", "csrf token"),
        ),
        Rule(
            "attachment-limit-client",
            RootCause.ATTACHMENT_LIMIT,
            90,
            stages=frozenset({STAGE_INTERACTION, STAGE_SUBMIT, STAGE_RECEIPT}),
            statuses=frozenset({400, 413}),
            message_regex=(
                "附件仅支持",
                "附件不能超过",
                "附件不能为空",
                "附件超出",
                "附件过大",
                "attachment too large",
                "file too large",
                "payload too large",
                "exceeds the (maximum|size limit)",
            ),
        ),
        Rule(
            "attachment-limit-page",
            RootCause.ATTACHMENT_LIMIT,
            88,
            stages=frozenset({STAGE_INTERACTION, STAGE_SUBMIT}),
            page_regex=(
                "附件仅支持",
                "附件不能超过",
                "附件不能为空",
                "附件超出",
                "附件过大",
                "attachment too large",
                "file too large",
                "exceeds the (maximum|size limit)",
            ),
        ),
        Rule(
            "attachment-missing",
            RootCause.ATTACHMENT_MISSING,
            85,
            error_classes=frozenset({"SCRIPT"}),
            message_regex=("upload file not found", "附件缺失", "file not found"),
        ),
        Rule(
            "form-field-server",
            RootCause.FORM_FIELD_MISMATCH,
            80,
            stages=frozenset({STAGE_INTERACTION, STAGE_SUBMIT}),
            statuses=frozenset({400, 422}),
            page_regex=(
                "不能为空",
                "必须是",
                "必须在",
                "不能超过 80",
                "格式应为",
                "与登录账号不匹配",
                "不匹配",
                "field",
                "invalid",
            ),
        ),
        Rule(
            "form-field-selector",
            RootCause.FORM_FIELD_MISMATCH,
            78,
            error_classes=frozenset({"SCRIPT"}),
            message_regex=("selector not found", "label not found", "label has no associated control", "control is not", "option .* not found", "not inside a form"),
        ),
        Rule(
            "state-order",
            RootCause.STATE_MISMATCH,
            75,
            statuses=frozenset({400}),
            page_regex=("请先完成", "步骤顺序", "step order"),
        ),
        Rule(
            "input-validation",
            RootCause.INPUT_INVALID,
            100,
            stages=frozenset({STAGE_INTAKE}),
            error_classes=frozenset({"SCRIPT"}),
        ),
        Rule(
            "script-contract",
            RootCause.SCRIPT_INVALID,
            40,
            error_classes=frozenset({"SCRIPT"}),
        ),
        Rule(
            "network-timeout",
            RootCause.NETWORK_FAILURE,
            60,
            error_classes=frozenset({"NETWORK"}),
        ),
        Rule(
            "server-5xx",
            RootCause.SERVER_FAILURE,
            60,
            error_classes=frozenset({"SERVER"}),
        ),
        Rule(
            "client-4xx-other",
            RootCause.FORM_FIELD_MISMATCH,
            55,
            error_classes=frozenset({"CLIENT"}),
            statuses=frozenset({400, 422}),
        ),
        Rule(
            "client-4xx-generic",
            RootCause.UNKNOWN,
            50,
            error_classes=frozenset({"CLIENT"}),
        ),
        Rule(
            "assertion-generic",
            RootCause.ASSERTION_FAILED,
            60,
            error_classes=frozenset({"ASSERT"}),
        ),
    )


RULES: tuple[Rule, ...] = _rules()
_COMPILED = {
    rule.id: (
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in rule.message_regex),
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in rule.page_regex),
    )
    for rule in RULES
}


def _matches(rule: Rule, features: ErrorFeatures) -> bool:
    if rule.stages is not None and features.stage not in rule.stages:
        return False
    if rule.error_classes is not None and (features.error_class or "") not in rule.error_classes:
        return False
    if rule.statuses is not None and not (features.status_values() & rule.statuses):
        return False
    if rule.excluded_statuses is not None and (features.status_values() & rule.excluded_statuses):
        return False
    if rule.actions is not None and (features.action or "") not in rule.actions:
        return False
    if rule.requires_login_page is True and not features.on_login_page:
        return False
    if rule.requires_retry_after is True and features.retry_after is None:
        return False
    message_patterns, page_patterns = _COMPILED[rule.id]
    if message_patterns and not any(pattern.search(features.searchable_text) for pattern in message_patterns):
        return False
    if page_patterns and not any(pattern.search(features.page_text_lower) for pattern in page_patterns):
        return False
    return True


def match_rule(features: ErrorFeatures) -> Rule | None:
    """Return the single winning rule, or ``None`` when nothing matches.

    Conflict resolution is explicit: highest priority first, then rule id.
    """
    candidates: Iterable[Rule] = (rule for rule in RULES if _matches(rule, features))
    winner = max(candidates, key=lambda rule: (rule.priority, rule.id), default=None)
    return winner


def explain_matches(features: ErrorFeatures) -> list[str]:
    """All matching rule ids, useful for diagnostics and tests."""
    return sorted(rule.id for rule in RULES if _matches(rule, features))
