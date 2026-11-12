"""Sensitive-field security levels shared by intake, DSL, evidence and ledger."""
from __future__ import annotations

from enum import IntEnum


class SecurityLevel(IntEnum):
    """Sensitivity grades. Higher integer value means more sensitive.

    ``L1`` is the lowest grade (mild sensitivity) and ``L4`` the highest.
    The special ``LEGACY`` grade marks fields governed by the pre-grading
    fixed HTML rules; masking of legacy-graded data stays byte-identical to
    the previous release.
    """

    LEGACY = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4

    @property
    def label(self) -> str:
        return f"L{int(self)}" if self != SecurityLevel.LEGACY else "legacy"

    @classmethod
    def parse(cls, value: object) -> "SecurityLevel":
        """Parse a level from task sheets / DSL, accepting ints or ``L1``..``L4``."""
        if isinstance(value, bool):
            raise ValueError("security level must not be a boolean")
        if isinstance(value, SecurityLevel):
            return value
        if isinstance(value, int):
            candidate = int(value)
        elif isinstance(value, str):
            text = value.strip().upper()
            if text in {"L1", "L2", "L3", "L4"}:
                candidate = int(text[1:])
            else:
                try:
                    candidate = int(text)
                except ValueError as exc:
                    raise ValueError(f"invalid security level {value!r}") from exc
        else:
            raise ValueError(f"invalid security level {value!r}")
        if candidate not in (1, 2, 3, 4):
            raise ValueError(f"security level must be one of L1..L4, got {value!r}")
        return cls(candidate)


#: The grade used whenever a rule/configuration lookup fails. Fail closed:
#: the highest configured sensitivity wins so plaintext is never emitted.
FAIL_CLOSED_LEVEL = SecurityLevel.L4

GRADED_LEVELS: tuple[SecurityLevel, ...] = (
    SecurityLevel.L1,
    SecurityLevel.L2,
    SecurityLevel.L3,
    SecurityLevel.L4,
)
