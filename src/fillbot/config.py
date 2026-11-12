"""Configuration precedence: CLI > environment > YAML file > defaults."""
from __future__ import annotations
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
import yaml
from fillbot.errors import ConfigError
_DEFAULTS: dict[str, Any] = {
    "base_url": "http://127.0.0.1:5000",
    "timeout_seconds": 10.0,
    "follow_redirects": True,
    "max_retries": 2,
    "backoff_base_seconds": 0.4,
    "backoff_max_seconds": 8.0,
    "jitter": True,
    "rate_limit_per_minute": 60,
    "evidence_keep_runs": 10,
    "evidence_dir": "evidence",
    "ledger_path": "data/ledger.db",
    "runs_dir": "runs",
    "mask_fields": ["password", "passwd", "secret", "token", "csrf_token"],
    "site_policies": {},
    "site_policy_recovery_streak": 3,
}
_ENV_MAP = {
    "FILLBOT_BASE_URL": "base_url",
    "FILLBOT_TIMEOUT_SECONDS": "timeout_seconds",
    "FILLBOT_FOLLOW_REDIRECTS": "follow_redirects",
    "FILLBOT_MAX_RETRIES": "max_retries",
    "FILLBOT_BACKOFF_BASE_SECONDS": "backoff_base_seconds",
    "FILLBOT_BACKOFF_MAX_SECONDS": "backoff_max_seconds",
    "FILLBOT_JITTER": "jitter",
    "FILLBOT_RATE_LIMIT_PER_MINUTE": "rate_limit_per_minute",
    "FILLBOT_EVIDENCE_KEEP_RUNS": "evidence_keep_runs",
    "FILLBOT_EVIDENCE_DIR": "evidence_dir",
    "FILLBOT_LEDGER_PATH": "ledger_path",
    "FILLBOT_RUNS_DIR": "runs_dir",
    "FILLBOT_SITE_POLICIES": "site_policies",
    "FILLBOT_SITE_POLICY_RECOVERY_STREAK": "site_policy_recovery_streak",
}
def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {value!r}")
def _num(value: Any, name: str, cast: type, minimum: float) -> float | int:
    try:
        parsed = cast(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be numeric, got {value!r}") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum:g}")
    return parsed
def _site_policies_and_warnings(value: Any) -> tuple[dict[str, Any], list[str]]:
    """Lenient per-site policy parsing with a diagnostic per ignored entry.

    Invalid configuration never raises: the entry is dropped so the target site
    silently keeps the global policy, which is exactly the documented fallback.
    """
    warnings: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}, warnings
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return {}, ["site_policies ignored: not valid JSON"]
    if not isinstance(value, Mapping):
        return {}, ["site_policies ignored: must be a mapping"]
    from fillbot.sites import canonical_site, parse_policy

    cleaned: dict[str, Any] = {}
    for raw_key, raw_policy in value.items():
        key = canonical_site(raw_key)
        if key is None:
            warnings.append(f"site policy {raw_key!r} ignored: invalid site key; using global policy")
            continue
        override, issues = parse_policy(raw_policy)
        if override is None or issues:
            warnings.append(
                f"site policy {raw_key!r} ignored: {'; '.join(issues) or 'invalid policy'}; using global policy"
            )
            continue
        cleaned[key] = override.as_dict()
    return cleaned, warnings

def _coerce(key: str, value: Any) -> Any:
    if key in {"timeout_seconds", "backoff_base_seconds", "backoff_max_seconds"}:
        return _num(value, key, float, 0.01)
    if key in {"max_retries", "evidence_keep_runs"}:
        return _num(value, key, int, 0)
    if key == "rate_limit_per_minute":
        return _num(value, key, int, 1)
    if key in {"follow_redirects", "jitter"}:
        return _bool(value, key)
    if key in {"evidence_dir", "ledger_path", "runs_dir"}:
        return str(value)
    if key == "mask_fields":
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
            raise ConfigError("mask_fields must be a list of strings")
        return list(value)
    if key == "site_policies":
        return value
    if key == "site_policy_recovery_streak":
        return _num(value, key, int, 1)
    if key == "base_url":
        text = str(value).strip().rstrip("/")
        if not text.startswith(("http://", "https://")):
            raise ConfigError(f"base_url must be http(s), got {text!r}")
        return text
    return value
@dataclass(frozen=True)
class BotConfig:
    base_url: str = "http://127.0.0.1:5000"
    timeout_seconds: float = 10.0
    follow_redirects: bool = True
    max_retries: int = 2
    backoff_base_seconds: float = 0.4
    backoff_max_seconds: float = 8.0
    jitter: bool = True
    rate_limit_per_minute: int = 60
    evidence_keep_runs: int = 10
    evidence_dir: Path = field(default_factory=lambda: Path("evidence"))
    ledger_path: Path = field(default_factory=lambda: Path("data/ledger.db"))
    runs_dir: Path = field(default_factory=lambda: Path("runs"))
    mask_fields: tuple[str, ...] = ("password", "passwd", "secret", "token", "csrf_token")
    site_policies: dict[str, Any] = field(default_factory=dict)
    site_policy_recovery_streak: int = 3
    site_policy_warnings: tuple[str, ...] = ()

    def with_overrides(self, **overrides: Any) -> "BotConfig":
        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean) if clean else self
    @property
    def requests_per_second(self) -> float:
        return self.rate_limit_per_minute / 60.0
    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("evidence_dir", "ledger_path", "runs_dir"):
            data[key] = str(data[key])
        data["mask_fields"] = list(self.mask_fields)
        data["site_policies"] = {key: dict(value) for key, value in self.site_policies.items()}
        data["site_policy_warnings"] = list(self.site_policy_warnings)
        return data
def load_config(
    path: str | Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> BotConfig:
    env = os.environ if environ is None else environ
    merged = dict(_DEFAULTS)
    if path:
        file_path = Path(path)
        if not file_path.is_file():
            raise ConfigError(f"config file not found: {file_path}")
        try:
            raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"cannot read config file {file_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config file must contain a YAML mapping")
        for key, value in raw.items():
            if key not in _DEFAULTS:
                raise ConfigError(f"unknown config key: {key}")
            merged[key] = _coerce(key, value)
    for env_name, key in _ENV_MAP.items():
        if env_name in env and str(env[env_name]).strip() != "":
            merged[key] = _coerce(key, env[env_name])
    for key, value in (overrides or {}).items():
        if value is not None:
            if key not in _DEFAULTS:
                raise ConfigError(f"unknown config override: {key}")
            merged[key] = _coerce(key, value)
    for key in ("evidence_dir", "ledger_path", "runs_dir"):
        merged[key] = Path(merged[key])
    merged["mask_fields"] = tuple(merged["mask_fields"])
    if merged["backoff_base_seconds"] > merged["backoff_max_seconds"]:
        raise ConfigError("backoff_base_seconds cannot exceed backoff_max_seconds")
    site_policies, warnings = _site_policies_and_warnings(merged.get("site_policies"))
    merged["site_policies"] = site_policies
    merged["site_policy_warnings"] = tuple(warnings)
    return BotConfig(**merged)
