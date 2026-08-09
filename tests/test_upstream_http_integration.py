from __future__ import annotations

import http.server
import json
import socket
import threading
import unittest
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from coding_tools_mcp.upstream import HttpUpstreamClient, UpstreamError, UpstreamServerConfig
from coding_tools_mcp.upstream_resilience import (
    BackoffPolicy,
    UpstreamClientState,
    UpstreamResilienceCoordinator,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class _Action:
    status: int = 200
    kind: str = "response"


class _ServerState:
    def __init__(self) -> None:
        self.actions: dict[str, list[_Action]] = defaultdict(list)
        self.posts: list[dict[str, Any]] = []
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

    def record_post(self, method: str, headers: dict[str, str]) -> None:
        with self.lock:
            self.posts.append({"method": method, "headers": headers})

    def next_session_id(self) -> str:
        with self.lock:
            self._session_count += 1
            return f"loopback-session-{self._session_count}"


class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            method = str(payload.get("method", ""))
            headers = {key.lower(): value for key, value in self.headers.items()}
            self.state.record_post(method, headers)
            action = self.state.take_action(method)
            if action.kind == "timeout":
                self.state.timeout_entered.set()
                self.state.timeout_release.wait(timeout=2)
                return
            if action.kind == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return

            body = b""
            response_headers: dict[str, str] = {}
            if method == "notifications/initialized":
                status = 202
            else:
                status = action.status
                response = {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"content": [], "structuredContent": {"ok": True}},
                }
                body = json.dumps(response).encode("utf-8")
                response_headers["Content-Type"] = "application/json"
                if method == "initialize" and status == 200:
                    response_headers["Mcp-Session-Id"] = self.state.next_session_id()
            self.send_response(status)
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # A timeout test intentionally closes the client socket while the
            # handler is waiting to be released.
            return
        except BaseException as exc:  # noqa: BLE001 - surface in the test thread
            self.state.errors.append(exc)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            headers = {key.lower(): value for key, value in self.headers.items()}
            with self.state.lock:
                self.state.deletes.append(headers)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except BaseException as exc:  # noqa: BLE001 - surface in the test thread
            self.state.errors.append(exc)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _LoopbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.state = _ServerState()
        super().__init__(("127.0.0.1", 0), _LoopbackHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/mcp"

    def close(self) -> None:
        self.state.timeout_release.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


def _coordinator(clock: FakeClock) -> UpstreamResilienceCoordinator:
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
    server: _LoopbackServer,
    clock: FakeClock,
    *,
    timeout_ms: int = 500,
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


class UpstreamHttpIntegrationTests(unittest.TestCase):
    def test_http_502_json_body_preserves_status_clears_session_and_does_not_replay(self) -> None:
        server = _LoopbackServer()
        clock = FakeClock()
        client = _client(server, clock)
        try:
            client.initialize()
            server.state.enqueue("tools/call", _Action(status=502))
            with self.assertRaises(UpstreamError) as caught:
                client.call_tool_raw("mutating", {"side_effect": True})
            self.assertEqual(caught.exception.details.get("status"), 502)
            self.assertEqual(client.transport_state, UpstreamClientState.BACKING_OFF)
            self.assertIsNone(client.session_id)
            self.assertEqual(
                sum(item["method"] == "tools/call" for item in server.state.posts),
                1,
            )
        finally:
            client.close()
            server.close()
        self.assertEqual(server.state.errors, [])

    def test_reinitialize_after_5xx_does_not_send_old_session_header(self) -> None:
        server = _LoopbackServer()
        clock = FakeClock()
        client = _client(server, clock)
        try:
            client.initialize()
            server.state.enqueue("tools/call", _Action(status=502))
            with self.assertRaises(UpstreamError):
                client.call_tool_raw("mutating", {})
            clock.advance(1.1)
            client.call_tool_raw("read", {})
            initialize_posts = [item for item in server.state.posts if item["method"] == "initialize"]
            self.assertEqual(len(initialize_posts), 2)
            self.assertNotIn("mcp-session-id", initialize_posts[1]["headers"])
        finally:
            client.close()
            server.close()
        self.assertEqual(server.state.errors, [])

    def test_timeout_and_disconnect_clear_session_before_backoff(self) -> None:
        for kind in ("timeout", "disconnect"):
            with self.subTest(kind=kind):
                server = _LoopbackServer()
                clock = FakeClock()
                client = _client(server, clock, timeout_ms=100)
                try:
                    client.initialize()
                    server.state.enqueue("tools/call", _Action(kind=kind))
                    with self.assertRaises(UpstreamError):
                        client.call_tool_raw("read", {})
                    self.assertIsNone(client.session_id)
                    self.assertEqual(client.transport_state, UpstreamClientState.BACKING_OFF)
                    if kind == "timeout":
                        self.assertTrue(server.state.timeout_entered.is_set())
                finally:
                    server.state.timeout_release.set()
                    client.close()
                    server.close()
                self.assertEqual(server.state.errors, [])

    def test_404_and_410_json_bodies_remain_stale_session_errors(self) -> None:
        for status in (404, 410):
            with self.subTest(status=status):
                server = _LoopbackServer()
                clock = FakeClock()
                client = _client(server, clock)
                try:
                    client.initialize()
                    server.state.enqueue("tools/call", _Action(status=status))
                    with self.assertRaises(UpstreamError) as caught:
                        client.call_tool_raw("read", {})
                    self.assertEqual(caught.exception.details.get("status"), status)
                    self.assertIsNone(client.session_id)
                    self.assertEqual(client.transport_state, UpstreamClientState.NEW)
                finally:
                    client.close()
                    server.close()
                self.assertEqual(server.state.errors, [])

    def test_delete_authoritative_session_header_cannot_be_overridden_by_config(self) -> None:
        for configured_name in ("Mcp-Session-Id", "mcp-session-id", "MCP-SESSION-ID"):
            with self.subTest(configured_name=configured_name):
                server = _LoopbackServer()
                clock = FakeClock()
                client = _client(server, clock, headers={configured_name: "attacker"})
                client.session_id = "real-session"
                try:
                    client.close()
                finally:
                    server.close()
                self.assertEqual(len(server.state.deletes), 1)
                self.assertEqual(server.state.deletes[0].get("mcp-session-id"), "real-session")
                self.assertEqual(server.state.errors, [])


if __name__ == "__main__":
    unittest.main()
