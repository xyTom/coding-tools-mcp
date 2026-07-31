from __future__ import annotations

from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Phase11PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_webui_and_desktop_package_data_are_both_included(self) -> None:
        package_data = self.pyproject["tool"]["setuptools"]["package-data"]
        self.assertEqual(package_data["coding_tools_mcp"], ["webui_dist/*"])
        self.assertEqual(
            package_data["mcp_desktop_client"],
            ["locales/*.qm", "locales/*.ts"],
        )
        self.assertTrue((ROOT / "coding_tools_mcp" / "webui_dist" / "admin.html").is_file())

    def test_upstream_desktop_discovery_and_entrypoint_are_preserved(self) -> None:
        project = self.pyproject["project"]
        discovery = self.pyproject["tool"]["setuptools"]["packages"]["find"]
        self.assertIn("desktop", project["optional-dependencies"])
        self.assertEqual(
            project["scripts"]["coding-tools-mcp-desktop"],
            "mcp_desktop_client.app:main",
        )
        self.assertEqual(discovery["where"], [".", "apps/desktop-client"])
        self.assertEqual(discovery["include"], ["coding_tools_mcp*", "mcp_desktop_client*"])

    def test_upstream_dev_and_image_extras_are_preserved(self) -> None:
        extras = self.pyproject["project"]["optional-dependencies"]
        self.assertIn("dev", extras)
        self.assertIn("image", extras)
        self.assertIn("mypy>=2.1,<2.2", extras["dev"])
        self.assertIn("Pillow>=10.0", extras["image"])

    def test_compliance_ci_allows_setup_node_toolchain_under_landlock(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "compliance.yml").read_text(
            encoding="utf-8"
        )
        setup_node = workflow.index("uses: actions/setup-node@v6")
        allow_root = workflow.index("CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS")
        unit_discovery = workflow.index("name: Run unit discovery")
        self.assertLess(setup_node, allow_root)
        self.assertLess(allow_root, unit_discovery)
        self.assertIn('readlink -f "$(command -v node)"', workflow)


if __name__ == "__main__":
    unittest.main()
