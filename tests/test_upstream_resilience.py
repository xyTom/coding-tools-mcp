from __future__ import annotations

import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

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


class ScriptedHttpClient(HttpUpstreamClient):
    def __init__(
        self,
        config: UpstreamServerConfig,
        coordinator: UpstreamResilienceCoordinator,
        *,
        initialize_hook: callable | None = None,
    ) -> None:
        super().__init__(config, "2025-11-25", resilience_coordinator=coordinator)
        self.initialize_hook = initialize_hook
        self.initialize_attempts = 0
        self.tool_call_attempts = 0
        self.initialize_error: UpstreamError | None = None
        self.call_errors: list[UpstreamError] = []

    def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        del params
        if method == "initialize":
            self.initialize_attempts += 1
            if self.initialize_hook is not None:
                self.initialize_hook()
            if self.initialize_error is not None:
                raise self.initialize_error
            return {}
        if method == "tools/call":
            self.tool_call_attempts += 1
            if self.call_errors:
                raise self.call_errors.pop(0)
            return {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"ok": True},
                "isError": False,
            }
        if method == "tools/list":
            return {"tools": []}
        raise AssertionError(method)

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        del params
        if method != "notifications/initialized":
            raise AssertionError(method)


def config() -> UpstreamServerConfig:
    return UpstreamServerConfig(
        alias="remote",
        transport="streamable_http",
        url="http://127.0.0.1/mcp",
        expose_mode="broker",
    )


def coordinator(
    clock: FakeClock,
    *,
    max_initializations: int = 4,
    sleeper: callable = time.sleep,
) -> UpstreamResilienceCoordinator:
    return UpstreamResilienceCoordinator(
        max_initializations=max_initializations,
        policy=BackoffPolicy(
            initial_seconds=1.0,
            maximum_seconds=8.0,
            multiplier=2.0,
            jitter_ratio=0.0,
        ),
        clock=clock,
        random_value=lambda: 0.5,
        sleeper=sleeper,
    )


class UpstreamResilienceTests(unittest.TestCase):
    def test_ambiguous_fault_matrix_never_replays_current_tool_call(self) -> None:
        faults = (
            UpstreamError(
                "UPSTREAM_HTTP_ERROR",
                "synthetic 502",
                category="upstream",
                retryable=True,
                details={"status": 502},
            ),
            UpstreamError(
                "UPSTREAM_HTTP_ERROR",
                "synthetic 503",
                category="upstream",
                retryable=True,
                details={"status": 503},
            ),
            UpstreamError(
                "UPSTREAM_HTTP_ERROR",
                "synthetic 504",
                category="upstream",
                retryable=True,
                details={"status": 504},
            ),
            UpstreamError(
                "UPSTREAM_TIMEOUT",
                "synthetic timeout",
                category="upstream",
                retryable=True,
            ),
            UpstreamError(
                "UPSTREAM_DISCONNECTED",
                "synthetic disconnect",
                category="upstream",
                retryable=True,
            ),
            UpstreamError(
                "UPSTREAM_CONNECTION_FAILED",
                "synthetic reset",
                category="upstream",
                retryable=True,
            ),
        )
        for fault in faults:
            with self.subTest(code=fault.code, status=fault.details.get("status")):
                clock = FakeClock()
                client = ScriptedHttpClient(config(), coordinator(clock))
                client.initialize()
                client.call_errors.append(fault)
                with self.assertRaises(UpstreamError):
                    client.call_tool_raw("mutating", {"side_effect": True})
                self.assertEqual(client.tool_call_attempts, 1)
                self.assertEqual(client.transport_state, UpstreamClientState.BACKING_OFF)
                with self.assertRaises(UpstreamError) as retry:
                    client.call_tool_raw("mutating", {"side_effect": True})
                self.assertEqual(retry.exception.code, "UPSTREAM_BACKING_OFF")
                self.assertEqual(client.tool_call_attempts, 1)
                client.close()

    def test_ambiguous_tool_call_is_never_replayed_and_next_call_respects_backoff(self) -> None:
        clock = FakeClock()
        shared = coordinator(clock)
        client = ScriptedHttpClient(config(), shared)
        client.initialize()
        client.call_errors.append(
            UpstreamError(
                "UPSTREAM_HTTP_ERROR",
                "synthetic 502",
                category="upstream",
                retryable=True,
                details={"status": 502},
            )
        )

        with self.assertRaises(UpstreamError) as failed_call:
            client.call_tool_raw("mutating", {"value": 1})
        self.assertEqual(failed_call.exception.code, "UPSTREAM_HTTP_ERROR")
        self.assertEqual(client.tool_call_attempts, 1)
        self.assertEqual(client.transport_state, UpstreamClientState.BACKING_OFF)

        with self.assertRaises(UpstreamError) as backing_off:
            client.call_tool_raw("mutating", {"value": 1})
        self.assertEqual(backing_off.exception.code, "UPSTREAM_BACKING_OFF")
        self.assertEqual(client.tool_call_attempts, 1)
        self.assertEqual(client.initialize_attempts, 1)

        clock.advance(1.1)
        result = client.call_tool_raw("mutating", {"value": 2})
        self.assertEqual(result["structuredContent"], {"ok": True})
        self.assertEqual(client.initialize_attempts, 2)
        self.assertEqual(client.tool_call_attempts, 2)
        self.assertEqual(client.transport_state, UpstreamClientState.READY)
        client.close()

    def test_unknown_session_404_and_410_clear_id_and_only_next_call_reinitializes(self) -> None:
        for status in (404, 410):
            with self.subTest(status=status):
                clock = FakeClock()
                client = ScriptedHttpClient(config(), coordinator(clock))
                client.initialize()
                client.session_id = "stale-session"
                client.call_errors.append(
                    UpstreamError(
                        "UPSTREAM_HTTP_ERROR",
                        "synthetic unknown session",
                        category="upstream",
                        retryable=False,
                        details={"status": status},
                    )
                )
                with self.assertRaises(UpstreamError):
                    client.call_tool_raw("read", {})
                self.assertIsNone(client.session_id)
                self.assertEqual(client.transport_state, UpstreamClientState.NEW)
                self.assertEqual(client.initialize_attempts, 1)
                client.call_tool_raw("read", {})
                self.assertEqual(client.initialize_attempts, 2)
                self.assertEqual(client.tool_call_attempts, 2)
                client.close()

    def test_remote_delete_fault_matrix_is_bounded_idempotent_and_releases_local_session(self) -> None:
        faults = (
            (urllib.error.HTTPError("http://127.0.0.1/mcp", 404, "gone", {}, None), 1, 0),
            (urllib.error.HTTPError("http://127.0.0.1/mcp", 410, "gone", {}, None), 1, 0),
            (urllib.error.HTTPError("http://127.0.0.1/mcp", 403, "forbidden", {}, None), 0, 1),
            (urllib.error.HTTPError("http://127.0.0.1/mcp", 502, "bad gateway", {}, None), 0, 1),
            (TimeoutError("synthetic close timeout"), 0, 1),
        )
        for fault, success_count, failure_count in faults:
            with self.subTest(fault=type(fault).__name__, status=getattr(fault, "code", None)):
                clock = FakeClock()
                client = ScriptedHttpClient(config(), coordinator(clock))
                client.session_id = "session-to-close"
                with patch("coding_tools_mcp.upstream.urllib.request.urlopen", side_effect=fault) as request:
                    client.close()
                    client.close()
                self.assertEqual(request.call_count, 1)
                self.assertIsNone(client.session_id)
                self.assertEqual(client.remote_delete_success_total, success_count)
                self.assertEqual(client.remote_delete_failure_total, failure_count)
                self.assertEqual(client.transport_state, UpstreamClientState.CLOSED)

    def test_auth_failure_does_not_enter_automatic_reconnect_loop(self) -> None:
        clock = FakeClock()
        client = ScriptedHttpClient(config(), coordinator(clock))
        client.initialize_error = UpstreamError(
            "UPSTREAM_HTTP_ERROR",
            "synthetic forbidden",
            category="upstream",
            retryable=False,
            details={"status": 403},
        )
        with self.assertRaises(UpstreamError):
            client.initialize()
        self.assertEqual(client.transport_state, UpstreamClientState.SUSPECT)
        with self.assertRaises(UpstreamError):
            client.initialize()
        self.assertEqual(client.initialize_attempts, 1)
        client.close()

    def test_shared_initialization_gate_bounds_one_hundred_runtime_clients(self) -> None:
        clock = FakeClock()
        shared = coordinator(clock, max_initializations=3)
        start = threading.Barrier(101)
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        max_active = 0

        def initialize_hook() -> None:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            release.wait(timeout=3)
            with lock:
                active -= 1

        clients = [ScriptedHttpClient(config(), shared, initialize_hook=initialize_hook) for _ in range(100)]
        errors: list[UpstreamError] = []

        def run(client: ScriptedHttpClient) -> None:
            start.wait(timeout=3)
            try:
                client.initialize()
            except UpstreamError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        start.wait(timeout=3)
        deadline = time.monotonic() + 3
        while max_active < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
        release.set()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(max_active, 3)
        self.assertLessEqual(max_active, shared.max_initializations)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(all(exc.code == "UPSTREAM_INITIALIZATION_LIMIT" for exc in errors))
        for client in clients:
            client.close()

    def test_half_open_circuit_allows_only_one_probe(self) -> None:
        clock = FakeClock()
        shared = coordinator(clock, max_initializations=4)
        key = "remote|http://127.0.0.1/mcp"
        shared.record_failure(key, "UPSTREAM_HTTP_ERROR")
        clock.advance(1.1)
        entered = threading.Event()
        release = threading.Event()

        def probe_hook() -> None:
            entered.set()
            release.wait(timeout=2)

        first = ScriptedHttpClient(config(), shared, initialize_hook=probe_hook)
        second = ScriptedHttpClient(config(), shared)
        first_error: list[BaseException] = []

        def first_probe() -> None:
            try:
                first.initialize()
            except BaseException as exc:  # pragma: no cover - asserted below
                first_error.append(exc)

        thread = threading.Thread(target=first_probe)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        with self.assertRaises(UpstreamError) as blocked:
            second.initialize()
        self.assertEqual(blocked.exception.code, "UPSTREAM_CIRCUIT_PROBE_IN_PROGRESS")
        release.set()
        thread.join(timeout=2)
        self.assertFalse(first_error)
        self.assertEqual(first.transport_state, UpstreamClientState.READY)
        first.close()
        second.close()

    def test_backoff_clock_random_and_sleeper_are_deterministic_and_injectable(self) -> None:
        clock = FakeClock()
        sleeps: list[float] = []
        shared = coordinator(clock, sleeper=sleeps.append)
        key = "remote|http://127.0.0.1/mcp"
        self.assertEqual(shared.record_failure(key, "first"), 1000)
        self.assertEqual(shared.snapshot(key).next_retry_in_ms, 1000)
        shared.sleep_until_retry(key)
        self.assertEqual(sleeps, [1.0])
        clock.advance(1.0)
        self.assertEqual(shared.record_failure(key, "second"), 2000)
        self.assertEqual(shared.snapshot(key).next_retry_in_ms, 2000)

    def test_close_waits_for_reinitialize_and_client_cannot_resurrect(self) -> None:
        clock = FakeClock()
        shared = coordinator(clock)
        entered = threading.Event()
        release = threading.Event()

        def hook() -> None:
            entered.set()
            release.wait(timeout=2)

        client = ScriptedHttpClient(config(), shared, initialize_hook=hook)
        init_thread = threading.Thread(target=client.initialize)
        init_thread.start()
        self.assertTrue(entered.wait(timeout=2))
        close_thread = threading.Thread(target=client.close)
        close_thread.start()
        time.sleep(0.05)
        self.assertTrue(close_thread.is_alive())
        release.set()
        init_thread.join(timeout=2)
        close_thread.join(timeout=2)
        self.assertFalse(init_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(client.transport_state, UpstreamClientState.CLOSED)


if __name__ == "__main__":
    unittest.main()
