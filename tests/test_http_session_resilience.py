from __future__ import annotations

import itertools
import threading
import time
import unittest
from dataclasses import dataclass

from coding_tools_mcp.transport_http import (
    HTTPSessionAdmissionError,
    HTTPSessionLimits,
    HTTPSessionManager,
    MAX_HTTP_SESSIONS,
)


class _Runtime:
    def __init__(self, session_id: str) -> None:
        self.http_session_id = session_id
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _BlockingCloseRuntime(_Runtime):
    def __init__(self, session_id: str, entered: threading.Event, release: threading.Event) -> None:
        super().__init__(session_id)
        self._entered = entered
        self._release = release

    def close(self) -> None:
        self._entered.set()
        self._release.wait(timeout=2)
        super().close()


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(frozen=True)
class _Identity:
    client_id: str
    grant_id: str


@dataclass(frozen=True)
class _Context:
    method: str = "oauth"
    oauth_identity: _Identity | None = None


def _limits(
    *,
    total: int = 8,
    per_identity: int = 8,
    ttl: int = 60,
    initializations: int = 8,
) -> HTTPSessionLimits:
    return HTTPSessionLimits(
        max_total=total,
        max_per_identity=per_identity,
        idle_ttl_seconds=ttl,
        max_initializations=initializations,
    )


class HTTPSessionCapacityBaselineTests(unittest.TestCase):
    def test_baseline_capacity_rejects_the_first_session_over_the_limit(self) -> None:
        sequence = itertools.count(1)
        runtimes: list[_Runtime] = []

        def factory(_context: object) -> _Runtime:
            runtime = _Runtime(f"session-{next(sequence)}")
            runtimes.append(runtime)
            return runtime

        manager = HTTPSessionManager(
            factory,
            limits=_limits(
                total=MAX_HTTP_SESSIONS,
                per_identity=MAX_HTTP_SESSIONS,
                initializations=MAX_HTTP_SESSIONS,
            ),
        )
        self.addCleanup(manager.close)

        for _ in range(MAX_HTTP_SESSIONS):
            manager.create(object())

        with self.assertRaisesRegex(RuntimeError, "maximum HTTP session count reached"):
            manager.create(object())

        self.assertEqual(len(runtimes), MAX_HTTP_SESSIONS)

    def test_create_delete_control_group_does_not_accumulate_sessions(self) -> None:
        sequence = itertools.count(1)
        runtimes: list[_Runtime] = []

        def factory(_context: object) -> _Runtime:
            runtime = _Runtime(f"session-{next(sequence)}")
            runtimes.append(runtime)
            return runtime

        manager = HTTPSessionManager(factory)
        self.addCleanup(manager.close)

        for _ in range(MAX_HTTP_SESSIONS * 2):
            runtime = manager.create(object())
            self.assertTrue(manager.delete(runtime.http_session_id))
            self.assertEqual(runtime.closed, 1)

        self.assertEqual(len(runtimes), MAX_HTTP_SESSIONS * 2)

    def test_ten_times_capacity_create_delete_soak_has_no_live_growth(self) -> None:
        sequence = itertools.count(1)
        manager = HTTPSessionManager(
            lambda _context: _Runtime(f"soak-{next(sequence)}"),
            limits=_limits(
                total=MAX_HTTP_SESSIONS,
                per_identity=MAX_HTTP_SESSIONS,
                initializations=MAX_HTTP_SESSIONS,
            ),
        )
        self.addCleanup(manager.close)
        for index in range(MAX_HTTP_SESSIONS * 10):
            runtime = manager.create(object())
            self.assertTrue(manager.delete(runtime.http_session_id))
            if index % MAX_HTTP_SESSIONS == 0:
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["active_sessions"], 0)
                self.assertEqual(snapshot["idle_sessions"], 0)
                self.assertEqual(snapshot["creating_sessions"], 0)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["created_total"], MAX_HTTP_SESSIONS * 10)
        self.assertEqual(snapshot["deleted_total"], MAX_HTTP_SESSIONS * 10)
        self.assertEqual(snapshot["expired_total"], 0)
        self.assertEqual(snapshot["idle_sessions"], 0)


class HTTPSessionLimitTests(unittest.TestCase):
    def test_invalid_limit_cross_constraints_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            HTTPSessionLimits(max_total=2, max_per_identity=3, max_initializations=1)
        with self.assertRaisesRegex(ValueError, "initializations cannot exceed"):
            HTTPSessionLimits(max_total=2, max_per_identity=2, max_initializations=3)
        with self.assertRaisesRegex(ValueError, "idle_ttl"):
            HTTPSessionLimits(
                max_total=2,
                max_per_identity=2,
                max_initializations=1,
                idle_ttl_seconds=0,
            )

    def test_identity_quota_does_not_consume_other_identity_capacity(self) -> None:
        sequence = itertools.count(1)
        manager = HTTPSessionManager(
            lambda _context: _Runtime(f"s-{next(sequence)}"),
            limits=_limits(total=4, per_identity=2, initializations=4),
        )
        self.addCleanup(manager.close)
        first = _Context(oauth_identity=_Identity("a", "grant-a"))
        second = _Context(oauth_identity=_Identity("b", "grant-b"))
        manager.create(first)
        manager.create(first)
        with self.assertRaises(HTTPSessionAdmissionError) as caught:
            manager.create(first)
        self.assertEqual(caught.exception.code, "http_session_identity_quota")
        manager.create(second)
        self.assertEqual(manager.snapshot()["idle_sessions"], 3)

    def test_concurrent_create_reservations_prevent_oversell(self) -> None:
        entered = threading.Barrier(3)
        release = threading.Event()
        sequence = itertools.count(1)

        def factory(_context: object) -> _Runtime:
            entered.wait(timeout=2)
            release.wait(timeout=2)
            return _Runtime(f"s-{next(sequence)}")

        manager = HTTPSessionManager(
            factory,
            limits=_limits(total=2, per_identity=2, initializations=2),
        )
        self.addCleanup(manager.close)
        context = _Context(oauth_identity=_Identity("a", "grant-a"))
        errors: list[BaseException] = []

        def create() -> None:
            try:
                manager.create(context)
            except BaseException as exc:  # pragma: no cover - captured below
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        entered.wait(timeout=2)
        with self.assertRaises(HTTPSessionAdmissionError) as caught:
            manager.create(context)
        self.assertEqual(caught.exception.code, "http_session_initialization_limit")
        release.set()
        for thread in threads:
            thread.join(timeout=2)
        self.assertFalse(errors)
        self.assertEqual(manager.snapshot()["idle_sessions"], 2)

    def test_factory_failure_releases_all_reservations(self) -> None:
        attempts = itertools.count(1)

        def factory(_context: object) -> _Runtime:
            if next(attempts) == 1:
                raise RuntimeError("synthetic factory failure")
            return _Runtime("second")

        manager = HTTPSessionManager(
            factory,
            limits=_limits(total=1, per_identity=1, initializations=1),
        )
        self.addCleanup(manager.close)
        context = _Context(oauth_identity=_Identity("a", "grant-a"))
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            manager.create(context)
        runtime = manager.create(context)
        self.assertEqual(runtime.http_session_id, "second")
        self.assertEqual(manager.snapshot()["creating_sessions"], 0)

    def test_monotonic_ttl_boundary_and_lru_refresh(self) -> None:
        clock = _Clock()
        sequence = itertools.count(1)
        manager = HTTPSessionManager(
            lambda _context: _Runtime(f"s-{next(sequence)}"),
            limits=_limits(total=3, per_identity=3, ttl=10, initializations=3),
            clock=clock,
        )
        self.addCleanup(manager.close)
        first = manager.create(object())
        clock.advance(5)
        second = manager.create(object())
        clock.advance(5)
        self.assertEqual(manager.prune_expired(), 0)
        self.assertIs(manager.get(first.http_session_id), first)
        clock.advance(6)
        self.assertEqual(manager.prune_expired(), 1)
        self.assertEqual(second.closed, 1)
        self.assertEqual(first.closed, 0)


class HTTPSessionLeaseTests(unittest.TestCase):
    def test_delete_waits_for_active_lease_and_blocks_new_lease(self) -> None:
        manager = HTTPSessionManager(
            lambda _context: _Runtime("s-1"),
            limits=_limits(total=1, per_identity=1, initializations=1),
        )
        runtime = manager.create(object())
        lease_entered = threading.Event()
        release_lease = threading.Event()
        delete_done = threading.Event()

        def hold_lease() -> None:
            with manager.lease(runtime.http_session_id) as leased:
                self.assertIs(leased, runtime)
                lease_entered.set()
                release_lease.wait(timeout=2)

        holder = threading.Thread(target=hold_lease)
        holder.start()
        self.assertTrue(lease_entered.wait(timeout=2))

        def delete() -> None:
            self.assertTrue(manager.delete(runtime.http_session_id))
            delete_done.set()

        deleter = threading.Thread(target=delete)
        deleter.start()
        deadline = time.monotonic() + 2
        while manager.snapshot()["closing_sessions"] != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(delete_done.is_set())
        with manager.lease(runtime.http_session_id) as second:
            self.assertIsNone(second)
        release_lease.set()
        holder.join(timeout=2)
        deleter.join(timeout=2)
        self.assertTrue(delete_done.is_set())
        self.assertEqual(runtime.closed, 1)
        manager.close()

    def test_long_active_request_is_not_pruned_after_ttl(self) -> None:
        clock = _Clock()
        manager = HTTPSessionManager(
            lambda _context: _Runtime("s-1"),
            limits=_limits(total=1, per_identity=1, ttl=5, initializations=1),
            clock=clock,
        )
        runtime = manager.create(object())
        with manager.lease(runtime.http_session_id) as leased:
            self.assertIs(leased, runtime)
            clock.advance(20)
            self.assertEqual(manager.prune_expired(), 0)
            self.assertEqual(runtime.closed, 0)
        clock.advance(6)
        self.assertEqual(manager.prune_expired(), 1)
        self.assertEqual(runtime.closed, 1)
        manager.close()

    def test_runtime_close_runs_outside_manager_global_lock(self) -> None:
        manager: HTTPSessionManager

        class ReentrantRuntime(_Runtime):
            def close(self) -> None:
                manager.snapshot()
                super().close()

        manager = HTTPSessionManager(
            lambda _context: ReentrantRuntime("s-1"),
            limits=_limits(total=1, per_identity=1, initializations=1),
        )
        runtime = manager.create(object())
        self.assertTrue(manager.delete(runtime.http_session_id))
        self.assertEqual(runtime.closed, 1)
        manager.close()

    def test_close_is_idempotent_and_rejects_new_create(self) -> None:
        manager = HTTPSessionManager(
            lambda _context: _Runtime("s-1"),
            limits=_limits(total=1, per_identity=1, initializations=1),
        )
        runtime = manager.create(object())
        manager.close()
        manager.close()
        self.assertEqual(runtime.closed, 1)
        with self.assertRaises(HTTPSessionAdmissionError) as caught:
            manager.create(object())
        self.assertEqual(caught.exception.code, "http_session_server_closing")

    def test_shutdown_racing_initialize_closes_uninstalled_runtime_and_returns_reservation(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        close_entered = threading.Event()
        close_release = threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(close_release.set)
        runtimes: list[_Runtime] = []

        def factory(_context: object) -> _Runtime:
            runtime = _BlockingCloseRuntime("race-1", close_entered, close_release)
            runtimes.append(runtime)
            entered.set()
            release.wait(timeout=2)
            return runtime

        manager = HTTPSessionManager(
            factory,
            limits=_limits(total=1, per_identity=1, initializations=1),
        )
        create_errors: list[BaseException] = []

        def create() -> None:
            try:
                manager.create(object())
            except BaseException as exc:  # pragma: no cover - asserted below
                create_errors.append(exc)

        creator = threading.Thread(target=create)
        creator.start()
        self.assertTrue(entered.wait(timeout=2))
        closer = threading.Thread(target=manager.close)
        closer.start()
        release.set()
        self.assertTrue(close_entered.wait(timeout=2))
        self.assertTrue(closer.is_alive())
        close_release.set()
        creator.join(timeout=2)
        closer.join(timeout=2)
        self.assertFalse(creator.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(create_errors), 1)
        self.assertIsInstance(create_errors[0], HTTPSessionAdmissionError)
        self.assertEqual(getattr(create_errors[0], "code", None), "http_session_server_closing")
        self.assertEqual(runtimes[0].closed, 1)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["creating_sessions"], 0)
        self.assertEqual(snapshot["idle_sessions"], 0)


if __name__ == "__main__":
    unittest.main()
