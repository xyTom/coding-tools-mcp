from __future__ import annotations

import sys
import unittest
from pathlib import Path

from coding_tools_mcp.semantic import LspSemanticBackend, LspServerSpec, NullSemanticBackend

TEST_ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = TEST_ROOT / "fixtures" / "semantic-project"
FAKE_LSP = TEST_ROOT / "fixtures" / "fake_lsp_server.py"


def fake_python_spec() -> LspServerSpec:
    return LspServerSpec(
        language="python",
        language_id="python",
        extensions=(".py",),
        candidates=((sys.executable, str(FAKE_LSP)),),
    )


class NullSemanticBackendTests(unittest.TestCase):
    def test_null_backend_reports_capability_unavailable(self) -> None:
        backend = NullSemanticBackend("disabled for test")
        status = backend.semantic_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["status"], "unavailable")

        result = backend.goto_definition("main.py", 0, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"]["code"], "SEMANTIC_UNAVAILABLE")


class LspSemanticBackendTests(unittest.TestCase):
    def backend(self, *, max_results: int = 200, max_text_chars: int = 4_000) -> LspSemanticBackend:
        return LspSemanticBackend(
            FIXTURE_ROOT,
            server_specs=(fake_python_spec(),),
            request_timeout=2.0,
            diagnostics_wait=0.1,
            max_results=max_results,
            max_text_chars=max_text_chars,
        )

    def test_status_is_lazy_then_ready_after_first_query(self) -> None:
        with self.backend() as backend:
            before = backend.semantic_status("main.py")
            self.assertEqual(before["status"], "available")
            self.assertFalse(before["languages"][0]["running"])

            symbols = backend.document_symbols("main.py")
            self.assertTrue(symbols["ok"], symbols)
            self.assertEqual([item["name"] for item in symbols["items"]], ["Greeter", "greet"])
            self.assertEqual(symbols["items"][1]["container_name"], "Greeter")

            after = backend.semantic_status("main.py")
            self.assertEqual(after["status"], "ready")
            self.assertTrue(after["languages"][0]["running"])

    def test_definition_and_references_are_workspace_scoped_and_bounded(self) -> None:
        with self.backend(max_results=2) as backend:
            definition = backend.goto_definition("main.py", 6, 18)
            self.assertTrue(definition["ok"], definition)
            self.assertEqual(definition["items"][0]["path"], "main.py")

            references = backend.find_references("main.py", 6, 18)
            self.assertTrue(references["ok"], references)
            self.assertEqual(references["count"], 2)
            self.assertTrue(references["truncated"])
            self.assertEqual(references["filtered_outside_workspace"], 1)
            self.assertTrue(all(item["path"] == "main.py" for item in references["items"]))

    def test_diagnostics_are_structured_and_text_is_bounded(self) -> None:
        with self.backend(max_text_chars=16) as backend:
            result = backend.document_diagnostics("main.py")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["diagnostic_source"], "textDocument/diagnostic")
            self.assertEqual(result["count"], 1)
            diagnostic = result["items"][0]
            self.assertLessEqual(len(diagnostic["message"]), 16)
            self.assertTrue(diagnostic["message_truncated"])
            self.assertEqual(diagnostic["source"], "fake-lsp")

    def test_workspace_escape_is_rejected_before_lsp_start(self) -> None:
        with self.backend() as backend:
            result = backend.document_symbols("../outside.py")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "SEMANTIC_PATH_OUTSIDE_WORKSPACE")
            status = backend.semantic_status()
            self.assertFalse(status["languages"][0]["running"])

    def test_missing_lsp_returns_unavailable_without_starting_a_process(self) -> None:
        missing_spec = LspServerSpec(
            language="python",
            language_id="python",
            extensions=(".py",),
            candidates=(("coding-tools-lsp-that-does-not-exist-7c1a6d", "--stdio"),),
        )
        with LspSemanticBackend(FIXTURE_ROOT, server_specs=(missing_spec,), request_timeout=0.2) as backend:
            status = backend.semantic_status("main.py")
            self.assertEqual(status["status"], "unavailable")
            result = backend.document_symbols("main.py")
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["error"]["code"], "SEMANTIC_LSP_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
