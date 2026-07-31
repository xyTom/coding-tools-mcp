from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_ROOT = REPO_ROOT / "apps" / "desktop-client"
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

try:
    import psutil  # noqa: F401
except ModuleNotFoundError:
    fake_psutil = types.ModuleType("psutil")

    class PsutilError(Exception):
        pass

    fake_psutil.Error = PsutilError
    fake_psutil.AccessDenied = PsutilError
    fake_psutil.CONN_LISTEN = "LISTEN"
    fake_psutil.Process = object
    fake_psutil.net_connections = lambda **_kwargs: []
    fake_psutil.process_iter = lambda *_args, **_kwargs: []
    fake_psutil.wait_procs = lambda processes, **_kwargs: (processes, [])
    sys.modules["psutil"] = fake_psutil

from mcp_desktop_client import runtime, storage  # noqa: E402
from mcp_desktop_client.i18n import tr  # noqa: E402
from mcp_desktop_client.models import WorkspaceProfile, build_profile  # noqa: E402


LOCALES_DIR = DESKTOP_ROOT / "mcp_desktop_client" / "locales"


class DesktopModelTests(unittest.TestCase):
    def test_build_profile_preserves_filesystem_root(self) -> None:
        profile = build_profile(os.path.abspath(os.path.sep))

        self.assertEqual(profile.path, os.path.normpath(os.path.abspath(os.path.sep)))

    def test_runtime_match_accepts_python_module_entrypoint(self) -> None:
        profile = build_profile(str(REPO_ROOT), "review")
        manager = runtime.RuntimeManager()
        command = [
            sys.executable,
            "-m",
            "coding_tools_mcp",
            "--workspace",
            profile.path,
            "--port",
            str(profile.runtime.local_port),
        ]

        with mock.patch.object(manager, "_command_for_pid", return_value=command):
            self.assertTrue(manager._process_matches_profile(123, profile))

    def test_local_checkout_is_a_valid_runtime_fallback(self) -> None:
        profile = build_profile(str(REPO_ROOT), "review")
        manager = runtime.RuntimeManager()

        with mock.patch.object(runtime.shutil, "which", return_value=None):
            command = manager._resolve_command(profile)

        self.assertEqual(command, [sys.executable, "-m", "coding_tools_mcp"])

    def test_custom_runtime_command_preserves_quoted_arguments(self) -> None:
        profile = build_profile(str(REPO_ROOT), "review")
        profile.runtime.runtime_command = (
            '"/path with spaces/coding-tools-mcp" --label "two words"'
        )

        command = runtime.RuntimeManager()._resolve_command(profile)

        self.assertEqual(
            command, ["/path with spaces/coding-tools-mcp", "--label", "two words"]
        )

    def test_frp_snippet_uses_loopback_and_sanitizes_name(self) -> None:
        profile = build_profile(str(REPO_ROOT), 'unsafe " name')
        profile.tunnel.frp_subdomain = "mcp"

        snippet = profile.frp_proxy_snippet()

        self.assertIn('name = "unsafe-name-mcp"', snippet)
        self.assertIn('localIP = "127.0.0.1"', snippet)


class DesktopStorageTests(unittest.TestCase):
    def test_profile_id_cannot_escape_state_directory(self) -> None:
        with self.assertRaisesRegex(ValueError, "ID"):
            storage.log_dir_for_profile("../../outside")

    def test_profile_and_secret_files_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app_home = Path(temporary_directory) / "desktop"
            replacements = {
                "APP_HOME": app_home,
                "PROFILES_FILE": app_home / "profiles.json",
                "SECRETS_FILE": app_home / "secrets.json",
                "STATE_DIR": app_home / "state",
            }
            profile = build_profile(str(REPO_ROOT), "review")

            with mock.patch.multiple(storage, **replacements):
                storage.save_profiles([profile])
                public_payload = json.loads(
                    storage.PROFILES_FILE.read_text(encoding="utf-8")
                )
                loaded = storage.load_profiles()

                self.assertEqual(
                    public_payload["profiles"][0]["auth"]["bearer_token"], ""
                )
                self.assertEqual(loaded[0].auth.bearer_token, profile.auth.bearer_token)
                if os.name != "nt":
                    self.assertEqual(storage.APP_HOME.stat().st_mode & 0o077, 0)
                    self.assertEqual(storage.PROFILES_FILE.stat().st_mode & 0o077, 0)
                    self.assertEqual(storage.SECRETS_FILE.stat().st_mode & 0o077, 0)


class DesktopI18nTests(unittest.TestCase):
    def test_translation_falls_back_to_english_without_qt(self) -> None:
        with mock.patch.dict(sys.modules, {"PySide6": None, "PySide6.QtCore": None}):
            self.assertEqual(tr("MainWindow", "Start"), "Start")

    def test_chinese_catalog_is_complete(self) -> None:
        import xml.etree.ElementTree as ET

        root = ET.parse(LOCALES_DIR / "app_zh_CN.ts").getroot()
        messages = [
            message
            for context in root.findall("context")
            for message in context.findall("message")
        ]

        self.assertGreater(len(messages), 100)
        self.assertTrue((LOCALES_DIR / "app_zh_CN.qm").is_file())
        for message in messages:
            translation = message.find("translation")
            self.assertIsNotNone(translation)
            self.assertNotEqual(translation.get("type"), "unfinished")
            self.assertTrue((translation.text or "").strip())

    def test_compiled_catalog_switches_between_english_and_chinese(self) -> None:
        try:
            from PySide6.QtCore import QCoreApplication, QSettings
        except (ImportError, ModuleNotFoundError):
            self.skipTest("PySide6 is not installed")

        from mcp_desktop_client.language_manager import LanguageManager

        application = QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = QSettings(
                str(Path(temporary_directory) / "settings.ini"),
                QSettings.Format.IniFormat,
            )
            manager = LanguageManager(settings=settings)

            manager.set_language("zh_CN")
            self.assertEqual(manager.effective_language, "zh_CN")
            self.assertEqual(tr("MainWindow", "Start"), "启动")
            invalid_profile = build_profile(
                str(REPO_ROOT / "missing-workspace"), "missing"
            )
            with self.assertRaisesRegex(RuntimeError, "工作区目录不存在"):
                runtime.RuntimeManager()._validate_tunnel_requirements(invalid_profile)

            manager.set_language("en_US")
            self.assertEqual(manager.effective_language, "en_US")
            self.assertEqual(tr("MainWindow", "Start"), "Start")
            with self.assertRaisesRegex(
                RuntimeError, "Workspace directory does not exist"
            ):
                runtime.RuntimeManager()._validate_tunnel_requirements(invalid_profile)
            self.assertEqual(settings.value(LanguageManager.SETTINGS_KEY), "en_US")
        application.processEvents()


class DesktopRuntimeSafetyTests(unittest.TestCase):
    def test_bearer_token_is_passed_via_environment_only(self) -> None:
        profile = build_profile(str(REPO_ROOT), "review")
        profile.auth.type = "bearer"
        profile.auth.bearer_token = "secret-token"
        environment = {
            "CODING_TOOLS_MCP_OAUTH_MODE": "1",
            "CODING_TOOLS_MCP_OAUTH_PASSWORD": "inherited",
        }

        arguments = runtime.RuntimeManager()._runtime_args(profile, environment)

        self.assertNotIn("secret-token", arguments)
        self.assertEqual(environment["CODING_TOOLS_MCP_AUTH_TOKEN"], "secret-token")
        self.assertNotIn("CODING_TOOLS_MCP_OAUTH_MODE", environment)
        self.assertNotIn("CODING_TOOLS_MCP_OAUTH_PASSWORD", environment)

    def test_state_runtime_uses_saved_identity_after_profile_changes(self) -> None:
        manager = runtime.RuntimeManager()
        state = {
            "runtime_pid": 4242,
            "runtime_create_time": 100.0,
            "port": 28766,
            "workspace": str(REPO_ROOT),
        }

        with (
            mock.patch.object(manager, "_read_runtime_state", return_value=state),
            mock.patch.object(manager, "_process_has_create_time", return_value=True),
            mock.patch.object(
                manager, "_process_matches_runtime", return_value=True
            ) as matches,
        ):
            self.assertEqual(manager._find_state_runtime("profile"), (4242, 28766))

        matches.assert_called_once_with(4242, workspace=str(REPO_ROOT), port=28766)

    def test_stale_tunnel_pid_is_not_reused(self) -> None:
        profile = build_profile(str(REPO_ROOT), "review")
        profile.tunnel.type = "cloudflare"
        manager = runtime.RuntimeManager()
        state = {"tunnel_pid": 4242, "tunnel_create_time": 100.0}

        with (
            mock.patch.object(manager, "_read_runtime_state", return_value=state),
            mock.patch.object(manager, "_process_has_create_time", return_value=False),
            mock.patch.object(
                manager, "_find_cloudflare_tunnel_pid", return_value=None
            ),
        ):
            self.assertIsNone(manager._find_tunnel_pid(profile))

    def test_terminate_refuses_reused_pid(self) -> None:
        manager = runtime.RuntimeManager()

        class FakeProcess:
            terminated = False

            def create_time(self) -> float:
                return 200.0

            def children(self, *, recursive: bool) -> list[FakeProcess]:
                self.assert_recursive = recursive
                return []

            def terminate(self) -> None:
                self.terminated = True

        process = FakeProcess()
        with mock.patch.object(runtime.psutil, "Process", return_value=process):
            manager._terminate_process_tree(4242, expected_create_time=100.0)

        self.assertFalse(process.terminated)

    def test_quick_tunnel_timeout_terminates_cloudflared(self) -> None:
        manager = runtime.RuntimeManager()
        profile = WorkspaceProfile(id="profile", name="review", path=str(REPO_ROOT))
        profile.tunnel.type = "cloudflare"
        profile.tunnel.cloudflare_mode = "quick"

        class FakePopen:
            pid = 4242
            stdout = None

            def poll(self) -> None:
                return None

        class FakeEvent:
            def set(self) -> None:
                pass

            def wait(self, *, timeout: float) -> bool:
                self.timeout = timeout
                return False

        class FakeThread:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                pass

            def join(self, *, timeout: float) -> None:
                self.timeout = timeout

        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                mock.patch.object(
                    manager, "_find_cloudflared_command", return_value="cloudflared"
                ),
                mock.patch.object(manager, "_process_create_time", return_value=100.0),
                mock.patch.object(manager, "_terminate_process_tree") as terminate,
                mock.patch.object(
                    runtime,
                    "log_dir_for_profile",
                    return_value=Path(temporary_directory),
                ),
                mock.patch.object(
                    runtime.subprocess, "Popen", return_value=FakePopen()
                ),
                mock.patch.object(runtime.threading, "Event", FakeEvent),
                mock.patch.object(runtime.threading, "Thread", FakeThread),
            ):
                with self.assertRaisesRegex(RuntimeError, "trycloudflare.com"):
                    manager._start_cloudflare_tunnel(profile)

        terminate.assert_called_once_with(4242, expected_create_time=100.0)


if __name__ == "__main__":
    unittest.main()
