"""DOM lookup helpers: CSS selectors plus human label-text matching."""
from __future__ import annotations
from typing import Iterable
from bs4 import BeautifulSoup, Tag
from fillbot.browser.base import Locator
from fillbot.errors import ScriptError
_SELECTOR_TARGETS = "input, select, textarea"
_CLICK_TARGETS = "button, a, input[type=submit], input[type=button]"
def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")
def normalized_text(value: str) -> str:
    return " ".join(value.split())
def _label_candidates(soup: BeautifulSoup, label: str) -> list[Tag]:
    wanted, exact, contains = normalized_text(label), [], []
    for node in soup.find_all("label"):
        text = normalized_text(node.get_text(" ", strip=True))
        if text == wanted:
            exact.append(node)
        elif wanted and wanted in text:
            contains.append(node)
    return exact or contains
def _control_from_label(label_tag: Tag, soup: BeautifulSoup) -> Tag | None:
    target = label_tag.get("for")
    if target:
        found = soup.find(id=target)
        if isinstance(found, Tag):
            return found
    for tag_name in ("input", "select", "textarea", "button"):
        found = label_tag.find(tag_name)
        if isinstance(found, Tag):
            return found
    return None
def _dedupe(nodes: Iterable[Tag]) -> list[Tag]:
    unique, seen = [], set()
    for node in nodes:
        if id(node) not in seen:
            seen.add(id(node))
            unique.append(node)
    return unique
def _fallback(soup: BeautifulSoup, label: str) -> Tag | None:
    for attr in ("aria-label", "placeholder", "title"):
        found = soup.find(attrs={attr: label})
        if isinstance(found, Tag):
            return found
    return None
def resolve_control(soup: BeautifulSoup, locator: Locator) -> Tag:
    if locator.css:
        found = soup.select_one(locator.css)
        if not isinstance(found, Tag):
            raise ScriptError(f"selector not found: {locator.describe()}")
        return found
    candidates = _label_candidates(soup, locator.label or "")
    if not candidates:
        found = _fallback(soup, locator.label or "")
        if found is not None:
            return found
        raise ScriptError(f"label not found: {locator.describe()}")
    controls = _dedupe(
        control for label_tag in candidates if (control := _control_from_label(label_tag, soup)) is not None
    )
    if len(controls) == 1:
        return controls[0]
    if len(controls) > 1:
        raise ScriptError(f"ambiguous label: {locator.describe()}")
    found = _fallback(soup, locator.label or "")
    if found is not None:
        return found
    raise ScriptError(f"label has no associated control: {locator.describe()}")
def resolve_clickable(soup: BeautifulSoup, locator: Locator) -> Tag:
    if locator.css:
        nodes = soup.select(locator.css)
        if not nodes:
            raise ScriptError(f"selector not found: {locator.describe()}")
        if len(nodes) > 1:
            raise ScriptError(f"selector matches {len(nodes)} elements, expected one: {locator.describe()}")
        return nodes[0]
    candidates = _label_candidates(soup, locator.label or "")
    targets = _dedupe(
        control
        for label_tag in candidates
        if (control := _control_from_label(label_tag, soup)) is not None and control.name in {"button", "a", "input"}
    )
    if len(targets) == 1:
        return targets[0]
    for node in soup.select(_CLICK_TARGETS):
        if normalized_text(node.get_text(" ", strip=True)) == normalized_text(locator.label or ""):
            targets.append(node)
    targets = _dedupe(targets)
    if len(targets) == 1:
        return targets[0]
    if len(targets) > 1:
        raise ScriptError(f"ambiguous label: {locator.describe()}")
    raise ScriptError(f"clickable not found: {locator.describe()}")
def text_content(tag: Tag) -> str:
    return str(tag.get("value", "")) if tag.name == "input" else normalized_text(tag.get_text(" ", strip=True))
def extract_text(html: str, locator: Locator, attr: str | None = None) -> str:
    soup = parse_html(html)
    if locator.css is None:
        tag = resolve_control(soup, locator)
    else:
        nodes = soup.select(locator.css)
        if not nodes:
            raise ScriptError(f"selector not found: {locator.describe()}")
        if len(nodes) > 1:
            raise ScriptError(f"selector is ambiguous: {locator.describe()}")
        tag = nodes[0]
    if attr:
        if not tag.has_attr(attr):
            raise ScriptError(f"attribute {attr!r} missing on {locator.describe()}")
        return str(tag.get(attr, ""))
    value = text_content(tag)
    if not value:
        raise ScriptError(f"element is empty: {locator.describe()}")
    return value
def text_present(html: str, text: str, locator: Locator | None = None) -> bool:
    soup = parse_html(html)
    target = resolve_control(soup, locator) if locator else soup
    return normalized_text(text) in normalized_text(target.get_text(" ", strip=True))
