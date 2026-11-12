"""requests + BeautifulSoup adapter: cookies, CSRF, forms and multipart upload."""
from __future__ import annotations
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup, Tag
from fillbot.browser.base import BrowserSession, Locator, Page
from fillbot.dsl import selectors
from fillbot.errors import ClientError, NetworkError, ScriptError, ServerError
@dataclass
class _Pending:
    locator: Locator
    kind: str
    value: str = ""
    path: Path | None = None
@dataclass
class _Submission:
    locator: Locator
    method: str
    url: str
    data: dict[str, str]
    files: dict[str, tuple[str, bytes, str]]
    timeout: float | None = None
class MechanicalBrowser(BrowserSession):
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        follow_redirects: bool = True,
        limiter: Callable[[], None] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout, self.follow_redirects, self._limiter = timeout, follow_redirects, limiter
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._session.headers.setdefault("User-Agent", "fillbot/0.1 (loopback mock only)")
        self._page: Page | None = None
        self._pending: dict[str, _Pending] = {}
        self._last_submission: _Submission | None = None
    @property
    def page(self) -> Page:
        if self._page is None:
            raise ScriptError("no page loaded")
        return self._page
    def open(self, url: str, *, timeout: float | None = None, follow_redirects: bool | None = None) -> Page:
        self._pending.clear()
        self._last_submission = None
        return self._request("GET", url, data=None, files=None, timeout=timeout, follow_redirects=follow_redirects)
    def _soup(self) -> BeautifulSoup:
        return selectors.parse_html(self.page.text)
    def _pending_from(self, locator: Locator, kind: str, value: str = "", path: Path | None = None) -> None:
        control = selectors.resolve_control(self._soup(), locator)
        name = str(control.get("name", "")).strip()
        if not name:
            raise ScriptError(f"control has no name: {locator.describe()}")
        self._pending[name] = _Pending(locator, kind, value, path)
    def fill(self, locator: Locator, value: str) -> None:
        control = selectors.resolve_control(self._soup(), locator)
        if control.name not in {"input", "textarea"} or str(control.get("type", "text")).lower() in {
            "file",
            "checkbox",
            "radio",
        }:
            raise ScriptError(f"control is not a text field: {locator.describe()}")
        self._pending_from(locator, "value", value)
    def select(self, locator: Locator, value: str) -> None:
        control = selectors.resolve_control(self._soup(), locator)
        if control.name != "select":
            raise ScriptError(f"control is not a select: {locator.describe()}")
        matches = []
        for option in control.find_all("option"):
            option_value = str(option.get("value", option.get_text(strip=True)))
            if value in {option_value, selectors.normalized_text(option.get_text(" ", strip=True))}:
                matches.append(option_value)
        if not matches:
            raise ScriptError(f"option {value!r} not found in {locator.describe()}")
        self._pending_from(locator, "value", matches[0])
    def check(self, locator: Locator, checked: bool = True) -> None:
        control = selectors.resolve_control(self._soup(), locator)
        if control.name != "input" or str(control.get("type", "")).lower() not in {"checkbox", "radio"}:
            raise ScriptError(f"control is not checkable: {locator.describe()}")
        self._pending_from(locator, "check" if checked else "uncheck", str(control.get("value", "on")))
    def upload(self, locator: Locator, path: Path) -> None:
        control = selectors.resolve_control(self._soup(), locator)
        if control.name != "input" or str(control.get("type", "")).lower() != "file":
            raise ScriptError(f"control is not a file input: {locator.describe()}")
        if not path.is_file():
            raise ScriptError(f"upload file not found: {path}")
        self._pending_from(locator, "file", value=str(path), path=path)
    def _form_for(self, soup: BeautifulSoup, trigger: Tag) -> Tag:
        form = soup.find("form", id=str(trigger.get("form"))) if trigger.get("form") else None
        form = form or trigger.find_parent("form")
        if not isinstance(form, Tag):
            raise ScriptError("clickable control is not inside a form")
        return form
    def _defaults(self, form: Tag) -> dict[str, str]:
        data: dict[str, str] = {}
        for control in form.find_all(["input", "textarea", "select"]):
            if control.has_attr("disabled") or not control.get("name"):
                continue
            name = str(control.get("name"))
            if control.name == "textarea":
                data[name] = control.get_text()
            elif control.name == "select":
                options = control.find_all("option")
                chosen = next(
                    (option for option in options if option.has_attr("selected")), options[0] if options else None
                )
                if chosen is not None:
                    data[name] = str(chosen.get("value", chosen.get_text(strip=True)))
            else:
                kind = str(control.get("type", "text")).lower()
                if kind in {"submit", "button", "reset", "file"}:
                    continue
                if kind in {"checkbox", "radio"}:
                    if control.has_attr("checked"):
                        data[name] = str(control.get("value", "on"))
                else:
                    data[name] = str(control.get("value", ""))
        return data
    def _prepare_form(self, locator: Locator) -> _Submission:
        soup = self._soup()
        trigger = selectors.resolve_clickable(soup, locator)
        form = self._form_for(soup, trigger)
        data, files = self._defaults(form), {}
        for name, pending in self._pending.items():
            if pending.kind in {"value", "check"}:
                data[name] = pending.value
            elif pending.kind == "uncheck":
                data.pop(name, None)
            elif pending.path is not None:
                files[name] = (
                    pending.path.name,
                    pending.path.read_bytes(),
                    mimetypes.guess_type(pending.path.name)[0] or "application/octet-stream",
                )
        if trigger.get("name") and str(trigger.get("type", "")).lower() in {"submit", "button"}:
            data[str(trigger.get("name"))] = str(trigger.get("value", ""))
        method = str(trigger.get("formmethod") or form.get("method") or "GET").upper()
        action = str(trigger.get("formaction") or form.get("action") or "")
        return _Submission(locator, method, urljoin(self.page.url, action) if action else self.page.url, data, files)
    def click(self, locator: Locator, *, timeout: float | None = None) -> Page:
        try:
            submission = self._prepare_form(locator)
        except ScriptError:
            previous = self._last_submission
            if previous is None or previous.locator != locator:
                raise
            submission = previous
        submission.timeout = timeout
        self._pending.clear()
        self._last_submission = submission
        if submission.method == "GET":
            return self._request(
                "GET", submission.url, data=submission.data or None, files=None, timeout=timeout, follow_redirects=None
            )
        return self._request(
            "POST",
            submission.url,
            data=submission.data,
            files=submission.files or None,
            timeout=timeout,
            follow_redirects=None,
        )
    def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None,
        files: dict[str, tuple[str, bytes, str]] | None,
        timeout: float | None,
        follow_redirects: bool | None,
    ) -> Page:
        if self._limiter:
            self._limiter()
        started, effective_timeout = time.monotonic(), timeout if timeout is not None else self.timeout
        try:
            response = self._session.request(
                method,
                url,
                data=data,
                files=files,
                timeout=effective_timeout,
                allow_redirects=self.follow_redirects if follow_redirects is None else follow_redirects,
            )
        except requests.Timeout as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raise NetworkError(f"timeout after {effective_timeout}s: {method} {url} ({elapsed}ms)") from exc
        except requests.RequestException as exc:
            raise NetworkError(f"network error: {method} {url}: {exc}") from exc
        page = Page(
            response.status_code,
            response.url,
            dict(response.headers),
            response.text,
            int((time.monotonic() - started) * 1000),
        )
        self._page = page
        if response.status_code == 429:
            try:
                retry_after = float(response.headers["Retry-After"]) if "Retry-After" in response.headers else None
            except ValueError:
                retry_after = None
            raise ServerError(
                f"rate limited (429) at {url}",
                status=429,
                retry_after=retry_after,
                page=page,
                details={"method": method, "request_url": url},
            )
        if response.status_code >= 500:
            raise ServerError(
                f"server error {response.status_code} at {url}",
                status=response.status_code,
                page=page,
                details={"method": method, "request_url": url},
            )
        if response.status_code >= 400:
            snippet = selectors.normalized_text(response.text)[:160]
            raise ClientError(
                f"client error {response.status_code} at {url}: {snippet}",
                status=response.status_code,
                page=page,
                details={"method": method, "request_url": url},
            )
        return page
    def close(self) -> None:
        self._session.close()
        self._pending.clear()
