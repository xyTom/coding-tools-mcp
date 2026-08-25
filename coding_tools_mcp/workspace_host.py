"""Coarse Workspace Data Plane boundary for local and remote execution hosts."""

from __future__ import annotations

import inspect
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .agent_backends import AgentSessionBackend, CodexAppServerBackend, CodexAppServerConfig
from .protocol import PROTOCOL_VERSION
from .runner.capabilities import (
    RemoteAgentBackendProxy,
    RemoteSemanticBackendProxy,
    RemoteValidationBackendProxy,
)
from .runner.jobs import JobAccessError, JobInventoryItem, WorkspaceJobManager
from .semantic import LspSemanticBackend, SemanticBackend
from .validation import NullValidationBackend, StructuredValidationBackend, ValidationBackend
from .workspace_catalog import WorkspaceEntry


class WorkspaceHostError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class WorkspaceHandle:
    workspace_id: str
    name: str
    target: str
    root: str
    runner_id: str | None = None

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "target": self.target,
            "root": self.root,
        }
        if self.runner_id is not None:
            result["runner_id"] = self.runner_id
        return result


@runtime_checkable
class WorkspaceHost(Protocol):
    """Create or access capabilities in one Workspace Data Plane.

    Remote roots are deliberately represented as opaque strings.  This
    interface exposes capabilities, not remote filesystem primitives.
    """

    @property
    def workspace_id(self) -> str: ...

    def resolve_workspace_handle(self) -> WorkspaceHandle: ...

    async def create_mcp_runtime(
        self,
        *,
        control_session_id: str | None = None,
        authorization_key: str | None = None,
        runtime_options: Mapping[str, Any] | None = None,
    ) -> Any: ...

    def get_agent_backend(self, backend_kind: str = "codex-app-server") -> AgentSessionBackend: ...

    def get_semantic_backend(self) -> SemanticBackend: ...

    def get_validation_backend(self) -> ValidationBackend: ...

    def get_job_manager(self) -> Any: ...

    def snapshot_status(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class LocalWorkspaceHost:
    """Local Data Plane adapter preserving existing Runtime/backend behavior."""

    def __init__(
        self,
        workspace: WorkspaceEntry,
        *,
        runtime_factory: Callable[[WorkspaceEntry, Mapping[str, Any]], Any] | None = None,
        agent_backend_factory: Callable[[WorkspaceEntry, str], AgentSessionBackend] | None = None,
        semantic_backend_factory: Callable[[WorkspaceEntry], SemanticBackend] | None = None,
        validation_backend_factory: Callable[[WorkspaceEntry], ValidationBackend] | None = None,
        job_manager: Any = None,
    ) -> None:
        self.workspace = workspace
        self._runtime_factory = runtime_factory or self._default_runtime_factory
        self._agent_backend_factory = agent_backend_factory or self._default_agent_backend_factory
        self._semantic_backend_factory = semantic_backend_factory or self._default_semantic_backend_factory
        self._validation_backend_factory = validation_backend_factory
        self._job_manager = job_manager
        self._owned: list[Any] = []

    @property
    def workspace_id(self) -> str:
        return self.workspace.id

    def resolve_workspace_handle(self) -> WorkspaceHandle:
        return WorkspaceHandle(
            workspace_id=self.workspace.id,
            name=self.workspace.name,
            target="local",
            root=str(self.workspace.root),
        )

    async def create_mcp_runtime(
        self,
        *,
        control_session_id: str | None = None,
        authorization_key: str | None = None,
        runtime_options: Mapping[str, Any] | None = None,
    ) -> Any:
        del control_session_id, authorization_key
        runtime = self._runtime_factory(self.workspace, dict(runtime_options or {}))
        self._owned.append(runtime)
        return runtime

    def get_agent_backend(self, backend_kind: str = "codex-app-server") -> AgentSessionBackend:
        backend = self._agent_backend_factory(self.workspace, backend_kind)
        self._owned.append(backend)
        return backend

    def get_semantic_backend(self) -> SemanticBackend:
        backend = self._semantic_backend_factory(self.workspace)
        self._owned.append(backend)
        return backend

    def get_validation_backend(self) -> ValidationBackend:
        if self._validation_backend_factory is not None:
            backend = self._validation_backend_factory(self.workspace)
        else:
            if self.workspace.target != "local":
                backend = NullValidationBackend("Local validation requires a local Workspace.")
            else:
                try:
                    runtime = self._runtime_factory(self.workspace, {"transport": "stdio"})
                    executor = getattr(runtime, "exec_command", None)
                    if not callable(executor):
                        close = getattr(runtime, "close", None)
                        if callable(close):
                            close()
                        backend = NullValidationBackend(
                            "Workspace Runtime does not expose exec_command validation execution."
                        )
                    else:
                        backend = StructuredValidationBackend(
                            self.workspace.root,
                            executor,
                            close_callback=runtime.close,
                        )
                except Exception as exc:  # noqa: BLE001 - capability discovery fails closed
                    backend = NullValidationBackend(
                        f"Structured validation is unavailable: {str(exc)[:200]}"
                    )
        self._owned.append(backend)
        return backend

    def get_job_manager(self) -> Any:
        return self._job_manager

    def snapshot_status(self) -> dict[str, Any]:
        return {
            "workspace": self.resolve_workspace_handle().payload(),
            "connected": True,
            "capabilities": {
                "mcp": True,
                "agent": True,
                "semantic": True,
                "validation": True,
                "jobs": self._job_manager is not None,
            },
        }

    def close(self) -> None:
        owned, self._owned = self._owned, []
        first_error: WorkspaceHostError | None = None
        for capability in reversed(owned):
            try:
                _close_capability_sync(capability)
            except WorkspaceHostError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _default_runtime_factory(workspace: WorkspaceEntry, options: Mapping[str, Any]) -> Any:
        from .server import Runtime
        from .workspace_binding import WorkspaceBinding

        kwargs = dict(options)
        transport = str(kwargs.get("transport", "stdio"))
        kwargs.setdefault(
            "workspace_binding",
            WorkspaceBinding(workspace.id, workspace.root, transport),
        )
        return Runtime(workspace.root, **kwargs)

    @staticmethod
    def _default_agent_backend_factory(
        workspace: WorkspaceEntry,
        backend_kind: str,
    ) -> AgentSessionBackend:
        if backend_kind != "codex-app-server":
            raise WorkspaceHostError(
                "AGENT_BACKEND_UNAVAILABLE",
                f"Unsupported local agent backend: {backend_kind}",
            )
        return CodexAppServerBackend(CodexAppServerConfig.create(workspace.root))

    @staticmethod
    def _default_semantic_backend_factory(workspace: WorkspaceEntry) -> SemanticBackend:
        return LspSemanticBackend(workspace.root)


class RemoteMcpRuntimeProxy:
    """One remote MCP Runtime route with original-Runner affinity."""

    def __init__(
        self,
        route_service: Any,
        *,
        control_session_id: str,
        runner_id: str,
        workspace_id: str,
        authorization_key: str,
    ) -> None:
        self.route_service = route_service
        self.control_session_id = control_session_id
        self.runner_id = runner_id
        self.workspace_id = workspace_id
        self.authorization_key = authorization_key
        self.closed = False

    async def initialize(self, client_info: dict[str, Any] | None = None) -> Any:
        return await self._call("initialize", {"clientInfo": client_info or {}})

    async def list_tools(self) -> Any:
        return await self._call("tools/list", {})

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        request_id: str | int | None = None,
    ) -> Any:
        return await self._call(
            "tools/call",
            {"name": name, "arguments": arguments or {}, "request_id": request_id},
        )

    async def close(self) -> Any:
        if self.closed:
            return {"status": "closed", "retryable": False}
        result = await self.route_service.close_session(
            self.control_session_id,
            authorization_key=self.authorization_key,
            workspace_id=self.workspace_id,
            expected_runner_id=self.runner_id,
        )
        self.closed = getattr(result, "status", None) == "closed"
        return result

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        if self.closed:
            raise WorkspaceHostError("RUNNER_ROUTE_NOT_AVAILABLE", "remote MCP route is closed")
        response = await self.route_service.call_session(
            self.control_session_id,
            authorization_key=self.authorization_key,
            workspace_id=self.workspace_id,
            method=method,
            params=params,
        )
        if isinstance(response, Mapping) and set(response) == {"result"}:
            return response["result"]
        return response


@dataclass(frozen=True)
class _RemoteRuntimeBinding:
    workspace_id: str


class RemoteMcpHttpRuntimeProxy:
    """Synchronous Runtime-shaped proxy used by Streamable HTTP sessions."""

    def __init__(
        self,
        route_service: Any,
        *,
        runner_id: str,
        workspace_id: str,
        authorization_key: str,
        session_authorization_key: tuple[str, str | None, str | None, str],
        job_manager: WorkspaceJobManager | None = None,
        owner_principal_id: str | None = None,
    ) -> None:
        self.route_service = route_service
        self.runner_id = runner_id
        self.workspace_binding = _RemoteRuntimeBinding(workspace_id)
        self.authorization_key = authorization_key
        self._session_authorization_key = session_authorization_key
        self._job_manager = job_manager
        self._owner_principal_id = owner_principal_id
        self.http_session_id = secrets.token_urlsafe(24)
        self.protocol_version = PROTOCOL_VERSION
        self.initialized = False
        self.closed = False
        self.route_service.create_session_sync(
            control_session_id=self.http_session_id,
            runner_id=self.runner_id,
            workspace_id=workspace_id,
            authorization_key=self.authorization_key,
        )

    def session_authorization_key(self) -> tuple[str, str | None, str | None, str]:
        return self._session_authorization_key

    def initialize(self, client_info: dict[str, Any] | None = None) -> Any:
        return self._call(
            "initialize",
            {"clientInfo": client_info or {}, "protocolVersion": self.protocol_version},
        )

    def list_tools(self) -> Any:
        return self._call("tools/list", {})

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        request_id: str | int | None = None,
    ) -> Any:
        return self._call(
            "tools/call",
            {"name": name, "arguments": arguments or {}, "request_id": request_id},
        )

    def cancel_request(self, request_id: str | int) -> None:
        self._call("notifications/cancelled", {"requestId": request_id})

    def close(self) -> None:
        if self.closed:
            return
        result = self.route_service.close_session_sync(
            self.http_session_id,
            authorization_key=self.authorization_key,
            workspace_id=self.workspace_binding.workspace_id,
            expected_runner_id=self.runner_id,
        )
        self.closed = getattr(result, "status", None) in {"closed", "lost"}

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        if self.closed:
            raise WorkspaceHostError("RUNNER_ROUTE_NOT_AVAILABLE", "remote MCP route is closed", retryable=True)
        response = self.route_service.call_session_sync(
            self.http_session_id,
            authorization_key=self.authorization_key,
            workspace_id=self.workspace_binding.workspace_id,
            method=method,
            params=params,
        )
        if isinstance(response, Mapping) and "result" in response:
            self._observe_job(response.get("job"))
            return response["result"]
        return response

    def _observe_job(self, raw: Any) -> None:
        manager = self._job_manager
        owner = self._owner_principal_id
        if manager is None or owner is None or not isinstance(raw, Mapping):
            return
        try:
            item = JobInventoryItem.from_payload(dict(raw))
            status = self.route_service.runner_status(self.runner_id)
            instance_id = status.get("instance_id") if isinstance(status, Mapping) else None
            manager.register_or_observe(
                owner,
                item,
                runner_instance_id=instance_id if isinstance(instance_id, str) else None,
            )
        except (JobAccessError, ValueError, TypeError):
            # The remote tool already executed. Tracking metadata must never
            # turn it into a retryable tool failure or trigger a replay.
            return


class RemoteRunnerWorkspaceHost:
    """Control-plane host for one Runner-advertised remote Workspace."""

    def __init__(
        self,
        *,
        runner_id: str,
        workspace_id: str,
        name: str,
        remote_root: str,
        route_service: Any,
        runner_status: Callable[[], Mapping[str, Any]] | None = None,
        agent_backend_factory: Callable[[str, str], AgentSessionBackend] | None = None,
        semantic_backend_factory: Callable[[str, str], SemanticBackend] | None = None,
        validation_backend_factory: Callable[[str, str], ValidationBackend] | None = None,
        job_manager: Any = None,
    ) -> None:
        if not all(isinstance(value, str) and value for value in (runner_id, workspace_id, name, remote_root)):
            raise ValueError("remote workspace identity fields must be non-empty strings")
        self.runner_id = runner_id
        self._workspace_id = workspace_id
        self.name = name
        # Deliberately opaque: never convert this to pathlib.Path on the Control Plane.
        self.remote_root = remote_root
        self.route_service = route_service
        self._runner_status = runner_status
        self._agent_backend_factory = agent_backend_factory
        self._semantic_backend_factory = semantic_backend_factory
        self._validation_backend_factory = validation_backend_factory
        self._job_manager = job_manager
        self._owned: list[Any] = []

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def resolve_workspace_handle(self) -> WorkspaceHandle:
        return WorkspaceHandle(
            workspace_id=self.workspace_id,
            name=self.name,
            target="runner",
            root=self.remote_root,
            runner_id=self.runner_id,
        )

    async def create_mcp_runtime(
        self,
        *,
        control_session_id: str | None = None,
        authorization_key: str | None = None,
        runtime_options: Mapping[str, Any] | None = None,
    ) -> RemoteMcpRuntimeProxy:
        if runtime_options:
            raise WorkspaceHostError(
                "RUNNER_RUNTIME_OPTIONS_UNSUPPORTED",
                "Remote MCP Runtime options are owned by the Runner Data Plane.",
            )
        if not control_session_id or not authorization_key:
            raise WorkspaceHostError(
                "RUNNER_AUTH_CONTEXT_INVALID",
                "Remote MCP Runtime requires session and authorization context.",
            )
        await self.route_service.create_session(
            control_session_id=control_session_id,
            runner_id=self.runner_id,
            workspace_id=self.workspace_id,
            authorization_key=authorization_key,
        )
        return RemoteMcpRuntimeProxy(
            self.route_service,
            control_session_id=control_session_id,
            runner_id=self.runner_id,
            workspace_id=self.workspace_id,
            authorization_key=authorization_key,
        )

    def get_agent_backend(self, backend_kind: str = "codex-app-server") -> AgentSessionBackend:
        if self._agent_backend_factory is None:
            raise WorkspaceHostError(
                "AGENT_BACKEND_UNAVAILABLE",
                "Remote agent backend routing is not configured.",
                retryable=True,
            )
        backend = self._agent_backend_factory(self.workspace_id, backend_kind)
        self._owned.append(backend)
        return backend

    def get_semantic_backend(self) -> SemanticBackend:
        if self._semantic_backend_factory is None:
            raise WorkspaceHostError(
                "SEMANTIC_UNAVAILABLE",
                "Remote semantic backend routing is not configured.",
                retryable=True,
            )
        backend = self._semantic_backend_factory(self.workspace_id, self.runner_id)
        self._owned.append(backend)
        return backend

    def get_validation_backend(self) -> ValidationBackend:
        if self._validation_backend_factory is None:
            return NullValidationBackend("Remote validation backend routing is not configured.")
        backend = self._validation_backend_factory(self.workspace_id, self.runner_id)
        self._owned.append(backend)
        return backend

    def get_job_manager(self) -> Any:
        return self._job_manager

    def snapshot_status(self) -> dict[str, Any]:
        raw = dict(self._runner_status() if self._runner_status is not None else {})
        connected = bool(raw.get("connected", False))
        return {
            "workspace": self.resolve_workspace_handle().payload(),
            "connected": connected,
            "runner": {
                "runner_id": self.runner_id,
                "instance_id": raw.get("instance_id"),
                "last_seen": raw.get("last_seen"),
            },
            "capabilities": {
                "mcp": True,
                "agent": self._agent_backend_factory is not None,
                "semantic": self._semantic_backend_factory is not None,
                "validation": self._validation_backend_factory is not None,
                "jobs": self._job_manager is not None,
            },
        }

    def close(self) -> None:
        owned, self._owned = self._owned, []
        first_error: WorkspaceHostError | None = None
        for capability in reversed(owned):
            try:
                _close_capability_sync(capability)
            except WorkspaceHostError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def _close_capability_sync(capability: Any) -> None:
    close_sync = getattr(capability, "close_sync", None)
    if callable(close_sync):
        close_sync()
        return
    close = getattr(capability, "close", None)
    if not callable(close):
        return
    if inspect.iscoroutinefunction(close):
        raise WorkspaceHostError(
            "ASYNC_CAPABILITY_CLOSE_UNSUPPORTED",
            "WorkspaceHost.close() requires a synchronous close or close_sync boundary.",
        )
    result = close()
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise WorkspaceHostError(
            "ASYNC_CAPABILITY_CLOSE_UNSUPPORTED",
            "WorkspaceHost.close() received an awaitable close result without close_sync.",
        )


class WorkspaceHostFactory:
    """Select the Data Plane host for one catalog entry.

    The factory deliberately refuses to synthesize a local host for a Runner
    workspace. Remote roots remain opaque catalog data until an authenticated
    Runner route service is explicitly injected.
    """

    def __init__(
        self,
        *,
        runtime_factory: Callable[[WorkspaceEntry, Mapping[str, Any]], Any] | None = None,
        local_agent_backend_factory: Callable[[WorkspaceEntry, str], AgentSessionBackend] | None = None,
        local_semantic_backend_factory: Callable[[WorkspaceEntry], SemanticBackend] | None = None,
        local_validation_backend_factory: Callable[[WorkspaceEntry], ValidationBackend] | None = None,
        local_job_manager_factory: Callable[[WorkspaceEntry], Any] | None = None,
        remote_route_service: Any = None,
        remote_runner_status: Callable[[str], Mapping[str, Any]] | None = None,
        remote_agent_backend_factory: Callable[[str, str, str], AgentSessionBackend] | None = None,
        remote_semantic_backend_factory: Callable[[str, str], SemanticBackend] | None = None,
        remote_validation_backend_factory: Callable[[str, str], ValidationBackend] | None = None,
        remote_job_manager_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._local_agent_backend_factory = local_agent_backend_factory
        self._local_semantic_backend_factory = local_semantic_backend_factory
        self._local_validation_backend_factory = local_validation_backend_factory
        self._local_job_manager_factory = local_job_manager_factory
        self._remote_route_service = remote_route_service
        self._remote_runner_status = remote_runner_status
        self._remote_agent_backend_factory = remote_agent_backend_factory
        self._remote_semantic_backend_factory = remote_semantic_backend_factory
        self._remote_validation_backend_factory = remote_validation_backend_factory
        if remote_route_service is not None:
            if self._remote_agent_backend_factory is None:
                self._remote_agent_backend_factory = (
                    lambda runner_id, workspace_id, backend_kind: RemoteAgentBackendProxy(
                        remote_route_service,
                        runner_id,
                        workspace_id,
                        backend_kind,
                    )
                )
            if self._remote_semantic_backend_factory is None:
                self._remote_semantic_backend_factory = (
                    lambda workspace_id, runner_id: RemoteSemanticBackendProxy(
                        remote_route_service,
                        runner_id,
                        workspace_id,
                    )
                )
            if self._remote_validation_backend_factory is None:
                self._remote_validation_backend_factory = (
                    lambda workspace_id, runner_id: RemoteValidationBackendProxy(
                        remote_route_service,
                        runner_id,
                        workspace_id,
                    )
                )
        self._remote_job_manager_factory = remote_job_manager_factory

    def create(self, workspace: WorkspaceEntry) -> WorkspaceHost:
        if workspace.target == "local":
            return LocalWorkspaceHost(
                workspace,
                runtime_factory=self._runtime_factory,
                agent_backend_factory=self._local_agent_backend_factory,
                semantic_backend_factory=self._local_semantic_backend_factory,
                validation_backend_factory=self._local_validation_backend_factory,
                job_manager=(
                    self._local_job_manager_factory(workspace)
                    if self._local_job_manager_factory is not None
                    else None
                ),
            )
        if workspace.target != "runner" or not workspace.runner_id:
            raise WorkspaceHostError(
                "WORKSPACE_TARGET_INVALID",
                "Workspace target is not routable.",
            )
        if self._remote_route_service is None:
            raise WorkspaceHostError(
                "RUNNER_ROUTE_NOT_AVAILABLE",
                "Remote Runner routing is not configured.",
                retryable=True,
            )
        runner_id = workspace.runner_id
        remote_root = str(workspace.root)
        remote_runner_status = self._remote_runner_status
        remote_agent_backend_factory = self._remote_agent_backend_factory
        return RemoteRunnerWorkspaceHost(
            runner_id=runner_id,
            workspace_id=workspace.id,
            name=workspace.name,
            remote_root=remote_root,
            route_service=self._remote_route_service,
            runner_status=(
                (lambda: remote_runner_status(runner_id))
                if remote_runner_status is not None
                else None
            ),
            agent_backend_factory=(
                (lambda workspace_id, backend_kind: remote_agent_backend_factory(
                    runner_id,
                    workspace_id,
                    backend_kind,
                ))
                if remote_agent_backend_factory is not None
                else None
            ),
            semantic_backend_factory=self._remote_semantic_backend_factory,
            validation_backend_factory=self._remote_validation_backend_factory,
            job_manager=(
                self._remote_job_manager_factory(runner_id, workspace.id)
                if self._remote_job_manager_factory is not None
                else None
            ),
        )


__all__ = [
    "LocalWorkspaceHost",
    "RemoteMcpHttpRuntimeProxy",
    "RemoteMcpRuntimeProxy",
    "RemoteRunnerWorkspaceHost",
    "WorkspaceHandle",
    "WorkspaceHost",
    "WorkspaceHostError",
    "WorkspaceHostFactory",
]
