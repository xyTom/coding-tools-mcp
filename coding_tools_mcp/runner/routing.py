"""Remote MCP Runtime routing and close reconciliation primitives."""

from __future__ import annotations

import hashlib
import hmac
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol

from .jobs import JobInventoryItem, JobState, MAX_RUNNER_JOBS
from .protocol import validate_identifier
from .transport import RunnerUnavailableError


MAX_REMOTE_MCP_ROUTES = 4096
MAX_REMOTE_MCP_INVENTORY = 1024
MAX_REMOTE_MCP_TOMBSTONES = 1024


class RemoteMcpRouteError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class RemoteMcpRouteState(str, Enum):
    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    CLOSE_PENDING = "close_pending"
    CLOSED = "closed"
    LOST = "lost"


def authorization_key_digest(authorization_key: str) -> str:
    if not isinstance(authorization_key, str) or not authorization_key:
        raise RemoteMcpRouteError("RUNNER_AUTH_CONTEXT_INVALID", "authorization context is required")
    return hashlib.sha256(authorization_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RemoteMcpRoute:
    control_session_id: str
    runner_id: str
    workspace_id: str
    remote_session_id: str
    authorization_digest: str
    state: RemoteMcpRouteState = RemoteMcpRouteState.ACTIVE
    runner_connected: bool = True
    close_attempts: int = 0

    def public_payload(self) -> dict[str, Any]:
        """Bounded redacted projection; never exposes either Session ID or auth digest."""

        return {
            "runner_id": self.runner_id,
            "workspace_id": self.workspace_id,
            "state": self.state.value,
            "runner_connected": self.runner_connected,
            "close_attempts": self.close_attempts,
        }


@dataclass(frozen=True)
class RemoteMcpSessionInventoryItem:
    control_session_id: str
    remote_session_id: str
    workspace_id: str
    authorization_digest: str

    def payload(self) -> dict[str, str]:
        return {
            "control_session_id": self.control_session_id,
            "remote_session_id": self.remote_session_id,
            "workspace_id": self.workspace_id,
            "authorization_digest": self.authorization_digest,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "RemoteMcpSessionInventoryItem":
        if not isinstance(payload, Mapping):
            raise RemoteMcpRouteError("RUNNER_SESSION_INVENTORY_INVALID", "session inventory item must be an object")
        return cls(
            control_session_id=_required_route_id(payload.get("control_session_id"), "control_session_id"),
            remote_session_id=_required_route_id(payload.get("remote_session_id"), "remote_session_id"),
            workspace_id=validate_identifier(payload.get("workspace_id"), "workspace_id"),
            authorization_digest=_required_digest(payload.get("authorization_digest")),
        )


@dataclass(frozen=True)
class RemoteCloseIntent:
    control_session_id: str
    runner_id: str
    workspace_id: str
    remote_session_id: str
    authorization_digest: str

    def rpc_params(self) -> dict[str, str]:
        return {
            "control_session_id": self.control_session_id,
            "remote_session_id": self.remote_session_id,
            "authorization_key_digest": self.authorization_digest,
        }


@dataclass(frozen=True)
class RemoteMcpReconcileResult:
    close_intents: tuple[RemoteCloseIntent, ...]
    orphan_intents: tuple[RemoteCloseIntent, ...]
    restored: tuple[str, ...]
    closed: tuple[str, ...]
    lost: tuple[str, ...]


class RemoteMcpRouteStore:
    """Control-plane bounded route map with close-before-purge ordering."""

    def __init__(self, *, max_routes: int = MAX_REMOTE_MCP_ROUTES) -> None:
        if not isinstance(max_routes, int) or isinstance(max_routes, bool) or max_routes < 1:
            raise ValueError("max_routes must be a positive integer")
        self.max_routes = max_routes
        self._lock = threading.RLock()
        self._routes: OrderedDict[str, RemoteMcpRoute] = OrderedDict()

    def bind(
        self,
        *,
        control_session_id: str,
        runner_id: str,
        workspace_id: str,
        remote_session_id: str,
        authorization_key: str,
    ) -> RemoteMcpRoute:
        control_session_id = _required_route_id(control_session_id, "control_session_id")
        remote_session_id = _required_route_id(remote_session_id, "remote_session_id")
        runner_id = validate_identifier(runner_id, "runner_id")
        workspace_id = validate_identifier(workspace_id, "workspace_id")
        auth_digest = authorization_key_digest(authorization_key)
        with self._lock:
            existing = self._routes.get(control_session_id)
            if existing is not None:
                if (
                    existing.runner_id == runner_id
                    and existing.workspace_id == workspace_id
                    and existing.remote_session_id == remote_session_id
                    and hmac.compare_digest(existing.authorization_digest, auth_digest)
                ):
                    return existing
                raise RemoteMcpRouteError(
                    "RUNNER_ROUTE_CONFLICT",
                    "control MCP session already belongs to a different remote route",
                )
            if len(self._routes) >= self.max_routes:
                raise RemoteMcpRouteError(
                    "RUNNER_ROUTE_CAPACITY",
                    "remote MCP route capacity reached",
                    retryable=True,
                )
            route = RemoteMcpRoute(
                control_session_id=control_session_id,
                runner_id=runner_id,
                workspace_id=workspace_id,
                remote_session_id=remote_session_id,
                authorization_digest=auth_digest,
            )
            self._routes[control_session_id] = route
            return route

    def get_authorized(
        self,
        control_session_id: str,
        *,
        authorization_key: str,
        workspace_id: str,
        expected_runner_id: str | None = None,
        allow_terminal: bool = False,
    ) -> RemoteMcpRoute:
        auth_digest = authorization_key_digest(authorization_key)
        with self._lock:
            route = self._routes.get(control_session_id)
            if route is None:
                raise RemoteMcpRouteError("RUNNER_ROUTE_NOT_FOUND", "remote MCP route is unknown")
            if not hmac.compare_digest(route.authorization_digest, auth_digest):
                raise RemoteMcpRouteError("RUNNER_ROUTE_FORBIDDEN", "authorization context does not match remote MCP route")
            if route.workspace_id != workspace_id:
                raise RemoteMcpRouteError("RUNNER_ROUTE_FORBIDDEN", "workspace does not match remote MCP route")
            if expected_runner_id is not None and route.runner_id != expected_runner_id:
                raise RemoteMcpRouteError("RUNNER_ROUTE_FORBIDDEN", "runner does not match remote MCP route")
            if not allow_terminal and route.state in {RemoteMcpRouteState.CLOSED, RemoteMcpRouteState.LOST}:
                raise RemoteMcpRouteError("RUNNER_ROUTE_NOT_AVAILABLE", "remote MCP route is no longer active")
            return route

    def request_close(
        self,
        control_session_id: str,
        *,
        authorization_key: str,
        workspace_id: str,
        expected_runner_id: str | None = None,
    ) -> tuple[RemoteMcpRoute, RemoteCloseIntent | None]:
        route = self.get_authorized(
            control_session_id,
            authorization_key=authorization_key,
            workspace_id=workspace_id,
            expected_runner_id=expected_runner_id,
            allow_terminal=True,
        )
        with self._lock:
            route = self._routes[control_session_id]
            if route.state == RemoteMcpRouteState.CLOSED:
                return route, None
            if route.state == RemoteMcpRouteState.LOST:
                return route, None
            route = replace(route, state=RemoteMcpRouteState.CLOSE_PENDING)
            self._routes[control_session_id] = route
            if not route.runner_connected:
                return route, None
            return route, _close_intent(route)

    def record_close_attempt(self, control_session_id: str) -> RemoteMcpRoute:
        with self._lock:
            route = self._routes.get(control_session_id)
            if route is None:
                raise RemoteMcpRouteError("RUNNER_ROUTE_NOT_FOUND", "remote MCP route is unknown")
            route = replace(route, close_attempts=route.close_attempts + 1)
            self._routes[control_session_id] = route
            return route

    def acknowledge_close(
        self,
        control_session_id: str,
        *,
        runner_id: str,
        remote_session_id: str,
    ) -> RemoteMcpRoute:
        with self._lock:
            route = self._routes.get(control_session_id)
            if route is None:
                raise RemoteMcpRouteError("RUNNER_ROUTE_NOT_FOUND", "remote MCP route is unknown")
            if route.runner_id != runner_id or route.remote_session_id != remote_session_id:
                raise RemoteMcpRouteError("RUNNER_ROUTE_FORBIDDEN", "close acknowledgement does not match remote route")
            route = replace(route, state=RemoteMcpRouteState.CLOSED, runner_connected=True)
            self._routes[control_session_id] = route
            return route

    def mark_runner_disconnected(self, runner_id: str) -> None:
        with self._lock:
            for session_id, route in tuple(self._routes.items()):
                if route.runner_id != runner_id or route.state in {RemoteMcpRouteState.CLOSED, RemoteMcpRouteState.LOST}:
                    continue
                state = (
                    RemoteMcpRouteState.CLOSE_PENDING
                    if route.state == RemoteMcpRouteState.CLOSE_PENDING
                    else RemoteMcpRouteState.UNREACHABLE
                )
                self._routes[session_id] = replace(route, state=state, runner_connected=False)

    def reconcile_runner(
        self,
        runner_id: str,
        inventory: list[RemoteMcpSessionInventoryItem],
    ) -> RemoteMcpReconcileResult:
        validate_identifier(runner_id, "runner_id")
        if len(inventory) > MAX_REMOTE_MCP_INVENTORY:
            raise RemoteMcpRouteError("RUNNER_SESSION_INVENTORY_INVALID", "remote MCP session inventory is too large")
        remote_by_control = {item.control_session_id: item for item in inventory}
        if len(remote_by_control) != len(inventory):
            raise RemoteMcpRouteError("RUNNER_SESSION_INVENTORY_INVALID", "remote MCP inventory has duplicate routes")
        close_intents: list[RemoteCloseIntent] = []
        orphan_intents: list[RemoteCloseIntent] = []
        restored: list[str] = []
        closed: list[str] = []
        lost: list[str] = []
        with self._lock:
            consumed_remote_control_ids: set[str] = set()
            for session_id, route in tuple(self._routes.items()):
                if route.runner_id != runner_id:
                    continue
                remote = remote_by_control.get(session_id)
                if route.state in {RemoteMcpRouteState.CLOSED, RemoteMcpRouteState.LOST}:
                    if remote is not None:
                        orphan_intents.append(
                            RemoteCloseIntent(
                                control_session_id=remote.control_session_id,
                                runner_id=runner_id,
                                workspace_id=remote.workspace_id,
                                remote_session_id=remote.remote_session_id,
                                authorization_digest=remote.authorization_digest,
                            )
                        )
                        consumed_remote_control_ids.add(remote.control_session_id)
                    continue
                if remote is not None and (
                    remote.workspace_id != route.workspace_id
                    or remote.remote_session_id != route.remote_session_id
                    or not hmac.compare_digest(remote.authorization_digest, route.authorization_digest)
                ):
                    self._routes[session_id] = replace(
                        route,
                        state=RemoteMcpRouteState.LOST,
                        runner_connected=True,
                    )
                    lost.append(session_id)
                    orphan_intents.append(
                        RemoteCloseIntent(
                            control_session_id=remote.control_session_id,
                            runner_id=runner_id,
                            workspace_id=remote.workspace_id,
                            remote_session_id=remote.remote_session_id,
                            authorization_digest=remote.authorization_digest,
                        )
                    )
                    consumed_remote_control_ids.add(remote.control_session_id)
                    continue
                if route.state == RemoteMcpRouteState.CLOSE_PENDING:
                    if remote is None:
                        self._routes[session_id] = replace(
                            route,
                            state=RemoteMcpRouteState.CLOSED,
                            runner_connected=True,
                        )
                        closed.append(session_id)
                    else:
                        connected = replace(route, runner_connected=True)
                        self._routes[session_id] = connected
                        close_intents.append(_close_intent(connected))
                        consumed_remote_control_ids.add(remote.control_session_id)
                    continue
                if remote is None:
                    self._routes[session_id] = replace(
                        route,
                        state=RemoteMcpRouteState.LOST,
                        runner_connected=True,
                    )
                    lost.append(session_id)
                else:
                    self._routes[session_id] = replace(
                        route,
                        state=RemoteMcpRouteState.ACTIVE,
                        runner_connected=True,
                    )
                    restored.append(session_id)
                    consumed_remote_control_ids.add(remote.control_session_id)

            for item in inventory:
                if item.control_session_id in consumed_remote_control_ids:
                    continue
                orphan_intents.append(
                    RemoteCloseIntent(
                        control_session_id=item.control_session_id,
                        runner_id=runner_id,
                        workspace_id=item.workspace_id,
                        remote_session_id=item.remote_session_id,
                        authorization_digest=item.authorization_digest,
                    )
                )
        return RemoteMcpReconcileResult(
            close_intents=tuple(close_intents),
            orphan_intents=tuple(orphan_intents),
            restored=tuple(restored),
            closed=tuple(closed),
            lost=tuple(lost),
        )

    def purge_closed(self, *, limit: int = 256) -> int:
        if limit < 1:
            return 0
        removed = 0
        with self._lock:
            for session_id, route in tuple(self._routes.items()):
                if removed >= limit:
                    break
                if route.state == RemoteMcpRouteState.CLOSED:
                    self._routes.pop(session_id, None)
                    removed += 1
        return removed

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = {state.value: 0 for state in RemoteMcpRouteState}
            for route in self._routes.values():
                counts[route.state.value] += 1
            return {
                "route_count": len(self._routes),
                "capacity": self.max_routes,
                "states": counts,
            }


class RemoteRunnerTransport(Protocol):
    @property
    def runner_id(self) -> str: ...

    @property
    def workspace_ids(self) -> tuple[str, ...]: ...

    async def call(
        self,
        *,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any: ...

    def call_sync(
        self,
        *,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class RemoteMcpCloseResult:
    status: str
    retryable: bool


class RemoteMcpRouteService:
    """Control-plane dispatcher. There is intentionally no local Runtime fallback."""

    def __init__(self, store: RemoteMcpRouteStore) -> None:
        self.store = store
        self._transports: dict[str, RemoteRunnerTransport] = {}
        self._lock = threading.Lock()

    async def attach_transport(self, transport: RemoteRunnerTransport) -> RemoteMcpReconcileResult:
        inventory: list[RemoteMcpSessionInventoryItem] = []
        for workspace_id in transport.workspace_ids:
            response = await transport.call(workspace_id=workspace_id, method="mcp.inventory", params={})
            raw_sessions = response.get("sessions") if isinstance(response, Mapping) else None
            if not isinstance(raw_sessions, list):
                raise RemoteMcpRouteError("RUNNER_SESSION_INVENTORY_INVALID", "Runner inventory response is invalid")
            if len(inventory) + len(raw_sessions) > MAX_REMOTE_MCP_INVENTORY:
                raise RemoteMcpRouteError("RUNNER_SESSION_INVENTORY_INVALID", "Runner inventory response is too large")
            inventory.extend(RemoteMcpSessionInventoryItem.from_payload(item) for item in raw_sessions)
        reconciled = self.store.reconcile_runner(transport.runner_id, inventory)
        for intent in (*reconciled.close_intents, *reconciled.orphan_intents):
            await self._dispatch_close(transport, intent, acknowledge=intent in reconciled.close_intents)
        with self._lock:
            self._transports[transport.runner_id] = transport
        return reconciled

    def detach_transport(
        self,
        runner_id: str,
        *,
        expected_transport: RemoteRunnerTransport | None = None,
    ) -> None:
        with self._lock:
            current = self._transports.get(runner_id)
            if expected_transport is not None and current is not expected_transport:
                return
            self._transports.pop(runner_id, None)
        self.store.mark_runner_disconnected(runner_id)

    def runner_status(self, runner_id: str) -> dict[str, Any]:
        with self._lock:
            transport = self._transports.get(runner_id)
        if transport is None:
            return {"connected": False, "runner_id": runner_id, "instance_id": None}
        return {
            "connected": True,
            "runner_id": runner_id,
            "instance_id": getattr(transport, "instance_id", None),
        }

    def next_runner_event_sync(self, runner_id: str, *, timeout: float = 5.0) -> Any:
        transport = self._transport(runner_id)
        reader = getattr(transport, "next_event_sync", None)
        if not callable(reader):
            raise RemoteMcpRouteError(
                "RUNNER_EVENT_UNAVAILABLE",
                "remote Runner event stream is unavailable",
                retryable=True,
            )
        try:
            return reader(timeout=timeout)
        except RunnerUnavailableError as exc:
            raise RemoteMcpRouteError(
                "RUNNER_UNAVAILABLE",
                str(exc),
                retryable=True,
            ) from exc

    def close_transports_sync(self) -> None:
        """Bounded Control Plane shutdown of all attached Runner transports."""

        with self._lock:
            transports = tuple(self._transports.values())
            self._transports.clear()
        for transport in transports:
            try:
                close_sync = getattr(transport, "close_sync", None)
                if callable(close_sync):
                    close_sync()
            except (RunnerUnavailableError, RemoteMcpRouteError):
                pass
            finally:
                self.store.mark_runner_disconnected(transport.runner_id)

    async def create_session(
        self,
        *,
        control_session_id: str,
        runner_id: str,
        workspace_id: str,
        authorization_key: str,
    ) -> RemoteMcpRoute:
        transport = self._transport(runner_id)
        auth_digest = authorization_key_digest(authorization_key)
        response = await transport.call(
            workspace_id=workspace_id,
            method="mcp.create",
            params={
                "control_session_id": control_session_id,
                "authorization_key_digest": auth_digest,
            },
        )
        remote_session_id = response.get("remote_session_id") if isinstance(response, Mapping) else None
        remote_session_id = _required_route_id(remote_session_id, "remote_session_id")
        try:
            return self.store.bind(
                control_session_id=control_session_id,
                runner_id=runner_id,
                workspace_id=workspace_id,
                remote_session_id=remote_session_id,
                authorization_key=authorization_key,
            )
        except BaseException:
            await transport.call(
                workspace_id=workspace_id,
                method="mcp.close",
                params={
                    "control_session_id": control_session_id,
                    "remote_session_id": remote_session_id,
                    "authorization_key_digest": auth_digest,
                },
            )
            raise

    def create_session_sync(
        self,
        *,
        control_session_id: str,
        runner_id: str,
        workspace_id: str,
        authorization_key: str,
    ) -> RemoteMcpRoute:
        transport = self._transport(runner_id)
        call_sync = getattr(transport, "call_sync", None)
        if not callable(call_sync):
            raise RemoteMcpRouteError(
                "RUNNER_SYNC_BRIDGE_UNAVAILABLE",
                "Runner transport does not provide a synchronous Control Plane bridge",
                retryable=True,
            )
        auth_digest = authorization_key_digest(authorization_key)
        response = call_sync(
            workspace_id=workspace_id,
            method="mcp.create",
            params={
                "control_session_id": control_session_id,
                "authorization_key_digest": auth_digest,
            },
        )
        remote_session_id = response.get("remote_session_id") if isinstance(response, Mapping) else None
        remote_session_id = _required_route_id(remote_session_id, "remote_session_id")
        try:
            return self.store.bind(
                control_session_id=control_session_id,
                runner_id=runner_id,
                workspace_id=workspace_id,
                remote_session_id=remote_session_id,
                authorization_key=authorization_key,
            )
        except BaseException:
            call_sync(
                workspace_id=workspace_id,
                method="mcp.close",
                params={
                    "control_session_id": control_session_id,
                    "remote_session_id": remote_session_id,
                    "authorization_key_digest": auth_digest,
                },
            )
            raise

    async def close_session(
        self,
        control_session_id: str,
        *,
        authorization_key: str,
        workspace_id: str,
        expected_runner_id: str | None = None,
    ) -> RemoteMcpCloseResult:
        route, intent = self.store.request_close(
            control_session_id,
            authorization_key=authorization_key,
            workspace_id=workspace_id,
            expected_runner_id=expected_runner_id,
        )
        if route.state == RemoteMcpRouteState.CLOSED:
            return RemoteMcpCloseResult("closed", False)
        if route.state == RemoteMcpRouteState.LOST:
            return RemoteMcpCloseResult("lost", False)
        if intent is None:
            return RemoteMcpCloseResult("close_pending", True)
        try:
            transport = self._transport(route.runner_id)
            await self._dispatch_close(transport, intent, acknowledge=True)
        except (RemoteMcpRouteError, RunnerUnavailableError):
            self.store.mark_runner_disconnected(route.runner_id)
            return RemoteMcpCloseResult("close_pending", True)
        return RemoteMcpCloseResult("closed", False)

    def close_session_sync(
        self,
        control_session_id: str,
        *,
        authorization_key: str,
        workspace_id: str,
        expected_runner_id: str | None = None,
    ) -> RemoteMcpCloseResult:
        route, intent = self.store.request_close(
            control_session_id,
            authorization_key=authorization_key,
            workspace_id=workspace_id,
            expected_runner_id=expected_runner_id,
        )
        if route.state == RemoteMcpRouteState.CLOSED:
            return RemoteMcpCloseResult("closed", False)
        if route.state == RemoteMcpRouteState.LOST:
            return RemoteMcpCloseResult("lost", False)
        if intent is None:
            return RemoteMcpCloseResult("close_pending", True)
        try:
            transport = self._transport(route.runner_id)
            call_sync = getattr(transport, "call_sync", None)
            if not callable(call_sync):
                raise RunnerUnavailableError("Runner synchronous close bridge is unavailable")
            self.store.record_close_attempt(intent.control_session_id)
            response = call_sync(
                workspace_id=intent.workspace_id,
                method="mcp.close",
                params=intent.rpc_params(),
            )
            if not isinstance(response, Mapping) or response.get("closed") is not True:
                raise RunnerUnavailableError("Runner did not acknowledge remote MCP close")
            self.store.acknowledge_close(
                intent.control_session_id,
                runner_id=intent.runner_id,
                remote_session_id=intent.remote_session_id,
            )
        except (RemoteMcpRouteError, RunnerUnavailableError):
            self.store.mark_runner_disconnected(route.runner_id)
            return RemoteMcpCloseResult("close_pending", True)
        return RemoteMcpCloseResult("closed", False)

    async def call_session(
        self,
        control_session_id: str,
        *,
        authorization_key: str,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        route = self.store.get_authorized(
            control_session_id,
            authorization_key=authorization_key,
            workspace_id=workspace_id,
        )
        if route.state != RemoteMcpRouteState.ACTIVE or not route.runner_connected:
            raise RemoteMcpRouteError(
                "RUNNER_UNAVAILABLE",
                "remote Runner session is unavailable",
                retryable=True,
            )
        transport = self._transport(route.runner_id)
        return await transport.call(
            workspace_id=workspace_id,
            method="mcp.call",
            params={
                "control_session_id": control_session_id,
                "remote_session_id": route.remote_session_id,
                "authorization_key_digest": route.authorization_digest,
                "method": method,
                "params": params or {},
            },
        )

    def call_session_sync(
        self,
        control_session_id: str,
        *,
        authorization_key: str,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        route = self.store.get_authorized(
            control_session_id,
            authorization_key=authorization_key,
            workspace_id=workspace_id,
        )
        if route.state != RemoteMcpRouteState.ACTIVE or not route.runner_connected:
            raise RemoteMcpRouteError(
                "RUNNER_UNAVAILABLE",
                "remote Runner session is unavailable",
                retryable=True,
            )
        transport = self._transport(route.runner_id)
        call_sync = getattr(transport, "call_sync", None)
        if not callable(call_sync):
            raise RemoteMcpRouteError(
                "RUNNER_SYNC_BRIDGE_UNAVAILABLE",
                "Runner transport does not provide a synchronous Control Plane bridge",
                retryable=True,
            )
        try:
            return call_sync(
                workspace_id=workspace_id,
                method="mcp.call",
                params={
                    "control_session_id": control_session_id,
                    "remote_session_id": route.remote_session_id,
                    "authorization_key_digest": route.authorization_digest,
                    "method": method,
                    "params": params or {},
                },
            )
        except RunnerUnavailableError:
            self.store.mark_runner_disconnected(route.runner_id)
            raise

    def call_runner_sync(
        self,
        *,
        runner_id: str,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Call a coarse Runner capability from synchronous Control Plane code."""

        transport = self._transport(runner_id)
        call_sync = getattr(transport, "call_sync", None)
        if not callable(call_sync):
            raise RemoteMcpRouteError(
                "RUNNER_SYNC_BRIDGE_UNAVAILABLE",
                "Runner transport does not provide a synchronous Control Plane bridge",
                retryable=True,
            )
        try:
            return call_sync(
                workspace_id=workspace_id,
                method=method,
                params=params or {},
            )
        except RunnerUnavailableError:
            self.store.mark_runner_disconnected(runner_id)
            raise

    def _transport(self, runner_id: str) -> RemoteRunnerTransport:
        with self._lock:
            transport = self._transports.get(runner_id)
        if transport is None:
            raise RemoteMcpRouteError("RUNNER_UNAVAILABLE", "remote Runner is unavailable", retryable=True)
        return transport

    async def _dispatch_close(
        self,
        transport: RemoteRunnerTransport,
        intent: RemoteCloseIntent,
        *,
        acknowledge: bool,
    ) -> None:
        if transport.runner_id != intent.runner_id:
            raise RemoteMcpRouteError("RUNNER_ROUTE_FORBIDDEN", "close intent cannot be sent to another Runner")
        if acknowledge:
            self.store.record_close_attempt(intent.control_session_id)
        response = await transport.call(
            workspace_id=intent.workspace_id,
            method="mcp.close",
            params=intent.rpc_params(),
        )
        if not isinstance(response, Mapping) or response.get("closed") is not True:
            raise RunnerUnavailableError("Runner did not acknowledge remote MCP close")
        if acknowledge:
            self.store.acknowledge_close(
                intent.control_session_id,
                runner_id=intent.runner_id,
                remote_session_id=intent.remote_session_id,
            )


class RunnerRuntime(Protocol):
    http_session_id: str

    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def list_tools(self) -> dict[str, Any]: ...

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        request_id: str | int | None = None,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RunnerMcpSessionRecord:
    control_session_id: str
    remote_session_id: str
    workspace_id: str
    authorization_digest: str
    runtime: RunnerRuntime

    def inventory_item(self) -> RemoteMcpSessionInventoryItem:
        return RemoteMcpSessionInventoryItem(
            control_session_id=self.control_session_id,
            remote_session_id=self.remote_session_id,
            workspace_id=self.workspace_id,
            authorization_digest=self.authorization_digest,
        )


@dataclass(frozen=True)
class _ClosedSession:
    workspace_id: str
    authorization_digest: str


@dataclass
class _SessionState:
    record: RunnerMcpSessionRecord
    active_call_leases: int = 0
    closing: bool = False
    close_started: bool = False
    close_completed: bool = False


class RunnerMcpSessionHost:
    """Runner-side owner of real Runtime instances for remote MCP routes."""

    def __init__(
        self,
        runtime_factory: Callable[[str, str], RunnerRuntime],
        *,
        max_sessions: int = MAX_REMOTE_MCP_ROUTES,
    ) -> None:
        self._runtime_factory = runtime_factory
        self.max_sessions = max_sessions
        self._condition = threading.Condition()
        self._by_control: dict[str, _SessionState] = {}
        self._by_remote: dict[str, _SessionState] = {}
        self._creating = 0
        self._creating_by_control: dict[str, tuple[str, str]] = {}
        self._closed: OrderedDict[str, _ClosedSession] = OrderedDict()
        self._pending_closes: dict[str, _SessionState] = {}
        self._shutting_down = False
        self._shutdown_complete = False

    def _remember_closed_locked(self, record: RunnerMcpSessionRecord) -> None:
        self._closed[record.remote_session_id] = _ClosedSession(
            record.workspace_id,
            record.authorization_digest,
        )
        self._closed.move_to_end(record.remote_session_id)
        while len(self._closed) > MAX_REMOTE_MCP_TOMBSTONES:
            self._closed.popitem(last=False)

    def _release_creation_locked(self, control_session_id: str) -> None:
        self._creating_by_control.pop(control_session_id, None)
        self._creating -= 1
        self._condition.notify_all()

    def create(
        self,
        *,
        control_session_id: str,
        workspace_id: str,
        authorization_digest: str,
    ) -> RunnerMcpSessionRecord:
        control_session_id = _required_route_id(control_session_id, "control_session_id")
        workspace_id = validate_identifier(workspace_id, "workspace_id")
        authorization_digest = _required_digest(authorization_digest)
        with self._condition:
            while True:
                if self._shutting_down:
                    raise RemoteMcpRouteError(
                        "RUNNER_SHUTTING_DOWN", "Runner MCP host is shutting down", retryable=True
                    )
                existing_state = self._by_control.get(control_session_id)
                if existing_state is not None:
                    existing = existing_state.record
                    if existing.workspace_id == workspace_id and hmac.compare_digest(
                        existing.authorization_digest, authorization_digest
                    ):
                        return existing
                    raise RemoteMcpRouteError("RUNNER_ROUTE_CONFLICT", "control session already exists on Runner")
                creating_identity = self._creating_by_control.get(control_session_id)
                if creating_identity is not None:
                    if creating_identity != (workspace_id, authorization_digest):
                        raise RemoteMcpRouteError("RUNNER_ROUTE_CONFLICT", "control session already exists on Runner")
                    self._condition.wait()
                    continue
                if len(self._by_control) + self._creating >= self.max_sessions:
                    raise RemoteMcpRouteError(
                        "RUNNER_SESSION_CAPACITY", "Runner MCP session capacity reached", retryable=True
                    )
                self._creating += 1
                self._creating_by_control[control_session_id] = (workspace_id, authorization_digest)
                break

        runtime: RunnerRuntime | None = None
        installed = False
        try:
            runtime = self._runtime_factory(workspace_id, authorization_digest)
            remote_session_id = _required_route_id(runtime.http_session_id, "remote_session_id")
            record = RunnerMcpSessionRecord(
                control_session_id=control_session_id,
                remote_session_id=remote_session_id,
                workspace_id=workspace_id,
                authorization_digest=authorization_digest,
                runtime=runtime,
            )
            with self._condition:
                if self._shutting_down:
                    raise RemoteMcpRouteError(
                        "RUNNER_SHUTTING_DOWN", "Runner MCP host is shutting down", retryable=True
                    )
                if (
                    control_session_id in self._by_control
                    or remote_session_id in self._by_remote
                    or remote_session_id in self._pending_closes
                    or remote_session_id in self._closed
                ):
                    raise RemoteMcpRouteError("RUNNER_ROUTE_CONFLICT", "Runner generated a duplicate MCP session id")
                state = _SessionState(record=record)
                self._by_control[control_session_id] = state
                self._by_remote[remote_session_id] = state
                installed = True
            return record
        finally:
            if runtime is not None and not installed:
                try:
                    runtime.close()
                finally:
                    with self._condition:
                        self._release_creation_locked(control_session_id)
            else:
                with self._condition:
                    self._release_creation_locked(control_session_id)

    def close_session(
        self,
        *,
        control_session_id: str,
        remote_session_id: str,
        workspace_id: str,
        authorization_digest: str,
    ) -> bool:
        authorization_digest = _required_digest(authorization_digest)
        with self._condition:
            while True:
                state = self._by_remote.get(remote_session_id)
                if state is None:
                    pending = self._pending_closes.get(remote_session_id)
                    if pending is not None:
                        record = pending.record
                        if (
                            record.control_session_id != control_session_id
                            or record.workspace_id != workspace_id
                            or not hmac.compare_digest(record.authorization_digest, authorization_digest)
                        ):
                            raise RemoteMcpRouteError(
                                "RUNNER_ROUTE_FORBIDDEN", "remote MCP close does not match Runner route"
                            )
                        while not pending.close_completed:
                            self._condition.wait()
                        return True
                    tombstone = self._closed.get(remote_session_id)
                    if tombstone is None:
                        return True
                    if tombstone.workspace_id != workspace_id or not hmac.compare_digest(
                        tombstone.authorization_digest, authorization_digest
                    ):
                        raise RemoteMcpRouteError(
                            "RUNNER_ROUTE_FORBIDDEN", "closed route authorization does not match"
                        )
                    return True
                record = state.record
                if (
                    record.control_session_id != control_session_id
                    or record.workspace_id != workspace_id
                    or not hmac.compare_digest(record.authorization_digest, authorization_digest)
                ):
                    raise RemoteMcpRouteError("RUNNER_ROUTE_FORBIDDEN", "remote MCP close does not match Runner route")
                if state.closing:
                    self._condition.wait()
                    continue
                state.closing = True
                while state.active_call_leases:
                    self._condition.wait()
                if state.close_started:
                    while not state.close_completed:
                        self._condition.wait()
                    return True
                state.close_started = True
                self._pending_closes[remote_session_id] = state
                self._by_remote.pop(remote_session_id, None)
                self._by_control.pop(control_session_id, None)
                self._remember_closed_locked(record)
                self._condition.notify_all()
                break
        try:
            record.runtime.close()
        finally:
            with self._condition:
                state.close_completed = True
                if self._pending_closes.get(record.remote_session_id) is state:
                    self._pending_closes.pop(record.remote_session_id, None)
                self._condition.notify_all()
        return True

    def inventory(self, workspace_id: str) -> tuple[RemoteMcpSessionInventoryItem, ...]:
        validate_identifier(workspace_id, "workspace_id")
        with self._condition:
            items = [
                state.record.inventory_item()
                for state in self._by_control.values()
                if state.record.workspace_id == workspace_id
            ]
        if len(items) > MAX_REMOTE_MCP_INVENTORY:
            raise RemoteMcpRouteError("RUNNER_SESSION_INVENTORY_INVALID", "Runner session inventory exceeds bound")
        return tuple(items)

    def job_inventory(self) -> tuple[JobInventoryItem, ...]:
        """Return bounded job facts derived from real Runtime ExecSessions."""

        with self._condition:
            records = tuple(state.record for state in self._by_control.values())
        items: list[JobInventoryItem] = []
        seen: set[str] = set()
        for record in records:
            for item in _runtime_job_inventory(record.runtime, record.workspace_id):
                if item.job_id in seen:
                    raise RemoteMcpRouteError(
                        "RUNNER_JOB_INVENTORY_INVALID",
                        "Runner job inventory contains a duplicate job id",
                    )
                seen.add(item.job_id)
                items.append(item)
                if len(items) > MAX_RUNNER_JOBS:
                    raise RemoteMcpRouteError(
                        "RUNNER_JOB_INVENTORY_INVALID",
                        "Runner job inventory exceeds the bounded limit",
                    )
        return tuple(items)

    def describe_job(
        self,
        *,
        control_session_id: str,
        remote_session_id: str,
        workspace_id: str,
        authorization_digest: str,
        job_id: str,
    ) -> JobInventoryItem:
        """Describe one ExecSession after applying the MCP route authorization boundary."""

        authorization_digest = _required_digest(authorization_digest)
        with self._condition:
            state = self._by_remote.get(remote_session_id)
            if state is None:
                raise RemoteMcpRouteError("RUNNER_ROUTE_NOT_FOUND", "remote MCP Runtime is unknown")
            record = state.record
            if (
                record.control_session_id != control_session_id
                or record.workspace_id != workspace_id
                or not hmac.compare_digest(record.authorization_digest, authorization_digest)
            ):
                raise RemoteMcpRouteError("RUNNER_ROUTE_FORBIDDEN", "remote MCP job route does not match")
            runtime = record.runtime
        item = _runtime_job_item(runtime, workspace_id, job_id)
        if item is None:
            raise RemoteMcpRouteError("RUNNER_JOB_NOT_FOUND", "Runner job is unknown")
        return item

    def call_session(
        self,
        *,
        control_session_id: str,
        remote_session_id: str,
        workspace_id: str,
        authorization_digest: str,
        method: str,
        params: Mapping[str, Any],
    ) -> Any:
        authorization_digest = _required_digest(authorization_digest)
        with self._condition:
            state = self._by_remote.get(remote_session_id)
            if state is None:
                raise RemoteMcpRouteError("RUNNER_ROUTE_NOT_FOUND", "remote MCP Runtime is unknown")
            record = state.record
            if (
                record.control_session_id != control_session_id
                or record.workspace_id != workspace_id
                or not hmac.compare_digest(record.authorization_digest, authorization_digest)
            ):
                raise RemoteMcpRouteError("RUNNER_ROUTE_FORBIDDEN", "remote MCP call does not match Runner route")
            if state.closing:
                code = "RUNNER_SHUTTING_DOWN" if self._shutting_down else "RUNNER_SESSION_CLOSING"
                raise RemoteMcpRouteError(code, "remote MCP Runtime is closing", retryable=code == "RUNNER_SHUTTING_DOWN")
            runtime = record.runtime
            state.active_call_leases += 1

        try:
            if method == "initialize":
                client_info = params.get("clientInfo")
                if client_info is not None and not isinstance(client_info, dict):
                    raise RemoteMcpRouteError("RUNNER_RPC_INVALID_PARAMS", "initialize clientInfo must be an object")
                protocol_version = params.get("protocolVersion")
                if protocol_version is not None:
                    if not isinstance(protocol_version, str) or not protocol_version:
                        raise RemoteMcpRouteError("RUNNER_RPC_INVALID_PARAMS", "initialize protocolVersion must be a string")
                    if hasattr(runtime, "protocol_version"):
                        runtime.protocol_version = protocol_version
                return runtime.initialize(client_info)
            if method == "notifications/cancelled":
                request_id = params.get("requestId")
                if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                    raise RemoteMcpRouteError("RUNNER_RPC_INVALID_PARAMS", "cancel requestId must be a string or integer")
                cancel = getattr(runtime, "cancel_request", None)
                if callable(cancel):
                    cancel(request_id)
                return {}
            if method == "ping":
                return {}
            if method == "tools/list":
                return runtime.list_tools()
            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments", {})
                request_id = params.get("request_id")
                if not isinstance(name, str) or not name:
                    raise RemoteMcpRouteError("RUNNER_RPC_INVALID_PARAMS", "tools/call name is required")
                if not isinstance(arguments, dict):
                    raise RemoteMcpRouteError("RUNNER_RPC_INVALID_PARAMS", "tools/call arguments must be an object")
                if request_id is not None and not isinstance(request_id, (str, int)):
                    raise RemoteMcpRouteError("RUNNER_RPC_INVALID_PARAMS", "tools/call request_id must be a string or integer")
                return runtime.call_tool(name, arguments, request_id=request_id)
            raise RemoteMcpRouteError("RUNNER_RPC_METHOD_UNSUPPORTED", "remote MCP method is unsupported")
        finally:
            with self._condition:
                state.active_call_leases -= 1
                self._condition.notify_all()

    def shutdown(self) -> None:
        with self._condition:
            if self._shutdown_complete:
                return
            if self._shutting_down:
                while not self._shutdown_complete:
                    self._condition.wait()
                return
            self._shutting_down = True
            states = tuple(self._by_control.values())
            for state in states:
                state.closing = True
            self._condition.notify_all()
            while self._creating or any(state.active_call_leases for state in states):
                self._condition.wait()
            owned_states: list[_SessionState] = []
            for state in states:
                if state.close_started:
                    while not state.close_completed:
                        self._condition.wait()
                    continue
                state.close_started = True
                self._pending_closes[state.record.remote_session_id] = state
                owned_states.append(state)
                record = state.record
                self._by_control.pop(record.control_session_id, None)
                self._by_remote.pop(record.remote_session_id, None)
                self._remember_closed_locked(record)
            self._by_control.clear()
            self._by_remote.clear()
            self._condition.notify_all()
        first_error: BaseException | None = None
        try:
            for state in owned_states:
                try:
                    state.record.runtime.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                finally:
                    with self._condition:
                        state.close_completed = True
                        if self._pending_closes.get(state.record.remote_session_id) is state:
                            self._pending_closes.pop(state.record.remote_session_id, None)
                        self._condition.notify_all()
        finally:
            with self._condition:
                while self._pending_closes:
                    self._condition.wait()
                self._shutdown_complete = True
                self._condition.notify_all()
        if first_error is not None:
            raise first_error


class RunnerMcpRouter:
    """Runner-side coarse RPC dispatcher; does not expose RemotePath primitives."""

    def __init__(
        self,
        sessions: RunnerMcpSessionHost,
        capability_dispatcher: Callable[[str, str, Mapping[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.capability_dispatcher = capability_dispatcher

    def dispatch(self, workspace_id: str, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method.startswith("capability.") or method.startswith("workspace."):
            if self.capability_dispatcher is None:
                raise RemoteMcpRouteError(
                    "RUNNER_RPC_METHOD_UNSUPPORTED",
                    "Runner capability routing is not configured",
                )
            return self.capability_dispatcher(workspace_id, method, params)
        if method == "job.inventory":
            return {"jobs": [item.payload() for item in self.sessions.job_inventory()]}
        if method == "mcp.create":
            record = self.sessions.create(
                control_session_id=_required_route_id(params.get("control_session_id"), "control_session_id"),
                workspace_id=workspace_id,
                authorization_digest=_required_digest(params.get("authorization_key_digest")),
            )
            return {"remote_session_id": record.remote_session_id}
        if method == "mcp.close":
            closed = self.sessions.close_session(
                control_session_id=_required_route_id(params.get("control_session_id"), "control_session_id"),
                remote_session_id=_required_route_id(params.get("remote_session_id"), "remote_session_id"),
                workspace_id=workspace_id,
                authorization_digest=_required_digest(params.get("authorization_key_digest")),
            )
            return {"closed": closed}
        if method == "mcp.inventory":
            return {"sessions": [item.payload() for item in self.sessions.inventory(workspace_id)]}
        if method == "mcp.job.describe":
            item = self.sessions.describe_job(
                control_session_id=_required_route_id(params.get("control_session_id"), "control_session_id"),
                remote_session_id=_required_route_id(params.get("remote_session_id"), "remote_session_id"),
                workspace_id=workspace_id,
                authorization_digest=_required_digest(params.get("authorization_key_digest")),
                job_id=_required_route_id(params.get("job_id"), "job_id"),
            )
            return {"job": item.payload()}
        if method == "mcp.call":
            nested_method = params.get("method")
            nested_params = params.get("params", {})
            if not isinstance(nested_method, str) or not nested_method:
                raise RemoteMcpRouteError("RUNNER_RPC_INVALID_PARAMS", "remote MCP method is required")
            if not isinstance(nested_params, Mapping):
                raise RemoteMcpRouteError("RUNNER_RPC_INVALID_PARAMS", "remote MCP params must be an object")
            control_session_id = _required_route_id(params.get("control_session_id"), "control_session_id")
            remote_session_id = _required_route_id(params.get("remote_session_id"), "remote_session_id")
            authorization_digest = _required_digest(params.get("authorization_key_digest"))
            result = self.sessions.call_session(
                control_session_id=control_session_id,
                remote_session_id=remote_session_id,
                workspace_id=workspace_id,
                authorization_digest=authorization_digest,
                method=nested_method,
                params=nested_params,
            )
            response: dict[str, Any] = {"result": result}
            if nested_method == "tools/call":
                tool_name = nested_params.get("name")
                job_id = _job_id_from_tool_result(result, nested_params)
                if tool_name in {"exec_command", "write_stdin", "kill_session"} and job_id is not None:
                    try:
                        item = self.sessions.describe_job(
                            control_session_id=control_session_id,
                            remote_session_id=remote_session_id,
                            workspace_id=workspace_id,
                            authorization_digest=authorization_digest,
                            job_id=job_id,
                        )
                    except RemoteMcpRouteError as exc:
                        if exc.code != "RUNNER_JOB_NOT_FOUND":
                            raise
                    else:
                        if tool_name != "exec_command" or item.state == JobState.RUNNING:
                            response["job"] = item.payload()
            return response
        raise RemoteMcpRouteError("RUNNER_RPC_METHOD_UNSUPPORTED", "Runner MCP method is unsupported")


def _runtime_job_inventory(runtime: Any, workspace_id: str) -> tuple[JobInventoryItem, ...]:
    sessions = getattr(runtime, "sessions", None)
    output_sessions = getattr(runtime, "output_sessions", None)
    lock = getattr(runtime, "sessions_lock", None)
    if not isinstance(sessions, dict) or not isinstance(output_sessions, dict) or lock is None:
        return ()
    with lock:
        items = tuple({**output_sessions, **sessions}.items())
    result: list[JobInventoryItem] = []
    for job_id, _session in items:
        item = _runtime_job_item(runtime, workspace_id, str(job_id))
        if item is not None:
            result.append(item)
    return tuple(result)


def _runtime_job_item(runtime: Any, workspace_id: str, job_id: str) -> JobInventoryItem | None:
    sessions = getattr(runtime, "sessions", None)
    output_sessions = getattr(runtime, "output_sessions", None)
    lock = getattr(runtime, "sessions_lock", None)
    if not isinstance(sessions, dict) or not isinstance(output_sessions, dict) or lock is None:
        return None
    with lock:
        session = sessions.get(job_id) or output_sessions.get(job_id)
    if session is None:
        return None
    refresh = getattr(session, "refresh_status", None)
    if callable(refresh):
        refresh()
    process = getattr(session, "process", None)
    pid = getattr(process, "pid", None)
    started_at = getattr(session, "started_at", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(started_at, (int, float)):
        raise RemoteMcpRouteError(
            "RUNNER_JOB_INVENTORY_INVALID",
            "Runner ExecSession process identity is unavailable",
        )
    fingerprint = hashlib.sha256(f"{pid}:{float(started_at):.9f}".encode("ascii")).hexdigest()
    exit_code = getattr(session, "exit_code", None)
    timed_out = bool(getattr(session, "timed_out", False))
    signal_name = getattr(session, "signal_name", None)
    poll = process.poll() if callable(getattr(process, "poll", None)) else exit_code
    if poll is None:
        state = JobState.RUNNING
    elif signal_name is not None:
        state = JobState.CANCELLED
    elif timed_out or exit_code not in {0, None}:
        state = JobState.FAILED
    else:
        state = JobState.COMPLETED
    stdout_cursor = getattr(session, "stdout_total_bytes", 0)
    stderr_cursor = getattr(session, "stderr_total_bytes", 0)
    return JobInventoryItem(
        job_id=job_id,
        workspace_id=workspace_id,
        process_fingerprint=fingerprint,
        state=state,
        stdout_cursor=stdout_cursor if isinstance(stdout_cursor, int) else 0,
        stderr_cursor=stderr_cursor if isinstance(stderr_cursor, int) else 0,
        started_at=f"{float(started_at):.9f}",
    )


def _job_id_from_tool_result(result: Any, params: Mapping[str, Any]) -> str | None:
    if isinstance(result, Mapping):
        structured = result.get("structuredContent")
        if isinstance(structured, Mapping):
            value = structured.get("session_id")
            if isinstance(value, str) and value:
                return value
    arguments = params.get("arguments")
    if isinstance(arguments, Mapping):
        value = arguments.get("session_id")
        if isinstance(value, str) and value:
            return value
    return None


def _close_intent(route: RemoteMcpRoute) -> RemoteCloseIntent:
    return RemoteCloseIntent(
        control_session_id=route.control_session_id,
        runner_id=route.runner_id,
        workspace_id=route.workspace_id,
        remote_session_id=route.remote_session_id,
        authorization_digest=route.authorization_digest,
    )


def _required_route_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise RemoteMcpRouteError("RUNNER_ROUTE_INVALID", f"{field} must be a non-empty bounded string")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise RemoteMcpRouteError("RUNNER_ROUTE_INVALID", f"{field} contains unsupported characters")
    return value


def _required_digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RemoteMcpRouteError("RUNNER_AUTH_CONTEXT_INVALID", "authorization digest is invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise RemoteMcpRouteError("RUNNER_AUTH_CONTEXT_INVALID", "authorization digest is invalid") from exc
    return value.lower()
