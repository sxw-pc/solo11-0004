"""Fictional accounts, one pre-existing submission and the in-memory store."""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
# Mock-only credentials, not real secrets.
ACCOUNTS = (
    {
        "username": "parkco-a",
        "password": "mock-only-alpha",
        "company_id": "C001",
        "company_name": "远山数据服务社（虚构）",
        "contact_name": "沈遥（虚构）",
    },
    {
        "username": "parkco-b",
        "password": "mock-only-beta",
        "company_id": "C002",
        "company_name": "青岚谷生态科技（虚构）",
        "contact_name": "林小岚（虚构）",
    },
    {
        "username": "parkco-c",
        "password": "mock-only-gamma",
        "company_id": "C003",
        "company_name": "溪谷智造工坊（虚构）",
        "contact_name": "顾青（虚构）",
    },
)
@dataclass(frozen=True)
class Account:
    username: str
    password: str
    company_id: str
    company_name: str
    contact_name: str
@dataclass
class Submission:
    company_id: str
    period: str
    receipt_no: str
    submitted_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    def as_public(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in ("company_id", "period", "receipt_no", "submitted_at")}
class MockStore:
    def __init__(self, *, clock: Any = None) -> None:
        self.accounts = {item["username"]: Account(**item) for item in ACCOUNTS}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.submissions = [
            Submission("C001", "2025-Q3", "RCP-20250115-0001", "2025-01-15T09:30:00+00:00", {"seeded": True})
        ]
        self._receipt_seq = 2
        # The mock runs a threaded Flask server; serialise receipt issuance so
        # concurrent workers cannot observe the same sequence number. RLock
        # because add_submission re-enters via find_submission.
        self._lock = threading.RLock()

    def authenticate(self, username: str, password: str) -> Account | None:
        account = self.accounts.get(username)
        return account if account is not None and account.password == password else None
    def find_submission(self, company_id: str, period: str) -> Submission | None:
        with self._lock:
            return next(
                (item for item in self.submissions if item.company_id == company_id and item.period == period), None
            )
    def find_receipt(self, receipt_no: str) -> Submission | None:
        with self._lock:
            return next((item for item in self.submissions if item.receipt_no == receipt_no), None)
    def add_submission(self, company_id: str, period: str, payload: dict[str, Any]) -> Submission:
        with self._lock:
            if self.find_submission(company_id, period) is not None:
                raise ValueError("duplicate submission")
            return self._append_submission(company_id, period, payload)

    def submit_if_absent(self, company_id: str, period: str, payload: dict[str, Any]) -> Submission | None:
        """Atomically add a submission only when none exists for the key.

        Returns the new submission, or ``None`` when the company/period pair
        was already submitted. The check and insert share one lock so
        concurrent requests cannot both pass the duplicate guard.
        """
        with self._lock:
            if self.find_submission(company_id, period) is not None:
                return None
            return self._append_submission(company_id, period, payload)

    def _append_submission(self, company_id: str, period: str, payload: dict[str, Any]) -> Submission:
        now = self.clock()
        stamp = now.strftime("%Y%m%d")
        receipt = f"RCP-{stamp}-{self._receipt_seq:04d}"
        while any(item.receipt_no == receipt for item in self.submissions):
            self._receipt_seq += 1
            receipt = f"RCP-{stamp}-{self._receipt_seq:04d}"
        self._receipt_seq += 1
        submission = Submission(company_id, period, receipt, now.isoformat(), dict(payload))
        self.submissions.append(submission)
        return submission

    def receipts(self) -> list[str]:
        with self._lock:
            return [item.receipt_no for item in self.submissions]

    def submission_count(self) -> int:
        with self._lock:
            return len(self.submissions)
