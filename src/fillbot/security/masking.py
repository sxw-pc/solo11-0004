"""Graded masking rule engine.

A :class:`MaskingPolicy` binds security levels (L1..L4) to configurable
:class:`MaskRule` strategies and remembers per-field levels declared in the
task sheet. Two operating modes exist:

* **legacy mode** -- no grading configured. HTML sanitization delegates to the
  original :func:`fillbot.browser.snapshot.sanitize_html`, so evidence output
  is byte-identical to the pre-grading release.
* **graded mode** -- at least one level rule / field grade exists. HTML form
  controls are masked according to the field's resolved level and free-text
  secret values are redacted with the highest-grade rule.

Any lookup failure (unknown field, missing/broken rule) fails closed by
falling back to :data:`fillbot.security.levels.FAIL_CLOSED_LEVEL`, never to
plaintext.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from fillbot.browser.snapshot import is_sensitive, sanitize_html
from fillbot.errors import ScriptError
from fillbot.security.levels import FAIL_CLOSED_LEVEL, GRADED_LEVELS, SecurityLevel

#: Sentinel level meaning "use the pre-grading fixed rules".
LEGACY_LEVEL = SecurityLevel.LEGACY

#: Replacement used by the ``redact`` strategy and by every fail-closed path.
REDACTION = "***REDACTED***"
#: Replacement used by the legacy fixed rules and the ``full`` strategy.
FULL_MASK = "***"

_STRATEGIES = frozenset({"partial", "full", "redact", "hash"})


@dataclass(frozen=True)
class MaskRule:
    """One masking strategy.

    Strategies:
        * ``partial`` -- keep ``keep_prefix``/``keep_suffix`` characters,
          replace the middle with ``mask_char`` (at least one mask char).
        * ``full``    -- replace the whole value with ``replacement``.
        * ``redact``  -- replace the whole value with :data:`REDACTION`.
        * ``hash``    -- emit ``sha256:<12 hex chars>`` of the UTF-8 value.
    """

    strategy: str = "full"
    keep_prefix: int = 0
    keep_suffix: int = 0
    mask_char: str = "*"
    replacement: str = FULL_MASK

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MaskRule":
        if not isinstance(raw, Mapping):
            raise ScriptError("mask rule must be a mapping")
        strategy = raw.get("strategy", "full")
        if not isinstance(strategy, str) or strategy not in _STRATEGIES:
            raise ScriptError(
                f"mask rule strategy must be one of {', '.join(sorted(_STRATEGIES))}, got {strategy!r}"
            )
        mask_char = raw.get("mask_char", "*")
        if not isinstance(mask_char, str) or len(mask_char) != 1:
            raise ScriptError("mask rule mask_char must be a single character")
        replacement = raw.get("replacement", FULL_MASK)
        if not isinstance(replacement, str) or not replacement:
            raise ScriptError("mask rule replacement must be a non-empty string")

        def _int(name: str) -> int:
            value = raw.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScriptError(f"mask rule {name} must be a non-negative integer")
            return value

        keep_prefix, keep_suffix = _int("keep_prefix"), _int("keep_suffix")
        if strategy == "partial" and keep_prefix == 0 and keep_suffix == 0:
            raise ScriptError("partial mask rule requires keep_prefix and/or keep_suffix > 0")
        return cls(strategy, keep_prefix, keep_suffix, mask_char, replacement)

    def apply(self, text: str) -> str:
        if self.strategy == "redact":
            return REDACTION
        if self.strategy == "full":
            return self.replacement
        if self.strategy == "hash":
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            return f"sha256:{digest}"
        # partial: never reveal more than the original value contains, and the
        # kept prefix/suffix ranges must never overlap.
        length = len(text)
        if length == 0:
            return self.mask_char
        keep_head = min(self.keep_prefix, max(0, length - 1))
        keep_tail = min(self.keep_suffix, max(0, length - keep_head - 1))
        head = text[:keep_head] if keep_head else ""
        tail = text[length - keep_tail :] if keep_tail else ""
        hidden = max(1, length - len(head) - len(tail))
        return f"{head}{self.mask_char * hidden}{tail}"


#: Built-in defaults, applied for levels the DSL does not override.
DEFAULT_RULES: dict[SecurityLevel, MaskRule] = {
    SecurityLevel.L1: MaskRule("partial", keep_prefix=1, keep_suffix=0),
    SecurityLevel.L2: MaskRule("partial", keep_prefix=0, keep_suffix=4),
    SecurityLevel.L3: MaskRule("full"),
    SecurityLevel.L4: MaskRule("redact"),
}


def _fallback_rule() -> MaskRule:
    """Rule used whenever configuration is missing or malformed (fail closed)."""
    return DEFAULT_RULES[FAIL_CLOSED_LEVEL]


def mask_value(value: Any, rule: MaskRule | None) -> str:
    """Mask a single scalar with fail-closed handling of bad rules/values."""
    try:
        if value is None:
            return (rule or _fallback_rule()).replacement
        return (rule or _fallback_rule()).apply(str(value))
    except Exception:
        # Defensive: an unexpected masking error must never surface plaintext.
        return REDACTION


def apply_secret_redactions(html: str, secrets: Iterable[str], rule: MaskRule) -> str:
    """Replace free-text secret occurrences in rendered HTML."""
    rendered = html
    for secret in secrets:
        if secret is None:
            continue
        text = str(secret)
        if text and len(text) >= 3:
            rendered = rendered.replace(text, rule.apply(text))
    return rendered


def build_secret_replacements(
    named_secrets: Iterable[tuple[str, str]], policy: "MaskingPolicy"
) -> dict[str, str]:
    """Build the raw -> masked replacement map for named secrets.

    Longer secrets are mapped first so a value containing another value
    cannot leak through ordering. Every replacement goes through a resolved
    :class:`MaskRule`, so plaintext is never emitted even on lookup failure.
    Callers apply the returned mapping once during HTML sanitization so the
    same masked representation is used for controls and free text.
    """
    ordered = sorted(
        ((field, str(value)) for field, value in named_secrets if value and len(str(value)) >= 3),
        key=lambda item: len(item[1]),
        reverse=True,
    )
    replacements: dict[str, str] = {}
    for field, value in ordered:
        replacements[value] = policy.mask_field_value(field, value)
    return replacements


def _apply_replacements(html: str, replacements: Mapping[str, str]) -> str:
    rendered = html
    masked_outputs = set(replacements.values())
    for raw, masked in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        # Never substitute inside an already-produced mask (e.g. the L2 mask
        # for a short substring must not corrupt a longer L4 redaction).
        if raw in masked_outputs and raw != masked:
            continue
        rendered = rendered.replace(raw, masked)
    return rendered


@dataclass
class MaskingPolicy:
    """Resolved masking configuration for a run."""

    rules: dict[SecurityLevel, MaskRule] = field(default_factory=dict)
    field_levels: dict[str, SecurityLevel] = field(default_factory=dict)
    default_level: SecurityLevel | None = None
    mask_fields: tuple[str, ...] = ("password", "token")
    legacy: bool = False
    rule_errors: tuple[str, ...] = ()
    # Hot-path caches populated lazily; grading configs are frozen per run.
    _token_index: dict[str, SecurityLevel] = field(default_factory=dict, init=False, repr=False)
    _level_cache: dict[str, SecurityLevel] = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------ build
    @classmethod
    def create(
        cls,
        *,
        rules: Mapping[Any, Any] | None = None,
        field_levels: Mapping[str, Any] | None = None,
        default_level: Any = None,
        mask_fields: Iterable[str] = ("password", "token"),
    ) -> "MaskingPolicy":
        """Build a policy from DSL/intake data with strict validation.

        Raises :class:`ScriptError` on structurally invalid configuration so
        that broken rules are rejected at script-validation time.
        """
        resolved_rules = {level: DEFAULT_RULES[level] for level in GRADED_LEVELS}
        errors: list[str] = []
        for raw_level, raw_rule in (rules or {}).items():
            try:
                level = SecurityLevel.parse(raw_level)
            except ValueError as exc:
                raise ScriptError(str(exc)) from exc
            resolved_rules[level] = MaskRule.from_dict(raw_rule)
        resolved_fields: dict[str, SecurityLevel] = {}
        for name, raw_level in (field_levels or {}).items():
            if not isinstance(name, str) or not name.strip():
                raise ScriptError("field security mapping keys must be non-empty strings")
            try:
                resolved_fields[name.strip()] = SecurityLevel.parse(raw_level)
            except ValueError as exc:
                raise ScriptError(f"field {name!r}: {exc}") from exc
        resolved_default: SecurityLevel | None = None
        if default_level is not None:
            try:
                resolved_default = SecurityLevel.parse(default_level)
            except ValueError as exc:
                raise ScriptError(f"default_security_level: {exc}") from exc
        is_legacy = not resolved_fields and resolved_default is None and not (rules or {})
        return cls(
            rules=resolved_rules,
            field_levels=resolved_fields,
            default_level=resolved_default,
            mask_fields=tuple(mask_fields),
            legacy=is_legacy,
            rule_errors=tuple(errors),
        )

    @classmethod
    def legacy_policy(cls, mask_fields: Iterable[str] = ("password", "token")) -> "MaskingPolicy":
        return cls(mask_fields=tuple(mask_fields), legacy=True)

    # ----------------------------------------------------------------- lookup
    @staticmethod
    def _control_tokens(identifier: str) -> tuple[str, ...]:
        tokens: list[str] = []
        for chunk in re.split(r"[\s,;:|/\\_\-\[\]()]+", identifier.lower()):
            if chunk:
                tokens.append(chunk)
        return tuple(tokens)

    def _ensure_token_index(self) -> dict[str, SecurityLevel]:
        """Pre-tokenize graded field names once per policy."""
        if not self._token_index and self.field_levels:
            index: dict[str, SecurityLevel] = {}
            for name, level in self.field_levels.items():
                for token in self._control_tokens(name):
                    # Strongest grade wins when tokens collide.
                    if token not in index or level > index[token]:
                        index[token] = level
            self._token_index = index
        return self._token_index

    def level_for(self, field_name: str) -> SecurityLevel:
        """Resolve the grade of a field; unknown lookups fail closed.

        Matching is exact first, then token-based so an HTML control whose
        name/id/aria-label combines several identifiers (e.g.
        ``contact_phone phone``) still resolves a grade declared for either
        token. The strongest matching grade wins. Results are cached because
        a run resolves the same control identifiers on every snapshot.
        """
        cached = self._level_cache.get(field_name)
        if cached is not None:
            return cached
        if field_name in self.field_levels:
            resolved = self.field_levels[field_name]
        else:
            lowered = field_name.lower()
            best: SecurityLevel | None = None
            for token in self._control_tokens(lowered):
                level = self._ensure_token_index().get(token)
                if level is not None and (best is None or level > best):
                    best = level
            if best is not None:
                resolved = best
            elif self.default_level is not None:
                resolved = self.default_level
            elif field_name in self.field_levels:
                resolved = self.field_levels[field_name]
            elif is_sensitive(field_name, self.mask_fields):
                # Legacy name-based sensitivity without an explicit grade:
                # fail closed at the strongest grade (never emit plaintext).
                resolved = FAIL_CLOSED_LEVEL
            else:
                resolved = LEGACY_LEVEL
        self._level_cache[field_name] = resolved
        return resolved

    def rule_for(self, level: SecurityLevel) -> MaskRule:
        if level == LEGACY_LEVEL:
            return _fallback_rule()
        rule = self.rules.get(level)
        if rule is None:
            # Configuration gap: degrade to the strongest grade, never plain.
            return _fallback_rule()
        return rule

    def mask_field_value(self, field_name: str, value: Any) -> str:
        return mask_value(value, self.rule_for(self.level_for(field_name)))

    # ------------------------------------------------------------- HTML output
    def sanitize_html(
        self,
        html: str,
        secret_values: Iterable[str] = (),
        named_secrets: Iterable[tuple[str, str]] | None = None,
    ) -> str:
        """Produce the safe HTML representation for a page snapshot."""
        if self.legacy:
            return sanitize_html(html or "", self.mask_fields, secret_values)
        return sanitize_html_graded(html or "", self, secret_values, named_secrets or ())


def sanitize_html_graded(
    html: str,
    policy: MaskingPolicy,
    secret_values: Iterable[str] = (),
    named_secrets: Iterable[tuple[str, str]] = (),
) -> str:
    """Graded HTML sanitization used when a grading configuration exists."""
    from bs4 import BeautifulSoup

    # Resolve every raw secret to its graded mask up front, so control values
    # and free-text occurrences use the exact same representation.
    replacements = build_secret_replacements(named_secrets, policy)
    strongest = policy.rule_for(FAIL_CLOSED_LEVEL)
    for secret in secret_values:
        text = "" if secret is None else str(secret)
        if text and len(text) >= 3 and text not in replacements:
            replacements[text] = strongest.apply(text)

    soup = BeautifulSoup(html or "", "html.parser")
    # Free-text replacements first, while every raw secret is still present.
    # Control handling below skips values that are already masked outputs, so
    # no value is ever masked twice (which would collapse L4 to a generic ***).
    masked_values = set(replacements.values())
    rendered = _apply_replacements(str(soup), replacements)
    soup = BeautifulSoup(rendered, "html.parser")
    for node in soup.find_all(["input", "textarea"]):
        identifier = " ".join(str(node.get(attr, "")) for attr in ("name", "id", "aria-label"))
        level = policy.level_for(identifier)
        if level == LEGACY_LEVEL:
            # Unmarked controls keep their rendered value; legacy substring
            # rules only apply when no grading configuration exists at all.
            continue
        rule = policy.rule_for(level)
        if node.name == "textarea":
            original = node.get_text() or ""
            if not original:
                # Empty sensitive control: use the grade rule on an empty
                # string so L4 still yields a redaction marker, never plain ***.
                node.clear()
                node.append(rule.apply(""))
            elif original not in masked_values:
                node.clear()
                node.append(rule.apply(original))
        else:
            current = node.get("value", "")
            if current in (None, ""):
                node["value"] = rule.apply("")
            elif str(current) not in masked_values:
                node["value"] = rule.apply(str(current))
    return str(soup)
