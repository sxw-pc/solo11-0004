"""Human confirmation gate: one-time tokens, durable pause state and audit trail.

Design rules:
* tokens are generated with :mod:`secrets`; only their SHA-256 hash is persisted,
  the plaintext token is returned to the caller exactly once and printed by the CLI;
* a token is consumed atomically (conditional UPDATE on the awaiting status), so
  concurrent or repeated approvals cannot double-fire a submission;
* every attempt — including unknown or already-spent tokens — is written to the
  ``confirmation_events`` audit table.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from fillbot.browser.base import Page
from fillbot.browser.snapshot import sanitize_html
from fillbot.errors import ConfirmationError
from fillbot.evidence.store import EvidenceStore
from fillbot.ledger.db import Ledger
from fillbot.models import ConfirmationRecord, iso_now

CONFIRMATION_STATUSES = frozenset({"approved", "rejected"})


def generate_token() -> str:
    return "fbc-" + secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_confirmation_id() -> str:
    return "CFM-" + uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class PausedConfirmation:
    confirmation: ConfirmationRecord
    token: str
    snapshot_path: Path


class ConfirmationGateway:
    def __init__(
        self,
        ledger: Ledger,
        evidence: EvidenceStore,
        *,
        token_generator: Callable[[], str] = generate_token,
        clock: Callable[[], str] = iso_now,
    ) -> None:
        self.ledger, self.evidence = ledger, evidence
        self._token_generator, self._clock = token_generator, clock

    def pause(
        self,
        *,
        run_id: str,
        task_id: str,
        biz_key: str,
        gate_step_index: int,
        page: Page,
        filled_fields: dict[str, Any],
        mask_fields: Sequence[str],
        secret_values: Sequence[str],
    ) -> PausedConfirmation:
        """Persist the awaiting state, masked snapshot and one-time token."""
        confirmation_id, token = new_confirmation_id(), self._token_generator()
        evidence_dir = self.evidence.task_dir(run_id, task_id)
        snapshot_path = evidence_dir / "confirmation-snapshot.html"
        snapshot_path.write_text(
            sanitize_html(page.text, tuple(mask_fields), tuple(secret_values)), encoding="utf-8"
        )
        meta_path = evidence_dir / "confirmation.json"
        meta_path.write_text(
            json.dumps(
                {
                    "confirmation_id": confirmation_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "biz_key": biz_key,
                    "gate_step_index": gate_step_index,
                    "created_at": self._clock(),
                    "page_url": page.url,
                    # Structured, value-free field inventory for the human reviewer.
                    "filled_fields": sorted(filled_fields),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.ledger.create_confirmation(
            confirmation_id=confirmation_id,
            run_id=run_id,
            task_id=task_id,
            biz_key=biz_key,
            gate_step_index=gate_step_index,
            token_hash=hash_token(token),
            token_hint=token[:8],
            evidence_dir=str(evidence_dir),
            snapshot_path=str(snapshot_path),
            created_at=self._clock(),
        )
        self.ledger.add_confirmation_event(
            confirmation_id=confirmation_id,
            event="paused",
            run_id=run_id,
            task_id=task_id,
            actor="bot",
            detail=f"gate step {gate_step_index}; masked snapshot stored",
            created_at=self._clock(),
        )
        record = self.ledger.get_confirmation(confirmation_id)
        assert record is not None
        return PausedConfirmation(self._to_record(record), token, snapshot_path)

    def decide(self, token: str, *, approve: bool, actor: str, note: str = "") -> ConfirmationRecord:
        """Validate a token and atomically record the human decision.

        Raises :class:`ConfirmationError` for unknown, malformed or already-used
        tokens; every rejected attempt is still audited.
        """
        if not token or not isinstance(token, str):
            raise ConfirmationError("confirmation token is required")
        token_hash = hash_token(token.strip())
        existing = self.ledger.find_confirmation_by_token(token_hash)
        if existing is None:
            self.ledger.add_confirmation_event(
                confirmation_id=None,
                event="invalid_token",
                run_id="",
                task_id="",
                actor=actor,
                detail=f"approve={approve}; no confirmation matches this token (hint={token[:8]})",
                created_at=self._clock(),
            )
            raise ConfirmationError("unknown confirmation token")
        new_status = "approved" if approve else "rejected"
        row = self.ledger.consume_confirmation(
            token_hash,
            new_status=new_status,
            decided_at=self._clock(),
            decided_by=actor,
            decision_note=note,
        )
        if row is None:
            self.ledger.add_confirmation_event(
                confirmation_id=existing["confirmation_id"],
                event="spent_token",
                run_id=str(existing["run_id"]),
                task_id=str(existing["task_id"]),
                actor=actor,
                detail=f"approve={approve}; token already consumed, current status={existing['status']}",
                created_at=self._clock(),
            )
            raise ConfirmationError(
                f"token already used (confirmation {existing['confirmation_id']} is {existing['status']})"
            )
        self.ledger.add_confirmation_event(
            confirmation_id=row["confirmation_id"],
            event=new_status,
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            actor=actor,
            detail=note or ("human approved submission" if approve else "human abandoned submission"),
            created_at=self._clock(),
        )
        return self._to_record(row)

    def list_pending(self, *, run_id: str | None = None) -> list[ConfirmationRecord]:
        return [
            self._to_record(row)
            for row in self.ledger.list_confirmations(status="awaiting_confirmation", run_id=run_id)
        ]

    @staticmethod
    def _to_record(row: dict[str, Any]) -> ConfirmationRecord:
        return ConfirmationRecord(
            confirmation_id=str(row["confirmation_id"]),
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            biz_key=str(row["biz_key"]),
            status=str(row["status"]),
            gate_step_index=int(row["gate_step_index"]),
            token_hint=str(row["token_hint"]),
            evidence_dir=str(row["evidence_dir"]),
            created_at=str(row["created_at"]),
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            decision_note=row["decision_note"],
        )
