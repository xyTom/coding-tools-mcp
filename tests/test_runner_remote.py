from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from coding_tools_mcp.runner.credentials import RunnerCredentialError, RunnerCredentialStore
from coding_tools_mcp.runner.jobs import (
    JobAccessError,
    JobInventoryItem,
    JobRecord,
    JobState,
    MAX_RUNNER_JOBS,
    RunnerJobReconciler,
)
from coding_tools_mcp.runner.protocol import (
    RunnerDisconnect,
    RunnerEvent,
    RunnerHello,
    RunnerProtocolError,
    WorkspaceInventoryItem,
    decode_message,
    encode_message,
    rpc_response_payload,
)
from coding_tools_mcp.runner.registry import (
    DuplicateRunnerInstanceError,
    RunnerConnectionError,
    RunnerRegistry,
)
from coding_tools_mcp.runner.transport import (
    RunnerAuthenticationError,
    RunnerRemoteError,
    RunnerUnavailableError,
    RunnerWebSocketTransport,
)
from coding_tools_mcp.secret_vault import SecretVault


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    def feed(self, payload: dict[str, Any]) -> None:
        self.incoming.put_nowait(encode_message(payload))

    def fail(self, error: Exception) -> None:
        self.incoming.put_nowait(error)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, (str, bytes)):
            raise AssertionError("fake websocket received an unsupported item")
        return item

    async def close(self) -> None:
        self.closed = True


def hello_for(credential: str, *, instance_id: str = "instance-1") -> RunnerHello:
    return RunnerHello(
        runner_id="runner-1",
        instance_id=instance_id,
        credential=credential,
        capabilities=("agent", "mcp", "semantic", "validation", "jobs"),
        workspaces=(WorkspaceInventoryItem("ws-1", "Workspace", r"G:\repo"),),
    )


async def wait_until(predicate: Any, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class RunnerProtocolTests(unittest.TestCase):
    def test_remote_workspace_root_stays_string_data(self) -> None:
        item = WorkspaceInventoryItem("ws-1", "Workspace", r"G:\repo")
        self.assertEqual(item.root, r"G:\repo")
        self.assertIsInstance(item.root, str)
        with self.assertRaises(RunnerProtocolError):
            WorkspaceInventoryItem("ws-2", "Bad", Path("remote-root"))  # type: ignore[arg-type]

    def test_public_hello_payload_never_contains_credential(self) -> None:
        hello = hello_for("runner-secret")
        public = hello.public_payload()
        self.assertNotIn("credential", public)
        self.assertNotIn("runner-secret", json.dumps(public))

    def test_message_size_is_bounded(self) -> None:
        with self.assertRaises(RunnerProtocolError):
            encode_message({"type": "oversized", "value": "x" * 32}, max_bytes=16)

    def test_event_and_graceful_disconnect_frames_are_identity_bound(self) -> None:
        event = RunnerEvent(
            "runner-1",
            "instance-1",
            "agent.turn.delta",
            {"sequence": 3, "text": "bounded"},
            workspace_id="ws-1",
        )
        decoded = RunnerEvent.from_payload(decode_message(encode_message(event.message_payload())))
        self.assertEqual(decoded, event)

        disconnect = RunnerDisconnect("runner-1", "instance-1", "operator shutdown")
        decoded_disconnect = RunnerDisconnect.from_payload(
            decode_message(encode_message(disconnect.message_payload()))
        )
        self.assertEqual(decoded_disconnect, disconnect)
        with self.assertRaises(RunnerProtocolError):
            RunnerDisconnect("runner-1", "instance-1", "x" * 257)


class RunnerCredentialTests(unittest.TestCase):
    def test_issue_authenticate_revoke(self) -> None:
        store = RunnerCredentialStore()
        issued = store.issue("runner-1")
        authenticated = store.authenticate("runner-1", issued.credential)
        self.assertEqual(authenticated.fingerprint, issued.fingerprint)
        self.assertNotEqual(authenticated.fingerprint, issued.credential)

        self.assertTrue(store.revoke("runner-1"))
        with self.assertRaises(RunnerCredentialError):
            store.authenticate("runner-1", issued.credential)

    def test_secret_vault_persists_without_plaintext_credential(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runner-vault.json"
            vault = SecretVault(path, "synthetic-runner-master-key")
            first = RunnerCredentialStore(vault)
            issued = first.issue("runner:home")

            second = RunnerCredentialStore(SecretVault(path, "synthetic-runner-master-key"))
            authenticated = second.authenticate("runner:home", issued.credential)
            self.assertEqual(authenticated.fingerprint, issued.fingerprint)
            self.assertNotIn(issued.credential, path.read_text(encoding="utf-8"))


class RunnerRegistryTests(unittest.TestCase):
    def test_duplicate_instance_and_heartbeat_replay_fail_closed(self) -> None:
        credentials = RunnerCredentialStore()
        issued = credentials.issue("runner-1")
        registry = RunnerRegistry()
        hello = hello_for(issued.credential)
        authenticated = credentials.authenticate("runner-1", issued.credential)
        registry.enroll(hello, authenticated.fingerprint)

        with self.assertRaises(DuplicateRunnerInstanceError):
            registry.enroll(hello_for(issued.credential, instance_id="instance-2"), authenticated.fingerprint)

        from coding_tools_mcp.runner.protocol import RunnerHeartbeat

        registry.heartbeat(RunnerHeartbeat("runner-1", "instance-1", 1))
        with self.assertRaises(RunnerConnectionError):
            registry.heartbeat(RunnerHeartbeat("runner-1", "instance-1", 1))


class RunnerJobReconciliationTests(unittest.TestCase):
    def test_disconnect_reconcile_and_process_identity(self) -> None:
        reconciler = RunnerJobReconciler()
        reconciler.register(
            JobRecord(
                "job-1",
                "runner-1",
                "ws-1",
                "owner-1",
                process_fingerprint="proc-1",
                runner_instance_id="instance-1",
                stdout_cursor=4,
                stderr_cursor=2,
                started_at="2026-08-08T07:00:00Z",
            )
        )
        reconciler.mark_runner_disconnected("runner-1", "instance-1")
        self.assertEqual(reconciler.get("job-1").state, JobState.RECOVERING)

        result = reconciler.reconcile(
            "runner-1",
            [
                JobInventoryItem(
                    "job-1",
                    "ws-1",
                    "proc-1",
                    JobState.RUNNING,
                    stdout_cursor=12,
                    stderr_cursor=3,
                    started_at="2026-08-08T07:00:00Z",
                )
            ],
            runner_instance_id="instance-1",
        )
        self.assertEqual(result.recovered, ("job-1",))
        recovered = reconciler.get("job-1")
        self.assertEqual(recovered.state, JobState.RUNNING)
        self.assertEqual((recovered.stdout_cursor, recovered.stderr_cursor), (12, 3))
        self.assertEqual(recovered.runner_instance_id, "instance-1")

        reconciler.mark_runner_disconnected("runner-1", "instance-1")
        result = reconciler.reconcile(
            "runner-1",
            [
                JobInventoryItem(
                    "job-1",
                    "ws-1",
                    "different-process",
                    JobState.RUNNING,
                    stdout_cursor=12,
                    stderr_cursor=3,
                    started_at="2026-08-08T07:00:00Z",
                )
            ],
            runner_instance_id="instance-1",
        )
        self.assertEqual(result.lost, ("job-1",))
        self.assertEqual(reconciler.get("job-1").state, JobState.LOST)

    def test_terminal_jobs_do_not_regress_and_cursor_regression_is_lost(self) -> None:
        reconciler = RunnerJobReconciler()
        reconciler.register(
            JobRecord(
                "job-complete",
                "runner-1",
                "ws-1",
                "owner-1",
                process_fingerprint="proc-complete",
                runner_instance_id="instance-1",
                stdout_cursor=8,
                stderr_cursor=1,
                started_at="2026-08-08T07:01:00Z",
            )
        )
        reconciler.mark_runner_disconnected("runner-1", "instance-1")
        result = reconciler.reconcile(
            "runner-1",
            [
                JobInventoryItem(
                    "job-complete",
                    "ws-1",
                    "proc-complete",
                    JobState.COMPLETED,
                    stdout_cursor=20,
                    stderr_cursor=2,
                    started_at="2026-08-08T07:01:00Z",
                )
            ],
            runner_instance_id="instance-1",
        )
        self.assertEqual(result.completed, ("job-complete",))
        terminal = reconciler.get("job-complete")
        self.assertEqual(terminal.state, JobState.COMPLETED)
        self.assertEqual((terminal.stdout_cursor, terminal.stderr_cursor), (20, 2))

        reconciler.reconcile("runner-1", [], runner_instance_id="instance-1")
        self.assertEqual(reconciler.get("job-complete").state, JobState.COMPLETED)

        reconciler.register(
            JobRecord(
                "job-regress",
                "runner-1",
                "ws-1",
                "owner-1",
                process_fingerprint="proc-regress",
                runner_instance_id="instance-1",
                stdout_cursor=50,
                stderr_cursor=10,
            )
        )
        reconciler.mark_runner_disconnected("runner-1", "instance-1")
        result = reconciler.reconcile(
            "runner-1",
            [
                JobInventoryItem(
                    "job-regress",
                    "ws-1",
                    "proc-regress",
                    JobState.RUNNING,
                    stdout_cursor=49,
                    stderr_cursor=10,
                )
            ],
            runner_instance_id="instance-1",
        )
        self.assertEqual(result.lost, ("job-regress",))
        self.assertEqual(reconciler.get("job-regress").state, JobState.LOST)

    def test_changed_runner_instance_cannot_recover_process_identity(self) -> None:
        reconciler = RunnerJobReconciler()
        reconciler.register(
            JobRecord(
                "job-1",
                "runner-1",
                "ws-1",
                "owner-1",
                process_fingerprint="proc-1",
                runner_instance_id="instance-1",
            )
        )
        reconciler.mark_runner_disconnected("runner-1", "instance-1")
        result = reconciler.reconcile(
            "runner-1",
            [JobInventoryItem("job-1", "ws-1", "proc-1", JobState.RUNNING)],
            runner_instance_id="instance-2",
        )
        self.assertEqual(result.lost, ("job-1",))
        self.assertEqual(reconciler.get("job-1").state, JobState.LOST)

    def test_registry_bound_owner_listing_and_snapshot_are_bounded(self) -> None:
        reconciler = RunnerJobReconciler()
        reconciler.register(
            JobRecord("job-1", "runner-1", "ws-1", "owner-1", process_fingerprint="proc-1")
        )
        reconciler.register(
            JobRecord("job-2", "runner-2", "ws-2", "owner-2", process_fingerprint="proc-2")
        )
        self.assertEqual(tuple(job.job_id for job in reconciler.list_for_owner("owner-1", "ws-1")), ("job-1",))
        snapshot = reconciler.snapshot()
        self.assertEqual(snapshot["job_count"], 2)
        self.assertEqual(snapshot["running"], 2)
        self.assertNotIn("owner-1", json.dumps(snapshot))
        self.assertNotIn("proc-1", json.dumps(snapshot))

        oversized = [
            JobInventoryItem(f"job-{index}", "ws-1", f"proc-{index}", JobState.RUNNING)
            for index in range(MAX_RUNNER_JOBS + 1)
        ]
        with self.assertRaises(JobAccessError):
            reconciler.reconcile("runner-1", oversized)

    def test_unknown_inventory_does_not_bind_and_owner_boundary_is_enforced(self) -> None:
        reconciler = RunnerJobReconciler()
        reconciler.register(
            JobRecord("job-1", "runner-1", "ws-1", "owner-1", process_fingerprint="proc-1")
        )
        with self.assertRaises(JobAccessError):
            reconciler.get_for_owner("job-1", "owner-2", "ws-1")
        with self.assertRaises(JobAccessError):
            reconciler.get_for_owner("job-1", "owner-1", "ws-2")

        empty = RunnerJobReconciler()
        result = empty.reconcile(
            "runner-1",
            [JobInventoryItem("unknown-job", "ws-1", "proc-x", JobState.RUNNING)],
        )
        self.assertEqual(result.recovered, ())
        with self.assertRaises(JobAccessError):
            empty.get("unknown-job")


class RunnerWebSocketTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.credentials = RunnerCredentialStore()
        self.issued = self.credentials.issue("runner-1")
        self.registry = RunnerRegistry()

    async def test_authenticated_enrollment_heartbeat_rpc_and_disconnect(self) -> None:
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential).message_payload())
        transport = await RunnerWebSocketTransport.accept(
            websocket,
            self.credentials,
            self.registry,
            request_timeout=1,
        )
        self.addAsyncCleanup(transport.close)

        ack = decode_message(websocket.sent[0])
        self.assertEqual(ack["type"], "hello_ack")
        self.assertNotIn("credential", ack)
        self.assertTrue(self.registry.get("runner-1").connected)  # type: ignore[union-attr]

        websocket.feed(
            {
                "type": "heartbeat",
                "runner_id": "runner-1",
                "instance_id": "instance-1",
                "sequence": 1,
            }
        )
        await wait_until(lambda: self.registry.get("runner-1").heartbeat_sequence == 1)  # type: ignore[union-attr]

        call = asyncio.create_task(
            transport.call(
                workspace_id="ws-1",
                method="agent.resume",
                params={"session_id": "agent-1"},
                request_id="req-1",
            )
        )
        await wait_until(lambda: len(websocket.sent) >= 2)
        request = decode_message(websocket.sent[-1])
        self.assertEqual(request["type"], "rpc_request")
        self.assertEqual(request["workspace_id"], "ws-1")
        websocket.feed(rpc_response_payload("req-1", result={"ok": True}))
        self.assertEqual(await call, {"ok": True})

        websocket.fail(ConnectionError("network dropped"))
        await wait_until(lambda: not self.registry.get("runner-1").connected)  # type: ignore[union-attr]
        self.assertTrue(websocket.closed)

    async def test_sync_bridge_schedules_on_transport_owner_loop_and_preserves_remote_error(self) -> None:
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential).message_payload())
        transport = await RunnerWebSocketTransport.accept(
            websocket,
            self.credentials,
            self.registry,
            request_timeout=1,
        )
        self.addAsyncCleanup(transport.close)

        sync_call = asyncio.create_task(
            asyncio.to_thread(
                transport.call_sync,
                workspace_id="ws-1",
                method="capability.call",
                params={"handle": "cap-1"},
                request_id="req-sync-1",
            )
        )
        await wait_until(lambda: len(websocket.sent) >= 2)
        request = decode_message(websocket.sent[-1])
        self.assertEqual(request["request_id"], "req-sync-1")
        websocket.feed(rpc_response_payload("req-sync-1", result={"ok": True}))
        self.assertEqual(await sync_call, {"ok": True})

        failed_call = asyncio.create_task(
            asyncio.to_thread(
                transport.call_sync,
                workspace_id="ws-1",
                method="capability.call",
                params={"handle": "cap-2"},
                request_id="req-sync-2",
            )
        )
        await wait_until(lambda: len(websocket.sent) >= 3)
        websocket.feed(
            rpc_response_payload(
                "req-sync-2",
                error={
                    "code": "RUNNER_CAPABILITY_CAPACITY",
                    "message": "capacity reached",
                    "retryable": True,
                    "details": {"limit": 512},
                },
            )
        )
        with self.assertRaises(RunnerRemoteError) as caught:
            await failed_call
        self.assertEqual(caught.exception.code, "RUNNER_CAPABILITY_CAPACITY")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.details, {"limit": 512})

    async def test_runner_event_push_is_bounded_and_workspace_scoped(self) -> None:
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential).message_payload())
        transport = await RunnerWebSocketTransport.accept(
            websocket,
            self.credentials,
            self.registry,
            event_queue_limit=2,
        )
        self.addAsyncCleanup(transport.close)

        event = RunnerEvent(
            "runner-1",
            "instance-1",
            "agent.turn.delta",
            {"sequence": 1},
            workspace_id="ws-1",
        )
        websocket.feed(event.message_payload())
        received = await transport.next_event(timeout=1)
        self.assertEqual(received, event)
        self.assertEqual(transport.event_count, 0)

        websocket.feed(
            RunnerEvent(
                "runner-1",
                "instance-1",
                "agent.turn.delta",
                {"sequence": 2},
                workspace_id="ws-other",
            ).message_payload()
        )
        await wait_until(lambda: websocket.closed)
        self.assertFalse(self.registry.get("runner-1").connected)  # type: ignore[union-attr]

    async def test_runner_event_queue_overflow_fails_closed(self) -> None:
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential).message_payload())
        transport = await RunnerWebSocketTransport.accept(
            websocket,
            self.credentials,
            self.registry,
            event_queue_limit=1,
        )
        self.addAsyncCleanup(transport.close)

        websocket.feed(
            RunnerEvent("runner-1", "instance-1", "job.output", {"n": 1}).message_payload()
        )
        websocket.feed(
            RunnerEvent("runner-1", "instance-1", "job.output", {"n": 2}).message_payload()
        )
        await wait_until(lambda: websocket.closed)
        self.assertEqual(transport.event_count, 1)
        self.assertFalse(self.registry.get("runner-1").connected)  # type: ignore[union-attr]

    async def test_graceful_runner_disconnect_acknowledges_and_marks_jobs_recovering(self) -> None:
        reconciler = RunnerJobReconciler()
        reconciler.register(
            JobRecord(
                "job-1",
                "runner-1",
                "ws-1",
                "owner-1",
                process_fingerprint="proc-1",
                runner_instance_id="instance-1",
            )
        )
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential).message_payload())
        transport = await RunnerWebSocketTransport.accept(
            websocket,
            self.credentials,
            self.registry,
            reconciler=reconciler,
        )

        websocket.feed(
            RunnerDisconnect("runner-1", "instance-1", "planned restart").message_payload()
        )
        await wait_until(lambda: websocket.closed)
        self.assertFalse(self.registry.get("runner-1").connected)  # type: ignore[union-attr]
        self.assertEqual(reconciler.get("job-1").state, JobState.RECOVERING)
        ack = decode_message(websocket.sent[-1])
        self.assertEqual(
            ack,
            {
                "type": "disconnect_ack",
                "runner_id": "runner-1",
                "instance_id": "instance-1",
            },
        )
        await transport.close()

    async def test_invalid_credential_is_rejected_before_registry_enrollment(self) -> None:
        websocket = FakeWebSocket()
        websocket.feed(hello_for("wrong-credential").message_payload())

        with self.assertRaises(RunnerAuthenticationError):
            await RunnerWebSocketTransport.accept(websocket, self.credentials, self.registry)
        self.assertTrue(websocket.closed)
        self.assertIsNone(self.registry.get("runner-1"))

    async def test_replayed_heartbeat_closes_connection_and_pending_call_fails(self) -> None:
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential).message_payload())
        transport = await RunnerWebSocketTransport.accept(
            websocket,
            self.credentials,
            self.registry,
            request_timeout=1,
        )

        websocket.feed(
            {
                "type": "heartbeat",
                "runner_id": "runner-1",
                "instance_id": "instance-1",
                "sequence": 1,
            }
        )
        await wait_until(lambda: self.registry.get("runner-1").heartbeat_sequence == 1)  # type: ignore[union-attr]

        call = asyncio.create_task(
            transport.call(
                workspace_id="ws-1",
                method="mcp.call",
                request_id="req-pending",
            )
        )
        await wait_until(lambda: transport.pending_count == 1)
        websocket.feed(
            {
                "type": "heartbeat",
                "runner_id": "runner-1",
                "instance_id": "instance-1",
                "sequence": 1,
            }
        )

        with self.assertRaises(RunnerUnavailableError):
            await call
        self.assertTrue(websocket.closed)
        self.assertFalse(self.registry.get("runner-1").connected)  # type: ignore[union-attr]

    async def test_workspace_routing_fails_closed(self) -> None:
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential).message_payload())
        transport = await RunnerWebSocketTransport.accept(websocket, self.credentials, self.registry)
        self.addAsyncCleanup(transport.close)

        with self.assertRaises(RunnerUnavailableError):
            await transport.call(workspace_id="ws-other", method="mcp.call")

    async def test_disconnect_moves_jobs_to_recovering_and_inventory_recovers(self) -> None:
        reconciler = RunnerJobReconciler()
        reconciler.register(
            JobRecord(
                "job-1",
                "runner-1",
                "ws-1",
                "owner-1",
                process_fingerprint="proc-1",
                runner_instance_id="instance-1",
                started_at="2026-08-08T07:02:00Z",
            )
        )
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential).message_payload())
        transport = await RunnerWebSocketTransport.accept(
            websocket,
            self.credentials,
            self.registry,
            reconciler=reconciler,
        )

        websocket.fail(ConnectionError("temporary disconnect"))
        await wait_until(lambda: reconciler.get("job-1").state == JobState.RECOVERING)
        self.assertFalse(self.registry.get("runner-1").connected)  # type: ignore[union-attr]

        reconnected = FakeWebSocket()
        reconnected.feed(hello_for(self.issued.credential).message_payload())
        second = await RunnerWebSocketTransport.accept(
            reconnected,
            self.credentials,
            self.registry,
            reconciler=reconciler,
        )
        self.addAsyncCleanup(second.close)
        reconnected.feed(
            {
                "type": "job_inventory",
                "runner_id": "runner-1",
                "instance_id": "instance-1",
                "jobs": [
                    JobInventoryItem(
                        "job-1",
                        "ws-1",
                        "proc-1",
                        JobState.RUNNING,
                        stdout_cursor=12,
                        stderr_cursor=3,
                        started_at="2026-08-08T07:02:00Z",
                    ).payload()
                ],
            }
        )
        await wait_until(lambda: reconciler.get("job-1").state == JobState.RUNNING)
        recovered = reconciler.get("job-1")
        self.assertEqual((recovered.stdout_cursor, recovered.stderr_cursor), (12, 3))
        self.assertEqual(recovered.runner_instance_id, "instance-1")

        await transport.close()

    async def test_stale_transport_disconnect_cannot_recover_jobs_after_new_instance_enrolls(self) -> None:
        reconciler = RunnerJobReconciler()
        reconciler.register(
            JobRecord(
                "job-1",
                "runner-1",
                "ws-1",
                "owner-1",
                process_fingerprint="proc-1",
                runner_instance_id="instance-1",
            )
        )
        websocket = FakeWebSocket()
        websocket.feed(hello_for(self.issued.credential, instance_id="instance-1").message_payload())
        transport = await RunnerWebSocketTransport.accept(
            websocket,
            self.credentials,
            self.registry,
            reconciler=reconciler,
        )

        self.registry.disconnect("runner-1", "instance-1")
        authenticated = self.credentials.authenticate("runner-1", self.issued.credential)
        self.registry.enroll(
            hello_for(self.issued.credential, instance_id="instance-2"),
            authenticated.fingerprint,
        )
        websocket.fail(ConnectionError("stale socket finally failed"))
        await wait_until(lambda: websocket.closed)

        self.assertEqual(self.registry.get("runner-1").instance_id, "instance-2")  # type: ignore[union-attr]
        self.assertEqual(reconciler.get("job-1").state, JobState.RUNNING)
        await transport.close()


if __name__ == "__main__":
    unittest.main()
