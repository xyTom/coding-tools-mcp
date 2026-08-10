"""Packaged Admin, Operator, and user Wiki entry points.

``webui/src/**`` is the only editable frontend source. The packaged HTML is
created by ``npm --prefix webui run build`` and is intentionally self-contained
so the public ``/admin``, ``/app``, and ``/wiki`` shells do not need a second
static-file router.
"""

from __future__ import annotations

from pathlib import Path

WEBUI_DIST = Path(__file__).with_name("webui_dist")
ADMIN_HTML = WEBUI_DIST / "admin.html"
APP_HTML = WEBUI_DIST / "app.html"
WIKI_HTML = WEBUI_DIST / "wiki.html"


def admin_console_html() -> str:
    try:
        return ADMIN_HTML.read_text(encoding="utf-8")
    except OSError:
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MCP Admin Console</title>
</head>
<body>
  <main style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:3rem auto;max-width:720px;line-height:1.6">
    <h1>MCP Admin Console</h1>
    <p>The generated WebUI artifact is missing.</p>
    <p>Run <code>npm --prefix webui run build</code> from the repository root.</p>
  </main>
</body>
</html>"""


def operator_app_html() -> str:
    try:
        return APP_HTML.read_text(encoding="utf-8")
    except OSError:
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Coding Tools MCP Agent Workbench</title>
</head>
<body>
  <main style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:3rem auto;max-width:720px;line-height:1.6">
    <h1>Coding Tools MCP Agent Workbench</h1>
    <p>The generated Operator WebUI artifact is missing.</p>
    <p>Run <code>npm --prefix webui run build</code> from the repository root.</p>
  </main>
</body>
</html>"""


def user_guide_html() -> str:
    try:
        return WIKI_HTML.read_text(encoding="utf-8")
    except OSError:
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Coding Tools MCP User Wiki</title>
</head>
<body>
  <main style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:3rem auto;max-width:720px;line-height:1.6">
    <h1>Coding Tools MCP User Wiki</h1>
    <p>The generated Wiki artifact is missing.</p>
    <p>Run <code>npm --prefix webui run build</code> from the repository root.</p>
  </main>
</body>
</html>"""


__all__ = [
    "ADMIN_HTML",
    "APP_HTML",
    "WIKI_HTML",
    "WEBUI_DIST",
    "admin_console_html",
    "operator_app_html",
    "user_guide_html",
]
