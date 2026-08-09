from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_tools_mcp.workspace_catalog import WorkspaceEntry
from coding_tools_mcp.workspace_host import (
    LocalWorkspaceHost,
    RemoteRunnerWorkspaceHost,
    WorkspaceHostError,
    WorkspaceHostFactory,
)


class WorkspaceHostFactoryTests(unittest.TestCase):
    def test_local_workspace_uses_local_host_and_preserves_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceEntry("local", "Local", root)
            host = WorkspaceHostFactory().create(workspace)
            self.assertIsInstance(host, LocalWorkspaceHost)
            handle = host.resolve_workspace_handle()
            self.assertEqual(handle.workspace_id, "local")
            self.assertEqual(handle.target, "local")
            self.assertEqual(handle.root, str(root))
            self.assertIsNone(handle.runner_id)

    def test_runner_workspace_never_falls_back_to_local_host(self) -> None:
        workspace = WorkspaceEntry(
            "remote",
            "Remote Windows",
            r"G:\LLM\coding-tools-mcp",
            target="runner",
            runner_id="home-win",
        )
        with self.assertRaises(WorkspaceHostError) as unavailable:
            WorkspaceHostFactory().create(workspace)
        self.assertEqual(unavailable.exception.code, "RUNNER_ROUTE_NOT_AVAILABLE")
        self.assertTrue(unavailable.exception.retryable)

    def test_runner_workspace_handle_keeps_remote_root_opaque(self) -> None:
        workspace = WorkspaceEntry(
            "remote",
            "Remote Windows",
            r"G:\LLM\coding-tools-mcp",
            target="runner",
            runner_id="home-win",
        )
        route_service = object()
        host = WorkspaceHostFactory(remote_route_service=route_service).create(workspace)
        self.assertIsInstance(host, RemoteRunnerWorkspaceHost)
        handle = host.resolve_workspace_handle()
        self.assertEqual(handle.target, "runner")
        self.assertEqual(handle.runner_id, "home-win")
        self.assertEqual(handle.root, r"G:\LLM\coding-tools-mcp")

    def test_local_validation_runs_through_workspace_runtime_exec_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
            workspace = WorkspaceEntry("local", "Local", root)
            host = WorkspaceHostFactory().create(workspace)
            try:
                backend = host.get_validation_backend()
                self.assertEqual(backend.status()["backend"], "structured")
                result = backend.run("python:syntax")
                self.assertEqual(result.status, "passed")
                self.assertEqual(result.exit_code, 0)
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
