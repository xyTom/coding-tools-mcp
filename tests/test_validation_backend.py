from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from coding_tools_mcp.validation import StructuredValidationBackend


class ValidationFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_sample.py").write_text("import unittest\n", encoding="utf-8")
        (self.root / "index.js").write_text("export const value = 1;\n", encoding="utf-8")
        (self.root / "package.json").write_text(
            json.dumps(
                {
                    "main": "index.js",
                    "scripts": {"test": "node --test"},
                }
            ),
            encoding="utf-8",
        )
        (self.root / "Cargo.toml").write_text(
            '[package]\nname = "fixture"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        (self.root / "go.mod").write_text("module example.test/fixture\n", encoding="utf-8")
        self.calls: list[dict[str, object]] = []

        def executor(args: dict[str, object]) -> dict[str, object]:
            self.calls.append(dict(args))
            return {"status": "exited", "exit_code": 0, "elapsed_ms": 7}

        self.backend = StructuredValidationBackend(
            self.root,
            executor,
            which=lambda tool: f"/fake/{tool}",
        )

    def tearDown(self) -> None:
        self.backend.close()
        self.tmp.cleanup()

    def _assert_passed(self, recipe: str) -> dict[str, object]:
        before = len(self.calls)
        result = self.backend.run(recipe)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.recipe, recipe)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(self.calls), before + 1)
        return self.calls[-1]

    def test_python_syntax_recipe(self) -> None:
        call = self._assert_passed("python:syntax")
        self.assertIn("compileall", str(call["cmd"]))

    def test_python_test_recipe(self) -> None:
        call = self._assert_passed("python:test")
        self.assertIn("unittest", str(call["cmd"]))

    def test_python_lint_recipe(self) -> None:
        call = self._assert_passed("python:lint")
        self.assertIn("ruff", str(call["cmd"]))

    def test_node_syntax_recipe(self) -> None:
        call = self._assert_passed("node:syntax")
        self.assertIn("--check", str(call["cmd"]))
        self.assertIn("index.js", str(call["cmd"]))

    def test_node_test_recipe_avoids_npm_lifecycle(self) -> None:
        call = self._assert_passed("node:test")
        self.assertIn("node", str(call["cmd"]))
        self.assertIn("--test", str(call["cmd"]))
        self.assertNotIn("npm", str(call["cmd"]))

    def test_node_lint_recipe(self) -> None:
        call = self._assert_passed("node:lint")
        self.assertIn("eslint", str(call["cmd"]))

    def test_rust_check_recipe_is_offline(self) -> None:
        call = self._assert_passed("rust:check")
        self.assertIn("--offline", str(call["cmd"]))
        self.assertEqual(call["env"], {"CARGO_NET_OFFLINE": "true"})

    def test_rust_test_recipe_is_offline(self) -> None:
        call = self._assert_passed("rust:test")
        self.assertIn("--offline", str(call["cmd"]))
        self.assertEqual(call["env"], {"CARGO_NET_OFFLINE": "true"})

    def test_go_test_recipe_disables_module_network(self) -> None:
        call = self._assert_passed("go:test")
        self.assertIn("go", str(call["cmd"]))
        self.assertEqual(call["env"], {"GOPROXY": "off", "GOSUMDB": "off"})

    def test_unknown_package_lifecycle_script_is_not_executed(self) -> None:
        (self.root / "package.json").write_text(
            json.dumps({"main": "index.js", "scripts": {"test": "vitest run"}}),
            encoding="utf-8",
        )
        before = len(self.calls)
        result = self.backend.run("node:test")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("node --test", result.reason or "")
        self.assertEqual(len(self.calls), before)

    def test_failure_is_projected_as_bounded_structured_diagnostic(self) -> None:
        backend = StructuredValidationBackend(
            self.root,
            lambda _args: {
                "status": "exited",
                "exit_code": 2,
                "stderr": "x" * 5000,
                "elapsed_ms": 3,
            },
            which=lambda tool: f"/fake/{tool}",
        )
        try:
            result = backend.run("python:syntax")
        finally:
            backend.close()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(len(result.diagnostics), 1)
        self.assertLessEqual(len(result.diagnostics[0]["message"]), 4000)


if __name__ == "__main__":
    unittest.main()
