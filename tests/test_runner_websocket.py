from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from coding_tools_mcp.runner.client import RunnerClientConfig, RunnerPeer
from coding_tools_mcp.runner.credentials import RunnerCredentialStore
from coding_tools_mcp.runner.jobs import RunnerJobReconciler
from coding_tools_mcp.runner.protocol import RunnerHello, WorkspaceInventoryItem
from coding_tools_mcp.runner.registry import RunnerRegistry
from coding_tools_mcp.runner.routing import (
    RemoteMcpRouteService,
    RemoteMcpRouteStore,
    RunnerMcpRouter,
    RunnerMcpSessionHost,
)
from coding_tools_mcp.runner.websocket import (
    BlockingWebSocket,
    WebSocketClosedError,
    websocket_accept_value,
)
from coding_tools_mcp.server import MCPHandler, Runtime, RuntimeHTTPServer


class FakeRunnerRuntime:
    _sequence = 0

    def __init__(self) -> None:
        type(self)._sequence += 1
        self.http_session_id = f"runner-runtime-{self._sequence}"
        self.closed = False

    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake-runner", "version": "1"},
            "clientInfoEcho": client_info or {},
        }

    def list_tools(self) -> dict[str, Any]:
        return {"tools": [{"name": "read_file"}]}

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        request_id: str | int | None = None,
    ) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": name}],
            "structuredContent": {
                "name": name,
                "arguments": dict(arguments or {}),
                "request_id": request_id,
            },
            "isError": False,
        }

    def close(self) -> None:
        self.closed = True


class RunnerWebSocketFrameTests(unittest.TestCase):
    def test_rfc_accept_vector_and_masked_round_trip(self) -> None:
        self.assertEqual(
            websocket_accept_value("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )
        left, right = socket.socketpair()
        client = BlockingWebSocket(left, client_side=True)
        server = BlockingWebSocket(right, client_side=False)
        try:
            client.send("hello")
            self.assertEqual(server.recv(), "hello")
            server.send("world")
            self.assertEqual(client.recv(), "world")
        finally:
            client.close()
            server.close()


class RunnerWebSocketEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.control_runtime = Runtime(root, auth_token="mcp-token", transport="http")
        self.credentials = RunnerCredentialStore()
        self.issued = self.credentials.issue("runner-one")
        self.registry = RunnerRegistry()
        self.reconciler = RunnerJobReconciler()
        self.routes = RemoteMcpRouteService(RemoteMcpRouteStore())
        self.server = RuntimeHTTPServer(
            ("127.0.0.1", 0),
            MCPHandler,
            self.control_runtime,
            lambda _context: Runtime(root, auth_token="mcp-token", transport="http"),
            runner_route_service=self.routes,
            runner_credentials=self.credentials,
            runner_registry=self.registry,
            runner_job_reconciler=self.reconciler,
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.runner_sessions = RunnerMcpSessionHost(lambda _workspace, _auth: FakeRunnerRuntime())
        self.router = RunnerMcpRouter(self.runner_sessions)

    def tearDown(self) -> None:
        self.runner_sessions.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp.cleanup()

    def _peer(self, credential: str, *, instance_id: str) -> RunnerPeer:
        port = self.server.server_address[1]
        config = RunnerClientConfig(
            server_url=f"ws://127.0.0.1:{port}/runner/ws",
            runner_id="runner-one",
            instance_id=instance_id,
            credential=credential,
            heartbeat_seconds=0.05,
        )
        hello = RunnerHello(
            runner_id="runner-one",
            instance_id=instance_id,
            credential=credential,
            capabilities=("mcp",),
            workspaces=(WorkspaceInventoryItem("ws-one", "Workspace One", r"G:\\repo"),),
        )
        return RunnerPeer(config, hello, self.router)

    def _wait_connected(self, expected: bool, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bool(self.routes.runner_status("runner-one")["connected"]) is expected:
                return
            time.sleep(0.01)
        self.fail(f"Runner connected state did not become {expected}")

    def test_runner_peer_serves_remote_mcp_over_real_websocket_upgrade(self) -> None:
        peer = self._peer(self.issued.credential, instance_id="instance-one")
        errors: list[BaseException] = []

        def run_peer() -> None:
            try:
                asyncio.run(peer.run_once())
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=run_peer, daemon=True)
        thread.start()
        self._wait_connected(True)

        route = self.routes.create_session_sync(
            control_session_id="control-one",
            runner_id="runner-one",
            workspace_id="ws-one",
            authorization_key="principal-one",
        )
        self.assertEqual(route.runner_id, "runner-one")
        initialized = self.routes.call_session_sync(
            "control-one",
            authorization_key="principal-one",
            workspace_id="ws-one",
            method="initialize",
            params={"clientInfo": {"name": "test"}},
        )
        self.assertEqual(initialized["result"]["capabilities"]["tools"]["listChanged"], False)
        tools = self.routes.call_session_sync(
            "control-one",
            authorization_key="principal-one",
            workspace_id="ws-one",
            method="tools/list",
            params={},
        )
        self.assertEqual(tools["result"]["tools"][0]["name"], "read_file")

        peer.stop_sync("test complete")
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self._wait_connected(False)

    def test_invalid_runner_credential_fails_before_route_attachment(self) -> None:
        peer = self._peer("wrong-credential", instance_id="instance-bad")
        with self.assertRaises((WebSocketClosedError, OSError)):
            asyncio.run(peer.run_once())
        self.assertFalse(self.routes.runner_status("runner-one")["connected"])
        self.assertIsNone(self.registry.get("runner-one"))

    def test_control_plane_runner_shutdown_is_bounded(self) -> None:
        peer = self._peer(self.issued.credential, instance_id="instance-shutdown")
        errors: list[BaseException] = []

        def run_peer() -> None:
            try:
                asyncio.run(peer.run_once())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_peer, daemon=True)
        thread.start()
        self._wait_connected(True)
        started = time.monotonic()
        self.routes.close_transports_sync()
        thread.join(timeout=3)
        elapsed = time.monotonic() - started
        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 3.0)
        self.assertTrue(
            not errors or all(isinstance(item, (WebSocketClosedError, OSError)) for item in errors),
            errors,
        )
        self.assertFalse(self.routes.runner_status("runner-one")["connected"])


if __name__ == "__main__":
    unittest.main()
