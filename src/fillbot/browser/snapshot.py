"""Safe evidence identifiers and HTML masking shared by the evidence store."""
from __future__ import annotations
import re
from typing import Iterable
from bs4 import BeautifulSoup
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
def safe_component(value: str) -> str:
    cleaned = _SAFE.sub("_", value).strip("._")
    return cleaned or "unnamed"
def is_sensitive(name: str, mask_fields: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(token.lower() in lowered for token in mask_fields)
def sanitize_html(html: str, mask_fields: Iterable[str], secret_values: Iterable[str] = ()) -> str:
    """Mask password-like controls and known secret strings."""
    fields = tuple(mask_fields)
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup.find_all(["input", "textarea"]):
        identifier = " ".join(str(node.get(attr, "")) for attr in ("name", "id", "aria-label"))
        if is_sensitive(identifier, fields):
            if node.name == "textarea":
                node.clear()
                node.append("***")
            else:
                node["value"] = "***"
    rendered = str(soup)
    for secret in secret_values:
        if secret and len(secret) >= 3:
            rendered = rendered.replace(secret, "***")
    return rendered
