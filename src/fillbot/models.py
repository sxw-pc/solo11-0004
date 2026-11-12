"""Typed domain records shared by intake, runner, ledger and reports."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
def utcnow() -> datetime:
    return datetime.now(timezone.utc)
def iso_now() -> str:
    return utcnow().isoformat(timespec="milliseconds")
def elapsed_ms(started: datetime, ended: datetime | None = None) -> int:
    return max(0, int(((ended or utcnow()) - started).total_seconds() * 1000))
class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    DRY_RUN = "dry_run"
class StepStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    biz_key: str
    payload: dict[str, Any]
    attachments: tuple[Path, ...] = ()
    source_row: int = 0
    source_file: str = ""
    @property
    def attachment(self) -> Path | None:
        return self.attachments[0] if self.attachments else None
@dataclass
class ValidationIssue:
    source_file: str
    row: int
    field: str
    message: str
    def render(self) -> str:
        return f"{self.source_file}:row {self.row} field={self.field}: {self.message}"
    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
@dataclass
class StepResult:
    step_index: int
    action: str
    status: StepStatus
    started_at: str
    finished_at: str
    duration_ms: int
    attempts: int = 1
    error_class: str | None = None
    error_message: str | None = None
    snapshot_path: str | None = None
@dataclass
class EngineResult:
    status: TaskStatus
    variables: dict[str, Any] = field(default_factory=dict)
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None
    error_class: str | None = None
    receipt_no: str | None = None
    attempts: int = 1
@dataclass
class RunRecord:
    run_id: str
    started_at: str
    status: RunStatus = RunStatus.RUNNING
    finished_at: str | None = None
    tasks_total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    task_source: str = ""
    script_source: str = ""
    dry_run: bool = False
    base_url: str = ""
    evidence_dir: str = ""
    options: dict[str, Any] = field(default_factory=dict)
@dataclass
class TaskSummary:
    task_id: str
    biz_key: str
    status: str
    duration_ms: int = 0
    attempts: int = 0
    error_class: str | None = None
    error_message: str | None = None
    receipt_no: str | None = None
    evidence_dir: str = ""
@dataclass
class RunSummary:
    run_id: str
    status: str
    tasks_total: int
    succeeded: int
    failed: int
    skipped: int
    failure_reasons: dict[str, int]
    average_duration_ms: float
    receipts: list[str]
    started_at: str
    finished_at: str | None = None
    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
