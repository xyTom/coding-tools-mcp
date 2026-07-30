"""Backend-only Admin console entry points.

Phase 08 intentionally does not ship or rebuild frontend assets.
"""

from __future__ import annotations


def admin_console_html() -> str:
    return """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MCP Admin API</title></head>
<body><main><h1>MCP Admin API</h1><p>The authenticated backend API is available under <code>/admin/api</code>. Frontend assets are not included in this integration phase.</p></main></body>
</html>"""


__all__ = ["admin_console_html"]
