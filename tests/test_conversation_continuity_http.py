from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from coding_tools_mcp.conversation_continuity import ConversationBindingStore
from coding_tools_mcp.server import (
    AuthorizationContext,
    BoundRuntimeFactory,
    MCPHandler,
    RuntimeHTTPServer,
    WorkspaceBinding,
    WorkspaceBindingResolver,
    WorkspaceCatalog,
    WorkspaceEntry,
    build_parser,
    build_runtime,
    load_project_context,
    runtime_policy_from_args,
)
from coding_tools_mcp.transcript import TranscriptStore


TOKEN = "synthetic-continuity-token"


class ConversationContinuityHttpTests(unittest.TestCase):
    def test_two_windows_restart_recovery_and_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace-original"
            changed_workspace = root / "workspace-changed"
            config = root / "config"
            workspace.mkdir()
            changed_workspace.mkdir()
            config.mkdir()
            (workspace / "identity.txt").write_text("continuity", encoding="utf-8")
            (changed_workspace / "identity.txt").write_text("changed", encoding="utf-8")
            transcript_path = config / "transcripts.sqlite3"
            binding_path = config / "bindings.sqlite3"

            def make_server(
                workspace_root: Path,
            ) -> tuple[RuntimeHTTPServer, Any, TranscriptStore, ConversationBindingStore]:
                # A restart must reconstruct every durable dependency rather than
                # reusing the first process's live Store instances.
                catalog = WorkspaceCatalog(
                    [WorkspaceEntry("ws", "Workspace", workspace_root, True, True)],
                    "ws",
                )
                resolver = WorkspaceBindingResolver(catalog)
                args = build_parser().parse_args(["--workspace", str(workspace_root)])
                policy = runtime_policy_from_args(args)
                transcripts = TranscriptStore(transcript_path)
                bindings = ConversationBindingStore(binding_path)
                binding = WorkspaceBinding("ws", workspace_root, "bearer")
                control = build_runtime(
                    args,
                    policy,
                    auth_token=TOKEN,
                    project_context=load_project_context(workspace_root),
                    workspace_binding=binding,
                    authorization_context=AuthorizationContext("bearer"),
                    transport="http",
                )
                factory = BoundRuntimeFactory(
                    args,
                    policy,
                resolver,
                auth_token=TOKEN,
                oauth_config=None,
            )
                server = RuntimeHTTPServer(
                    ("127.0.0.1", 0),
                    MCPHandler,
                    control,
                    factory,
                    conversation_binding_store=bindings,
                )
                # The production constructor wires this through persisted services;
                # the focused HTTP contract supplies the same independent stores.
                server.operator_service = type(
                    "ContinuitySource",
                    (),
                    {"transcript_store": transcripts, "close": lambda _self: None},
                )()
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                return server, thread, transcripts, bindings

            def stop_server(server: RuntimeHTTPServer, thread: Any) -> None:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                server.control_runtime.close()

            def rpc(request: dict[str, Any], *, session_id: str | None = None) -> tuple[int, dict[str, str], dict[str, Any]]:
                body = json.dumps(request).encode("utf-8")
                headers = {
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2025-06-18",
                }
                if session_id:
                    headers["Mcp-Session-Id"] = session_id
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("POST", "/mcp", body=body, headers=headers)
                    response = connection.getresponse()
                    raw = response.read()
                    response_headers = {key.lower(): value for key, value in response.getheaders()}
                    return response.status, response_headers, json.loads(raw) if raw else {}
                finally:
                    connection.close()

            def initialize(request_id: int) -> str:
                status, headers, payload = rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": f"window-{request_id}", "version": "1"},
                        },
                    }
                )
                self.assertEqual(status, 200, payload)
                session_id = headers.get("mcp-session-id")
                assert session_id
                return session_id

            def start_conversation(session_id: str, request_id: int) -> dict[str, Any]:
                status, _headers, payload = rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "conversation_start", "arguments": {}},
                    },
                    session_id=session_id,
                )
                self.assertEqual(status, 200, payload)
                if "result" not in payload:
                    raise AssertionError(payload)
                return payload["result"]["structuredContent"]

            server, thread, first_transcripts, first_bindings = make_server(workspace)
            port = int(server.server_address[1])
            try:
                window_a = initialize(1)
                window_b = initialize(2)
                first = start_conversation(window_a, 3)
                second = start_conversation(window_b, 4)
                self.assertNotEqual(first["conversation_id"], second["conversation_id"])
                self.assertFalse(first["resumed"])
                again = start_conversation(window_a, 5)
                self.assertEqual(again["conversation_id"], first["conversation_id"])
                self.assertTrue(again["resumed"])
            finally:
                stop_server(server, thread)

            server, thread, second_transcripts, second_bindings = make_server(workspace)
            self.assertIsNot(first_transcripts, second_transcripts)
            self.assertIsNot(first_bindings, second_bindings)
            port = int(server.server_address[1])
            try:
                recovered = start_conversation(window_a, 6)
                self.assertTrue(recovered["resumed"])
                self.assertEqual(recovered["conversation_id"], first["conversation_id"])

                window_c = initialize(7)
                third = start_conversation(window_c, 8)
                self.assertFalse(third["resumed"])
                self.assertNotIn(third["conversation_id"], {first["conversation_id"], second["conversation_id"]})
                status, _headers, payload = rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {"name": "conversation_list", "arguments": {}},
                    },
                    session_id=window_c,
                )
                self.assertEqual(status, 200, payload)
                items = payload["result"]["structuredContent"]["items"]
                # The new transport is not bound to either window automatically.
                # Because this focused HTTP case intentionally reuses one bearer
                # principal, owner-scoped listing may show that principal's rows.
                self.assertIn(
                    first["conversation_id"],
                    {item["conversation_id"] for item in items},
                )
                status, _headers, payload = rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {
                            "name": "conversation_resume",
                            "arguments": {"conversation_id": first["conversation_id"]},
                        },
                    },
                    session_id=window_c,
                )
                self.assertEqual(status, 200, payload)
                explicit = payload["result"]["structuredContent"]
                self.assertEqual(explicit["conversation_id"], first["conversation_id"])
            finally:
                stop_server(server, thread)

            # Retained headers cannot recover, list, or resume a Conversation
            # after the repository scope changes, even with the same workspace
            # identifier and bearer principal.
            server, thread, _changed_transcripts, _changed_bindings = make_server(changed_workspace)
            port = int(server.server_address[1])
            try:
                window_d = initialize(11)
                changed = start_conversation(window_d, 12)
                self.assertFalse(changed["resumed"])
                self.assertNotEqual(changed["conversation_id"], first["conversation_id"])

                status, _headers, payload = rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 13,
                        "method": "tools/call",
                        "params": {"name": "conversation_list", "arguments": {}},
                    },
                    session_id=window_d,
                )
                self.assertEqual(status, 200, payload)
                changed_items = payload["result"]["structuredContent"]["items"]
                self.assertNotIn(first["conversation_id"], {item["conversation_id"] for item in changed_items})

                status, _headers, payload = rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 14,
                        "method": "tools/call",
                        "params": {
                            "name": "conversation_resume",
                            "arguments": {"conversation_id": first["conversation_id"]},
                        },
                    },
                    session_id=window_d,
                )
                self.assertEqual(status, 200, payload)
                self.assertTrue(payload["result"]["isError"])
                self.assertEqual(
                    payload["result"]["structuredContent"]["error"]["code"],
                    "CONVERSATION_NOT_FOUND",
                )
                self.assertNotIn(first["conversation_id"], json.dumps(payload))
            finally:
                stop_server(server, thread)

            # Restoring the original repository scope restores only its exact
            # durable binding and does not launch a replacement Conversation.
            server, thread, _restored_transcripts, _restored_bindings = make_server(workspace)
            port = int(server.server_address[1])
            try:
                restored = start_conversation(window_a, 15)
                self.assertTrue(restored["resumed"])
                self.assertEqual(restored["conversation_id"], first["conversation_id"])
            finally:
                stop_server(server, thread)
            for secret in (TOKEN, window_a, window_b, window_c, window_d):
                self.assertNotIn(secret.encode("utf-8"), binding_path.read_bytes())
                self.assertNotIn(secret.encode("utf-8"), transcript_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
