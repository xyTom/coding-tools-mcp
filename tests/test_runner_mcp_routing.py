from __future__ import annotations

import itertools
import time
import threading
import unittest
from typing import Any
from unittest.mock import patch

from coding_tools_mcp import upstream as upstream_module
from coding_tools_mcp.runner.jobs import JobAccessError, RunnerJobReconciler, WorkspaceJobManager
from coding_tools_mcp.runner.routing import (
    RemoteMcpRouteError,
    RemoteMcpRouteService,
    RemoteMcpRouteState,
    RemoteMcpRouteStore,
    RunnerMcpRouter,
    RunnerMcpSessionHost,
    authorization_key_digest,
)
from coding_tools_mcp.runner.transport import RunnerUnavailableError
from coding_tools_mcp.upstream import HttpUpstreamClient, UpstreamServerConfig
from coding_tools_mcp.workspace_host import RemoteMcpHttpRuntimeProxy


class FakeRuntime:
    def __init__(self, session_id: str, *, on_close: Any | None = None) -> None:
        self.http_session_id = session_id
        self.close_count = 0
        self._on_close = on_close
        self.initialize_count = 0
        self.tool_calls: list[tuple[str, dict[str, Any], str | int | None]] = []

    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]:
        self.initialize_count += 1
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
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
        payload = dict(arguments or {})
        self.tool_calls.append((name, payload, request_id))
        return {
            "content": [{"type": "text", "text": name}],
            "structuredContent": {"name": name, "arguments": payload},
            "isError": False,
        }

    def close(self) -> None:
        self.close_count += 1
        if self._on_close is not None:
            self._on_close()


class _GatedRuntime(FakeRuntime):
    def __init__(
        self,
        session_id: str,
        *,
        call_entered: threading.Event | None = None,
        call_release: threading.Event | None = None,
        close_entered: threading.Event | None = None,
        close_release: threading.Event | None = None,
        order: list[str] | None = None,
    ) -> None:
        super().__init__(session_id)
        self.call_entered = call_entered
        self.call_release = call_release
        self.close_entered = close_entered
        self.close_release = close_release
        self.order = order

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        request_id: str | int | None = None,
    ) -> dict[str, Any]:
        if self.call_entered is not None:
            self.call_entered.set()
        if self.call_release is not None:
            self.call_release.wait()
        result = super().call_tool(name, arguments, request_id=request_id)
        if self.order is not None:
            self.order.append("call")
        return result

    def close(self) -> None:
        if self.order is not None:
            self.order.append("close")
        super().close()
        if self.close_entered is not None:
            self.close_entered.set()
        if self.close_release is not None:
            self.close_release.wait()


class FakeRunnerTransport:
    def __init__(
        self,
        runner_id: str,
        workspace_ids: tuple[str, ...],
        router: RunnerMcpRouter,
    ) -> None:
        self._runner_id = runner_id
        self._workspace_ids = workspace_ids
        self.router = router
        self.available = True
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    @property
    def runner_id(self) -> str:
        return self._runner_id

    @property
    def workspace_ids(self) -> tuple[str, ...]:
        return self._workspace_ids

    async def call(
        self,
        *,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        del request_id
        if not self.available:
            raise RunnerUnavailableError("synthetic Runner disconnect")
        payload = dict(params or {})
        self.calls.append((workspace_id, method, payload))
        return self.router.dispatch(workspace_id, method, payload)

    def call_sync(
        self,
        *,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        del request_id
        if not self.available:
            raise RunnerUnavailableError("synthetic Runner disconnect")
        payload = dict(params or {})
        self.calls.append((workspace_id, method, payload))
        return self.router.dispatch(workspace_id, method, payload)

    def method_count(self, method: str) -> int:
        return sum(1 for _workspace, called_method, _params in self.calls if called_method == method)


class DeleteResponse:
    status = 204
    headers: dict[str, str] = {}

    def __enter__(self) -> "DeleteResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class RunnerMcpRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sequence = itertools.count(1)
        self.created: list[FakeRuntime] = []

        def runtime_factory(workspace_id: str, auth_digest: str) -> FakeRuntime:
            self.assertEqual(workspace_id, "ws-a")
            self.assertEqual(len(auth_digest), 64)
            runtime = FakeRuntime(f"remote-{next(self.sequence)}")
            self.created.append(runtime)
            return runtime

        self.host = RunnerMcpSessionHost(runtime_factory, max_sessions=16)
        self.router = RunnerMcpRouter(self.host)
        self.transport = FakeRunnerTransport("runner-a", ("ws-a",), self.router)
        self.store = RemoteMcpRouteStore(max_routes=16)
        self.service = RemoteMcpRouteService(self.store)
        self.authorization_key = "oauth:client-a:grant-a:ws-a"
        self.addCleanup(self.host.shutdown)

    async def attach(self) -> None:
        result = await self.service.attach_transport(self.transport)
        self.assertFalse(result.close_intents)
        self.assertFalse(result.orphan_intents)

    async def create(self, session_id: str = "control-1") -> Any:
        return await self.service.create_session(
            control_session_id=session_id,
            runner_id="runner-a",
            workspace_id="ws-a",
            authorization_key=self.authorization_key,
        )

    async def test_remote_exec_job_sidecar_registers_owner_and_matches_runner_inventory(self) -> None:
        class FakeProcess:
            pid = 4321

            @staticmethod
            def poll() -> None:
                return None

        class FakeExecSession:
            session_id = "exec-job-1"
            process = FakeProcess()
            started_at = 1234.5
            stdout_total_bytes = 12
            stderr_total_bytes = 3
            timed_out = False
            signal_name = None
            exit_code = None

            @staticmethod
            def refresh_status() -> None:
                return None

        class JobRuntime(FakeRuntime):
            def __init__(self) -> None:
                super().__init__("remote-job-runtime")
                self.sessions_lock = threading.Lock()
                self.sessions = {"exec-job-1": FakeExecSession()}
                self.output_sessions: dict[str, Any] = {}

            def call_tool(
                self,
                name: str,
                arguments: dict[str, Any] | None,
                *,
                request_id: str | int | None = None,
            ) -> dict[str, Any]:
                if name == "exec_command":
                    return {
                        "content": [{"type": "text", "text": "running"}],
                        "structuredContent": {
                            "ok": True,
                            "session_id": "exec-job-1",
                            "status": "running",
                        },
                        "isError": False,
                    }
                return super().call_tool(name, arguments, request_id=request_id)

        host = RunnerMcpSessionHost(lambda _workspace, _auth: JobRuntime())
        service = RemoteMcpRouteService(RemoteMcpRouteStore(max_routes=4))
        transport = FakeRunnerTransport("runner-job", ("ws-a",), RunnerMcpRouter(host))
        await service.attach_transport(transport)
        reconciler = RunnerJobReconciler()
        manager = WorkspaceJobManager(reconciler, runner_id="runner-job", workspace_id="ws-a")
        proxy = RemoteMcpHttpRuntimeProxy(
            service,
            runner_id="runner-job",
            workspace_id="ws-a",
            authorization_key=self.authorization_key,
            session_authorization_key=("oauth", "client-a", "grant-a", "ws-a"),
            job_manager=manager,
            owner_principal_id="oauth:client-a:grant-a",
        )
        try:
            result = proxy.call_tool("exec_command", {"cmd": "synthetic-long-command"})
            self.assertEqual(result["structuredContent"]["session_id"], "exec-job-1")
            self.assertNotIn("job", result)
            job = manager.get_for_owner("exec-job-1", "oauth:client-a:grant-a")
            inventory = host.job_inventory()
            self.assertEqual(len(inventory), 1)
            self.assertEqual(job.process_fingerprint, inventory[0].process_fingerprint)
            self.assertEqual((job.stdout_cursor, job.stderr_cursor), (12, 3))
            refreshed = service.call_runner_sync(
                runner_id="runner-job",
                workspace_id="ws-a",
                method="job.inventory",
                params={},
            )
            self.assertEqual(refreshed["jobs"], [inventory[0].payload()])
            with self.assertRaises(JobAccessError):
                manager.get_for_owner("exec-job-1", "oauth:other:grant")
        finally:
            proxy.close()
            host.shutdown()

    async def test_delete_routes_to_creator_runner_and_duplicate_delete_is_idempotent(self) -> None:
        await self.attach()
        route = await self.create()
        self.assertEqual(route.runner_id, "runner-a")
        self.assertEqual(len(self.created), 1)

        result = await self.service.close_session(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
            expected_runner_id="runner-a",
        )
        self.assertEqual(result.status, "closed")
        self.assertFalse(result.retryable)
        self.assertEqual(self.transport.method_count("mcp.close"), 1)
        self.assertEqual(self.created[0].close_count, 1)

        duplicate = await self.service.close_session(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
            expected_runner_id="runner-a",
        )
        self.assertEqual(duplicate.status, "closed")
        self.assertEqual(self.transport.method_count("mcp.close"), 1)
        self.assertEqual(self.created[0].close_count, 1)

    async def test_wrong_principal_workspace_and_runner_are_rejected_before_remote_close(self) -> None:
        await self.attach()
        await self.create()
        cases = (
            {"authorization_key": "oauth:other", "workspace_id": "ws-a", "expected_runner_id": "runner-a"},
            {"authorization_key": self.authorization_key, "workspace_id": "ws-b", "expected_runner_id": "runner-a"},
            {"authorization_key": self.authorization_key, "workspace_id": "ws-a", "expected_runner_id": "runner-b"},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RemoteMcpRouteError) as caught:
                    await self.service.close_session("control-1", **kwargs)
                self.assertEqual(caught.exception.code, "RUNNER_ROUTE_FORBIDDEN")
        self.assertEqual(self.transport.method_count("mcp.close"), 0)
        self.assertEqual(self.created[0].close_count, 0)

    async def test_offline_close_stays_pending_until_original_runner_reconciles(self) -> None:
        await self.attach()
        route = await self.create()
        self.transport.available = False
        self.service.detach_transport("runner-a")

        pending = await self.service.close_session(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
        )
        self.assertEqual(pending.status, "close_pending")
        self.assertTrue(pending.retryable)
        pending_route = self.store.get_authorized(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
            allow_terminal=True,
        )
        self.assertEqual(pending_route.state, RemoteMcpRouteState.CLOSE_PENDING)
        self.assertFalse(pending_route.runner_connected)
        self.assertEqual(self.store.purge_closed(), 0)
        self.assertEqual(len(self.created), 1, "offline close must not create a local or replacement Runtime")

        wrong_host = RunnerMcpSessionHost(lambda _ws, _auth: FakeRuntime("wrong-1"))
        wrong_transport = FakeRunnerTransport("runner-b", ("ws-a",), RunnerMcpRouter(wrong_host))
        try:
            await self.service.attach_transport(wrong_transport)
            self.assertEqual(self.created[0].close_count, 0)
        finally:
            wrong_host.shutdown()

        reconnect = FakeRunnerTransport("runner-a", ("ws-a",), self.router)
        reconciled = await self.service.attach_transport(reconnect)
        self.assertEqual(len(reconciled.close_intents), 1)
        self.assertEqual(reconciled.close_intents[0].remote_session_id, route.remote_session_id)
        self.assertEqual(reconnect.method_count("mcp.close"), 1)
        self.assertEqual(self.created[0].close_count, 1)
        closed_route = self.store.get_authorized(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
            allow_terminal=True,
        )
        self.assertEqual(closed_route.state, RemoteMcpRouteState.CLOSED)
        self.assertEqual(self.store.purge_closed(), 1)

    async def test_reconnect_closes_runner_orphan_before_transport_is_accepted(self) -> None:
        orphan = self.host.create(
            control_session_id="orphan-control",
            workspace_id="ws-a",
            authorization_digest=authorization_key_digest(self.authorization_key),
        )
        self.assertEqual(orphan.runtime.close_count, 0)
        result = await self.service.attach_transport(self.transport)
        self.assertEqual(len(result.orphan_intents), 1)
        self.assertEqual(result.orphan_intents[0].remote_session_id, orphan.remote_session_id)
        self.assertEqual(self.transport.method_count("mcp.close"), 1)
        self.assertEqual(orphan.runtime.close_count, 1)
        self.assertEqual(self.store.snapshot()["route_count"], 0)

    async def test_runner_unavailable_is_retryable_and_never_creates_replacement_runtime(self) -> None:
        with self.assertRaises(RemoteMcpRouteError) as create_error:
            await self.create()
        self.assertEqual(create_error.exception.code, "RUNNER_UNAVAILABLE")
        self.assertTrue(create_error.exception.retryable)
        self.assertEqual(len(self.created), 0)

        await self.attach()
        await self.create()
        self.service.detach_transport("runner-a")
        with self.assertRaises(RemoteMcpRouteError) as call_error:
            await self.service.call_session(
                "control-1",
                authorization_key=self.authorization_key,
                workspace_id="ws-a",
                method="tools/call",
                params={"name": "read_file"},
            )
        self.assertEqual(call_error.exception.code, "RUNNER_UNAVAILABLE")
        self.assertTrue(call_error.exception.retryable)
        self.assertEqual(len(self.created), 1)

    async def test_call_routes_to_existing_runner_runtime_without_local_fallback(self) -> None:
        await self.attach()
        await self.create()
        self.assertEqual(len(self.created), 1)

        listed = await self.service.call_session(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
            method="tools/list",
            params={},
        )
        self.assertEqual(listed["result"]["tools"], [{"name": "read_file"}])

        called = await self.service.call_session(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
            method="tools/call",
            params={"name": "read_file", "arguments": {"path": "README.md"}, "request_id": 7},
        )
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(
            self.created[0].tool_calls,
            [("read_file", {"path": "README.md"}, 7)],
        )
        self.assertEqual(self.transport.method_count("mcp.call"), 2)
        self.assertEqual(len(self.created), 1)

    async def test_reconciliation_mismatch_marks_route_lost_instead_of_rebinding(self) -> None:
        await self.attach()
        route = await self.create()
        self.service.detach_transport("runner-a")
        self.host.close_session(
            control_session_id="control-1",
            remote_session_id=route.remote_session_id,
            workspace_id="ws-a",
            authorization_digest=authorization_key_digest(self.authorization_key),
        )
        replacement = self.host.create(
            control_session_id="control-1",
            workspace_id="ws-a",
            authorization_digest=authorization_key_digest(self.authorization_key),
        )
        self.assertNotEqual(replacement.remote_session_id, route.remote_session_id)
        reconnect = FakeRunnerTransport("runner-a", ("ws-a",), self.router)
        reconciled = await self.service.attach_transport(reconnect)
        self.assertEqual(reconciled.lost, ("control-1",))
        self.assertEqual(len(reconciled.orphan_intents), 1)
        self.assertEqual(reconciled.orphan_intents[0].remote_session_id, replacement.remote_session_id)
        self.assertEqual(replacement.runtime.close_count, 1)
        lost_route = self.store.get_authorized(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
            allow_terminal=True,
        )
        self.assertEqual(lost_route.state, RemoteMcpRouteState.LOST)
        self.assertEqual(len(self.created), 2)

        second_reconnect = FakeRunnerTransport("runner-a", ("ws-a",), self.router)
        second_result = await self.service.attach_transport(second_reconnect)
        self.assertEqual(second_result.restored, ())
        still_lost = self.store.get_authorized(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
            allow_terminal=True,
        )
        self.assertEqual(still_lost.state, RemoteMcpRouteState.LOST)

    async def test_reconnect_storm_reconciles_inventory_without_creating_new_runtime(self) -> None:
        await self.attach()
        await self.create()
        self.assertEqual(len(self.created), 1)
        for _ in range(50):
            self.service.detach_transport("runner-a")
            reconnect = FakeRunnerTransport("runner-a", ("ws-a",), self.router)
            result = await self.service.attach_transport(reconnect)
            self.assertEqual(result.restored, ("control-1",))
            self.assertEqual(reconnect.method_count("mcp.inventory"), 1)
            self.assertEqual(reconnect.method_count("mcp.create"), 0)
            self.assertEqual(len(self.created), 1)
            self.assertEqual(self.created[0].initialize_count, 0)
        route = self.store.get_authorized(
            "control-1",
            authorization_key=self.authorization_key,
            workspace_id="ws-a",
        )
        self.assertEqual(route.state, RemoteMcpRouteState.ACTIVE)

    def test_runner_host_duplicate_close_is_idempotent_and_authorization_bound(self) -> None:
        record = self.host.create(
            control_session_id="control-1",
            workspace_id="ws-a",
            authorization_digest=authorization_key_digest(self.authorization_key),
        )
        with self.assertRaises(RemoteMcpRouteError) as forbidden:
            self.host.close_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=authorization_key_digest("other-principal"),
            )
        self.assertEqual(forbidden.exception.code, "RUNNER_ROUTE_FORBIDDEN")
        self.assertTrue(
            self.host.close_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=authorization_key_digest(self.authorization_key),
            )
        )
        self.assertTrue(
            self.host.close_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=authorization_key_digest(self.authorization_key),
            )
        )
        self.assertEqual(record.runtime.close_count, 1)

    def test_runner_shutdown_propagates_to_http_upstream_delete(self) -> None:
        upstream = HttpUpstreamClient(
            UpstreamServerConfig(
                alias="remote",
                transport="streamable_http",
                url="http://127.0.0.1/upstream",
            ),
            "2025-11-25",
        )
        upstream.session_id = "synthetic-upstream-session"
        runtime = FakeRuntime("runner-runtime-1", on_close=upstream.close)
        host = RunnerMcpSessionHost(lambda _workspace, _auth: runtime)
        host.create(
            control_session_id="control-1",
            workspace_id="ws-a",
            authorization_digest=authorization_key_digest(self.authorization_key),
        )
        with patch.object(
            upstream_module.urllib.request,
            "urlopen",
            return_value=DeleteResponse(),
        ) as urlopen:
            host.shutdown()
            host.shutdown()
        self.assertEqual(runtime.close_count, 1)
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["mcp-session-id"], "synthetic-upstream-session")

    def test_public_route_snapshot_contains_no_session_ids_or_auth_digest(self) -> None:
        route = self.store.bind(
            control_session_id="control-secret-session",
            runner_id="runner-a",
            workspace_id="ws-a",
            remote_session_id="remote-secret-session",
            authorization_key=self.authorization_key,
        )
        payload = route.public_payload()
        serialized = repr(payload)
        self.assertNotIn("control-secret-session", serialized)
        self.assertNotIn("remote-secret-session", serialized)
        self.assertNotIn(authorization_key_digest(self.authorization_key), serialized)
        self.assertNotIn(self.authorization_key, serialized)


class RunnerMcpSessionHostConcurrencyTests(unittest.TestCase):
    def _digest(self) -> str:
        return authorization_key_digest("oauth:client-a:grant-a:ws-a")

    def test_concurrent_creates_respect_max_sessions_without_overselling(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        factory_calls = 0
        created: list[FakeRuntime] = []

        def factory(_workspace: str, _auth: str) -> FakeRuntime:
            nonlocal factory_calls
            factory_calls += 1
            entered.set()
            release.wait()
            runtime = FakeRuntime(f"remote-{factory_calls}")
            created.append(runtime)
            return runtime

        host = RunnerMcpSessionHost(factory, max_sessions=1)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        results: dict[str, Any] = {}
        errors: list[RemoteMcpRouteError] = []

        def do_create(control_session_id: str) -> None:
            try:
                results[control_session_id] = host.create(
                    control_session_id=control_session_id,
                    workspace_id="ws-a",
                    authorization_digest=digest,
                )
            except RemoteMcpRouteError as exc:
                errors.append(exc)

        first = threading.Thread(target=do_create, args=("control-a",))
        first.start()
        self.assertTrue(entered.wait(3), "first create should reach the factory")
        second = threading.Thread(target=do_create, args=("control-b",))
        second.start()
        second.join(3)
        self.assertFalse(second.is_alive(), "second create should fail fast at capacity")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "RUNNER_SESSION_CAPACITY")
        self.assertEqual(factory_calls, 1)
        release.set()
        first.join(3)
        self.assertFalse(first.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(len(host.inventory("ws-a")), 1)

    def test_same_control_create_single_flights_and_conflicts_on_identity(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        factory_calls = 0

        def factory(_workspace: str, _auth: str) -> FakeRuntime:
            nonlocal factory_calls
            factory_calls += 1
            entered.set()
            release.wait()
            return FakeRuntime(f"remote-{factory_calls}")

        host = RunnerMcpSessionHost(factory, max_sessions=4)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        records: list[RunnerMcpSessionRecord | None] = []

        def do_create() -> None:
            records.append(
                host.create(
                    control_session_id="control-1",
                    workspace_id="ws-a",
                    authorization_digest=digest,
                )
            )

        first = threading.Thread(target=do_create)
        first.start()
        self.assertTrue(entered.wait(3), "first create should reach the factory")
        second = threading.Thread(target=do_create)
        second.start()
        second.join(0.2)
        self.assertTrue(second.is_alive(), "same-identity create should wait for the in-flight create")
        self.assertEqual(factory_calls, 1)

        conflicting: list[RemoteMcpRouteError] = []

        def do_conflict() -> None:
            try:
                host.create(
                    control_session_id="control-1",
                    workspace_id="ws-b",
                    authorization_digest=digest,
                )
            except RemoteMcpRouteError as exc:
                conflicting.append(exc)

        conflict_thread = threading.Thread(target=do_conflict)
        conflict_thread.start()
        conflict_thread.join(0.3)
        self.assertFalse(
            conflict_thread.is_alive(),
            "different identity on the same control id must fail immediately",
        )
        release.set()
        first.join(3)
        second.join(3)
        conflict_thread.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(conflict_thread.is_alive())
        self.assertEqual(len(conflicting), 1)
        self.assertEqual(conflicting[0].code, "RUNNER_ROUTE_CONFLICT")
        self.assertEqual(len(records), 2)
        self.assertIs(records[0], records[1])
        self.assertEqual(factory_calls, 1)

    def test_factory_failure_releases_reservation(self) -> None:
        attempts = 0

        def factory(_workspace: str, _auth: str) -> FakeRuntime:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic factory failure")
            return FakeRuntime("remote-ok")

        host = RunnerMcpSessionHost(factory, max_sessions=1)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        with self.assertRaises(RuntimeError):
            host.create(
                control_session_id="control-a",
                workspace_id="ws-a",
                authorization_digest=digest,
            )
        record = host.create(
            control_session_id="control-b",
            workspace_id="ws-a",
            authorization_digest=digest,
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(record.runtime.http_session_id, "remote-ok")

    def test_duplicate_remote_id_conflict_closes_and_releases_reservation(self) -> None:
        made: list[FakeRuntime] = []

        def factory(_workspace: str, _auth: str) -> FakeRuntime:
            runtime = FakeRuntime("remote-dup" if len(made) < 2 else "remote-ok")
            made.append(runtime)
            return runtime

        host = RunnerMcpSessionHost(factory, max_sessions=2)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        first = host.create(
            control_session_id="control-a",
            workspace_id="ws-a",
            authorization_digest=digest,
        )
        with self.assertRaises(RemoteMcpRouteError) as caught:
            host.create(
                control_session_id="control-b",
                workspace_id="ws-a",
                authorization_digest=digest,
            )
        self.assertEqual(caught.exception.code, "RUNNER_ROUTE_CONFLICT")
        self.assertEqual(made[1].close_count, 1)
        third = host.create(
            control_session_id="control-c",
            workspace_id="ws-a",
            authorization_digest=digest,
        )
        self.assertEqual(third.runtime.http_session_id, "remote-ok")
        self.assertEqual(first.runtime.close_count, 0)

    def test_close_waits_for_active_call_and_never_closes_early(self) -> None:
        call_entered = threading.Event()
        call_release = threading.Event()
        self.addCleanup(call_release.set)
        order: list[str] = []
        runtime = _GatedRuntime(
            "remote-1",
            call_entered=call_entered,
            call_release=call_release,
            order=order,
        )
        host = RunnerMcpSessionHost(lambda _ws, _auth: runtime)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        record = host.create(
            control_session_id="control-1",
            workspace_id="ws-a",
            authorization_digest=digest,
        )

        def do_call() -> None:
            host.call_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=digest,
                method="tools/call",
                params={"name": "read_file"},
            )
            order.append("call-done")

        call_thread = threading.Thread(target=do_call)
        call_thread.start()
        self.assertTrue(call_entered.wait(3), "call should enter the runtime")

        def do_close() -> None:
            host.close_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=digest,
            )
            order.append("close-done")

        close_thread = threading.Thread(target=do_close)
        close_thread.start()
        self.assertEqual(runtime.close_count, 0, "close must not reach Runtime.close while a call is active")
        call_release.set()
        call_thread.join(3)
        close_thread.join(3)
        self.assertFalse(call_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(runtime.close_count, 1)
        self.assertEqual(order, ["call", "call-done", "close", "close-done"])

    def test_call_after_close_marked_is_rejected(self) -> None:
        call_entered = threading.Event()
        call_release = threading.Event()
        self.addCleanup(call_release.set)
        runtime = _GatedRuntime("remote-1", call_entered=call_entered, call_release=call_release)
        host = RunnerMcpSessionHost(lambda _ws, _auth: runtime)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        record = host.create(
            control_session_id="control-1",
            workspace_id="ws-a",
            authorization_digest=digest,
        )

        def do_call() -> None:
            host.call_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=digest,
                method="tools/call",
                params={"name": "read_file"},
            )

        call_thread = threading.Thread(target=do_call)
        call_thread.start()
        self.assertTrue(call_entered.wait(3), "call should enter the runtime")

        def do_close() -> None:
            host.close_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=digest,
            )

        close_thread = threading.Thread(target=do_close)
        close_thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            ref = host._by_remote.get(record.remote_session_id)
            if ref is not None and getattr(ref, "closing", False):
                break
            time.sleep(0.001)
        else:
            self.fail("close did not mark the session as closing")

        with self.assertRaises(RemoteMcpRouteError) as caught:
            host.call_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=digest,
                method="tools/call",
                params={"name": "read_file"},
            )
        self.assertEqual(caught.exception.code, "RUNNER_SESSION_CLOSING")
        call_release.set()
        call_thread.join(3)
        close_thread.join(3)
        self.assertFalse(call_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(runtime.close_count, 1)

    def test_shutdown_waits_for_uninstalled_runtime_close(self) -> None:
        factory_entered = threading.Event()
        factory_release = threading.Event()
        self.addCleanup(factory_release.set)
        order: list[str] = []
        runtime = _GatedRuntime("remote-pending", order=order)
        digest = self._digest()
        create_errors: list[str] = []

        def do_create() -> None:
            try:
                host.create(
                    control_session_id="control-1",
                    workspace_id="ws-a",
                    authorization_digest=digest,
                )
            except RemoteMcpRouteError as exc:
                create_errors.append(exc.code)

        def factory(_workspace: str, _auth: str) -> FakeRuntime:
            factory_entered.set()
            factory_release.wait()
            return runtime

        host = RunnerMcpSessionHost(factory, max_sessions=2)
        self.addCleanup(host.shutdown)
        create_thread = threading.Thread(target=do_create)
        create_thread.start()
        self.assertTrue(factory_entered.wait(3), "create should reach the factory")

        def do_shutdown() -> None:
            host.shutdown()
            order.append("shutdown-done")

        shutdown_thread = threading.Thread(target=do_shutdown)
        shutdown_thread.start()
        factory_release.set()
        create_thread.join(3)
        shutdown_thread.join(3)
        self.assertFalse(create_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(runtime.close_count, 1)
        self.assertEqual(order, ["close", "shutdown-done"])
        self.assertEqual(create_errors, ["RUNNER_SHUTTING_DOWN"])
        self.assertEqual(host.inventory("ws-a"), ())

    def test_shutdown_waits_for_active_call_before_closing(self) -> None:
        call_entered = threading.Event()
        call_release = threading.Event()
        self.addCleanup(call_release.set)
        order: list[str] = []
        runtime = _GatedRuntime(
            "remote-1",
            call_entered=call_entered,
            call_release=call_release,
            order=order,
        )
        host = RunnerMcpSessionHost(lambda _ws, _auth: runtime)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        record = host.create(
            control_session_id="control-1",
            workspace_id="ws-a",
            authorization_digest=digest,
        )

        def do_call() -> None:
            host.call_session(
                control_session_id="control-1",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=digest,
                method="tools/call",
                params={"name": "read_file"},
            )
            order.append("call-done")

        call_thread = threading.Thread(target=do_call)
        call_thread.start()
        self.assertTrue(call_entered.wait(3), "call should enter the runtime")

        def do_shutdown() -> None:
            host.shutdown()
            order.append("shutdown-done")

        shutdown_thread = threading.Thread(target=do_shutdown)
        shutdown_thread.start()
        self.assertEqual(runtime.close_count, 0, "shutdown must not close an active runtime")
        call_release.set()
        call_thread.join(3)
        shutdown_thread.join(3)
        self.assertFalse(call_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(runtime.close_count, 1)
        self.assertEqual(order, ["call", "call-done", "close", "shutdown-done"])

    def test_shutdown_waits_for_detached_route_runtime_close(self) -> None:
        close_entered = threading.Event()
        close_release = threading.Event()
        shutdown_done = threading.Event()
        self.addCleanup(close_release.set)
        runtime = _GatedRuntime(
            "remote-detached",
            close_entered=close_entered,
            close_release=close_release,
        )
        host = RunnerMcpSessionHost(lambda _ws, _auth: runtime)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        record = host.create(
            control_session_id="control-detached",
            workspace_id="ws-a",
            authorization_digest=digest,
        )

        close_thread = threading.Thread(
            target=lambda: host.close_session(
                control_session_id="control-detached",
                remote_session_id=record.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=digest,
            )
        )
        close_thread.start()
        self.assertTrue(close_entered.wait(timeout=3))

        def shutdown() -> None:
            host.shutdown()
            shutdown_done.set()

        shutdown_thread = threading.Thread(target=shutdown)
        shutdown_thread.start()
        self.assertFalse(shutdown_done.wait(timeout=0.1))
        close_release.set()
        close_thread.join(timeout=3)
        shutdown_thread.join(timeout=3)
        self.assertFalse(close_thread.is_alive())
        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(runtime.close_count, 1)

    def test_runtime_io_does_not_hold_global_lock(self) -> None:
        close_entered = threading.Event()
        close_release = threading.Event()
        self.addCleanup(close_release.set)
        runtime_a = _GatedRuntime("remote-a", close_entered=close_entered, close_release=close_release)
        runtime_b = _GatedRuntime("remote-b")
        factory_calls = itertools.count(1)

        def factory(_workspace: str, _auth: str) -> FakeRuntime:
            return runtime_a if next(factory_calls) == 1 else runtime_b

        host = RunnerMcpSessionHost(factory, max_sessions=2)
        self.addCleanup(host.shutdown)
        digest = self._digest()
        record_a = host.create(
            control_session_id="control-a",
            workspace_id="ws-a",
            authorization_digest=digest,
        )
        host.create(
            control_session_id="control-b",
            workspace_id="ws-a",
            authorization_digest=digest,
        )

        def do_close() -> None:
            host.close_session(
                control_session_id="control-a",
                remote_session_id=record_a.remote_session_id,
                workspace_id="ws-a",
                authorization_digest=digest,
            )

        close_thread = threading.Thread(target=do_close)
        close_thread.start()
        self.assertTrue(close_entered.wait(3), "Runtime.close should be entered")

        inventory: list[Any] = []

        def do_inventory() -> None:
            inventory.append(host.inventory("ws-a"))

        inventory_thread = threading.Thread(target=do_inventory)
        inventory_thread.start()
        inventory_thread.join(3)
        self.assertFalse(inventory_thread.is_alive(), "inventory must not block on Runtime.close")
        self.assertEqual(len(inventory[0]), 1)

        result = host.call_session(
            control_session_id="control-b",
            remote_session_id=runtime_b.http_session_id,
            workspace_id="ws-a",
            authorization_digest=digest,
            method="tools/call",
            params={"name": "read_file"},
        )
        self.assertEqual(result["content"][0]["text"], "read_file")
        close_release.set()
        close_thread.join(3)
        self.assertFalse(close_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
