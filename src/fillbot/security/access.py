"""Role/company access control primitives and the audit-log facade.

The storage-agnostic pieces live here so evidence and ledger share one
decision model. The SQLite-backed implementation (grant table, audit table)
is provided by :class:`fillbot.ledger.db.Ledger` through the
:class:`Authorizer` protocol.

Default behaviour is intentionally open: with no principal supplied, or with
no grants configured at all, every request is allowed -- matching the
pre-access-control release. Once a grants table exists the model becomes
deny-by-default and every decision (allow, deny or degraded allow) is written
to the audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

#: Read-only access: masked/filtered views of data.
ACCESS_READ = "read"
#: Full access: unmasked evidence and cross-company rows.
ACCESS_ADMIN = "admin"
ACCESS_LEVELS = frozenset({ACCESS_READ, ACCESS_ADMIN})

#: Wildcard matching every company.
WILDCARD = "*"


@dataclass(frozen=True)
class Principal:
    """A caller of a guarded entry point."""

    identity: str
    roles: tuple[str, ...] = ()
    companies: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: str | Mapping[str, Any] | None) -> "Principal | None":
        """Parse ``identity:role1,role2@C001,C002`` style CLI strings."""
        if raw is None:
            return None
        if isinstance(raw, Mapping):
            identity = str(raw.get("identity", "")).strip()
            if not identity:
                raise ValueError("principal requires an identity")
            roles = tuple(item.strip() for item in raw.get("roles", ()) if str(item).strip())
            companies = tuple(item.strip() for item in raw.get("companies", ()) if str(item).strip())
            return cls(identity, roles, companies)
        text = str(raw).strip()
        if not text:
            return None
        identity, _, remainder = text.partition(":")
        if not identity:
            raise ValueError(f"invalid principal {raw!r}: missing identity")
        roles_part, _, companies_part = remainder.partition("@")
        roles = tuple(item.strip() for item in roles_part.split(",") if item.strip())
        companies = tuple(item.strip() for item in companies_part.split(",") if item.strip())
        return cls(identity.strip(), roles, companies)

    def grant_key(self) -> tuple[str, ...]:
        return tuple(sorted(self.roles)) or (self.identity,)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    company_scope: tuple[str, ...] = ()
    degraded: bool = False
    open_mode: bool = False

    @property
    def scoped(self) -> bool:
        return WILDCARD not in self.company_scope


@dataclass(frozen=True)
class AuditEvent:
    """One auditable action. Secrets are never part of an AuditEvent."""

    action: str
    principal: str
    resource: str
    decision: str
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


class Authorizer(Protocol):
    """Storage contract implemented by the ledger."""

    def access_enabled(self) -> bool: ...

    def grants_for(self, principal: Principal) -> list[dict[str, Any]]: ...

    def record_audit(self, event: AuditEvent) -> None: ...


@dataclass
class AccessPolicy:
    """Decide access and guarantee every decision is audited.

    ``authorizer`` may be ``None`` (e.g. evidence used standalone); in that
    case the policy operates in open mode and audit events are collected
    in-memory via :meth:`pending_events` instead of being persisted.
    """

    authorizer: Authorizer | None = None

    def enabled(self) -> bool:
        return bool(self.authorizer and self.authorizer.access_enabled())

    def _grants(self, principal: Principal) -> list[dict[str, Any]]:
        if self.authorizer is None:
            return []
        try:
            return list(self.authorizer.grants_for(principal))
        except Exception:
            # Corrupt permission configuration: deny, and let the caller audit.
            return []

    def decide(self, principal: Principal | None, resource: str, *, company: str | None = None) -> AccessDecision:
        """Evaluate a request without side effects.

        ``resource`` is a logical entry-point name (ledger/report/evidence).
        ``company`` is the owning company of the requested row/file.
        """
        if principal is None or not self.enabled():
            return AccessDecision(True, "open-default", (WILDCARD,), open_mode=True)
        grants = self._grants(principal)
        if not grants:
            return AccessDecision(False, "no-grant-configured")
        matched = [grant for grant in grants if self._grant_matches(grant, company)]
        if not matched:
            return AccessDecision(False, f"company {company or '-'} not in grant scope")
        levels = {str(grant.get("access_level", "")).lower() for grant in matched}
        if levels - ACCESS_LEVELS:
            return AccessDecision(False, "invalid-grant-configuration", degraded=True)
        companies = tuple(sorted({str(grant["company_id"]) for grant in matched if grant.get("company_id")}))
        if not companies or WILDCARD in companies:
            companies = (WILDCARD,)
        return AccessDecision(True, "grant-match", companies)

    @staticmethod
    def _grant_matches(grant: Mapping[str, Any], company: str | None) -> bool:
        grant_company = str(grant.get("company_id", "")).strip()
        if grant_company in {"", WILDCARD}:
            return True
        if company is None:
            # Listing requests are allowed and filtered by company scope.
            return True
        return grant_company == company

    def authorize(
        self,
        principal: Principal | None,
        resource: str,
        *,
        company: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> AccessDecision:
        """Decide and write an audit record in one call (never raises)."""
        try:
            decision = self.decide(principal, resource, company=company)
        except Exception as exc:  # pragma: no cover - defensive guard
            decision = AccessDecision(False, f"policy-error: {exc}", degraded=True)
        event = AuditEvent(
            action=resource,
            principal=principal.identity if principal else "anonymous",
            resource=f"{resource}:{company or '*'}",
            decision="allow" if decision.allowed else "deny",
            reason=decision.reason,
            details=dict(details or {}),
        )
        self.audit(event)
        return decision

    def audit(self, event: AuditEvent) -> None:
        """Persist an audit event, swallowing storage failures after a retry.

        Audit failures must never crash the guarded business operation; the
        event is retained in-memory so callers/tests can still observe it.
        """
        self._pending.append(event)
        if self.authorizer is not None:
            try:
                self.authorizer.record_audit(event)
            except Exception:
                self._audit_failures.append(event)

    _pending: list[AuditEvent] = field(default_factory=list)
    _audit_failures: list[AuditEvent] = field(default_factory=list)

    def pending_events(self) -> list[AuditEvent]:
        return list(self._pending)

    def audit_failures(self) -> list[AuditEvent]:
        return list(self._audit_failures)
