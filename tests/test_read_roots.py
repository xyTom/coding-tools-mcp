from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import (
    Runtime,
    build_parser,
    outside_read_policy_from_args,
    read_roots_from_args,
)


class ReadRootTests(unittest.TestCase):
    def test_external_read_root_supports_direct_read_listing_and_search(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            read_root = base / "profile-data"
            workspace.mkdir()
            read_root.mkdir()
            external_file = read_root / "settings.txt"
            external_file.write_text("external marker\n", encoding="utf-8")

            runtime = Runtime(workspace, read_roots=[read_root])
            try:
                read = runtime.read_file({"path": str(external_file)})
                self.assertEqual(read["path"], str(external_file.resolve()))
                self.assertEqual(read["content"].splitlines(), ["external marker"])

                listed = runtime.list_dir({"path": str(read_root), "include_hidden": True})
                self.assertEqual([item["path"] for item in listed["entries"]], [str(external_file.resolve())])

                files = runtime.list_files({"path": str(read_root), "patterns": ["*"]})
                self.assertEqual([item["path"] for item in files["files"]], [str(external_file.resolve())])

                searched = runtime.search_text({"path": str(read_root), "query": "external marker"})
                self.assertEqual(searched["total_matches"], 1)
                self.assertEqual(searched["matches"][0]["path"], str(external_file.resolve()))

                info = runtime.server_info({})
                self.assertEqual(info["read_roots"], [str(read_root.resolve())])
            finally:
                runtime.close()

    def test_external_read_root_does_not_expand_write_or_command_workdir(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            read_root = base / "profile-data"
            workspace.mkdir()
            read_root.mkdir()
            external_file = read_root / "settings.txt"
            external_file.write_text("unchanged\n", encoding="utf-8")

            runtime = Runtime(workspace, read_roots=[read_root], permission_mode="trusted")
            try:
                with self.assertRaises(ToolFailure) as write_error:
                    runtime.resolve_for_write(str(external_file))
                self.assertEqual(write_error.exception.code, "ABSOLUTE_PATH_DENIED")

                with self.assertRaises(ToolFailure) as command_error:
                    runtime.exec_command(
                        {"cmd": "echo should-not-run", "workdir": str(read_root), "yield_time_ms": 1000}
                    )
                self.assertEqual(command_error.exception.code, "ABSOLUTE_PATH_DENIED")
                self.assertEqual(external_file.read_text(encoding="utf-8"), "unchanged\n")
            finally:
                runtime.close()

    def test_absolute_path_outside_configured_roots_is_denied(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            read_root = base / "allowed"
            denied_root = base / "denied"
            workspace.mkdir()
            read_root.mkdir()
            denied_root.mkdir()
            denied_file = denied_root / "secret.txt"
            denied_file.write_text("denied\n", encoding="utf-8")

            runtime = Runtime(workspace, read_roots=[read_root])
            try:
                result = runtime.call_tool("read_file", {"path": str(denied_file)})["structuredContent"]
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "ABSOLUTE_PATH_DENIED")
            finally:
                runtime.close()

    def test_request_policy_returns_manual_permission_request_without_granting(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            outside = base / "outside"
            workspace.mkdir()
            outside.mkdir()
            outside_file = outside / "requested.txt"
            outside_file.write_text("still denied\n", encoding="utf-8")

            runtime = Runtime(workspace, outside_read_policy="request")
            try:
                denied = runtime.call_tool("read_file", {"path": str(outside_file)})
                payload = denied["structuredContent"]
                self.assertTrue(denied["isError"])
                self.assertEqual(payload["error"]["code"], "PERMISSION_REQUIRED")
                self.assertEqual(payload["permission_request"]["permission"], "filesystem_read")
                self.assertEqual(payload["error"]["details"]["requested_path"], str(outside_file))

                requested = runtime.call_tool(
                    "request_permissions",
                    {
                        "tool_name": "read_file",
                        "permission": "filesystem_read",
                        "reason": "read the requested local file",
                        "arguments": {"path": str(outside_file)},
                    },
                )
                request_payload = requested["structuredContent"]
                self.assertTrue(requested["isError"])
                self.assertEqual(request_payload["status"], "user_confirmation_required")
                self.assertIsNone(request_payload["grant_id"])
                self.assertEqual(request_payload["error"]["code"], "PERMISSION_REQUIRED")

                repeated = runtime.call_tool("read_file", {"path": str(outside_file)})["structuredContent"]
                self.assertEqual(repeated["error"]["code"], "PERMISSION_REQUIRED")
            finally:
                runtime.close()

    def test_symlink_escape_from_read_root_is_denied(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            read_root = base / "allowed"
            denied_root = base / "denied"
            workspace.mkdir()
            read_root.mkdir()
            denied_root.mkdir()
            denied_file = denied_root / "secret.txt"
            denied_file.write_text("denied\n", encoding="utf-8")
            link = read_root / "escape.txt"
            try:
                link.symlink_to(denied_file)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            runtime = Runtime(workspace, read_roots=[read_root])
            try:
                result = runtime.call_tool("read_file", {"path": str(link)})["structuredContent"]
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "ABSOLUTE_PATH_DENIED")
                listed = runtime.list_dir({"path": str(read_root), "include_hidden": True})
                self.assertEqual(listed["entries"], [])
                files = runtime.list_files({"path": str(read_root), "patterns": ["*"]})
                self.assertEqual(files["files"], [])
                searched = runtime.search_text({"path": str(read_root), "query": "denied"})
                self.assertEqual(searched["total_matches"], 0)
            finally:
                runtime.close()

    def test_read_root_cli_and_environment_are_additive(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--read-root", "first", "--read-root", "second"])
        env_name = "CODING_TOOLS_MCP_READ_ROOTS"
        previous = os.environ.get(env_name)
        os.environ[env_name] = os.pathsep.join(("third", "fourth"))
        try:
            self.assertEqual(
                read_roots_from_args(args),
                [Path("first"), Path("second"), Path("third"), Path("fourth")],
            )
        finally:
            if previous is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = previous

    def test_outside_read_policy_uses_cli_then_environment_then_deny_default(self) -> None:
        parser = build_parser()
        env_name = "CODING_TOOLS_MCP_OUTSIDE_READ_POLICY"
        previous = os.environ.pop(env_name, None)
        try:
            self.assertEqual(outside_read_policy_from_args(parser.parse_args([])), "deny")
            os.environ[env_name] = "request"
            self.assertEqual(outside_read_policy_from_args(parser.parse_args([])), "request")
            self.assertEqual(
                outside_read_policy_from_args(parser.parse_args(["--outside-read-policy", "deny"])),
                "deny",
            )
        finally:
            if previous is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = previous


if __name__ == "__main__":
    unittest.main()
