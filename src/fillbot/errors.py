"""Central error taxonomy; NETWORK and SERVER are transient, the rest are not."""
from __future__ import annotations
from enum import Enum
from typing import Any
class ErrorClass(str, Enum):
    NETWORK = "NETWORK"
    SERVER = "SERVER"
    CLIENT = "CLIENT"
    ASSERT = "ASSERT"
    SCRIPT = "SCRIPT"
RETRYABLE_CLASSES = frozenset({ErrorClass.NETWORK, ErrorClass.SERVER})
class BotError(Exception):
    category = ErrorClass.SCRIPT
    def __init__(
        self,
        message: str,
        category: ErrorClass | None = None,
        *,
        retry_after: float | None = None,
        page: Any | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if category is not None:
            self.category = category
        self.retry_after = retry_after
        self.page = page
        self.details = details or {}
    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CLASSES
    def __str__(self) -> str:
        return f"[{self.category.value}] {self.message}"
class NetworkError(BotError):
    category = ErrorClass.NETWORK
class _StatusError(BotError):
    def __init__(self, message: str, *, status: int | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.status = status
        if status is not None:
            self.details.setdefault("status", status)
class ServerError(_StatusError):
    category = ErrorClass.SERVER
class ClientError(_StatusError):
    category = ErrorClass.CLIENT
class AssertionFailure(BotError):
    category = ErrorClass.ASSERT
class ScriptError(BotError):
    category = ErrorClass.SCRIPT
class TaskValidationError(BotError):
    def __init__(self, message: str, issues: list[Any] | None = None) -> None:
        super().__init__(message, details={"issues": issues or []})
        self.issues = issues or []
class ConfigError(ScriptError):
    pass
class UndefinedVariableError(ScriptError):
    pass
class UnregisteredActionError(ScriptError):
    pass
class DryRunStop(Exception):
    pass
class ConfirmationError(BotError):
    """Rejected human-confirmation-gate operation (bad or spent token)."""
    category = ErrorClass.SCRIPT
