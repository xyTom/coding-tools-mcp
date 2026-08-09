from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterable

from coding_tools_mcp.runner.client import LocalRunnerApplication, RunnerClientConfig, RunnerPeer
from coding_tools_mcp.runner.credentials import RunnerCredentialStore
from coding_tools_mcp.runner.jobs import (
    JobInventoryItem,
    JobRecord,
    JobState,
    RunnerJobInventoryRegistry,
    RunnerJobReconciler,
)
from coding_tools_mcp.runner.protocol import RunnerEvent, RunnerHello, WorkspaceInventoryItem
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
from coding_tools_mcp.upstream import UpstreamManager
from coding_tools_mcp.workspace_catalog import WorkspaceEntry


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


class RunnerLocalApplicationTests(unittest.TestCase):
    def test_remote_mcp_runtimes_get_isolated_runner_local_upstream_managers(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            managers: list[UpstreamManager] = []

            def upstream_factory() -> UpstreamManager:
                manager = UpstreamManager.empty()
                managers.append(manager)
                return manager

            application = LocalRunnerApplication(
                [WorkspaceEntry("ws-one", "Workspace One", root, True, True)],
                upstream_manager_factory=upstream_factory,
            )
            first = application._runtime_factory("ws-one", "auth-one")
            second = application._runtime_factory("ws-one", "auth-two")
            try:
                self.assertEqual(len(managers), 2)
                self.assertIs(first.upstream_manager, managers[0])
                self.assertIs(second.upstream_manager, managers[1])
                self.assertIsNot(first.upstream_manager, second.upstream_manager)
            finally:
                first.close()
                second.close()
                application.close()


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

    def _peer(
        self,
        credential: str,
        *,
        instance_id: str,
        job_inventory_provider: Callable[[], Iterable[JobInventoryItem]] | None = None,
    ) -> RunnerPeer:
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
        return RunnerPeer(
            config,
            hello,
            self.router,
            job_inventory_provider=job_inventory_provider,
        )

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

        peer.publish_event(
            RunnerEvent(
                "runner-one",
                "instance-one",
                "job/progress",
                {"job_id": "job-one", "progress": 50},
                "ws-one",
            )
        )
        pushed = self.routes.next_runner_event_sync("runner-one", timeout=2.0)
        self.assertEqual(pushed.name, "job/progress")
        self.assertEqual(pushed.workspace_id, "ws-one")
        self.assertEqual(pushed.payload["progress"], 50)
        with self.assertRaisesRegex(Exception, "Workspace"):
            peer.publish_event(
                RunnerEvent(
                    "runner-one",
                    "instance-one",
                    "job/progress",
                    {"progress": 75},
                    "ws-other",
                )
            )

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

    def test_job_inventory_is_sent_on_reconnect_and_recovers_known_job(self) -> None:
        instance_id = "instance-jobs"
        inventory = RunnerJobInventoryRegistry()
        inventory.upsert(
            JobInventoryItem(
                "job-one",
                "ws-one",
                "process-one",
                JobState.RUNNING,
                stdout_cursor=12,
                stderr_cursor=3,
                started_at="2026-08-09T08:00:00Z",
            )
        )
        self.reconciler.register(
            JobRecord(
                "job-one",
                "runner-one",
                "ws-one",
                "owner-one",
                process_fingerprint="process-one",
                runner_instance_id=instance_id,
                stdout_cursor=4,
                stderr_cursor=1,
                started_at="2026-08-09T08:00:00Z",
            )
        )

        def start_peer() -> tuple[RunnerPeer, threading.Thread, list[BaseException]]:
            peer = self._peer(
                self.issued.credential,
                instance_id=instance_id,
                job_inventory_provider=inventory.snapshot,
            )
            errors: list[BaseException] = []

            def run_peer() -> None:
                try:
                    asyncio.run(peer.run_once())
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            thread = threading.Thread(target=run_peer, daemon=True)
            thread.start()
            self._wait_connected(True)
            return peer, thread, errors

        first, first_thread, first_errors = start_peer()
        first.stop_sync("planned reconnect")
        first_thread.join(timeout=3)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(first_errors, first_errors)
        self._wait_connected(False)
        self.assertEqual(self.reconciler.get("job-one").state, JobState.RECOVERING)

        second, second_thread, second_errors = start_peer()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.reconciler.get("job-one").state == JobState.RUNNING:
                break
            time.sleep(0.01)
        recovered = self.reconciler.get("job-one")
        self.assertEqual(recovered.state, JobState.RUNNING)
        self.assertEqual((recovered.stdout_cursor, recovered.stderr_cursor), (12, 3))
        second.stop_sync("test complete")
        second_thread.join(timeout=3)
        self.assertFalse(second_thread.is_alive())
        self.assertFalse(second_errors, second_errors)


if __name__ == "__main__":
    unittest.main()
