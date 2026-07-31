"""Small stdlib-only helpers shared across the package.

A leaf module: it imports nothing from the package, so both server.py and
modules server.py itself imports (like telemetry.py) can use these without
creating an import cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone

ENV_PREFIX = "CODING_TOOLS_MCP"


def truthy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
