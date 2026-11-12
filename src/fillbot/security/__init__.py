"""Graded sensitive-field masking, role/company access control and audit.

The package is deliberately split from the storage modules so that both the
evidence writer (files) and the ledger (SQLite) can share one policy engine:

* :mod:`fillbot.security.levels`  -- security level taxonomy
* :mod:`fillbot.security.masking` -- graded masking rules and policy engine
* :mod:`fillbot.security.access`  -- principals, access decisions, audit facade

When no grading configuration is present every component falls back to the
legacy fixed-rule behaviour; see :data:`fillbot.security.masking.LEGACY_LEVEL`.
"""
from fillbot.security.access import (
    AccessDecision,
    AccessPolicy,
    AuditEvent,
    Authorizer,
    Principal,
)
from fillbot.security.levels import SecurityLevel
from fillbot.security.masking import (
    LEGACY_LEVEL,
    MaskingPolicy,
    MaskRule,
    apply_secret_redactions,
    mask_value,
)

__all__ = [
    "AccessDecision",
    "AccessPolicy",
    "AuditEvent",
    "Authorizer",
    "LEGACY_LEVEL",
    "MaskingPolicy",
    "MaskRule",
    "Principal",
    "SecurityLevel",
    "apply_secret_redactions",
    "mask_value",
]
