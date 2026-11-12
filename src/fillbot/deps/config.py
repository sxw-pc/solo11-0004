"""Configuration for the opt-in dependency orchestration layer.

Deliberately separate from :class:`fillbot.config.BotConfig` so the strict
existing config contract (unknown keys raise) is untouched: users who never
declare dependencies never add any configuration.  Only the location of the
dependency store and scheduling knobs live here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from fillbot.errors import ConfigError


@dataclass(frozen=True)
class DepConfig:
    store_path: Path = field(default_factory=lambda: Path("data/dependencies.db"))
    poll_interval_seconds: float = 1.0
    default_timeout_seconds: int | None = None

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "DepConfig":
        env = os.environ if environ is None else environ
        data: dict[str, Any] = {
            "store_path": Path(str(env.get("FILLBOT_DEPS_STORE", "data/dependencies.db"))),
            "poll_interval_seconds": 1.0,
            "default_timeout_seconds": None,
        }
        allowed = set(data)
        for key, value in (raw or {}).items():
            if key not in allowed:
                raise ConfigError(f"unknown dependency config key: {key}")
            data[key] = value
        if env.get("FILLBOT_DEPS_POLL_INTERVAL"):
            data["poll_interval_seconds"] = env["FILLBOT_DEPS_POLL_INTERVAL"]
        if env.get("FILLBOT_DEPS_TIMEOUT"):
            data["default_timeout_seconds"] = env["FILLBOT_DEPS_TIMEOUT"]
        if env.get("FILLBOT_DEPS_STORE"):
            data["store_path"] = env["FILLBOT_DEPS_STORE"]
        interval = data["poll_interval_seconds"]
        if isinstance(interval, str):
            try:
                interval = float(interval)
            except ValueError as exc:
                raise ConfigError("poll_interval_seconds must be numeric") from exc
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            raise ConfigError("poll_interval_seconds must be a positive number")
        timeout = data["default_timeout_seconds"]
        if timeout is not None:
            if isinstance(timeout, str):
                try:
                    timeout = int(timeout)
                except ValueError as exc:
                    raise ConfigError("default_timeout_seconds must be an integer") from exc
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
                raise ConfigError("default_timeout_seconds must be a positive integer")
            data["default_timeout_seconds"] = timeout
        data["poll_interval_seconds"] = float(interval)
        data["store_path"] = Path(str(data["store_path"]))
        return cls(**data)
