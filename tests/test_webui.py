from __future__ import annotations

import re
import unittest
from pathlib import Path

from coding_tools_mcp.webui import ADMIN_HTML, WEBUI_DIST, admin_console_html


ROOT = Path(__file__).resolve().parents[1]
WEBUI_SRC = ROOT / "webui" / "src"


class WebUIBuildTests(unittest.TestCase):
    def test_packaged_admin_page_is_generated_self_contained_source(self) -> None:
        self.assertEqual([path.name for path in WEBUI_DIST.iterdir() if path.is_file()], ["admin.html"])
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

    def test_frontend_has_no_obsolete_or_unsafe_control_paths(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(WEBUI_SRC.iterdir())
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
