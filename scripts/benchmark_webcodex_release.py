from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import json
import math
import os
import platform
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from coding_tools_mcp.agent_backends.base import AgentBackendEvent, BackendHealth, BackendThread, BackendTurn
from coding_tools_mcp.agent_session_store import AgentSessionStore
from coding_tools_mcp.agent_sessions import AgentSessionService
from coding_tools_mcp.json_utils import strict_json_bytes
from coding_tools_mcp.runner.client import RunnerClientConfig, RunnerPeer
from coding_tools_mcp.runner.credentials import RunnerCredentialStore
from coding_tools_mcp.runner.jobs import RunnerJobReconciler
from coding_tools_mcp.runner.protocol import RunnerHello, WorkspaceInventoryItem
from coding_tools_mcp.runner.registry import RunnerRegistry
from coding_tools_mcp.runner.routing import RemoteMcpRouteService, RemoteMcpRouteStore, RunnerMcpRouter, RunnerMcpSessionHost
from coding_tools_mcp.semantic import LspSemanticBackend, LspServerSpec
from coding_tools_mcp.server import MCPHandler, Runtime, RuntimeHTTPServer
from coding_tools_mcp.transport_http import HTTPSessionAdmissionError, HTTPSessionLimits, HTTPSessionManager
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def latency_summary(values_ms: list[float]) -> dict[str, float]:
    return {
        "count": float(len(values_ms)),
        "p50_ms": round(percentile(values_ms, 0.50), 4),
        "p95_ms": round(percentile(values_ms, 0.95), 4),
        "max_ms": round(max(values_ms) if values_ms else 0.0, 4),
    }


def timed_ms(callable_: Any) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    result = callable_()
    return result, (time.perf_counter_ns() - started) / 1_000_000.0


def current_rss_bytes() -> tuple[int, str]:
    if os.name == "nt":
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        get_process_memory_info.restype = ctypes.c_int
        process = get_current_process()
        ok = get_process_memory_info(
            process,
            ctypes.byref(counters),
            counters.cb,
        )
        if ok:
            return int(counters.WorkingSetSize), "windows-working-set"
    statm = Path("/proc/self/statm")
    if statm.is_file():
        fields = statm.read_text(encoding="ascii").split()
        if len(fields) >= 2:
            return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE")), "linux-proc-statm"
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        multiplier = 1 if sys.platform == "darwin" else 1024
        return value * multiplier, "resource-peak-rss-fallback"
    except Exception:
        return 0, "unavailable"


def runtime_memory_benchmark(root: Path, counts: Iterable[int]) -> dict[str, Any]:
    requested = sorted(set(max(0, int(value)) for value in counts))
    runtimes: list[Runtime] = []
    points: list[dict[str, Any]] = []
    try:
        for target in requested:
            while len(runtimes) < target:
                runtimes.append(Runtime(root, transport="http"))
            gc.collect()
            rss, method = current_rss_bytes()
            points.append({"sessions": target, "rss_bytes": rss, "rss_method": method})
        slopes: list[float] = []
        for left, right in zip(points, points[1:]):
            delta_count = right["sessions"] - left["sessions"]
            delta_rss = right["rss_bytes"] - left["rss_bytes"]
            if delta_count > 0:
                slopes.append(delta_rss / delta_count)
        estimate = sum(slopes) / len(slopes) if slopes else 0.0
        return {
            "points": points,
            "incremental_rss_bytes_per_runtime_estimate": round(estimate, 2),
            "incremental_rss_bytes_per_runtime_range": [
                round(min(slopes), 2) if slopes else 0.0,
                round(max(slopes), 2) if slopes else 0.0,
            ],
            "uncertainty": "RSS includes allocator/process noise; slope range is reported instead of a single exact cost.",
        }
    finally:
        for runtime in runtimes:
            runtime.close()
        gc.collect()


def runtime_initialize_benchmark(root: Path, repeats: int) -> dict[str, Any]:
    initialize_ms: list[float] = []
    list_ms: list[float] = []
    initialize_bytes: list[int] = []
    list_bytes: list[int] = []
    for _ in range(repeats):
        runtime = Runtime(root, transport="http")
        try:
            initialized, elapsed = timed_ms(lambda: runtime.initialize({"name": "benchmark"}))
            initialize_ms.append(elapsed)
            initialize_bytes.append(len(strict_json_bytes(initialized)))
            listed, elapsed = timed_ms(runtime.list_tools)
            list_ms.append(elapsed)
            list_bytes.append(len(strict_json_bytes(listed)))
        finally:
            runtime.close()
    return {
        "initialize": latency_summary(initialize_ms),
        "tools_list": latency_summary(list_ms),
        "initialize_bytes": {"min": min(initialize_bytes), "max": max(initialize_bytes)},
        "tools_list_bytes": {"min": min(list_bytes), "max": max(list_bytes)},
    }


class BenchmarkSessionRuntime:
    sequence = 0

    def __init__(self) -> None:
        type(self).sequence += 1
        self.http_session_id = f"benchmark-http-{self.sequence}"
        self.closed = False

    def close(self) -> None:
        self.closed = True


def session_manager_benchmark(root: Path, capacity: int, lease_counts: Iterable[int]) -> dict[str, Any]:
    del root
    sequence = 0

    def factory(_context: object) -> BenchmarkSessionRuntime:
        nonlocal sequence
        sequence += 1
        return BenchmarkSessionRuntime()

    limits = HTTPSessionLimits(
        max_total=capacity,
        max_per_identity=capacity,
        idle_ttl_seconds=3600,
        max_initializations=capacity,
    )
    rss_points: list[dict[str, Any]] = []
    for count in sorted({0, 100, capacity, 500}):
        gc.collect()
        if count == 0:
            rss, method = current_rss_bytes()
            rss_points.append({"sessions": 0, "rss_bytes": rss, "rss_method": method})
            continue
        manager = HTTPSessionManager(
            factory,
            limits=HTTPSessionLimits(
                max_total=count,
                max_per_identity=count,
                idle_ttl_seconds=3600,
                max_initializations=count,
            ),
        )
        for _ in range(count):
            manager.create(object())
        gc.collect()
        rss, method = current_rss_bytes()
        rss_points.append({"sessions": count, "rss_bytes": rss, "rss_method": method})
        manager.close()
    soak = HTTPSessionManager(factory, limits=limits)
    before_rss, rss_method = current_rss_bytes()
    soak_started = time.perf_counter()
    for _ in range(capacity * 10):
        runtime = soak.create(object())
        if not soak.delete(runtime.http_session_id):
            raise RuntimeError("benchmark create/delete failed to delete session")
    soak_seconds = time.perf_counter() - soak_started
    gc.collect()
    after_rss, _ = current_rss_bytes()
    soak_snapshot = soak.snapshot()
    soak.close()

    lease_points: list[dict[str, Any]] = []
    for count in sorted(set(int(value) for value in lease_counts if int(value) > 0)):
        manager = HTTPSessionManager(
            factory,
            limits=HTTPSessionLimits(
                max_total=count,
                max_per_identity=count,
                idle_ttl_seconds=3600,
                max_initializations=count,
            ),
        )
        sessions = [manager.create(object()).http_session_id for _ in range(count)]
        target = sessions[-1]
        samples: list[float] = []
        for _ in range(200):
            started = time.perf_counter_ns()
            with manager.lease(target) as runtime:
                if runtime is None:
                    raise RuntimeError("benchmark lease unexpectedly missed session")
            samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
        lease_points.append({"sessions": count, **latency_summary(samples)})
        manager.close()

    capacity_manager = HTTPSessionManager(factory, limits=limits)
    active = [capacity_manager.create(object()).http_session_id for _ in range(capacity)]
    rejection: dict[str, Any]
    try:
        capacity_manager.create(object())
        rejection = {"rejected": False}
    except HTTPSessionAdmissionError as exc:
        rejection = {
            "rejected": True,
            "code": exc.code,
            "retry_after_seconds": exc.retry_after_seconds,
        }
    with capacity_manager.lease(active[0]) as existing:
        existing_remained_available = existing is not None
    capacity_manager.close()
    return {
        "boundary": (
            "HTTPSessionManager lifecycle/lookup with lightweight closable session runtimes; "
            "real Runtime RSS is measured separately and upstream lifecycle is covered by resilience tests"
        ),
        "configured_capacity": capacity,
        "rss_points": rss_points,
        "soak_cycles": capacity * 10,
        "soak_seconds": round(soak_seconds, 4),
        "soak_snapshot": soak_snapshot,
        "soak_rss_delta_bytes": after_rss - before_rss,
        "rss_method": rss_method,
        "lease_latency": lease_points,
        "over_capacity": rejection,
        "existing_session_after_rejection": existing_remained_available,
    }


class BenchmarkAgentBackend:
    backend_kind = "benchmark"

    def __init__(self) -> None:
        self.events: list[AgentBackendEvent] = []
        self.thread_sequence = 0

    def health(self) -> BackendHealth:
        return BackendHealth(True, self.backend_kind, version="benchmark")

    def create_thread(self, *, instructions: str | None = None) -> BackendThread:
        self.thread_sequence += 1
        return BackendThread(f"thread-{self.thread_sequence}", {"instructions": instructions})

    def resume_thread(self, thread_id: str, *, instructions: str | None = None) -> BackendThread:
        return BackendThread(thread_id, {"instructions": instructions, "resumed": True})

    def send_turn(self, thread_id: str, message: str) -> BackendTurn:
        self.events.append(AgentBackendEvent(1, "assistant", "turn/assistant", {"text": message}))
        return BackendTurn("turn-1", {"thread_id": thread_id})

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        return None

    def approve(self, approval_id: str, decision: str) -> None:
        return None

    def list_threads(self, *, limit: int = 50) -> list[BackendThread]:
        return []

    def close_thread(self, thread_id: str) -> None:
        return None

    def stream_events(self, *, timeout: float | None = None):
        del timeout
        yield from self.drain_events()

    def drain_events(self, *, limit: int = 100) -> list[AgentBackendEvent]:
        items = self.events[:limit]
        del self.events[:limit]
        return items

    def close(self) -> None:
        return None


def agent_session_benchmark(root: Path, repeats: int = 100) -> dict[str, Any]:
    workspace = root / "agent-workspace"
    workspace.mkdir(exist_ok=True)
    db = root / "agent-benchmark.sqlite3"
    catalog = WorkspaceCatalog([WorkspaceEntry("bench", "Bench", workspace, True, True)], "bench")
    factory = lambda _workspace, _kind: BenchmarkAgentBackend()
    service = AgentSessionService(
        AgentSessionStore(db),
        catalog,
        factory,
        fingerprint_factory=lambda _workspace: {
            "version": 1,
            "git_available": True,
            "head": "benchmark",
            "worktree_digest": "benchmark",
            "instruction_digest": "benchmark",
            "changed_paths": [],
            "context_changed": False,
            "changes": [],
        },
    )
    create_samples: list[float] = []
    sessions: list[str] = []
    try:
        for _ in range(repeats):
            record, elapsed = timed_ms(
                lambda: service.create_session(
                    workspace_id="bench",
                    owner_principal_id="benchmark-owner",
                    backend_kind="benchmark",
                    instructions="benchmark",
                )
            )
            create_samples.append(elapsed)
            sessions.append(record.session_id)
        db_size_after_create = db.stat().st_size
        resume_samples: list[float] = []
        for session_id in sessions[: min(25, len(sessions))]:
            _record, elapsed = timed_ms(lambda sid=session_id: service.resume_session(sid, "benchmark-owner"))
            resume_samples.append(elapsed)
        first_event_samples: list[float] = []
        for session_id in sessions[: min(25, len(sessions))]:
            started = time.perf_counter_ns()
            service.send_turn(session_id, "benchmark-owner", "hello")
            events = service.drain_events(session_id, "benchmark-owner", limit=10)
            if not events:
                raise RuntimeError("benchmark Agent backend did not produce first event")
            first_event_samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
        return {
            "boundary": "AgentSessionService + SQLite + in-process benchmark backend; not real Codex process startup",
            "sessions_created": len(sessions),
            "create": latency_summary(create_samples),
            "resume": latency_summary(resume_samples),
            "first_event": latency_summary(first_event_samples),
            "db_size_bytes": db_size_after_create,
            "db_bytes_per_session_estimate": round(db_size_after_create / max(1, len(sessions)), 2),
        }
    finally:
        service.close()


def lsp_benchmark(repo_root: Path) -> dict[str, Any]:
    fixture_root = repo_root / "tests" / "fixtures" / "semantic-project"
    fake_lsp = repo_root / "tests" / "fixtures" / "fake_lsp_server.py"
    spec = LspServerSpec(
        language="python",
        language_id="python",
        extensions=(".py",),
        candidates=((sys.executable, str(fake_lsp)),),
    )
    backend = LspSemanticBackend(
        fixture_root,
        server_specs=(spec,),
        request_timeout=2.0,
        diagnostics_wait=0.05,
    )
    try:
        _cold, cold_ms = timed_ms(lambda: backend.document_symbols("main.py"))
        warm_samples: list[float] = []
        for _ in range(20):
            _warm, elapsed = timed_ms(lambda: backend.document_symbols("main.py"))
            warm_samples.append(elapsed)
        return {
            "boundary": "repository fake-LSP fixture",
            "cold_start_ms": round(cold_ms, 4),
            "warm_query": latency_summary(warm_samples),
        }
    finally:
        backend.close()


class BenchmarkRunnerRuntime:
    sequence = 0

    def __init__(self) -> None:
        type(self).sequence += 1
        self.http_session_id = f"bench-remote-{self.sequence}"

    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"protocolVersion": "2025-11-25", "capabilities": {"tools": {"listChanged": False}}}

    def list_tools(self) -> dict[str, Any]:
        return {"tools": []}

    def call_tool(self, name: str, arguments: dict[str, Any] | None, *, request_id: Any = None) -> dict[str, Any]:
        return {"content": [], "structuredContent": {}, "isError": False}

    def close(self) -> None:
        return None


def _wait_runner(routes: RemoteMcpRouteService, runner_id: str, expected: bool, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bool(routes.runner_status(runner_id)["connected"]) is expected:
            return
        time.sleep(0.005)
    raise RuntimeError("Runner connection state benchmark timed out")


def runner_rpc_benchmark(root: Path, repeats: int = 100) -> dict[str, Any]:
    control_runtime = Runtime(root, auth_token="benchmark-mcp", transport="http")
    credentials = RunnerCredentialStore()
    issued = credentials.issue("benchmark-runner")
    registry = RunnerRegistry()
    reconciler = RunnerJobReconciler()
    routes = RemoteMcpRouteService(RemoteMcpRouteStore())
    server = RuntimeHTTPServer(
        ("127.0.0.1", 0),
        MCPHandler,
        control_runtime,
        lambda _context: Runtime(root, transport="http"),
        runner_route_service=routes,
        runner_credentials=credentials,
        runner_registry=registry,
        runner_job_reconciler=reconciler,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    runner_sessions = RunnerMcpSessionHost(lambda _workspace, _auth: BenchmarkRunnerRuntime())
    router = RunnerMcpRouter(runner_sessions)

    def start_peer(instance_id: str) -> tuple[RunnerPeer, threading.Thread, list[BaseException]]:
        config = RunnerClientConfig(
            server_url=f"ws://127.0.0.1:{server.server_address[1]}/runner/ws",
            runner_id="benchmark-runner",
            instance_id=instance_id,
            credential=issued.credential,
            heartbeat_seconds=1.0,
        )
        hello = RunnerHello(
            "benchmark-runner",
            instance_id,
            issued.credential,
            ("mcp",),
            (WorkspaceInventoryItem("benchmark-workspace", "Benchmark", str(root)),),
        )
        peer = RunnerPeer(config, hello, router)
        errors: list[BaseException] = []

        def target() -> None:
            try:
                asyncio.run(peer.run_once())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return peer, thread, errors

    try:
        peer, peer_thread, errors = start_peer("instance-one")
        _wait_runner(routes, "benchmark-runner", True)
        rpc_samples: list[float] = []
        for _ in range(repeats):
            _value, elapsed = timed_ms(
                lambda: routes.call_runner_sync(
                    runner_id="benchmark-runner",
                    workspace_id="benchmark-workspace",
                    method="mcp.inventory",
                    params={},
                )
            )
            rpc_samples.append(elapsed)
        peer.stop_sync("benchmark reconnect")
        peer_thread.join(timeout=5)
        _wait_runner(routes, "benchmark-runner", False)
        reconnect_started = time.perf_counter_ns()
        second, second_thread, second_errors = start_peer("instance-two")
        _wait_runner(routes, "benchmark-runner", True)
        reconnect_ms = (time.perf_counter_ns() - reconnect_started) / 1_000_000.0
        second.stop_sync("benchmark complete")
        second_thread.join(timeout=5)
        if errors or second_errors:
            raise RuntimeError(f"Runner benchmark peer failed: {errors or second_errors}")
        return {
            "boundary": "real loopback HTTP Upgrade/WebSocket with in-process fake Runner Runtime",
            "rpc": latency_summary(rpc_samples),
            "reconnect_ms": round(reconnect_ms, 4),
        }
    finally:
        runner_sessions.shutdown()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def build_report(repo_root: Path, *, capacity: int, runtime_counts: list[int]) -> dict[str, Any]:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "cpu_count": os.cpu_count(),
            },
            "runtime": {
                "catalog_and_initialize": runtime_initialize_benchmark(workspace, 10),
                "memory": runtime_memory_benchmark(workspace, runtime_counts),
            },
            "http_sessions": session_manager_benchmark(
                workspace,
                capacity,
                [1, 100, capacity, 500],
            ),
            "agent_sessions": agent_session_benchmark(root, 100),
            "lsp": lsp_benchmark(repo_root),
            "runner": runner_rpc_benchmark(workspace, 100),
            "claims": {
                "mutating_call_replay": False,
                "network": "loopback only; no public network used",
                "real_credentials": False,
                "codex_process_benchmark": False,
            },
        }


def build_section(
    section: str,
    repo_root: Path,
    *,
    capacity: int,
    runtime_counts: list[int],
) -> dict[str, Any]:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        report: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "cpu_count": os.cpu_count(),
            },
            "section": section,
        }
        if section == "runtime":
            report["runtime"] = {
                "catalog_and_initialize": runtime_initialize_benchmark(workspace, 10),
                "memory": runtime_memory_benchmark(workspace, runtime_counts),
            }
        elif section == "sessions":
            report["http_sessions"] = session_manager_benchmark(
                workspace,
                capacity,
                [1, 100, capacity, 500],
            )
        elif section == "agent":
            report["agent_sessions"] = agent_session_benchmark(root, 100)
        elif section == "lsp":
            report["lsp"] = lsp_benchmark(repo_root)
        elif section == "runner":
            report["runner"] = runner_rpc_benchmark(workspace, 100)
        else:
            raise ValueError(f"unknown benchmark section: {section}")
        return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capacity", type=int, default=128)
    parser.add_argument("--runtime-counts", default="0,100,128,500")
    parser.add_argument(
        "--section",
        choices=("all", "runtime", "sessions", "agent", "lsp", "runner"),
        default="all",
    )
    args = parser.parse_args()
    counts = [int(value.strip()) for value in args.runtime_counts.split(",") if value.strip()]
    if args.capacity < 1 or any(value < 0 for value in counts):
        parser.error("capacity/counts must be non-negative and capacity must be positive")
    report = (
        build_report(REPO_ROOT, capacity=args.capacity, runtime_counts=counts)
        if args.section == "all"
        else build_section(
            args.section,
            REPO_ROOT,
            capacity=args.capacity,
            runtime_counts=counts,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
