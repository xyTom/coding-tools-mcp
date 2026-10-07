"""Policy compilation tests, not native sandbox acceptance."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.policy import IsolationConfig, compile_policy


class ExecutionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runtime = self.root / "runtime"

    def test_strict_helper_is_readonly_offline_and_has_no_destinations(self) -> None:
        config = IsolationConfig(mode="strict", network="proxy", allowed_destinations=("example.com:443",))
        policy = compile_policy(config, self.workspace, self.runtime, purpose="read-helper")
        self.assertEqual(policy.network, "offline")
        self.assertEqual(policy.write_roots, (self.runtime,))
        self.assertEqual(policy.allowed_destinations, ())
        with self.assertRaises(FrozenInstanceError):
            policy.network = "proxy"  # type: ignore[misc]

    def test_structured_only_does_not_authorize_workspace_writes(self) -> None:
        build = self.workspace / "build"
        policy = compile_policy(IsolationConfig(mode="strict"), self.workspace, self.runtime,
                                structured_only=True, write_paths=(build,))
        self.assertEqual(policy.write_roots, (self.runtime, build))
        self.assertIn(self.workspace, policy.read_roots)

    def test_denial_overrides_read_root(self) -> None:
        secret = self.workspace / "credentials"
        policy = compile_policy(IsolationConfig(mode="strict", deny_roots=(secret,)), self.workspace, self.runtime)
        self.assertFalse(policy.permits_read(secret / "key"))
        self.assertTrue(policy.permits_read(self.workspace / "src"))

    def test_runtime_and_service_roots_cannot_be_accidentally_widened(self) -> None:
        with self.assertRaises(ToolFailure):
            compile_policy(IsolationConfig(mode="strict"), self.workspace, self.workspace / "runtime")
        with self.assertRaises(ToolFailure):
            compile_policy(IsolationConfig(mode="strict", deny_roots=(self.root,)), self.workspace, self.runtime)

    def test_config_requires_absolute_pinned_helper(self) -> None:
        for config in ({"helper_path": Path("helper")}, {"helper_sha256": "bad"}, {"mode": "best-effort"}):
            with self.subTest(config=config), self.assertRaises(ToolFailure):
                IsolationConfig(**config)

    def test_cli_is_additive_and_environment_configuration_is_static(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(IsolationConfig.from_args(Namespace()).mode, "compatibility")
            config = IsolationConfig.from_args(Namespace(execution_isolation="strict", sandbox_network="proxy",
                                                         sandbox_allow_destination=["example.com:443"]))
            self.assertEqual(config.allowed_destinations, ("example.com:443",))

    def test_default_toolchain_roots_do_not_expose_private_tls_keys(self) -> None:
        policy = compile_policy(IsolationConfig(mode="strict"), self.workspace, self.runtime)
        self.assertFalse(policy.permits_read(Path("/etc/ssl/private/server.key")))
        if Path("/etc/ssl/certs").exists():
            self.assertTrue(policy.permits_read(Path("/etc/ssl/certs/ca-certificates.crt")))

    def test_missing_static_root_is_a_configuration_error(self) -> None:
        with self.assertRaises(ToolFailure) as caught:
            compile_policy(IsolationConfig(mode="strict", read_roots=(self.root / "missing",)), self.workspace, self.runtime)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_unresolvable_roots_remain_configuration_errors(self) -> None:
        loop = self.root / "loop"
        loop.symlink_to(loop)
        cases = (
            (IsolationConfig(mode="strict", read_roots=(loop,)), self.runtime, ()),
            (IsolationConfig(mode="strict", deny_roots=(loop,)), self.runtime, ()),
            (IsolationConfig(mode="strict"), loop, ()),
            (IsolationConfig(mode="strict"), self.runtime, (loop,)),
        )
        for config, runtime, writes in cases:
            with self.subTest(config=config, runtime=runtime, writes=writes), self.assertRaises(ToolFailure) as caught:
                compile_policy(config, self.workspace, runtime, structured_only=True, write_paths=writes)
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
            self.assertEqual(caught.exception.details["path"], str(loop))

    def test_nul_root_is_a_configuration_error(self) -> None:
        with self.assertRaises(ToolFailure) as caught:
            compile_policy(IsolationConfig(mode="strict", read_roots=(self.root / "nul\x00root",)), self.workspace, self.runtime)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_unexpandable_cli_or_environment_roots_are_configuration_errors(self) -> None:
        for name in ("SANDBOX_READ_ROOT", "SANDBOX_DENY_ROOT"):
            for source in ("cli", "environment"):
                with self.subTest(name=name, source=source), \
                        patch.dict(os.environ, {f"CODING_TOOLS_MCP_{name}S": "~/root"}, clear=True), \
                        patch("coding_tools_mcp.policy.Path.expanduser", side_effect=RuntimeError("Home unavailable")):
                    args = Namespace(**{name.lower(): ["~/root"]}) if source == "cli" else Namespace()
                    with self.assertRaises(ToolFailure) as caught:
                        IsolationConfig.from_args(args)
                    self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_cli_rejects_invalid_roots_without_traceback(self) -> None:
        loop = self.workspace / "loop"
        loop.symlink_to(loop)
        cases = (
            (("--sandbox-read-root", str(loop)), "ERROR: INVALID_ARGUMENT:"),
            (("--sandbox-read-root", "~coding_tools_missing_user_3fa946a9/root"), "ERROR:"),
            (("--workspace-mutation", "structured-only", "--write-path", str(loop)), "ERROR: INVALID_ARGUMENT:"),
        )
        for options, diagnostic in cases:
            for transport in (("--stdio",), ("--host", "127.0.0.1", "--port", "0")):
                self._check_invalid_cli_options(options, transport, diagnostic)

    def _check_invalid_cli_options(self, options: tuple[str, ...], transport: tuple[str, ...], diagnostic: str) -> None:
        with self.subTest(options=options, transport=transport):
            result = subprocess.run(
                [sys.executable, "-m", "coding_tools_mcp", "--workspace", str(self.workspace),
                 "--execution-isolation", "strict", *options, *transport],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=10, env={**os.environ, "CODING_TOOLS_MCP_TELEMETRY": "off"},
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn(diagnostic, result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_invalid_cli_configuration_is_reportable_without_traceback(self) -> None:
        from coding_tools_mcp.server import build_parser, runtime_policy_from_args
        args = build_parser().parse_args(["--sandbox-allow-destination", "https://example.com"])
        with self.assertRaises(ValueError):
            runtime_policy_from_args(args)
