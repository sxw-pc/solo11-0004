"""Diagnostics package: feature collection, rule matching and diagnosis records."""
from fillbot.diagnostics.collector import (
    attach_evidence,
    build_diagnosis,
    classify_error,
    classify_failure_fields,
    detail_text,
    diagnosis_from_intake,
    features_from_error,
)
from fillbot.diagnostics.models import (
    ROOT_CAUSE_DETAILS,
    Diagnosis,
    ErrorFeatures,
    RootCause,
    RootCauseDetail,
    Rule,
    detail_for,
)
from fillbot.diagnostics.rules import RULES, explain_matches, match_rule

__all__ = [
    "RULES",
    "ROOT_CAUSE_DETAILS",
    "Diagnosis",
    "ErrorFeatures",
    "RootCause",
    "RootCauseDetail",
    "Rule",
    "attach_evidence",
    "build_diagnosis",
    "classify_error",
    "classify_failure_fields",
    "detail_for",
    "detail_text",
    "diagnosis_from_intake",
    "explain_matches",
    "features_from_error",
    "match_rule",
]
