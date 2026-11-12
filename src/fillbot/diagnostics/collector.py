"""Fault-isolated feature collection and root-cause classification.

Every public function here is defensive: diagnostics must never raise, never
change task status and never interrupt the run. Collection cost is bounded to
a couple of regex scans plus at most one small evidence file read.
"""
from __future__ import annotations

import re
import time
import traceback
from pathlib import Path
from typing import Any, Iterable, Mapping

from fillbot.diagnostics.models import (
    Diagnosis,
    ErrorFeatures,
    RootCause,
    Rule,
    detail_for,
)
from fillbot.diagnostics.rules import STAGE_INTAKE, STAGE_RUNTIME, match_rule
from fillbot.errors import BotError

_MAX_PAGE_TEXT = 4000
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _safe_str(value: Any, maximum: int = 600) -> str:
    try:
        text = str(value or "")
    except Exception:  # pragma: no cover - __str__ should not fail, but stay safe
        return ""
    return text if len(text) <= maximum else text[:maximum]


def normalize_page_text(html: str | None) -> tuple[str, str]:
    """Extract a bounded, tag-free text and title from an HTML snapshot."""
    source = html or ""
    title = ""
    match = _TITLE_RE.search(source)
    if match:
        title = _WS_RE.sub(" ", _TAG_RE.sub(" ", match.group(1))).strip()
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", source)).strip()
    return title, text[:_MAX_PAGE_TEXT]


def page_features(page: Any) -> dict[str, Any]:
    """Read page hints without assuming any adapter contract."""
    if page is None:
        return {}
    try:
        html = _safe_str(getattr(page, "text", ""), 200_000)
        url = _safe_str(getattr(page, "url", ""), 1000)
        status = getattr(page, "status", None)
        title, text = normalize_page_text(html)
        return {
            "page_url": url,
            "page_status": status if isinstance(status, int) else None,
            "page_title": title,
            "page_text": text,
        }
    except Exception:  # pragma: no cover - defensive
        return {}


def exchange_features(page: Any, *, method: str | None = None) -> dict[str, Any]:
    """Bounded request/response summary stored with the diagnosis."""
    if page is None:
        return {}
    try:
        headers = dict(getattr(page, "headers", {}) or {})
    except Exception:  # pragma: no cover - defensive
        headers = {}
    interesting = {
        key: _safe_str(headers.get(key), 300)
        for key in ("Retry-After", "Content-Type", "X-Request-Id", "Location", "Server")
        if key in headers or key.lower() in {item.lower() for item in headers}
    }
    # requests headers are case-insensitive when read through the adapter.
    for key, value in headers.items():
        if _safe_str(key).lower() in {"retry-after", "location", "x-request-id"}:
            interesting[key] = _safe_str(value, 300)
    return {
        "method": method,
        "url": _safe_str(getattr(page, "url", ""), 1000),
        "status": getattr(page, "status", None),
        "elapsed_ms": getattr(page, "elapsed_ms", None),
        "headers": interesting,
    }


def features_from_error(
    error: BaseException,
    *,
    stage: str,
    action: str | None = None,
    locator: str | None = None,
    step_index: int | None = None,
    receipt: str | None = None,
    issues: Iterable[Any] | None = None,
    resources: Iterable[str] | None = None,
    details: Mapping[str, Any] | None = None,
    page: Any | None = None,
) -> tuple[ErrorFeatures, Rule | None]:
    """Collect features for a runtime/intake exception and match a rule.

    Adaptive signature: callers pass whatever context they own. This function
    never raises and returns ``(features, None)`` on any internal failure.
    """
    try:
        return _collect(
            error,
            stage=stage,
            action=action,
            locator=locator,
            step_index=step_index,
            receipt=receipt,
            issues=issues,
            resources=resources,
            details=details,
            page=page,
        )
    except Exception:  # pragma: no cover - diagnostics must be inert
        return ErrorFeatures(stage=stage, message=_safe_str(error)), None


def _collect(
    error: BaseException,
    *,
    stage: str,
    action: str | None,
    locator: str | None,
    step_index: int | None,
    receipt: str | None,
    issues: Iterable[Any] | None,
    resources: Iterable[str] | None,
    details: Mapping[str, Any] | None,
    page: Any | None,
) -> tuple[ErrorFeatures, Rule | None]:
    from fillbot.diagnostics.rules import match_rule

    error_page = getattr(error, "page", None) if isinstance(error, BotError) else None
    page = error_page if error_page is not None else page
    bot_details: dict[str, Any] = {}
    if isinstance(error, BotError):
        bot_details = dict(getattr(error, "details", {}) or {})
    merged = {**bot_details, **dict(details or {})}
    fields: list[str] = []
    messages: list[str] = []
    for issue in list(issues or [])[:50]:
        fields.append(_safe_str(getattr(issue, "field", ""), 200))
        messages.append(_safe_str(getattr(issue, "message", ""), 300))
    file_names = [Path(str(item)).name for item in list(resources or [])[:5]]
    first_suffix = ""
    first_size: int | None = None
    for item in list(resources or [])[:1]:
        try:
            path = Path(str(item))
            first_suffix = path.suffix.lower()
            first_size = path.stat().st_size if path.is_file() else None
        except Exception:  # pragma: no cover - defensive
            pass
    features = ErrorFeatures(
        stage=stage,
        error_class=getattr(getattr(error, "category", None), "value", None)
        or _safe_str(merged.get("error_class"), 40)
        or None,
        error_type=type(error).__name__,
        message=_safe_str(getattr(error, "message", None) or str(error), 1200),
        status=getattr(error, "status", None) if isinstance(error, BotError) else None,
        action=action,
        locator=_safe_str(locator, 400),
        step_index=step_index,
        retry_after=getattr(error, "retry_after", None) if isinstance(error, BotError) else None,
        receipt=receipt,
        attempts=int(merged.get("attempts", 1) or 1),
        issue_fields=tuple(fields),
        issue_messages=tuple(messages),
        attachment_name=file_names[0] if file_names else None,
        attachment_size=first_size,
        attachment_suffix=first_suffix,
        details=merged,
        **page_features(page),
    )
    exchange = exchange_features(page, method=_safe_str(merged.get("method"), 20) or None)
    if exchange:
        features.exchange = exchange
    rule = match_rule(features)
    return features, rule


def build_diagnosis(
    error: BaseException | None,
    features: ErrorFeatures,
    rule: Rule | None,
    *,
    stack: str = "",
    original_stage: str = "",
    overhead_ms: float = 0.0,
) -> Diagnosis:
    """Turn collected features into a persisted diagnosis. Never raises."""
    try:
        if rule is None:
            root = RootCause.UNKNOWN
            rule_id, priority = "fallback-unknown", 0
        else:
            root, rule_id, priority = rule.root_cause, rule.id, rule.priority
        return Diagnosis(
            root_cause=root,
            rule_id=rule_id,
            priority=priority,
            stage=features.stage or original_stage or STAGE_RUNTIME,
            error_class=features.error_class,
            message=features.message,
            features=features,
            stack=_safe_str(stack, 12_000),
            overhead_ms=overhead_ms,
        )
    except Exception:  # pragma: no cover - diagnostics must be inert
        fallback = ErrorFeatures(stage=features.stage, message=_safe_str(error))
        return Diagnosis(RootCause.UNKNOWN, "fallback-unknown", 0, features.stage, None, fallback.message, fallback)


def classify_error(error: BaseException, *, stage: str, **context: Any) -> tuple[ErrorFeatures, Diagnosis]:
    """Convenience entry point: collect, match and build in one call."""
    started = time.perf_counter()
    features, rule = features_from_error(error, stage=stage, **context)
    stack = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    diagnosis = build_diagnosis(
        error,
        features,
        rule,
        stack=stack,
        overhead_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    return features, diagnosis


def diagnosis_from_intake(error: BaseException, *, stage: str = STAGE_INTAKE) -> Diagnosis:
    """Classify a pre-runtime intake/validation error using the same taxonomy."""
    issues = getattr(error, "issues", None) or []
    _, diagnosis = classify_error(error, stage=stage, issues=issues)
    return diagnosis


def attach_evidence(
    diagnosis: Diagnosis,
    *,
    evidence_dir: str,
    snapshot_path: str | None = None,
    action_log_path: str | None = None,
    receipt_path: str | None = None,
) -> Diagnosis:
    """Attach evidence jump pointers; tolerate missing/legacy evidence files."""

    def rel(path: str | None) -> str:
        if not path:
            return ""
        try:
            return str(Path(path).relative_to(Path(evidence_dir))) if evidence_dir else str(path)
        except ValueError:
            return str(path)

    entries = [
        ("step_snapshot", snapshot_path, "页面快照"),
        ("action_log", action_log_path, "动作日志"),
        ("receipt", receipt_path, "提交回执"),
    ]
    try:
        built = []
        for kind, path, label in entries:
            if not path:
                continue
            try:
                exists = Path(str(path)).is_file()
            except OSError:  # pragma: no cover - defensive
                exists = False
            built.append(
                {
                    "kind": kind,
                    "path": str(path),
                    "relative_path": rel(path),
                    "label": label,
                    "exists": exists,
                }
            )
        diagnosis.evidence_path = evidence_dir or ""
        diagnosis.evidence = built
    except Exception:  # pragma: no cover - diagnostics must be inert
        pass
    return diagnosis


def detail_text(diagnosis: Diagnosis) -> dict[str, str]:
    detail = detail_for(diagnosis.root_cause)
    return {"label": detail.label, "suggestion": detail.suggestion}


def classify_failure_fields(
    *,
    stage: str,
    error_class: str | None,
    message: str,
    status: int | None = None,
    page_url: str = "",
    page_text: str = "",
    page_title: str = "",
    action: str | None = None,
    locator: str = "",
    step_index: int | None = None,
    receipt: str | None = None,
    issue_fields: Iterable[str] = (),
    issue_messages: Iterable[str] = (),
    resources: Iterable[str] = (),
    details: Mapping[str, Any] | None = None,
    stack: str = "",
    retry_after: float | None = None,
) -> Diagnosis:
    """Classify synthesized signals (e.g. receipt contract checks) without an exception.

    Used by the runner's submit/receipt stage where failures are detected rather
    than raised. Fault-isolated: falls back to ``UNKNOWN`` with the original text.
    """
    started = time.perf_counter()
    try:
        features = ErrorFeatures(
            stage=stage,
            error_class=error_class,
            error_type="SynthesizedFailure",
            message=_safe_str(message, 1200),
            status=status,
            page_url=_safe_str(page_url, 1000),
            page_text=_safe_str(page_text, _MAX_PAGE_TEXT),
            page_title=_safe_str(page_title, 300),
            action=action,
            locator=_safe_str(locator, 400),
            step_index=step_index,
            retry_after=retry_after,
            receipt=receipt,
            issue_fields=tuple(_safe_str(item, 200) for item in list(issue_fields)[:50]),
            issue_messages=tuple(_safe_str(item, 300) for item in list(issue_messages)[:50]),
            details=dict(details or {}),
        )
        rule = match_rule(features)
        return build_diagnosis(
            None,
            features,
            rule,
            stack=stack,
            overhead_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    except Exception:  # pragma: no cover - diagnostics must be inert
        fallback = ErrorFeatures(stage=stage, error_class=error_class, message=_safe_str(message))
        return Diagnosis(RootCause.UNKNOWN, "fallback-unknown", 0, stage, error_class, fallback.message, fallback)


__all__ = [
    "ErrorFeatures",
    "Diagnosis",
    "RootCause",
    "STAGE_INTAKE",
    "attach_evidence",
    "build_diagnosis",
    "classify_error",
    "classify_failure_fields",
    "detail_text",
    "diagnosis_from_intake",
    "exchange_features",
    "features_from_error",
    "normalize_page_text",
    "page_features",
]
