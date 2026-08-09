from __future__ import annotations

import http.client
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from coding_tools_mcp.agent_backends.base import (
    AgentBackendError,
    AgentBackendEvent,
    BackendHealth,
    BackendThread,
    BackendTurn,
)
from coding_tools_mcp.agent_session_store import AgentSessionStore
from coding_tools_mcp.agent_sessions import AgentSessionService
from coding_tools_mcp.oauth import OAuthIdentity
from coding_tools_mcp.protocol import dispatch_rpc
from coding_tools_mcp.runner.capabilities import RunnerCapabilityError, RunnerCapabilityHost
from coding_tools_mcp.runner.routing import (
    RemoteMcpRouteService,
    RemoteMcpRouteStore,
    RunnerMcpRouter,
    RunnerMcpSessionHost,
)
from coding_tools_mcp.runner.transport import RunnerUnavailableError
from coding_tools_mcp.server import (
    AuthorizationContext,
    BoundRuntimeFactory,
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    build_parser,
    runtime_policy_from_args,
)
from coding_tools_mcp.transport_http import HTTPSessionManager
from coding_tools_mcp.validation import ValidationResult
from coding_tools_mcp.workspace_binding import WorkspaceBindingResolver
from coding_tools_mcp.workspace_catalog import WorkspaceCatalog, WorkspaceEntry
from coding_tools_mcp.workspace_host import WorkspaceHostFactory


class FakeRuntime:
    def __init__(self, session_id: str = "unused") -> None:
        self.http_session_id = session_id
        self.protocol_version = "2025-11-25"
        self.closed = False
        self.cancelled: list[str | int] = []

    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "clientInfoEcho": client_info or {},
        }

    def list_tools(self) -> dict[str, Any]:
        return {"tools": [{"name": "read_file", "annotations": {"readOnlyHint": True}}]}

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

    def cancel_request(self, request_id: str | int) -> None:
        self.cancelled.append(request_id)

    def close(self) -> None:
        self.closed = True


class FakeAgentBackend:
    backend_kind = "codex-app-server"

    def __init__(self) -> None:
        self.events: list[AgentBackendEvent] = []
        self.approvals: list[tuple[str, str]] = []
        self.closed = False

    def health(self) -> BackendHealth:
        return BackendHealth(True, self.backend_kind, version="fake-1")

    def create_thread(self, *, instructions: str | None = None) -> BackendThread:
        return BackendThread("thread-1", {"instructions": instructions})

    def resume_thread(self, thread_id: str, *, instructions: str | None = None) -> BackendThread:
        return BackendThread(thread_id, {"instructions": instructions, "resumed": True})

    def send_turn(self, thread_id: str, message: str) -> BackendTurn:
        self.events.append(AgentBackendEvent(1, "assistant", "text", {"text": message}))
        return BackendTurn("turn-1", {"thread_id": thread_id})

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        return None

    def approve(self, approval_id: str, decision: str) -> None:
        self.approvals.append((approval_id, decision))

    def list_threads(self, *, limit: int = 50) -> list[BackendThread]:
        return [BackendThread("thread-1", {})][:limit]

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
        self.closed = True


class FakeSemanticBackend:
    def semantic_status(self, path: str | None = None) -> dict[str, Any]:
        return {"ok": True, "status": "ready", "path": path, "backend": "fake-remote"}

    def document_symbols(self, path: str) -> dict[str, Any]:
        return {"ok": True, "items": [{"name": "RemoteSymbol", "path": path}]}

    def goto_definition(self, path: str, line: int, character: int) -> dict[str, Any]:
        return {"ok": True, "items": [{"path": path, "line": line, "character": character}]}

    def find_references(self, path: str, line: int, character: int) -> dict[str, Any]:
        return {"ok": True, "items": [{"path": path, "line": line, "character": character}]}

    def document_diagnostics(self, path: str) -> dict[str, Any]:
        return {"ok": True, "items": [], "path": path}

    def close(self) -> None:
        return None


class FakeValidationBackend:
    def status(self) -> dict[str, Any]:
        return {"ok": True, "status": "ready", "recipes": ["python.syntax"]}

    def run(self, recipe: str) -> ValidationResult:
        return ValidationResult("passed", recipe, command="python -m py_compile", exit_code=0)

    def close(self) -> None:
        return None


class FakeRunnerTransport:
    def __init__(self, router: RunnerMcpRouter) -> None:
        self.router = router
        self.available = True

    @property
    def runner_id(self) -> str:
        return "runner-a"

    @property
    def workspace_ids(self) -> tuple[str, ...]:
        return ("ws-remote", "ws-other")

    def _call(self, workspace_id: str, method: str, params: dict[str, Any] | None) -> Any:
        if not self.available:
            raise RunnerUnavailableError("synthetic disconnect")
        if workspace_id not in self.workspace_ids:
            raise RunnerUnavailableError("workspace is not advertised")
        return self.router.dispatch(workspace_id, method, dict(params or {}))

    async def call(self, *, workspace_id: str, method: str, params: dict[str, Any] | None = None, request_id: str | None = None) -> Any:
        del request_id
        return self._call(workspace_id, method, params)

    def call_sync(self, *, workspace_id: str, method: str, params: dict[str, Any] | None = None, request_id: str | None = None) -> Any:
        del request_id
        return self._call(workspace_id, method, params)


class RunnerCapabilityRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.agent = FakeAgentBackend()
        self.remote_fingerprint = {
            "version": 1,
            "git_available": True,
            "head": "aaa",
            "worktree_digest": "clean",
            "instruction_digest": "instructions-a",
            "changed_paths": [],
            "context_changed": False,
            "changes": [],
        }
        self.capabilities = RunnerCapabilityHost(
            agent_backend_factory=lambda workspace_id, kind: self.agent,
            semantic_backend_factory=lambda workspace_id: FakeSemanticBackend(),
            validation_backend_factory=lambda workspace_id: FakeValidationBackend(),
            fingerprint_factory=lambda workspace_id: dict(self.remote_fingerprint),
        )
        self.remote_runtimes: list[FakeRuntime] = []

        def runtime_factory(workspace_id: str, auth: str) -> FakeRuntime:
            del workspace_id, auth
            runtime = FakeRuntime(f"remote-{len(self.remote_runtimes) + 1}")
            self.remote_runtimes.append(runtime)
            return runtime

        sessions = RunnerMcpSessionHost(runtime_factory)
        self.sessions = sessions
        self.router = RunnerMcpRouter(sessions, self.capabilities.dispatch)
        self.transport = FakeRunnerTransport(self.router)
        self.service = RemoteMcpRouteService(RemoteMcpRouteStore())
        await self.service.attach_transport(self.transport)
        self.remote = WorkspaceEntry(
            "ws-remote",
            "Remote",
            r"G:\\repo",
            True,
            False,
            target="runner",
            runner_id="runner-a",
        )

    async def asyncTearDown(self) -> None:
        self.capabilities.shutdown()
        self.sessions.shutdown()

    def test_remote_agent_lifecycle_runs_on_runner_capability_host(self) -> None:
        host = WorkspaceHostFactory(remote_route_service=self.service).create(self.remote)
        backend = host.get_agent_backend()
        self.assertTrue(backend.health().available)
        thread = backend.create_thread(instructions="remote")
        self.assertEqual(thread.thread_id, "thread-1")
        turn = backend.send_turn(thread.thread_id, "hello from remote")
        self.assertEqual(turn.turn_id, "turn-1")
        events = backend.drain_events()
        self.assertEqual(events[0].params["text"], "hello from remote")
        backend.approve("approval-1", "accept")
        self.assertEqual(self.agent.approvals, [("approval-1", "accept")])
        host.close()
        self.assertTrue(self.agent.closed)

    def test_remote_semantic_and_validation_are_routed_without_remote_paths(self) -> None:
        host = WorkspaceHostFactory(remote_route_service=self.service).create(self.remote)
        semantic = host.get_semantic_backend()
        symbols = semantic.document_symbols("src/main.py")
        self.assertEqual(symbols["items"][0]["name"], "RemoteSymbol")
        validation = host.get_validation_backend()
        result = validation.run("python.syntax")
        self.assertEqual(result.status, "passed")
        self.assertEqual(host.resolve_workspace_handle().root, r"G:\\repo")
        host.close()

    def test_remote_workspace_fingerprint_is_bounded_metadata_only(self) -> None:
        response = self.service.call_runner_sync(
            runner_id="runner-a",
            workspace_id="ws-remote",
            method="workspace.fingerprint",
            params={},
        )
        fingerprint = response["fingerprint"]
        self.assertEqual(fingerprint["head"], "aaa")
        self.assertTrue(fingerprint["remote"])
        self.assertNotIn("root", fingerprint)
        self.assertNotIn("content", fingerprint)

    def test_remote_agent_resume_detects_runner_repo_context_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            catalog = WorkspaceCatalog([self.remote], "ws-remote")
            hosts = WorkspaceHostFactory(remote_route_service=self.service)

            def backend_factory(workspace: WorkspaceEntry, kind: str):
                return hosts.create(workspace).get_agent_backend(kind)

            def fingerprint_factory(workspace: WorkspaceEntry) -> dict[str, Any]:
                response = self.service.call_runner_sync(
                    runner_id=workspace.runner_id or "",
                    workspace_id=workspace.id,
                    method="workspace.fingerprint",
                    params={},
                )
                return dict(response["fingerprint"])

            service = AgentSessionService(
                AgentSessionStore(Path(tmp) / "agent-sessions.sqlite3"),
                catalog,
                backend_factory,
                fingerprint_factory=fingerprint_factory,
            )
            try:
                created = service.create_session(
                    workspace_id="ws-remote",
                    owner_principal_id="owner-a",
                    backend_kind="codex-app-server",
                )
                self.assertEqual(created.repo_fingerprint["head"], "aaa")
                self.remote_fingerprint["head"] = "bbb"
                self.remote_fingerprint["instruction_digest"] = "instructions-b"
                resumed = service.resume_session(created.session_id, "owner-a")
                self.assertTrue(resumed.repo_fingerprint["context_changed"])
                self.assertEqual(
                    resumed.repo_fingerprint["changes"],
                    ["HEAD changed", "Project instructions changed"],
                )
            finally:
                service.close()

    def test_capability_handle_cannot_cross_workspace(self) -> None:
        opened = self.capabilities.dispatch(
            "ws-remote",
            "capability.open",
            {"kind": "semantic"},
        )
        with self.assertRaises(RunnerCapabilityError) as caught:
            self.capabilities.dispatch(
                "ws-other",
                "capability.call",
                {
                    "handle": opened["handle"],
                    "kind": "semantic",
                    "operation": "semantic_status",
                    "arguments": {},
                },
            )
        self.assertEqual(caught.exception.code, "RUNNER_CAPABILITY_FORBIDDEN")

    def test_offline_runner_is_retryable_and_never_falls_back_local(self) -> None:
        self.transport.available = False
        self.service.detach_transport("runner-a")
        host = WorkspaceHostFactory(remote_route_service=self.service).create(self.remote)
        with self.assertRaises(AgentBackendError) as caught:
            host.get_agent_backend()
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.code, "RUNNER_UNAVAILABLE")

    def test_remote_mcp_http_runtime_preserves_session_auth_and_closes_original_runner(self) -> None:
        with TemporaryDirectory() as tmp:
            local = Path(tmp)
            catalog = WorkspaceCatalog(
                [
                    WorkspaceEntry("ws-local", "Local", local, True, True),
                    self.remote,
                ],
                "ws-local",
            )
            resolver = WorkspaceBindingResolver(catalog)
            args = build_parser().parse_args([])
            context = AuthorizationContext(
                "oauth",
                OAuthIdentity("client-a", "grant-a", "ws-remote", "jti-a"),
            )
            factory = BoundRuntimeFactory(
                args,
                runtime_policy_from_args(args),
                resolver,
                auth_token=None,
                oauth_config=None,
                runner_route_service=self.service,
            )
            sessions = HTTPSessionManager(factory)
            runtime = sessions.create(context)
            self.assertEqual(
                runtime.session_authorization_key(),
                context.authorization_key("ws-remote"),
            )

            initialized = dispatch_rpc(
                runtime,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "remote-test", "version": "1"},
                    },
                },
            )
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
            listed = dispatch_rpc(
                runtime,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            self.assertEqual(listed["result"]["tools"][0]["name"], "read_file")
            called = dispatch_rpc(
                runtime,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "read_file",
                        "arguments": {"path": "README.md"},
                    },
                },
            )
            self.assertEqual(
                called["result"]["structuredContent"]["arguments"]["path"],
                "README.md",
            )
            session_id = runtime.http_session_id
            self.assertTrue(sessions.delete(session_id))
            self.assertTrue(self.remote_runtimes[-1].closed)
            self.assertEqual(self.service.store.snapshot()["states"]["closed"], 1)

    def test_streamable_http_routes_remote_default_and_reports_offline_retryable(self) -> None:
        with TemporaryDirectory() as tmp:
            local = Path(tmp)
            catalog = WorkspaceCatalog([self.remote], "ws-remote")
            resolver = WorkspaceBindingResolver(catalog)
            args = build_parser().parse_args([])
            factory = BoundRuntimeFactory(
                args,
                runtime_policy_from_args(args),
                resolver,
                auth_token="ordinary-token",
                oauth_config=None,
                runner_route_service=self.service,
            )
            control = Runtime(local, auth_token="ordinary-token", transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                control,
                factory,
                runner_route_service=self.service,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(
                method: str,
                body: dict[str, Any] | None = None,
                *,
                session_id: str | None = None,
            ) -> tuple[int, dict[str, str], dict[str, Any]]:
                headers = {"Authorization": "Bearer ordinary-token"}
                data: bytes | None = None
                if body is not None:
                    data = json.dumps(body).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                    headers["MCP-Protocol-Version"] = "2025-11-25"
                if session_id is not None:
                    headers["Mcp-Session-Id"] = session_id
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_address[1],
                    timeout=5,
                )
                try:
                    connection.request(method, "/mcp", body=data, headers=headers)
                    response = connection.getresponse()
                    raw = response.read()
                    response_headers = {key.lower(): value for key, value in response.getheaders()}
                    return response.status, response_headers, json.loads(raw) if raw else {}
                finally:
                    connection.close()

            try:
                status, headers, initialized = request(
                    "POST",
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "http-remote", "version": "1"},
                        },
                    },
                )
                self.assertEqual(status, 200, initialized)
                session_id = headers["mcp-session-id"]
                self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")

                status, _headers, listed = request(
                    "POST",
                    {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
                    session_id=session_id,
                )
                self.assertEqual(status, 200, listed)
                self.assertEqual(listed["result"]["tools"][0]["name"], "read_file")

                self.transport.available = False
                self.service.detach_transport("runner-a")
                status, _headers, unavailable = request(
                    "POST",
                    {"jsonrpc": "2.0", "id": 12, "method": "tools/list", "params": {}},
                    session_id=session_id,
                )
                self.assertEqual(status, 200, unavailable)
                self.assertEqual(unavailable["error"]["code"], -32003)
                self.assertTrue(unavailable["error"]["data"]["retryable"])

                status, _headers, new_session = request(
                    "POST",
                    {
                        "jsonrpc": "2.0",
                        "id": 13,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
                    },
                )
                self.assertEqual(status, 503, new_session)
                self.assertEqual(new_session["error"]["code"], -32003)
                self.assertTrue(new_session["error"]["data"]["retryable"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
