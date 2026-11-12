"""Token-bucket pacing and run identifiers.

``RateLimiter`` is kept as an alias of the site-aware adaptive limiter so
existing imports keep working unchanged.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from fillbot.sites import AdaptiveRateLimiter as RateLimiter  # noqa: F401  (re-exported for compatibility)
def new_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
