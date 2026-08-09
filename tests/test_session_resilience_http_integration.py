from __future__ import annotations

import http.client
import http.server
import json
import socket
import tempfile
import threading
import unittest
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_tools_mcp.server import (
    AuthorizationContext,
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    WorkspaceBinding,
)
from coding_tools_mcp.transport_http import HTTPSessionLimits, HTTPSessionManager
from coding_tools_mcp.upstream import HttpUpstreamClient, UpstreamError, UpstreamServerConfig
from coding_tools_mcp.upstream_resilience import BackoffPolicy, UpstreamClientState, UpstreamResilienceCoordinator


class _CloseSpyRuntime:
    def __init__(self, session_id: str, entered: threading.Event | None = None, release: threading.Event | None = None) -> None:
        self.http_session_id = session_id
        self.close_count = 0
        self._entered = entered
        self._release = release

    def close(self) -> None:
        self.close_count += 1
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            self._release.wait(timeout=2)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


@dataclass(frozen=True)
class _Action:
    status: int = 200
    kind: str = "response"


class _FakeUpstreamState:
    def __init__(self) -> None:
        self.actions: dict[str, list[_Action]] = defaultdict(list)
        self.posts: list[dict[str, str]] = []
        self.deletes: list[dict[str, str]] = []
        self.errors: list[BaseException] = []
        self.lock = threading.Lock()
        self.timeout_entered = threading.Event()
        self.timeout_release = threading.Event()
        self._session_count = 0

    def enqueue(self, method: str, action: _Action) -> None:
        with self.lock:
            self.actions[method].append(action)

    def take_action(self, method: str) -> _Action:
        with self.lock:
            actions = self.actions[method]
            return actions.pop(0) if actions else _Action()

    def record_post(self, headers: dict[str, str]) -> None:
        with self.lock:
            self.posts.append(headers)

    def record_delete(self, headers: dict[str, str]) -> None:
        with self.lock:
            self.deletes.append(headers)

    def next_session_id(self) -> str:
        with self.lock:
            self._session_count += 1
            return f"fake-session-{self._session_count}"


class _FakeUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _FakeUpstreamState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            method = str(payload.get("method", ""))
            headers = {key.lower(): value for key, value in self.headers.items()}
            self.state.record_post(headers)
            action = self.state.take_action(method)
            if action.kind == "timeout":
                self.state.timeout_entered.set()
                self.state.timeout_release.wait(timeout=2)
                return
            if action.kind == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if method == "notifications/initialized":
                self._send(202)
                return
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"content": [], "structuredContent": {"ok": True}},
                }
            ).encode("utf-8")
            response_headers = {"Content-Type": "application/json"}
            if method == "initialize" and action.status == 200:
                response_headers["Mcp-Session-Id"] = self.state.next_session_id()
            self._send(action.status, body, response_headers)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except BaseException as exc:  # noqa: BLE001 - collected and asserted by the test
            with self.state.lock:
                self.state.errors.append(exc)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            headers = {key.lower(): value for key, value in self.headers.items()}
            self.state.record_delete(headers)
            action = self.state.take_action("DELETE")
            if action.kind == "timeout":
                self.state.timeout_entered.set()
                self.state.timeout_release.wait(timeout=2)
                return
            if action.kind == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self._send(action.status)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except BaseException as exc:  # noqa: BLE001 - collected and asserted by the test
            with self.state.lock:
                self.state.errors.append(exc)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _FakeUpstreamServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.state = _FakeUpstreamState()
        self.serve_errors: list[BaseException] = []
        self._closed = False
        super().__init__(("127.0.0.1", 0), _FakeUpstreamHandler)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            self.serve_forever()
        except BaseException as exc:  # noqa: BLE001 - collected and asserted by the test
            self.serve_errors.append(exc)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/mcp"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.state.timeout_release.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class _SpyRuntime(Runtime):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class _CollectingMCPHandler(MCPHandler):
    def handle(self) -> None:
        try:
            super().handle()
        except BaseException as exc:  # noqa: BLE001 - collected and asserted by the test
            self.server.handler_errors.append(exc)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def _coordinator(clock: _FakeClock) -> UpstreamResilienceCoordinator:
    return UpstreamResilienceCoordinator(
        policy=BackoffPolicy(
            initial_seconds=1.0,
            maximum_seconds=8.0,
            multiplier=2.0,
            jitter_ratio=0.0,
        ),
        clock=clock,
        random_value=lambda: 0.5,
        sleeper=lambda _seconds: None,
    )


def _client(
    server: _FakeUpstreamServer,
    clock: _FakeClock,
    *,
    timeout_ms: int = 250,
    headers: dict[str, str] | None = None,
) -> HttpUpstreamClient:
    config = UpstreamServerConfig(
        alias="loopback",
        transport="streamable_http",
        url=server.url,
        expose_mode="broker",
        timeout_ms=timeout_ms,
        headers=headers or {},
    )
    return HttpUpstreamClient(config, "2025-11-25", resilience_coordinator=_coordinator(clock))


def _rpc(
    port: int,
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
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("POST", "/mcp", body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, response_headers, json.loads(raw) if raw else {}
    finally:
        connection.close()


def _delete(port: int, token: str, session_id: str) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            "DELETE",
            "/mcp",
            headers={"Authorization": f"Bearer {token}", "Mcp-Session-Id": session_id},
        )
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}
    finally:
        connection.close()


class SessionResilienceHttpIntegrationTests(unittest.TestCase):
    def _assert_fake_server_clean(self, server: _FakeUpstreamServer) -> None:
        self.assertEqual(server.state.errors, [])
        self.assertEqual(server.serve_errors, [])
        self.assertFalse(server.thread.is_alive())

    def test_session_manager_shutdown_waits_for_uninstalled_runtime(self) -> None:
        factory_entered = threading.Event()
        factory_release = threading.Event()
        close_entered = threading.Event()
        close_release = threading.Event()
        runtime = _CloseSpyRuntime("unit-session", close_entered, close_release)

        def factory(_context: object) -> _CloseSpyRuntime:
            factory_entered.set()
            factory_release.wait(timeout=2)
            return runtime

        manager = HTTPSessionManager(
            factory,
            limits=HTTPSessionLimits(max_total=1, max_per_identity=1, max_initializations=1),
        )
        create_errors: list[BaseException] = []

        def create() -> None:
            try:
                manager.create(object())
            except BaseException as exc:  # pragma: no cover - asserted below
                create_errors.append(exc)

        creator = threading.Thread(target=create)
        creator.start()
        self.assertTrue(factory_entered.wait(timeout=2))
        closer = threading.Thread(target=manager.close)
        closer.start()
        factory_release.set()
        self.assertTrue(close_entered.wait(timeout=2))
        self.assertTrue(closer.is_alive())
        close_release.set()
        creator.join(timeout=2)
        closer.join(timeout=2)
        self.assertFalse(creator.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(create_errors), 1)
        self.assertEqual(getattr(create_errors[0], "code", None), "http_session_server_closing")
        self.assertEqual(runtime.close_count, 1)

    def test_runtime_http_server_initialize_delete_and_duplicate_delete(self) -> None:
        token = "synthetic-http-token"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = _SpyRuntime(
                root,
                auth_token=token,
                workspace_binding=WorkspaceBinding("control", root, "bearer"),
                authorization_context=AuthorizationContext("bearer"),
                transport="http",
            )
            created: list[_SpyRuntime] = []

            def factory(context: AuthorizationContext) -> _SpyRuntime:
                runtime = _SpyRuntime(
                    root,
                    auth_token=token,
                    workspace_binding=WorkspaceBinding("session", root, context.method),
                    authorization_context=context,
                    transport="http",
                )
                created.append(runtime)
                return runtime

            server = RuntimeHTTPServer(("127.0.0.1", 0), _CollectingMCPHandler, control, factory)
            server.handler_errors = []
            thread_errors: list[BaseException] = []

            def serve() -> None:
                try:
                    server.serve_forever()
                except BaseException as exc:  # pragma: no cover - asserted below
                    thread_errors.append(exc)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            port = int(server.server_address[1])
            try:
                status, headers, payload = _rpc(
                    port,
                    token,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "rm07", "version": "1"},
                        },
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["result"]["protocolVersion"], "2025-06-18")
                session_id = headers.get("mcp-session-id")
                self.assertTrue(session_id)
                first_delete, _ = _delete(port, token, session_id)
                second_delete, _ = _delete(port, token, session_id)
                self.assertEqual(first_delete, 200)
                self.assertEqual(second_delete, 404)
                self.assertEqual(len(created), 1)
                self.assertEqual(created[0].close_count, 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual(thread_errors, [])
            self.assertEqual(server.handler_errors, [])
            self.assertFalse(thread.is_alive())
            self.assertEqual(control.close_count, 1)

    def test_http_upstream_post_matrix_preserves_status_and_does_not_replay(self) -> None:
        cases = (
            (404, "response"),
            (410, "response"),
            (502, "response"),
            (503, "response"),
            (504, "response"),
            (None, "timeout"),
            (None, "disconnect"),
        )
        for status, kind in cases:
            with self.subTest(status=status, kind=kind):
                server = _FakeUpstreamServer()
                clock = _FakeClock()
                client = _client(server, clock, timeout_ms=80)
                try:
                    client.initialize()
                    server.state.enqueue("tools/call", _Action(status=status or 200, kind=kind))
                    with self.assertRaises(UpstreamError) as caught:
                        client.call_tool_raw("mutating", {"side_effect": True})
                    if status is not None:
                        self.assertEqual(caught.exception.details.get("status"), status)
                    self.assertEqual(
                        sum(1 for headers in server.state.posts if headers.get("content-type") == "application/json"),
                        3,
                    )
                    self.assertEqual(
                        len([headers for headers in server.state.posts if headers.get("mcp-session-id")]),
                        2,
                    )
                    self.assertEqual(client.transport_state, UpstreamClientState.NEW if status in {404, 410} else UpstreamClientState.BACKING_OFF)
                    self.assertIsNone(client.session_id)
                finally:
                    server.state.timeout_release.set()
                    client.close()
                    server.close()
                self._assert_fake_server_clean(server)

    def test_never_initialized_upstream_close_does_not_send_delete(self) -> None:
        server = _FakeUpstreamServer()
        client = _client(server, _FakeClock())
        try:
            client.close()
            client.close()
            self.assertEqual(server.state.deletes, [])
        finally:
            client.close()
            server.close()
        self._assert_fake_server_clean(server)

    def test_http_upstream_delete_matrix_is_idempotent_and_authoritative(self) -> None:
        cases = (
            (200, "response", 1, 0),
            (204, "response", 1, 0),
            (404, "response", 1, 0),
            (410, "response", 1, 0),
            (403, "response", 0, 1),
            (502, "response", 0, 1),
            (None, "timeout", 0, 1),
        )
        for status, kind, expected_success, expected_failure in cases:
            with self.subTest(status=status, kind=kind):
                server = _FakeUpstreamServer()
                clock = _FakeClock()
                client = _client(
                    server,
                    clock,
                    timeout_ms=80,
                    headers={
                        "Mcp-Session-Id": "attacker-session",
                        "mcp-protocol-version": "attacker-version",
                        "Authorization": "Bearer attacker-token",
                    },
                )
                try:
                    client.initialize()
                    real_session_id = client.session_id
                    self.assertTrue(real_session_id)
                    server.state.enqueue("DELETE", _Action(status=status or 204, kind=kind))
                    client.close()
                    client.close()
                    self.assertEqual(len(server.state.deletes), 1)
                    delete_headers = server.state.deletes[0]
                    self.assertEqual(delete_headers.get("mcp-session-id"), real_session_id)
                    self.assertEqual(delete_headers.get("mcp-protocol-version"), "2025-11-25")
                    self.assertNotEqual(delete_headers.get("authorization"), "Bearer attacker-token")
                    self.assertEqual(client.remote_delete_success_total, expected_success)
                    self.assertEqual(client.remote_delete_failure_total, expected_failure)
                finally:
                    server.state.timeout_release.set()
                    client.close()
                    server.close()
                self._assert_fake_server_clean(server)


if __name__ == "__main__":
    unittest.main()
