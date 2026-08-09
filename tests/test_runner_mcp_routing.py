from __future__ import annotations

import itertools
import unittest
from typing import Any
from unittest.mock import patch

from coding_tools_mcp import upstream as upstream_module
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


if __name__ == "__main__":
    unittest.main()
