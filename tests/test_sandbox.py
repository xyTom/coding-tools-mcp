from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.sandbox import (
    HELPER_VERSION, PROTOCOL_VERSION, SandboxBackend, SandboxSpec,
    _canonical_roots, _open_pinned, _trusted_install, seatbelt_profile,
)


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.install = self.base / "install"
        self.install.mkdir(mode=0o700)
        self.helper = self.install / "helper"
        self.helper.write_bytes(b"trusted pinned binary")
        self.helper.chmod(0o755)
        self.pin = hashlib.sha256(self.helper.read_bytes()).hexdigest()

    def spec(self, **kwargs):
        values = dict(workspace=self.workspace, read_roots=(self.workspace,), write_roots=(self.workspace,), deny_roots=(), helper_path=self.helper, helper_sha256=self.pin)
        values.update(kwargs)
        return SandboxSpec(**values)

    def require_trusted_fixture_ancestors(self):
        # Managed executors can expose / and /tmp as nobody-owned mounts.
        # A user-created fixture cannot provide a positive trust baseline in
        # that host layout. Check only this prerequisite: permission, pin and
        # production trust failures must still fail the actual test normally.
        owners = (0, os.geteuid())
        untrusted = []
        for ancestor in self.helper.parents:
            owner = ancestor.stat().st_uid
            if owner not in owners:
                untrusted.append(f"{ancestor} uid={owner}")
        if untrusted:
            message = ("Trusted-helper fixture requires all ancestors owned by root or "
                       f"the effective user uid={os.geteuid()}; host prerequisites unavailable: "
                       + ", ".join(untrusted))
            if os.environ.get("CODING_TOOLS_SANDBOX_REQUIRE_NATIVE") == "1":
                self.fail("Required native sandbox validation cannot skip: " + message)
            self.skipTest(message)

    def test_windows_strict_is_rejected_before_any_spawn(self):
        backend = SandboxBackend(self.spec())
        backend.platform = "win32"
        with patch("coding_tools_mcp.sandbox.spawn_process") as spawn:
            with self.assertRaises(ToolFailure) as caught:
                backend.spawn(["C:\\Windows\\System32\\cmd.exe"], cwd=self.workspace, env={})
            self.assertEqual(caught.exception.code, "SANDBOX_UNAVAILABLE")
            spawn.assert_not_called()
        self.assertFalse(backend.capability_report()["descendant_cleanup"])

    def test_macos_strict_rejects_unenforced_descendant_lifetime(self):
        backend = SandboxBackend(self.spec())
        backend.platform = "darwin"
        with patch("coding_tools_mcp.sandbox.spawn_process") as spawn:
            with self.assertRaises(ToolFailure) as caught:
                backend.spawn(["/bin/sh", "-c", "touch side-effect"], cwd=self.workspace, env={})
            self.assertIn("descendant", caught.exception.message)
            spawn.assert_not_called()
        self.assertFalse((self.workspace / "side-effect").exists())

    def test_proxy_is_fail_closed(self):
        backend = SandboxBackend(self.spec(network="proxy"))
        with patch("coding_tools_mcp.sandbox.spawn_process") as spawn:
            with self.assertRaises(ToolFailure) as caught:
                backend.spawn(["/bin/true"], cwd=self.workspace, env={})
            self.assertEqual(caught.exception.code, "SANDBOX_NETWORK_UNSUPPORTED")
            spawn.assert_not_called()

    def test_capability_errors_identify_the_failed_prerequisite(self):
        cases = (
            ("linux", "offline", (), None, True, "SANDBOX_UNAVAILABLE"),
            ("linux", "proxy", ("example.com:443",), None, True, "SANDBOX_UNAVAILABLE"),
            ("linux", "proxy", ("example.com:443",), self.helper, False, "SANDBOX_UNAVAILABLE"),
            ("darwin", "proxy", ("example.com:443",), self.helper, True, "SANDBOX_UNAVAILABLE"),
            ("win32", "proxy", ("example.com:443",), self.helper, True, "SANDBOX_UNAVAILABLE"),
            ("linux", "proxy", (), self.helper, True, "SANDBOX_NETWORK_UNSUPPORTED"),
            ("linux", "unknown", (), self.helper, True, "SANDBOX_NETWORK_UNSUPPORTED"),
        )
        for platform, network, destinations, helper, installed, expected in cases:
            with self.subTest(platform=platform, network=network, helper=helper, installed=installed):
                backend = SandboxBackend(self.spec(network=network, allowed_destinations=destinations, helper_path=helper))
                backend.platform = platform
                with patch("coding_tools_mcp.sandbox.platform.machine", return_value="x86_64"), \
                        patch("coding_tools_mcp.sandbox.Path.is_file", return_value=installed), \
                        patch("coding_tools_mcp.sandbox.socket.socketpair") as control, \
                        patch("coding_tools_mcp.sandbox.spawn_process") as spawn:
                    report = backend.capability_report()
                    self.assertEqual(report["error_code"], expected)
                    self.assertFalse(report["strict_supported"])
                    with self.assertRaises(ToolFailure) as caught:
                        backend.spawn([str(self.helper)], cwd=self.workspace, env={})
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(caught.exception.details["capabilities"], report)
                    control.assert_not_called()
                    spawn.assert_not_called()

    def test_unavailable_cwd_is_policy_error_before_setup(self):
        ordinary_file = self.workspace / "file"
        ordinary_file.write_text("not a directory")
        for cwd in (self.workspace / "missing", ordinary_file, self.workspace / "nul\x00path"):
            with self.subTest(cwd=cwd):
                backend = SandboxBackend(self.spec())
                with patch.object(backend, "capability_report", return_value={"reason": None}), \
                        patch("coding_tools_mcp.sandbox.socket.socketpair") as control, \
                        patch("coding_tools_mcp.sandbox.spawn_process") as spawn:
                    with self.assertRaises(ToolFailure) as caught:
                        backend.spawn([str(self.helper)], cwd=cwd, env={})
                self.assertEqual(caught.exception.code, "SANDBOX_POLICY_INVALID")
                self.assertFalse(backend._last_confirmed)
                control.assert_not_called()
                spawn.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_symlink_loops_have_domain_errors_before_setup(self):
        loop = self.base / "loop"
        loop.symlink_to(loop)
        with self.assertRaises(ToolFailure) as caught:
            _canonical_roots((loop,), must_exist=True)
        self.assertEqual(caught.exception.code, "SANDBOX_POLICY_INVALID")
        with self.assertRaises(ToolFailure) as caught:
            SandboxBackend(self.spec(helper_path=loop))._helper_fd(())
        self.assertEqual(caught.exception.code, "SANDBOX_HELPER_UNTRUSTED")
        backend = SandboxBackend(self.spec())
        with patch.object(backend, "capability_report", return_value={"reason": None}), \
                patch("coding_tools_mcp.sandbox.socket.socketpair") as control:
            with self.assertRaises(ToolFailure) as caught:
                backend.spawn([str(self.helper)], cwd=loop, env={})
        self.assertEqual(caught.exception.code, "SANDBOX_POLICY_INVALID")
        control.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX control descriptors")
    def test_interrupted_setup_closes_control_and_pinned_descriptors(self):
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        pinned = []

        def interrupted_argv(argv, cwd, control_fd, nonce, fds, reads, writes):
            descriptor = os.open(self.helper, os.O_RDONLY)
            pinned.append(descriptor)
            fds.append(descriptor)
            raise KeyboardInterrupt

        backend = SandboxBackend(self.spec())
        with patch.object(backend, "capability_report", return_value={"reason": None}), \
                patch.object(backend, "_linux_argv", side_effect=interrupted_argv), \
                patch("coding_tools_mcp.sandbox.socket.socketpair", return_value=(parent, child)), \
                patch("coding_tools_mcp.sandbox.spawn_process") as spawn:
            with self.assertRaises(KeyboardInterrupt):
                backend.spawn([str(self.helper)], cwd=self.workspace, env={})
        self.assertFalse(backend._last_confirmed)
        self.assertEqual((parent.fileno(), child.fileno()), (-1, -1))
        for descriptor in pinned:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        spawn.assert_not_called()

    def test_seatbelt_is_deny_default_offline_and_escaped(self):
        odd = self.base / 'quote" newline\npath'
        profile = seatbelt_profile(self.spec(read_roots=(odd,), deny_roots=(self.base / "secret",)))
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny network*)", profile)
        self.assertIn('quote\\" newline\\npath', profile)
        self.assertNotIn("(allow network", profile)
        self.assertNotIn("(allow mach-lookup", profile)
        self.assertIn("(allow file-map-executable (subpath", profile)
        self.assertIn('(allow file-read* (literal "/"))', profile)
        self.assertNotIn('(subpath "/")', profile)
        self.assertIn("(deny file-read* file-write*", profile)

    def test_root_grant_rejected(self):
        with self.assertRaises(ToolFailure):
            _canonical_roots((Path("/"),), must_exist=True)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor and ownership checks")
    def test_pinned_open_rejects_intermediate_symlink(self):
        target = self.base / "target"
        target.mkdir()
        (target / "file").write_text("outside")
        link = self.workspace / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            _open_pinned(link / "file")
        with self.assertRaises(ToolFailure):
            _canonical_roots((link,), must_exist=True)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor and ownership checks")
    def test_helper_workspace_and_shared_install_rejected(self):
        with self.assertRaises(ToolFailure):
            _trusted_install(self.helper, (self.base,))
        self.install.chmod(0o777)
        self.addCleanup(self.install.chmod, 0o700)
        with self.assertRaises(ToolFailure):
            _trusted_install(self.helper, ())

    @unittest.skipIf(os.name == "nt", "POSIX descriptor and ownership checks")
    def test_helper_pin_and_trust_checked_without_executing_it(self):
        self.require_trusted_fixture_ancestors()
        backend = SandboxBackend(self.spec(helper_sha256="0" * 64))
        with self.assertRaises(ToolFailure) as caught:
            backend._helper_fd(())
        self.assertEqual(caught.exception.code, "SANDBOX_HELPER_UNTRUSTED")
        self.assertIn("SHA-256 does not match", caught.exception.message)
        fd = SandboxBackend(self.spec())._helper_fd(())
        self.addCleanup(os.close, fd)
        self.assertEqual(os.read(fd, 100), b"trusted pinned binary")

    def test_invalid_helper_pin_rejected_before_install_checks(self):
        # These rejection cases need no trusted host ancestry and remain
        # exercised even when a positive helper fixture is unavailable.
        for pin in (None, "", "0" * 63, "0" * 65, "g" * 64):
            with self.subTest(pin=pin):
                with self.assertRaises(ToolFailure) as caught:
                    SandboxBackend(self.spec(helper_sha256=pin))._helper_fd(())
                self.assertEqual(caught.exception.code, "SANDBOX_HELPER_UNTRUSTED")
                self.assertIn("exact SHA-256 pin", caught.exception.message)

    @unittest.skipIf(os.name == "nt", "POSIX control descriptors")
    def test_stdout_cannot_forge_startup(self):
        backend = SandboxBackend(self.spec())
        backend.platform = "linux"
        # The replacement launcher can only print to stdout. It never has the
        # inherited control descriptor and must not be accepted as isolated.
        def fake_argv(argv, cwd, control_fd, nonce, fds, reads, writes):
            return ["/bin/sh", "-c", f"printf 'CTMCP_SANDBOX {PROTOCOL_VERSION} {HELPER_VERSION} linux-bwrap {nonce}\\n'"]
        with patch.object(backend, "_linux_argv", side_effect=fake_argv), patch.object(backend, "capability_report", return_value={"reason": None}), patch("coding_tools_mcp.sandbox.os.killpg"):
            # The short-lived spoof fixture is not a namespace process tree.
            # Never signal a real process group from this protocol unit test.
            with self.assertRaises(ToolFailure) as caught:
                backend.spawn(["/bin/true"], cwd=self.workspace, env={})
        self.assertEqual(caught.exception.code, "SANDBOX_INITIALIZATION_FAILED")
        self.assertFalse(backend.capability_report()["last_launch_confirmed"])

    @unittest.skipIf(os.name == "nt", "POSIX control descriptors")
    def test_wrong_version_control_message_rejected_before_go(self):
        backend = SandboxBackend(self.spec())
        backend.platform = "linux"
        marker = self.workspace / "side-effect"
        code = (
            "import os,socket,sys; s=socket.socket(fileno=int(sys.argv[1])); "
            "s.sendall(('CTMCP_SANDBOX 999 9.9.9 linux-bwrap '+sys.argv[2]+'\\n').encode()); "
            "gate=s.recv(100); "
            "open(sys.argv[3],'w').write('bad') if gate else None"
        )
        import sys
        def fake_argv(argv, cwd, control_fd, nonce, fds, reads, writes):
            return [sys.executable, "-c", code, str(control_fd), nonce, str(marker)]
        with patch.object(backend, "_linux_argv", side_effect=fake_argv), patch.object(backend, "capability_report", return_value={"reason": None}), patch("coding_tools_mcp.sandbox.os.killpg"):
            # The short-lived spoof fixture is not a namespace process tree.
            # Never signal a real process group from this protocol unit test.
            with self.assertRaises(ToolFailure):
                backend.spawn(["/bin/true"], cwd=self.workspace, env={})
        self.assertFalse(marker.exists())

    @unittest.skipUnless(__import__("sys").platform.startswith("linux"), "Linux version probe")
    def test_old_bwrap_is_rejected(self):
        backend = SandboxBackend(self.spec())
        with patch("coding_tools_mcp.sandbox.Path.resolve", return_value=self.helper), patch("coding_tools_mcp.sandbox._trusted_install"), patch("coding_tools_mcp.sandbox.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"bubblewrap 0.9.0\n", b"")):
            with self.assertRaises(ToolFailure) as caught:
                backend._bwrap(())
        self.assertIn("0.12.0", caught.exception.message)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor-backed launch planning")
    def test_user_namespace_is_required_not_optional(self):
        self.require_trusted_fixture_ancestors()
        backend = SandboxBackend(self.spec())
        descriptors = []
        try:
            with patch.object(backend, "_bwrap", return_value=Path("/usr/bin/bwrap")):
                argv = backend._linux_argv(["/bin/true"], self.workspace, 77, "a" * 64, descriptors, (self.workspace,), (self.workspace,))
            self.assertIn("--unshare-all", argv)
            self.assertIn("--unshare-user", argv)
            self.assertIn("--disable-userns", argv)
            self.assertNotIn("--unshare-user-try", argv)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    def test_missing_bwrap_raises_domain_failure(self):
        backend = SandboxBackend(self.spec())
        with patch("coding_tools_mcp.sandbox.Path.resolve", side_effect=FileNotFoundError("missing bwrap")):
            with self.assertRaises(ToolFailure) as caught:
                backend._bwrap(())
        self.assertEqual(caught.exception.code, "SANDBOX_UNAVAILABLE")

    def test_nested_denies_rejected_even_for_readonly_roots(self):
        secret = self.workspace / "credential"
        secret.write_text("private")
        for writes in ((self.workspace,), ()):
            backend = SandboxBackend(self.spec(write_roots=writes, deny_roots=(secret,)))
            backend.platform = "linux"
            with patch.object(backend, "capability_report", return_value={"reason": None}), patch("coding_tools_mcp.sandbox.spawn_process") as spawn:
                with self.assertRaises(ToolFailure) as caught:
                    backend.spawn(["/bin/true"], cwd=self.workspace, env={})
                self.assertEqual(caught.exception.code, "SANDBOX_POLICY_INVALID")
                self.assertIn("disjoint", caught.exception.message)
                spawn.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX inode trust checks")
    def test_hardlinked_helper_rejected(self):
        os.link(self.helper, self.workspace / "alias")
        with self.assertRaises(ToolFailure) as caught:
            SandboxBackend(self.spec())._helper_fd(())
        self.assertEqual(caught.exception.code, "SANDBOX_HELPER_UNTRUSTED")

    def test_capabilities_do_not_claim_completed_probe(self):
        report = SandboxBackend(self.spec()).capability_report()
        self.assertFalse(report["last_launch_confirmed"])
        self.assertFalse(report["proxy_egress"])
        self.assertEqual(report["protocol_version"], 1)


if __name__ == "__main__":
    unittest.main()
