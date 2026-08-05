from __future__ import annotations

from pathlib import Path


WEBUI_DIST = Path(__file__).with_name("webui_dist")
ADMIN_HTML = WEBUI_DIST / "admin.html"
ADMIN_CSS = WEBUI_DIST / "admin.css"
ADMIN_JS = WEBUI_DIST / "admin.js"
ADMIN_I18N = WEBUI_DIST / "i18n.js"
ADMIN_SETTINGS_MODEL = WEBUI_DIST / "settings-model.js"
ADMIN_SETTINGS_COPY = WEBUI_DIST / "settings-copy.js"
ADMIN_WORKSPACE_EDITOR = WEBUI_DIST / "workspace-editor.js"
ADMIN_SETTINGS_PAGE = WEBUI_DIST / "settings-page.js"
ADMIN_ASSET_TYPES = {
    "admin.css": "text/css; charset=utf-8",
    "admin.js": "application/javascript; charset=utf-8",
    "i18n.js": "application/javascript; charset=utf-8",
    "settings-model.js": "application/javascript; charset=utf-8",
    "settings-copy.js": "application/javascript; charset=utf-8",
    "workspace-editor.js": "application/javascript; charset=utf-8",
    "settings-page.js": "application/javascript; charset=utf-8",
}


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
    <p>The WebUI build artifact is missing.</p>
    <p>Run <code>npm --prefix webui run build</code> to generate <code>coding_tools_mcp/webui_dist/admin.html</code>.</p>
  </main>
</body>
</html>"""


def admin_asset_response(asset_name: str) -> tuple[bytes, str] | None:
    if asset_name not in ADMIN_ASSET_TYPES:
        return None
    try:
        return (WEBUI_DIST / asset_name).read_bytes(), ADMIN_ASSET_TYPES[asset_name]
    except OSError:
        return None


__all__ = ["ADMIN_CSS", "ADMIN_HTML", "ADMIN_I18N", "ADMIN_JS", "ADMIN_SETTINGS_COPY", "ADMIN_SETTINGS_MODEL", "ADMIN_SETTINGS_PAGE", "ADMIN_WORKSPACE_EDITOR", "WEBUI_DIST", "admin_asset_response", "admin_console_html"]
