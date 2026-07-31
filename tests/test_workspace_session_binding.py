from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from unittest.mock import patch
from pathlib import Path
from typing import Any, Iterator

from coding_tools_mcp.oauth import (
    OAuthIdentity,
    OAuthServiceError,
    create_access_token,
    create_authorization_grant,
)
from coding_tools_mcp.server import (
    AuthorizationContext,
    BoundRuntimeFactory,
    MCPHandler,
    RuntimeHTTPServer,
    WorkspaceBinding,
    apply_oauth_workspace_bindings,
    build_parser,
    build_persistent_oauth_config,
    build_runtime,
    load_project_context,
    load_workspace_startup,
    runtime_policy_from_args,
)
from coding_tools_mcp.settings_store import ServerSettingsStore
from coding_tools_mcp.workspace_binding import (
    WorkspaceBindingError,
    WorkspaceBindingResolver,
)
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


@contextmanager
def test_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp())
    try:
        yield root
    finally:
        database = root / "config" / "oauth.sqlite3"
        if database.exists():
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()
        for attempt in range(20):
            try:
                shutil.rmtree(root)
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                retryable = os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 145}
                if not retryable or attempt == 19:
                    raise
                time.sleep(0.05)


def rpc(
    base_port: int,
    token: str,
    request: dict[str, Any],
    *,
    session_id: str | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    body = json.dumps(request).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    connection = http.client.HTTPConnection("127.0.0.1", base_port, timeout=5)
    try:
        connection.request("POST", "/mcp", body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        payload = json.loads(raw) if raw else {}
        return response.status, response_headers, payload
    finally:
        connection.close()


def delete_session(port: int, token: str, session_id: str) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "DELETE",
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Mcp-Session-Id": session_id,
            },
        )
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}
    finally:
        connection.close()


def initialize(port: int, token: str, request_id: int) -> tuple[str, dict[str, Any]]:
    status, headers, response = rpc(
        port,
        token,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": f"agent-{request_id}", "version": "1"},
            },
        },
    )
    if status != 200:
        raise AssertionError(response)
    session_id = headers.get("mcp-session-id")
    if not session_id:
        raise AssertionError("initialize did not return Mcp-Session-Id")
    return session_id, response["result"]


def call_tool(
    port: int,
    token: str,
    session_id: str,
    request_id: int,
    name: str,
    arguments: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    status, _headers, response = rpc(
        port,
        token,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        session_id=session_id,
    )
    return status, response


class WorkspaceSessionBindingTests(unittest.TestCase):
    def test_oauth_sessions_bind_immutable_isolated_workspaces(self) -> None:
        with test_root() as root:
            first = root / "first"
            second = root / "second"
            config_dir = root / "config"
            first.mkdir()
            second.mkdir()
            (first / "identity.txt").write_text("FIRST", encoding="utf-8")
            (second / "identity.txt").write_text("SECOND", encoding="utf-8")
            (first / "AGENTS.md").write_text("FIRST-INSTRUCTIONS", encoding="utf-8")
            (second / "AGENTS.md").write_text("SECOND-INSTRUCTIONS", encoding="utf-8")
            (first / "subdir").mkdir()
            (second / "subdir").mkdir()

            catalog = WorkspaceCatalog(
                [
                    WorkspaceEntry("first", "First", first, enabled=True, default=True),
                    WorkspaceEntry("second", "Second", second, enabled=True),
                ],
                "first",
            )
            resolver = WorkspaceBindingResolver(catalog)
            oauth_config, _created = build_persistent_oauth_config(
                config_dir,
                master_key="synthetic-workspace-master-key",
                password="synthetic-workspace-password",
                server_url=None,
                token_ttl=86_400,
                registration_workspace_id=None,
            )
            oauth_config.registry.add_preregistered(
                "agent-first",
                ("http://127.0.0.1/callback",),
                client_secret=None,
                workspace_id="first",
            )
            oauth_config.registry.add_preregistered(
                "agent-second",
                ("http://127.0.0.1/callback",),
                client_secret=None,
                workspace_id="second",
            )

            args = build_parser().parse_args(["--workspace", str(first)])
            policy = runtime_policy_from_args(args)
            control_binding = WorkspaceBinding("first", first, "control")
            control_runtime = build_runtime(
                args,
                policy,
                oauth_config=oauth_config,
                project_context=load_project_context(first),
                workspace_binding=control_binding,
                authorization_context=AuthorizationContext("control"),
                transport="http",
            )
            factory = BoundRuntimeFactory(
                args,
                policy,
                resolver,
                auth_token=None,
                oauth_config=oauth_config,
            )
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                control_runtime,
                factory,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = int(server.server_address[1])
            base = f"http://127.0.0.1:{port}"

            first_grant = create_authorization_grant(
                oauth_config,
                client_id="agent-first",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            second_grant = create_authorization_grant(
                oauth_config,
                client_id="agent-second",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            first_token = create_access_token(
                oauth_config,
                base,
                client_id="agent-first",
                grant_id=first_grant,
            )
            second_token = create_access_token(
                oauth_config,
                base,
                client_id="agent-second",
                grant_id=second_grant,
            )

            try:
                first_session, first_init = initialize(port, first_token, 1)
                second_session, second_init = initialize(port, second_token, 2)
                self.assertIn("FIRST-INSTRUCTIONS", first_init["instructions"])
                self.assertNotIn("SECOND-INSTRUCTIONS", first_init["instructions"])
                self.assertIn("SECOND-INSTRUCTIONS", second_init["instructions"])
                self.assertNotIn("FIRST-INSTRUCTIONS", second_init["instructions"])

                first_status, first_read = call_tool(
                    port, first_token, first_session, 3, "read_file", {"path": "identity.txt"}
                )
                second_status, second_read = call_tool(
                    port, second_token, second_session, 4, "read_file", {"path": "identity.txt"}
                )
                self.assertEqual(first_status, 200)
                self.assertEqual(second_status, 200)
                self.assertEqual(first_read["result"]["structuredContent"]["content"], "FIRST")
                self.assertEqual(second_read["result"]["structuredContent"]["content"], "SECOND")

                escape_status, escape = call_tool(
                    port,
                    first_token,
                    first_session,
                    5,
                    "read_file",
                    {"path": str(second / "identity.txt")},
                )
                self.assertEqual(escape_status, 200)
                self.assertTrue(escape["result"]["isError"])
                self.assertFalse(escape["result"]["structuredContent"]["ok"])

                cwd_status, cwd = call_tool(
                    port,
                    first_token,
                    first_session,
                    6,
                    "set_default_cwd",
                    {"path": "subdir"},
                )
                self.assertEqual(cwd_status, 200)
                self.assertEqual(cwd["result"]["structuredContent"]["default_cwd"], "subdir")
                _status, second_cwd = call_tool(
                    port, second_token, second_session, 7, "get_default_cwd", {}
                )
                self.assertEqual(
                    second_cwd["result"]["structuredContent"]["default_cwd"], "."
                )

                first_runtime = server.sessions.get(first_session)
                second_runtime = server.sessions.get(second_session)
                self.assertIsNot(first_runtime, second_runtime)
                self.assertEqual(first_runtime.workspace.root, first.resolve())
                self.assertEqual(second_runtime.workspace.root, second.resolve())
                self.assertIsNot(first_runtime.sessions, second_runtime.sessions)
                self.assertIsNot(first_runtime.output_sessions, second_runtime.output_sessions)
                self.assertIsNot(first_runtime.project_context, second_runtime.project_context)

                mismatch_status, _headers, mismatch = rpc(
                    port,
                    second_token,
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "tools/call",
                        "params": {"name": "get_default_cwd", "arguments": {}},
                    },
                    session_id=first_session,
                )
                self.assertEqual(mismatch_status, 403)
                self.assertIn("does not match", mismatch["error"]["message"])
                delete_status, delete_response = delete_session(
                    port, second_token, first_session
                )
                self.assertEqual(delete_status, 403)
                self.assertIn("does not match", delete_response["error"]["message"])
                still_alive_status, _still_alive = call_tool(
                    port, first_token, first_session, 81, "get_default_cwd", {}
                )
                self.assertEqual(still_alive_status, 200)

                resolver.update_catalog(
                    WorkspaceCatalog(
                        [
                            WorkspaceEntry(
                                "first", "First", first, enabled=True, default=True
                            ),
                            WorkspaceEntry("second", "Second", second, enabled=False),
                        ],
                        "first",
                    )
                )
                existing_status, existing = call_tool(
                    port,
                    second_token,
                    second_session,
                    9,
                    "read_file",
                    {"path": "identity.txt"},
                )
                self.assertEqual(existing_status, 200)
                self.assertEqual(
                    existing["result"]["structuredContent"]["content"], "SECOND"
                )
                denied_status, _headers, denied = rpc(
                    port,
                    second_token,
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "disabled", "version": "1"},
                        },
                    },
                )
                self.assertEqual(denied_status, 503)
                self.assertIn("Workspace mapping", denied["error"]["message"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_startup_binding_migrates_only_a_single_enabled_workspace(self) -> None:
        with test_root() as root:
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            single = WorkspaceCatalog(
                [WorkspaceEntry("first", "First", first, enabled=True, default=True)],
                "first",
            )
            single_config, _created = build_persistent_oauth_config(
                root / "single-config",
                master_key="single-master-key",
                password="single-password",
                server_url=None,
                token_ttl=86_400,
                registration_workspace_id=None,
            )
            single_config.registry.add_preregistered(
                "single-agent",
                ("http://127.0.0.1/callback",),
                client_secret=None,
            )
            self.assertIsNone(single_config.store.get_client("single-agent")["workspace_id"])
            apply_oauth_workspace_bindings(single_config, single, {})
            self.assertEqual(
                single_config.store.get_client("single-agent")["workspace_id"],
                "first",
            )

            multiple = WorkspaceCatalog(
                [
                    WorkspaceEntry("first", "First", first, enabled=True, default=True),
                    WorkspaceEntry("second", "Second", second, enabled=True),
                ],
                "first",
            )
            multi_config, _created = build_persistent_oauth_config(
                root / "multi-config",
                master_key="multi-master-key",
                password="multi-password",
                server_url=None,
                token_ttl=86_400,
                registration_workspace_id=None,
            )
            multi_config.registry.add_preregistered(
                "multi-agent",
                ("http://127.0.0.1/callback",),
                client_secret=None,
            )
            apply_oauth_workspace_bindings(multi_config, multiple, {})
            self.assertIsNone(multi_config.store.get_client("multi-agent")["workspace_id"])
            with self.assertRaises(OAuthServiceError):
                create_authorization_grant(
                    multi_config,
                    client_id="multi-agent",
                    redirect_uri="http://127.0.0.1/callback",
                    scopes="mcp",
                )
            apply_oauth_workspace_bindings(
                multi_config,
                multiple,
                {"multi-agent": "second"},
            )
            grant_id = create_authorization_grant(
                multi_config,
                client_id="multi-agent",
                redirect_uri="http://127.0.0.1/callback",
                scopes="mcp",
            )
            self.assertEqual(multi_config.store.get_grant(grant_id)["workspace_id"], "second")

    def test_startup_loader_reads_catalog_from_server_settings(self) -> None:
        with test_root() as root:
            first = root / "first"
            second = root / "second"
            config_dir = root / "settings"
            first.mkdir()
            second.mkdir()
            ServerSettingsStore(config_dir / "server-settings.json").write(
                {
                    "workspace_catalog": [
                        {
                            "id": "first",
                            "name": "First",
                            "root": str(first),
                            "enabled": True,
                            "default": True,
                        },
                        {
                            "id": "second",
                            "name": "Second",
                            "root": str(second),
                            "enabled": True,
                            "default": False,
                        },
                    ],
                    "default_workspace_id": "first",
                    "oauth_client_workspace_bindings": {"agent-second": "second"},
                }
            )
            args = build_parser().parse_args(["--workspace", str(first)])
            with patch.dict(
                os.environ,
                {"CODING_TOOLS_MCP_CONFIG_DIR": str(config_dir)},
                clear=False,
            ):
                loaded_dir, settings, catalog = load_workspace_startup(args)
            self.assertEqual(loaded_dir, config_dir)
            self.assertEqual(catalog.default_id, "first")
            self.assertEqual(catalog.get("second").root, second.resolve())
            self.assertEqual(
                settings["oauth_client_workspace_bindings"],
                {"agent-second": "second"},
            )

    def test_resolver_fails_closed_and_stdio_uses_only_default_workspace(self) -> None:
        with test_root() as root:
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            catalog = WorkspaceCatalog(
                [
                    WorkspaceEntry("first", "First", first, enabled=True, default=True),
                    WorkspaceEntry("second", "Second", second, enabled=False),
                ],
                "first",
            )
            resolver = WorkspaceBindingResolver(catalog)
            stdio = resolver.resolve_stdio()
            self.assertEqual(stdio.workspace_id, "first")
            self.assertEqual(stdio.authorization_method, "stdio")
            with self.assertRaises(WorkspaceBindingError):
                resolver.resolve_http("oauth", None)
            with self.assertRaises(WorkspaceBindingError):
                resolver.resolve_http(
                    "oauth",
                    OAuthIdentity("agent", "grant", "second", "jti"),
                )
