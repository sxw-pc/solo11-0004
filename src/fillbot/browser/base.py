"""Stable browser interface consumed by the DSL engine."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
@dataclass(frozen=True)
class Locator:
    css: str | None = None
    label: str | None = None
    def __post_init__(self) -> None:
        if bool(self.css) == bool(self.label):
            raise ValueError("Locator requires exactly one of css or label")
    def describe(self) -> str:
        return f"css={self.css}" if self.css else f"label={self.label}"
@dataclass
class Page:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    elapsed_ms: int = 0
    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400
class BrowserSession(ABC):
    @property
    @abstractmethod
    def page(self) -> Page: ...
    @abstractmethod
    def open(self, url: str, *, timeout: float | None = None, follow_redirects: bool | None = None) -> Page: ...
    @abstractmethod
    def fill(self, locator: Locator, value: str) -> None: ...
    @abstractmethod
    def select(self, locator: Locator, value: str) -> None: ...
    @abstractmethod
    def check(self, locator: Locator, checked: bool = True) -> None: ...
    @abstractmethod
    def upload(self, locator: Locator, path: Path) -> None: ...
    @abstractmethod
    def click(self, locator: Locator, *, timeout: float | None = None, final: bool = False) -> Page: ...
    @abstractmethod
    def close(self) -> None: ...
    def __enter__(self) -> "BrowserSession":
        return self
    def __exit__(self, *exc_info: object) -> None:
        self.close()
