"""Per-step HTML snapshots, action journals, graded masking and retention."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from fillbot.browser.base import Page
from fillbot.browser.snapshot import is_sensitive, safe_component, sanitize_html
from fillbot.errors import ScriptError
from fillbot.security.access import AccessDecision, AccessPolicy, AuditEvent, Authorizer, Principal
from fillbot.security.masking import MaskingPolicy

__all__ = ["EvidenceStore", "is_sensitive", "safe_component", "sanitize_html", "EvidenceAccessDenied"]


class EvidenceAccessDenied(ScriptError):
    """Raised when a guarded evidence read is denied by the access policy."""


#: Sidecar file recording how a task directory was produced.
MANIFEST_NAME = "security_manifest.json"
#: Filenames a caller is never allowed to escape to via path traversal.
_FORBIDDEN_NAMES = {"", ".", ".."}


class EvidenceStore:
    def __init__(
        self,
        base_dir: str | Path,
        *,
        mask_fields: Iterable[str] = ("password", "token"),
        keep_runs: int = 10,
        policy: MaskingPolicy | None = None,
        authorizer: Authorizer | None = None,
        audit_sink: Callable[[AuditEvent], None] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.mask_fields = tuple(mask_fields)
        self.keep_runs = max(0, keep_runs)
        # Default policy keeps the pre-grading fixed-rule behaviour exactly.
        self.policy = policy or MaskingPolicy.legacy_policy(self.mask_fields)
        self.access = AccessPolicy(authorizer)
        self._audit_sink = audit_sink

    # ------------------------------------------------------------- audit hooks
    def _audit(self, event: AuditEvent) -> None:
        self.access.audit(event)
        if self._audit_sink is not None:
            try:
                self._audit_sink(event)
            except Exception:
                # Audit delivery failures must not break evidence handling.
                pass

    def bind_ledger(self, ledger: Any) -> None:
        """Wire the shared SQLite authorizer/audit log after construction."""
        self.access = ledger.access_policy()
        self._audit_sink = None  # the policy itself persists through its authorizer

    # ------------------------------------------------------------- path layout
    def run_dir(self, run_id: str) -> Path:
        path = self.base_dir / safe_component(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def task_dir(self, run_id: str, task_id: str) -> Path:
        path = self.run_dir(run_id) / safe_component(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _manifest(self, run_id: str, task_id: str) -> dict[str, Any]:
        return {
            "version": 1,
            "run_id": run_id,
            "task_id": task_id,
            "graded": not self.policy.legacy,
            "field_levels": {name: level.label for name, level in sorted(self.policy.field_levels.items())},
            "default_level": self.policy.default_level.label if self.policy.default_level else None,
            "rules": {
                level.label: {"strategy": rule.strategy, "replacement": rule.replacement}
                for level, rule in sorted(self.policy.rules.items())
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _write_manifest(self, run_id: str, task_id: str) -> None:
        path = self.task_dir(run_id, task_id) / MANIFEST_NAME
        path.write_text(
            json.dumps(self._manifest(run_id, task_id), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )

    # -------------------------------------------------------------- write path
    def snapshot(
        self,
        run_id: str,
        task_id: str,
        step_index: int,
        page: Page,
        *,
        secret_values: Iterable[str] = (),
        named_secrets: Iterable[tuple[str, str]] = (),
        filename: str | None = None,
    ) -> Path:
        safe_html = self.policy.sanitize_html(page.text, secret_values, named_secrets=named_secrets)
        path = self.task_dir(run_id, task_id) / (filename or f"step-{step_index:02d}.html")
        path.write_text(safe_html, encoding="utf-8")
        self._audit(
            AuditEvent(
                action="evidence.mask",
                principal="system",
                resource=f"evidence:{run_id}/{task_id}/{path.name}",
                decision="masked",
                reason="graded" if not self.policy.legacy else "legacy-fixed-rule",
                details={"graded": not self.policy.legacy, "step_index": step_index},
            )
        )
        return path

    def save_receipt(
        self,
        run_id: str,
        task_id: str,
        page: Page,
        *,
        secret_values: Iterable[str] = (),
        named_secrets: Iterable[tuple[str, str]] = (),
    ) -> Path:
        path = self.snapshot(
            run_id,
            task_id,
            0,
            page,
            secret_values=secret_values,
            named_secrets=named_secrets,
            filename="receipt.html",
        )
        if not self.policy.legacy:
            # Legacy runs keep the old on-disk layout: no security sidecar.
            self._write_manifest(run_id, task_id)
        return path

    def append_action(self, run_id: str, task_id: str, record: dict[str, Any]) -> Path:
        path = self.task_dir(run_id, task_id) / "actions.jsonl"
        payload = dict(record)
        payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return path

    # --------------------------------------------------------------- read path
    def _resolve_evidence_path(self, run_id: str, task_id: str, filename: str) -> Path:
        if filename in _FORBIDDEN_NAMES or Path(filename).is_absolute() or ".." in Path(filename).parts:
            raise ScriptError(f"invalid evidence filename: {filename!r}")
        base = self.task_dir(run_id, task_id)
        candidate = (base / filename).resolve()
        if base.resolve() not in candidate.parents and candidate != base.resolve():
            raise ScriptError(f"evidence path escapes task directory: {filename!r}")
        if not candidate.is_file():
            raise ScriptError(f"evidence file not found: {run_id}/{task_id}/{filename}")
        return candidate

    def read_evidence(
        self,
        run_id: str,
        task_id: str,
        filename: str,
        principal: Principal | None,
        *,
        company_resolver: Callable[[str, str], str] | None = None,
    ) -> str:
        """Guarded evidence read: authorize first, then return file content.

        Every call -- allowed, denied or open-mode -- is written to the audit
        trail. Denied or malformed requests raise :class:`EvidenceAccessDenied`
        instead of returning any content.
        """
        company = company_resolver(run_id, task_id) if company_resolver else ""
        try:
            decision = self.access.decide(principal, "evidence.read", company=company or None)
        except Exception as exc:  # pragma: no cover - defensive guard
            decision = AccessDecision(False, f"policy-error: {exc}", degraded=True)
        # Single audit record per request (decide() has no side effects).
        self._audit(
            AuditEvent(
                action="evidence.read",
                principal=principal.identity if principal else "anonymous",
                resource=f"evidence:{run_id}/{task_id}/{filename}",
                decision="allow" if decision.allowed else "deny",
                reason=decision.reason,
                details={"run_id": run_id, "task_id": task_id, "file": filename},
            )
        )
        if not decision.allowed:
            raise EvidenceAccessDenied(
                f"evidence access denied for {principal.identity if principal else 'anonymous'}: {decision.reason}"
            )
        path = self._resolve_evidence_path(run_id, task_id, filename)
        return path.read_text(encoding="utf-8", errors="replace")

    def list_evidence(
        self,
        run_id: str,
        task_id: str,
        principal: Principal | None,
        *,
        company_resolver: Callable[[str, str], str] | None = None,
    ) -> list[str]:
        """Guarded listing of a task evidence directory (filenames only)."""
        company = company_resolver(run_id, task_id) if company_resolver else ""
        try:
            decision = self.access.decide(principal, "evidence.list", company=company or None)
        except Exception as exc:  # pragma: no cover - defensive guard
            decision = AccessDecision(False, f"policy-error: {exc}", degraded=True)
        self._audit(
            AuditEvent(
                action="evidence.list",
                principal=principal.identity if principal else "anonymous",
                resource=f"evidence:{run_id}/{task_id}",
                decision="allow" if decision.allowed else "deny",
                reason=decision.reason,
                details={"run_id": run_id, "task_id": task_id},
            )
        )
        if not decision.allowed:
            raise EvidenceAccessDenied(
                f"evidence listing denied for {principal.identity if principal else 'anonymous'}: {decision.reason}"
            )
        directory = self.task_dir(run_id, task_id)
        return sorted(item.name for item in directory.iterdir() if item.is_file())

    # -------------------------------------------------------------- retention
    def cleanup(self, keep: int | None = None) -> list[str]:
        retain = self.keep_runs if keep is None else max(0, keep)
        if not self.base_dir.is_dir():
            return []
        candidates = [item for item in self.base_dir.iterdir() if item.is_dir()]
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        removed = []
        for directory in candidates[retain:]:
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(directory.name)
        return removed
