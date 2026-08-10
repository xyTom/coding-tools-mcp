from __future__ import annotations

import re
import unittest
from pathlib import Path

from coding_tools_mcp.webui import (
    ADMIN_HTML,
    APP_HTML,
    WIKI_HTML,
    WEBUI_DIST,
    admin_console_html,
    operator_app_html,
    user_guide_html,
)


ROOT = Path(__file__).resolve().parents[1]
WEBUI_SRC = ROOT / "webui" / "src"


class WebUIBuildTests(unittest.TestCase):
    def test_packaged_admin_page_is_generated_self_contained_source(self) -> None:
        self.assertEqual(
            sorted(path.name for path in WEBUI_DIST.iterdir() if path.is_file()),
            ["admin.html", "app.html", "wiki.html"],
        )
        built = ADMIN_HTML.read_text(encoding="utf-8")
        self.assertEqual(admin_console_html(), built)
        for name in (
            "admin.css",
            "settings-copy.js",
            "settings-model.js",
            "workspace-editor.js",
            "settings-page.js",
            "admin.js",
        ):
            source = (WEBUI_SRC / name).read_text(encoding="utf-8").strip()
            self.assertIn(f'data-build-source="{name}"', built)
            self.assertIn(source, built)
        self.assertIsNone(re.search(r'<link\b[^>]*href=["\'][^"\']+\.css', built, re.I))
        self.assertIsNone(re.search(r'<script\b[^>]*src=["\'][^"\']+\.js', built, re.I))
        self.assertIn('href="/app" target="_blank" rel="noopener"', built)

    def test_packaged_wiki_is_self_contained_and_explains_auth_bootstrap(self) -> None:
        built = WIKI_HTML.read_text(encoding="utf-8")
        self.assertEqual(user_guide_html(), built)
        for name in ("wiki.css", "wiki.js"):
            source = (WEBUI_SRC / name).read_text(encoding="utf-8").strip()
            self.assertIn(f'data-build-source="{name}"', built)
            self.assertIn(source, built)
        self.assertIn("CODING_TOOLS_MCP_AUTH_TOKEN", built)
        self.assertIn("POST /admin/api/session", built)
        self.assertIn("HttpOnly", built)
        self.assertIn("Web Storage", built)
        self.assertIn("coding-tools-mcp --workspace", built)
        self.assertIn("Codex CLI", built)
        self.assertIn("Codex thread store", built)
        self.assertIn('id="app-token"', built)
        self.assertIn('href="/app"', built)
        self.assertIsNone(re.search(r'<link\b[^>]*href=["\'][^"\']+\.css', built, re.I))
        self.assertIsNone(re.search(r'<script\b[^>]*src=["\'][^"\']+\.js', built, re.I))

    def test_packaged_operator_page_is_generated_self_contained_source(self) -> None:
        built = APP_HTML.read_text(encoding="utf-8")
        self.assertEqual(operator_app_html(), built)
        for name in (
            "admin.css",
            "app/app.css",
            "app/model.js",
            "app/api-client.js",
            "app/app.js",
        ):
            source = (WEBUI_SRC / name).read_text(encoding="utf-8").strip()
            self.assertIn(f'data-build-source="{name}"', built)
            self.assertIn(source, built)
        self.assertIn("/api/app", built)
        self.assertNotIn("/admin/api", built)
        self.assertIn('href="/wiki#app-token"', built)
        self.assertIn("CODING_TOOLS_MCP_AUTH_TOKEN", built)
        self.assertIn("OAuth access token", built)
        self.assertIn("CODING_TOOLS_MCP_ADMIN_TOKEN", built)
        self.assertIn("这是可选的浏览器版 Codex Agent 工作台", built)
        self.assertIn("可以忽略 /app", built)
        self.assertIn("不是 GPT/ChatGPT 网页", built)
        self.assertIn("执行主机必须安装 Codex CLI", built)
        self.assertIn("三者不能互相替代", built)
        self.assertIn("/mcp", built)
        self.assertIn("/admin", built)
        self.assertIn("/app", built)
        self.assertIn("如果只通过 Claude、Cursor、Codex 等外部客户端使用 MCP，可以忽略 /app", built)
        self.assertIn("Coding Tools MCP SQLite 与 Codex thread store", built)
        self.assertIsNone(re.search(r'<link\b[^>]*href=["\'][^"\']+\.css', built, re.I))
        self.assertIsNone(re.search(r'<script\b[^>]*src=["\'][^"\']+\.js', built, re.I))

    def test_frontend_has_no_obsolete_or_unsafe_control_paths(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(WEBUI_SRC.rglob("*"))
            if path.suffix in {".html", ".js"}
        )
        self.assertNotRegex(source, r"(?i)tool_profile")
        self.assertNotIn("innerHTML", source)
        self.assertNotRegex(source, r"localStorage|sessionStorage")
        self.assertNotRegex(source, r"reload_upstream|start_server|stop_server")
        self.assertIn("stale_revision", source)
        self.assertIn("textContent", source)


if __name__ == "__main__":
    unittest.main()
