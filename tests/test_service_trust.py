"""Strict startup must never grant writes to the service's importable code."""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock, patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.file_broker import broker_supported
from coding_tools_mcp.policy import IsolationConfig
from coding_tools_mcp.project_context import ProjectContext
from coding_tools_mcp.server import Runtime, WorkspaceMutationPolicy
from coding_tools_mcp.service_trust import ServiceImportSnapshot


class ServiceTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def runtime(self, **kwargs) -> Runtime:
        runtime = Runtime(self.workspace, project_context=ProjectContext((), (), ()), **kwargs)
        self.addCleanup(runtime.close)
        return runtime

    def assert_rejected(self, **kwargs) -> None:
        with self.assertRaises(ToolFailure) as caught:
            self.runtime(isolation=IsolationConfig(mode="strict"), **kwargs)
        self.assertEqual(caught.exception.code, "SANDBOX_POLICY_INVALID")

    def test_service_package_root_is_mandatory_without_sys_path_entry(self) -> None:
        import coding_tools_mcp.service_trust as service_trust
        package = Path(service_trust.__file__).resolve().parent
        with patch.object(sys, "path", []), patch.object(sys, "modules", {}):
            snapshot = ServiceImportSnapshot.capture()
        with self.assertRaises(ToolFailure):
            snapshot.reject_overlapping_writes((package,))

    def test_served_service_checkout_is_rejected_before_command_setup(self) -> None:
        import coding_tools_mcp.server as server
        self.workspace = Path(server.__file__).resolve().parent.parent
        source = Path(server.__file__).read_bytes()
        for mode in ("unrestricted", "structured-only"):
            with self.subTest(mode=mode), patch("coding_tools_mcp.server.WorkspaceCommandManager") as manager:
                self.assert_rejected(workspace_mutation=WorkspaceMutationPolicy(mode=mode))
                manager.assert_not_called()
        self.assertEqual(Path(server.__file__).read_bytes(), source)

    def test_lazy_import_attack_and_structured_source_mutation_never_run(self) -> None:
        module_name = "_strict_service_lazy_attack"
        source = self.workspace / f"{module_name}.py"
        marker = self.base / "escaped-service-authority"
        original = "VALUE = 'trusted'\n"
        source.write_text(original)
        payload = f"from pathlib import Path; Path({str(marker)!r}).write_text('escaped')"
        self.addCleanup(sys.modules.pop, module_name, None)
        for mode in ("unrestricted", "structured-only"):
            with self.subTest(mode=mode), patch.object(sys, "path", [str(self.workspace), *sys.path]):
                with self.assertRaises(ToolFailure) as caught:
                    runtime = self.runtime(
                        isolation=IsolationConfig(mode="strict"),
                        workspace_mutation=WorkspaceMutationPolicy(mode=mode),
                    )
                    # This is the exploit sequence a missing startup check would
                    # permit: structured tools change a lazily imported module,
                    # then Python executes it with the service's host authority.
                    runtime.apply_patch({"patch": f"*** Begin Patch\n*** Update File: {module_name}.py\n@@\n-VALUE = 'trusted'\n+{payload}\n*** End Patch"})
                    importlib.invalidate_caches()
                    importlib.import_module(module_name)
                self.assertEqual(caught.exception.code, "SANDBOX_POLICY_INVALID")
                self.assertEqual(source.read_text(), original)
                self.assertFalse(marker.exists())
                self.assertNotIn(module_name, sys.modules)

    def test_command_denials_do_not_exempt_import_trees_from_startup_check(self) -> None:
        package = self.workspace / "protected-package"
        package.mkdir()
        with patch.object(sys, "path", [str(package), *sys.path]), self.assertRaises(ToolFailure) as caught:
            self.runtime(
                isolation=IsolationConfig(mode="strict", deny_roots=(package,)),
                workspace_mutation=WorkspaceMutationPolicy(mode="structured-only"),
            )
        self.assertEqual(caught.exception.code, "SANDBOX_POLICY_INVALID")

    def test_empty_and_relative_search_paths_use_startup_cwd(self) -> None:
        previous = Path.cwd()
        try:
            os.chdir(self.workspace)
            for entry in ("", ".", "missing-import-directory"):
                with self.subTest(entry=entry), patch.object(sys, "path", [entry]):
                    self.assert_rejected()
            with patch.object(sys, "path", ["", "relative"]):
                snapshot = ServiceImportSnapshot.capture()
        finally:
            os.chdir(previous)
        self.assertIn(self.workspace, snapshot.roots)
        self.assertIn(self.workspace / "relative", snapshot.roots)
        with self.assertRaises(FrozenInstanceError):
            snapshot.roots = ()  # type: ignore[misc]

    def test_missing_import_tree_and_search_ancestor_are_rejected(self) -> None:
        for entry in (self.workspace / "not-created-yet", self.base):
            with self.subTest(entry=entry), patch.object(sys, "path", [str(entry)]):
                self.assert_rejected()
        self.assertFalse((self.workspace / "not-created-yet").exists())

    def test_loaded_package_paths_outside_sys_path_are_protected(self) -> None:
        module = types.ModuleType("_strict_loaded_package")
        module.__path__ = [str(self.workspace / "package")]
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assert_rejected()

    def test_namespace_snapshot_does_not_run_dynamic_import_hooks(self) -> None:
        from importlib._bootstrap_external import _NamespacePath
        module = types.ModuleType("_strict_namespace_package")
        finder = Mock(side_effect=AssertionError("namespace import hook must not run"))
        module.__path__ = _NamespacePath(module.__name__, [str(self.workspace / "namespace")], finder)
        # Changing sys.path would make normal namespace iteration call finder.
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(sys, "path", [*sys.path, "/unused"]):
            self.assert_rejected()
        finder.assert_not_called()

    def test_editable_package_and_namespace_targets_are_protected_without_import(self) -> None:
        module = types.ModuleType("__editable___strict_test_finder")
        module.PATH_PLACEHOLDER = "__editable__.strict_test.__path_hook__"
        module.find_spec = Mock(side_effect=AssertionError("must not import to inspect paths"))
        for mapping, namespaces in (
            ({"dependency": str(self.workspace / "source")}, {}),
            ({}, {"dependency": [str(self.workspace / "namespace")]}),
        ):
            module.MAPPING = mapping
            module.NAMESPACES = namespaces
            with self.subTest(mapping=mapping), patch.dict(sys.modules, {module.__name__: module}), \
                    patch.object(sys, "path", [module.PATH_PLACEHOLDER, *sys.path]):
                self.assert_rejected()
            module.find_spec.assert_not_called()

    @unittest.skipIf(os.name == "nt", "symlink creation needs Windows privileges")
    def test_import_path_symlink_spelling_cannot_be_retargeted_by_workspace(self) -> None:
        trusted = self.base / "trusted-imports"
        trusted.mkdir()
        alias = self.workspace / "alias"
        alias.symlink_to(trusted, target_is_directory=True)
        with patch.object(sys, "path", [str(alias)]):
            self.assert_rejected()
        self.assertEqual(alias.resolve(), trusted)

    def test_command_runtime_and_fallback_cannot_be_importable(self) -> None:
        trusted = self.base / "trusted-imports"
        trusted.mkdir()
        for selected in ("runtime_dir_for_workspace", "fallback_runtime_dir_for_workspace"):
            with self.subTest(selected=selected), patch.object(sys, "path", [str(trusted), *sys.path]), \
                    patch(f"coding_tools_mcp.server.{selected}", return_value=trusted / "command-state"):
                self.assert_rejected()
            self.assertFalse((trusted / "command-state").exists())

    def test_compatibility_does_not_capture_or_reject_import_roots(self) -> None:
        with patch.object(sys, "path", [str(self.workspace), *sys.path]), \
                patch.object(ServiceImportSnapshot, "capture", side_effect=AssertionError("compatibility changed")):
            self.runtime(isolation=IsolationConfig(mode="compatibility"))

    @unittest.skipUnless(broker_supported(), "POSIX handle-relative file broker")
    def test_disjoint_import_and_workspace_roots_accept_structured_edits(self) -> None:
        trusted = self.base / "trusted-imports"
        trusted.mkdir()
        (trusted / "lazy.py").write_text("VALUE = 'trusted'\n")
        with patch.object(sys, "path", [str(trusted), *sys.path]):
            runtime = self.runtime(
                isolation=IsolationConfig(mode="strict"),
                workspace_mutation=WorkspaceMutationPolicy(mode="structured-only"),
            )
            runtime.apply_patch({"patch": "*** Begin Patch\n*** Add File: source.py\n+VALUE = 'workspace'\n*** End Patch"})
        self.assertEqual((self.workspace / "source.py").read_text(), "VALUE = 'workspace'\n")
        self.assertEqual((trusted / "lazy.py").read_text(), "VALUE = 'trusted'\n")


if __name__ == "__main__":
    unittest.main()
