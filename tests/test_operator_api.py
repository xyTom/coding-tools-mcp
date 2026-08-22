from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.agent_backends.base import (
    AgentBackendEvent,
    BackendHealth,
    BackendThread,
    BackendTurn,
)
from coding_tools_mcp.agent_session_store import AgentSessionStore
from coding_tools_mcp.agent_sessions import AgentSessionService, AgentSessionServiceError
from coding_tools_mcp.admin_sessions import ADMIN_SESSION_COOKIE
from coding_tools_mcp.oauth import OAuthConfig, OAuthIdentity
from coding_tools_mcp.operator_api import OperatorAPIError, OperatorAPIService, OperatorPrincipal
from coding_tools_mcp.operator_sessions import OPERATOR_SESSION_COOKIE
from coding_tools_mcp.server import MCPHandler, Runtime, RuntimeHTTPServer, configure_allowed_origins
from coding_tools_mcp.transcript import TranscriptStore
from coding_tools_mcp.validation import ValidationResult
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry


class FakeAgentBackend:
    backend_kind = "codex-app-server"

    def __init__(self) -> None:
        self.thread_id = "thread-1"
        self.closed = False
        self.events: deque[AgentBackendEvent] = deque()
        self.approvals: list[tuple[str, str]] = []
        self.interrupts: list[tuple[str, str]] = []

    def health(self) -> BackendHealth:
        return BackendHealth(not self.closed, self.backend_kind)

    def create_thread(self, *, instructions: str | None = None) -> BackendThread:
        return BackendThread(self.thread_id, {"instructions": instructions})

    def resume_thread(
        self,
        thread_id: str,
        *,
        instructions: str | None = None,
    ) -> BackendThread:
        self.thread_id = thread_id
        return BackendThread(thread_id, {"instructions": instructions})

    def send_turn(self, thread_id: str, message: str) -> BackendTurn:
        self.events.append(
            AgentBackendEvent(
                sequence=len(self.events) + 1,
                kind="assistant",
                method="turn/assistant",
                params={"text": message},
            )
        )
        return BackendTurn("turn-1", {"thread_id": thread_id})

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupts.append((thread_id, turn_id))

    def approve(self, approval_id: str, decision: str) -> None:
        self.approvals.append((approval_id, decision))

    def list_threads(self, *, limit: int = 50) -> list[BackendThread]:
        return [BackendThread(self.thread_id, {})][:limit]

    def close_thread(self, thread_id: str) -> None:
        del thread_id

    def stream_events(self, *, timeout: float | None = None):
        del timeout
        while self.events:
            yield self.events.popleft()

    def drain_events(self, *, limit: int = 100) -> list[AgentBackendEvent]:
        result: list[AgentBackendEvent] = []
        while self.events and len(result) < limit:
            result.append(self.events.popleft())
        return result

    def close(self) -> None:
        self.closed = True


class FakeValidationBackend:
    def __init__(self) -> None:
        self.closed = False

    def status(self) -> dict[str, object]:
        return {"ok": True, "backend": "fake", "status": "ready", "recipes": ["python:test"]}

    def run(self, recipe: str) -> ValidationResult:
        return ValidationResult(
            "passed",
            recipe,
            command="python -m unittest",
            exit_code=0,
            duration_ms=7,
        )

    def close(self) -> None:
        self.closed = True


class OperatorAPIServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        first = root / "first"
        second = root / "second"
        first.mkdir()
        second.mkdir()
        self.catalog = WorkspaceCatalog(
            [
                WorkspaceEntry("ws-a", "Alpha", first, True, True),
                WorkspaceEntry("ws-b", "Beta", second, True, False),
            ],
            "ws-a",
        )
        self.backends: list[FakeAgentBackend] = []

        def backend_factory(_workspace: WorkspaceEntry, backend_kind: str) -> FakeAgentBackend:
            self.assertEqual(backend_kind, "codex-app-server")
            backend = FakeAgentBackend()
            self.backends.append(backend)
            return backend

        store = AgentSessionStore(root / "agent-sessions.sqlite3")
        self.transcripts = TranscriptStore(root / "transcripts.sqlite3")
        sessions = AgentSessionService(store, self.catalog, backend_factory)
        self.handoff_jobs: list[dict[str, object]] = []
        self.validation_backends: list[FakeValidationBackend] = []

        def validation_factory(_workspace: WorkspaceEntry) -> FakeValidationBackend:
            backend = FakeValidationBackend()
            self.validation_backends.append(backend)
            return backend

        self.service = OperatorAPIService(
            sessions,
            self.catalog,
            handoff_jobs=lambda principal, workspace: (
                tuple(self.handoff_jobs)
                if principal.principal_id == "oauth:alice:grant-1" and workspace.id == "ws-a"
                else ()
            ),
            validation_backend_factory=validation_factory,
            transcript_store=self.transcripts,
        )
        self.alice = OperatorPrincipal("oauth:alice:grant-1", ("ws-a",))
        self.bob = OperatorPrincipal("oauth:bob:grant-2", ("ws-b",))

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_workspace_projection_is_authorized_and_does_not_leak_roots(self) -> None:
        payload = self.service.list_workspaces(self.alice)
        self.assertEqual([item["id"] for item in payload["workspaces"]], ["ws-a"])
        self.assertNotIn("root", payload["workspaces"][0])
        self.assertTrue(payload["workspaces"][0]["authorized"])
        with self.assertRaises(OperatorAPIError) as denied:
            self.service.list_sessions(self.alice, "ws-b")
        self.assertEqual(denied.exception.status, 404)

    def test_create_list_get_and_cross_principal_access_are_partitioned(self) -> None:
        created = self.service.create_session(
            self.alice,
            {
                "workspace_id": "ws-a",
                "backend_kind": "codex",
                "instructions": "Keep changes focused.",
            },
        )["session"]
        self.assertEqual(created["backend_kind"], "codex-app-server")
        self.assertEqual(created["explicit_instructions"], "Keep changes focused.")
        self.assertNotIn("backend_thread_id", created)
        listed = self.service.list_sessions(self.alice, "ws-a")["sessions"]
        self.assertEqual([item["session_id"] for item in listed], [created["session_id"]])
        detail = self.service.get_session(self.alice, created["session_id"])["session"]
        self.assertEqual(detail["session_id"], created["session_id"])
        with self.assertRaises(AgentSessionServiceError) as denied:
            self.service.get_session(self.bob, created["session_id"])
        self.assertEqual(denied.exception.code, "AGENT_SESSION_NOT_FOUND")

    def test_event_buffer_supports_multiple_windows_after_backend_drain(self) -> None:
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        session_id = created["session_id"]
        self.service.send_turn(self.alice, session_id, {"message": "hello"})

        first_window = self.service.events(self.alice, session_id, after=0)
        self.assertEqual(len(first_window["events"]), 1)
        self.assertEqual(first_window["events"][0]["sequence"], 1)
        self.assertEqual(first_window["events"][0]["params"]["text"], "hello")

        second_window = self.service.events(self.alice, session_id, after=0)
        self.assertEqual(second_window["events"], first_window["events"])
        caught_up = self.service.events(self.alice, session_id, after=1)
        self.assertEqual(caught_up["events"], [])

        detail = self.transcripts.conversation_detail("ws-a", created["conversation_id"])
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual([item["role"] for item in detail["messages"]], ["user", "assistant"])
        self.assertEqual([item["content"] for item in detail["messages"]], ["hello", "hello"])
        listed = self.service.list_sessions(self.alice, "ws-a")["sessions"]
        self.assertEqual(listed[0]["title"], "hello")
        self.assertEqual(listed[0]["summary"], "hello")

    def test_operator_decisions_map_to_codex_approval_values(self) -> None:
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        session_id = created["session_id"]
        self.service.approve(self.alice, session_id, "approval-1", {"decision": "approve"})
        self.service.approve(self.alice, session_id, "approval-2", {"decision": "deny"})
        self.assertEqual(
            self.backends[0].approvals,
            [("approval-1", "accept"), ("approval-2", "decline")],
        )

    def test_validation_result_is_bounded_runtime_evidence_in_handoff(self) -> None:
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        session_id = created["session_id"]
        validation = self.service.run_validation(
            self.alice,
            session_id,
            {"recipe": "python:test"},
        )["validation"]
        self.assertEqual(validation["status"], "passed")
        self.assertEqual(validation["recipe"], "python:test")
        self.assertEqual(validation["exit_code"], 0)
        self.assertTrue(self.validation_backends[-1].closed)
        handoff = self.service.handoff(self.alice, session_id)["handoff"]
        self.assertEqual(
            handoff["validation"],
            {"status": "passed", "recipe": "python:test", "exit_code": 0},
        )
        with self.assertRaises(AgentSessionServiceError):
            self.service.run_validation(self.bob, session_id, {"recipe": "python:test"})

    def test_resume_persists_repo_context_changes_across_windows(self) -> None:
        first = {
            "version": 1,
            "git_available": True,
            "branch": "main",
            "head": "aaa",
            "worktree_digest": "clean",
            "instruction_digest": "instructions-a",
            "changed_paths": [],
            "context_changed": False,
            "changes": [],
        }
        second = {
            **first,
            "branch": "feature/continuity",
            "head": "bbb",
            "instruction_digest": "instructions-b",
        }
        self.service.agent_sessions.fingerprint_factory = lambda _workspace: dict(first)
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        self.assertFalse(created["repo_fingerprint"]["context_changed"])

        self.service.agent_sessions.fingerprint_factory = lambda _workspace: dict(second)
        changed = self.service.get_session(self.alice, created["session_id"])["session"]
        self.assertTrue(changed["repo_fingerprint"]["context_changed"])
        self.assertEqual(
            changed["repo_fingerprint"]["changes"],
            ["Branch changed", "HEAD changed", "Project instructions changed"],
        )

        reopened = self.service.get_session(self.alice, created["session_id"])["session"]
        self.assertTrue(reopened["repo_fingerprint"]["context_changed"])
        self.assertEqual(reopened["repo_fingerprint"]["head"], "bbb")

    def test_legacy_fingerprint_without_branch_does_not_report_upgrade_drift(self) -> None:
        previous = {
            "version": 1,
            "git_available": True,
            "head": "aaa",
            "worktree_digest": "clean",
            "instruction_digest": "instructions-a",
            "changed_paths": [],
            "context_changed": False,
            "changes": [],
        }
        current = {**previous, "branch": "main"}
        self.service.agent_sessions.fingerprint_factory = lambda _workspace: dict(previous)
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        self.service.agent_sessions.fingerprint_factory = lambda _workspace: dict(current)
        resumed = self.service.get_session(self.alice, created["session_id"])["session"]
        self.assertFalse(resumed["repo_fingerprint"]["context_changed"])
        self.assertEqual(resumed["repo_fingerprint"]["changes"], [])

    def test_handoff_is_deterministic_bounded_and_contains_no_workspace_root_or_backend_thread(self) -> None:
        created = self.service.create_session(
            self.alice,
            {"workspace_id": "ws-a", "backend_kind": "codex"},
        )["session"]
        session_id = created["session_id"]
        record = self.service.agent_sessions.get_session(session_id, self.alice.principal_id)
        self.service.agent_sessions.store.update_state(
            record.session_id,
            record.workspace_id,
            record.owner_principal_id,
            repo_fingerprint={
                "version": 1,
                "git_available": True,
                "branch": "feature/handoff",
                "head": "abc123",
                "worktree_digest": "digest",
                "instruction_digest": "instructions",
                "changed_paths": ["src/main.py"],
                "context_changed": True,
                "changes": ["HEAD changed"],
            },
        )
        self.backends[0].events.append(
            AgentBackendEvent(
                sequence=1,
                kind="approval",
                method="approval/requested",
                params={"reason": "write file"},
                approval_id="approval-1",
            )
        )
        self.handoff_jobs[:] = [
            {"job_id": "job-running", "status": "running"},
            {"job_id": "job-recovering", "status": "recovering"},
            {"job_id": "job-complete", "status": "completed"},
        ]

        first = self.service.handoff(self.alice, session_id)["handoff"]
        second = self.service.handoff(self.alice, session_id)["handoff"]
        self.assertEqual(first, second)
        self.assertEqual(first["workspace"]["id"], "ws-a")
        self.assertEqual(first["repository"]["branch"], "feature/handoff")
        self.assertEqual(first["repository"]["head"], "abc123")
        self.assertEqual(first["repository"]["changed_paths"], ["src/main.py"])
        self.assertEqual(
            first["active_jobs"],
            [
                {"job_id": "job-running", "status": "running"},
                {"job_id": "job-recovering", "status": "recovering"},
            ],
        )
        self.assertEqual(first["unresolved_approval"]["approval_id"], "approval-1")
        self.assertIn("resolve_approval", first["next_actions"])
        self.assertIn("review_context_changes", first["next_actions"])
        self.assertIn("wait_for_job_recovery", first["next_actions"])
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn(str(self.catalog.get("ws-a").root), serialized)
        self.assertNotIn("thread-1", serialized)


@unittest.skip("The standalone /api/app surface was removed in favor of Admin Conversation APIs.")
class OperatorHTTPAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.other_workspace = root / "other"
        self.workspace.mkdir()
        self.other_workspace.mkdir()
        self.catalog = WorkspaceCatalog(
            [
                WorkspaceEntry("ws-default", "Default", self.workspace, True, True),
                WorkspaceEntry("ws-other", "Other", self.other_workspace, True, False),
            ],
            "ws-default",
        )
        store = AgentSessionStore(root / "agent-sessions.sqlite3")
        sessions = AgentSessionService(
            store,
            self.catalog,
            lambda _workspace, _kind: FakeAgentBackend(),
        )
        self.service = OperatorAPIService(
            sessions,
            self.catalog,
            validation_backend_factory=lambda _workspace: FakeValidationBackend(),
        )
        configure_allowed_origins(())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _server(
        self,
        *,
        auth_token: str | None = "ordinary-mcp-token",
        admin_token: str = "dedicated-admin-token",
        oauth_config: OAuthConfig | None = None,
    ) -> tuple[RuntimeHTTPServer, threading.Thread]:
        runtime = Runtime(
            self.workspace,
            auth_token=auth_token,
            oauth_config=oauth_config,
            transport="http",
        )
        server = RuntimeHTTPServer(
            ("127.0.0.1", 0),
            MCPHandler,
            runtime,
            lambda _context: Runtime(
                self.workspace,
                oauth_config=oauth_config,
                transport="http",
            ),
            admin_token=admin_token,
            operator_service=self.service,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def _request(
        server: RuntimeHTTPServer,
        path: str,
        *,
        token: str | None = None,
        admin_header: str | None = None,
        cookie: str | None = None,
        csrf: str | None = None,
        origin: str | None = None,
        method: str = "GET",
        body: dict[str, object] | None = None,
    ) -> urllib.request.Request:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if admin_header is not None:
            headers["X-Admin-Token"] = admin_header
        if cookie is not None:
            headers["Cookie"] = cookie
        if csrf is not None:
            headers["X-Operator-CSRF"] = csrf
        if origin is not None:
            headers["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}{path}",
            data=data,
            headers=headers,
            method=method,
        )

    def test_operator_api_uses_ordinary_bearer_and_never_admin_header(self) -> None:
        server, thread = self._server()
        try:
            self.assertIsNotNone(server.admin_sessions)
            admin_session_id, _csrf_token = server.admin_sessions.create()
            for request in (
                self._request(server, "/api/app/workspaces"),
                self._request(
                    server,
                    "/api/app/workspaces",
                    admin_header="dedicated-admin-token",
                ),
                self._request(
                    server,
                    "/api/app/workspaces",
                    token="dedicated-admin-token",
                ),
                self._request(
                    server,
                    "/api/app/workspaces",
                    cookie=f"{ADMIN_SESSION_COOKIE}={admin_session_id}",
                ),
            ):
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(denied.exception.code, 401)

            with urllib.request.urlopen(
                self._request(
                    server,
                    "/api/app/workspaces",
                    token="ordinary-mcp-token",
                ),
                timeout=5,
            ) as response:
                payload = json.loads(response.read())
            self.assertEqual([item["id"] for item in payload["workspaces"]], ["ws-default"])
            self.assertNotIn("root", payload["workspaces"][0])

            with self.assertRaises(urllib.error.HTTPError) as bad_origin:
                urllib.request.urlopen(
                    self._request(
                        server,
                        "/api/app/workspaces",
                        token="ordinary-mcp-token",
                        origin="https://evil.example",
                    ),
                    timeout=5,
                )
            self.assertEqual(bad_origin.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_operator_browser_session_survives_refresh_and_requires_csrf_for_writes(self) -> None:
        server, thread = self._server()
        try:
            with urllib.request.urlopen(
                self._request(server, "/api/app/session"),
                timeout=5,
            ) as response:
                probe = json.loads(response.read())
            self.assertFalse(probe["authenticated"])
            self.assertFalse(probe["persistent"])

            with urllib.request.urlopen(
                self._request(
                    server,
                    "/api/app/session",
                    token="ordinary-mcp-token",
                    method="POST",
                ),
                timeout=5,
            ) as response:
                self.assertEqual(response.status, 201)
                payload = json.loads(response.read())
                set_cookie = response.headers.get("Set-Cookie", "")
            self.assertTrue(payload["persistent"])
            csrf = payload["csrf_token"]
            cookie = set_cookie.split(";", 1)[0]
            self.assertTrue(cookie.startswith(f"{OPERATOR_SESSION_COOKIE}="))
            self.assertNotIn("ordinary-mcp-token", set_cookie)

            with urllib.request.urlopen(
                self._request(server, "/api/app/workspaces", cookie=cookie),
                timeout=5,
            ) as response:
                workspaces = json.loads(response.read())["workspaces"]
            self.assertEqual([item["id"] for item in workspaces], ["ws-default"])

            with urllib.request.urlopen(
                self._request(server, "/api/app/session", cookie=cookie),
                timeout=5,
            ) as response:
                refreshed = json.loads(response.read())
            self.assertTrue(refreshed["persistent"])
            csrf = refreshed["csrf_token"]

            without_csrf = self._request(
                server,
                "/api/app/sessions",
                cookie=cookie,
                method="POST",
                body={"workspace_id": "ws-default", "backend_kind": "codex"},
            )
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(without_csrf, timeout=5)
            self.assertEqual(denied.exception.code, 403)

            with urllib.request.urlopen(
                self._request(
                    server,
                    "/api/app/sessions",
                    cookie=cookie,
                    csrf=csrf,
                    method="POST",
                    body={"workspace_id": "ws-default", "backend_kind": "codex"},
                ),
                timeout=5,
            ) as response:
                self.assertEqual(response.status, 201)

            with urllib.request.urlopen(
                self._request(
                    server,
                    "/api/app/session",
                    cookie=cookie,
                    csrf=csrf,
                    method="DELETE",
                ),
                timeout=5,
            ) as response:
                self.assertTrue(json.loads(response.read())["revoked"])

            with self.assertRaises(urllib.error.HTTPError) as expired:
                urllib.request.urlopen(
                    self._request(server, "/api/app/workspaces", cookie=cookie),
                    timeout=5,
                )
            self.assertEqual(expired.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_operator_http_session_flow_and_shared_admin_token_fail_closed(self) -> None:
        server, thread = self._server()
        try:
            create = self._request(
                server,
                "/api/app/sessions",
                token="ordinary-mcp-token",
                method="POST",
                body={
                    "workspace_id": "ws-default",
                    "backend_kind": "codex",
                    "instructions": "Keep changes focused.",
                },
            )
            with urllib.request.urlopen(create, timeout=5) as response:
                self.assertEqual(response.status, 201)
                created = json.loads(response.read())["session"]
            self.assertNotIn("backend_thread_id", created)
            session_id = created["session_id"]

            with urllib.request.urlopen(
                self._request(
                    server,
                    "/api/app/sessions?workspace_id=ws-default",
                    token="ordinary-mcp-token",
                ),
                timeout=5,
            ) as response:
                listed = json.loads(response.read())["sessions"]
            self.assertEqual([item["session_id"] for item in listed], [session_id])

            with urllib.request.urlopen(
                self._request(
                    server,
                    f"/api/app/sessions/{session_id}/turns",
                    token="ordinary-mcp-token",
                    method="POST",
                    body={"message": "hello"},
                ),
                timeout=5,
            ) as response:
                self.assertEqual(response.status, 200)

            with urllib.request.urlopen(
                self._request(
                    server,
                    f"/api/app/sessions/{session_id}/validation",
                    token="ordinary-mcp-token",
                    method="POST",
                    body={"recipe": "python:test"},
                ),
                timeout=5,
            ) as response:
                validation = json.loads(response.read())["validation"]
            self.assertEqual(validation["status"], "passed")
            self.assertEqual(validation["recipe"], "python:test")

            with self.assertRaises(urllib.error.HTTPError) as admin_denied:
                urllib.request.urlopen(
                    self._request(
                        server,
                        f"/api/app/sessions/{session_id}/validation",
                        token="dedicated-admin-token",
                        method="POST",
                        body={"recipe": "python:test"},
                    ),
                    timeout=5,
                )
            self.assertEqual(admin_denied.exception.code, 401)

            with urllib.request.urlopen(
                self._request(
                    server,
                    f"/api/app/sessions/{session_id}/handoff",
                    token="ordinary-mcp-token",
                ),
                timeout=5,
            ) as response:
                handoff = json.loads(response.read())["handoff"]
            self.assertEqual(handoff["agent_session"]["session_id"], session_id)
            self.assertEqual(
                handoff["validation"],
                {"status": "passed", "recipe": "python:test", "exit_code": 0},
            )
            self.assertNotIn("root", handoff["workspace"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        shared_service = OperatorAPIService(
            AgentSessionService(
                AgentSessionStore(Path(self.tmp.name) / "shared-agent-sessions.sqlite3"),
                self.catalog,
                lambda _workspace, _kind: FakeAgentBackend(),
            ),
            self.catalog,
        )
        self.service = shared_service
        shared_server, shared_thread = self._server(
            auth_token="shared-token",
            admin_token="shared-token",
        )
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(
                    self._request(
                        shared_server,
                        "/api/app/workspaces",
                        token="shared-token",
                    ),
                    timeout=5,
                )
            self.assertEqual(denied.exception.code, 401)
        finally:
            shared_server.shutdown()
            shared_server.server_close()
            shared_thread.join(timeout=5)

    def test_oauth_identity_cannot_use_shared_admin_bearer_and_is_workspace_scoped(self) -> None:
        oauth_config = OAuthConfig(
            password="synthetic-oauth-password",
            server_url=None,
            token_secret=b"synthetic-oauth-secret",
        )
        server, thread = self._server(
            auth_token=None,
            admin_token="oauth-admin-token",
            oauth_config=oauth_config,
        )
        identity = OAuthIdentity(
            client_id="oauth-client",
            grant_id="grant-1",
            workspace_id="ws-other",
            jti="jti-1",
        )
        try:
            with patch(
                "coding_tools_mcp.server.authenticate_access_token",
                return_value=identity,
            ):
                with self.assertRaises(urllib.error.HTTPError) as collision:
                    urllib.request.urlopen(
                        self._request(
                            server,
                            "/api/app/workspaces",
                            token="oauth-admin-token",
                        ),
                        timeout=5,
                    )
                self.assertEqual(collision.exception.code, 401)

                with urllib.request.urlopen(
                    self._request(
                        server,
                        "/api/app/workspaces",
                        token="ordinary-oauth-token",
                    ),
                    timeout=5,
                ) as response:
                    payload = json.loads(response.read())
            self.assertEqual([item["id"] for item in payload["workspaces"]], ["ws-other"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
