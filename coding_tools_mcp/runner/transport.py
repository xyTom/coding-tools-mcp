"""Authenticated WebSocket transport boundary for remote Runner RPC."""

from __future__ import annotations

import asyncio
import concurrent.futures
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from .credentials import RunnerCredentialError, RunnerCredentialStore
from .jobs import JobInventoryItem, RunnerJobReconciler
from .protocol import (
    RUNNER_PROTOCOL_VERSION,
    RunnerDisconnect,
    RunnerEvent,
    RunnerHeartbeat,
    RunnerHello,
    RunnerProtocolError,
    decode_message,
    disconnect_ack_payload,
    encode_message,
    parse_rpc_response,
    rpc_request_payload,
)
from .registry import RunnerConnectionError, RunnerRegistry, RunnerSnapshot


class RunnerTransportError(RuntimeError):
    pass


class RunnerUnavailableError(RunnerTransportError):
    pass


class RunnerRemoteError(RunnerTransportError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "RUNNER_REMOTE_ERROR",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})


class RunnerAuthenticationError(RunnerTransportError):
    pass


class RunnerWebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class RunnerCall:
    request_id: str
    workspace_id: str
    method: str


class RunnerWebSocketTransport:
    """Control-plane view of one authenticated Runner WebSocket.

    The Runner initiates the socket and sends a credential-bearing ``hello`` as
    the first frame. Only :meth:`accept` can construct an active transport.
    After authentication, RPC calls are correlated on this connection while
    heartbeat and job inventory frames update registry state.
    """

    def __init__(
        self,
        websocket: RunnerWebSocket,
        snapshot: RunnerSnapshot,
        registry: RunnerRegistry,
        *,
        reconciler: RunnerJobReconciler | None = None,
        request_timeout: float = 30.0,
        event_queue_limit: int = 256,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if event_queue_limit <= 0 or event_queue_limit > 4096:
            raise ValueError("event_queue_limit must be between 1 and 4096")
        self._websocket = websocket
        self._snapshot = snapshot
        self._registry = registry
        self._reconciler = reconciler
        self._request_timeout = request_timeout
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[RunnerEvent] = asyncio.Queue(maxsize=event_queue_limit)
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._owner_loop = asyncio.get_running_loop()

    @classmethod
    async def accept(
        cls,
        websocket: RunnerWebSocket,
        credentials: RunnerCredentialStore,
        registry: RunnerRegistry,
        *,
        reconciler: RunnerJobReconciler | None = None,
        request_timeout: float = 30.0,
        event_queue_limit: int = 256,
    ) -> "RunnerWebSocketTransport":
        """Authenticate the first frame and enroll the Runner fail-closed."""

        try:
            raw_hello = await asyncio.wait_for(websocket.recv(), timeout=request_timeout)
            hello = RunnerHello.from_payload(decode_message(raw_hello))
            authenticated = credentials.authenticate(hello.runner_id, hello.credential)
            snapshot = registry.enroll(hello, authenticated.fingerprint)
        except (TimeoutError, RunnerProtocolError, RunnerCredentialError, RunnerConnectionError) as exc:
            try:
                await websocket.close()
            finally:
                raise RunnerAuthenticationError("runner enrollment failed") from exc

        transport = cls(
            websocket,
            snapshot,
            registry,
            reconciler=reconciler,
            request_timeout=request_timeout,
            event_queue_limit=event_queue_limit,
        )
        await websocket.send(
            encode_message(
                {
                    "type": "hello_ack",
                    "protocol_version": RUNNER_PROTOCOL_VERSION,
                    "runner_id": snapshot.runner_id,
                    "instance_id": snapshot.instance_id,
                }
            )
        )
        transport._reader_task = asyncio.create_task(transport._receive_loop())
        return transport

    @property
    def runner_id(self) -> str:
        return self._snapshot.runner_id

    @property
    def instance_id(self) -> str:
        return self._snapshot.instance_id

    @property
    def workspace_ids(self) -> tuple[str, ...]:
        return self._snapshot.workspace_ids

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def event_count(self) -> int:
        return self._events.qsize()

    async def wait_closed(self) -> None:
        """Wait until the authenticated Runner receive loop terminates."""

        reader = self._reader_task
        if reader is None:
            return
        try:
            await reader
        except asyncio.CancelledError:
            if not self._closed:
                raise

    async def next_event(self, *, timeout: float | None = None) -> RunnerEvent:
        if self._closed and self._events.empty():
            raise RunnerUnavailableError("runner transport is closed")
        try:
            if timeout is None:
                return await self._events.get()
            if timeout <= 0:
                raise ValueError("timeout must be positive")
            return await asyncio.wait_for(self._events.get(), timeout=timeout)
        except TimeoutError as exc:
            raise RunnerUnavailableError("runner event wait timed out") from exc

    def next_event_sync(self, *, timeout: float = 5.0) -> RunnerEvent:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if self._closed or not self._owner_loop.is_running():
            raise RunnerUnavailableError("runner transport is closed")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._owner_loop:
            raise RunnerTransportError(
                "synchronous Runner event wait cannot block the transport event loop"
            )
        future = asyncio.run_coroutine_threadsafe(
            self.next_event(timeout=timeout),
            self._owner_loop,
        )
        try:
            return future.result(timeout=timeout + 1.0)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RunnerUnavailableError("runner synchronous event wait timed out") from exc

    async def call(
        self,
        *,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        if self._closed:
            raise RunnerUnavailableError("runner transport is closed")
        snapshot = self._registry.get(self.runner_id)
        if snapshot is None or not snapshot.connected or snapshot.instance_id != self.instance_id:
            raise RunnerUnavailableError("runner is not connected")
        if workspace_id not in snapshot.workspace_ids:
            raise RunnerUnavailableError("workspace is not advertised by this runner")

        request_id = request_id or f"req-{secrets.token_hex(12)}"
        future = self.register_request(request_id)
        try:
            await self._websocket.send(
                encode_message(
                    rpc_request_payload(
                        request_id,
                        workspace_id=workspace_id,
                        method=method,
                        params=params,
                    )
                )
            )
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._request_timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise RunnerUnavailableError("runner RPC timed out") from exc
        except Exception:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise

    def call_sync(
        self,
        *,
        workspace_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        """Schedule one RPC on the transport's owning event loop and block here."""

        if self._closed or not self._owner_loop.is_running():
            raise RunnerUnavailableError("runner transport is closed")
        if self._on_owner_loop():
            raise RunnerTransportError(
                "synchronous runner RPC cannot block the transport event loop"
            )
        future = asyncio.run_coroutine_threadsafe(
            self.call(
                workspace_id=workspace_id,
                method=method,
                params=params,
                request_id=request_id,
            ),
            self._owner_loop,
        )
        try:
            return future.result(timeout=self._request_timeout + 1.0)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RunnerUnavailableError("runner synchronous RPC timed out") from exc

    def _on_owner_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._owner_loop
        except RuntimeError:
            return False

    def register_request(self, request_id: str) -> asyncio.Future[Any]:
        if request_id in self._pending:
            raise RunnerTransportError("duplicate runner request id")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    def resolve_response(
        self,
        request_id: str,
        *,
        result: Any = None,
        error: Exception | None = None,
    ) -> bool:
        future = self._pending.pop(request_id, None)
        if future is None:
            return False
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)
        return True

    def fail_all(self, error: Exception | None = None) -> None:
        error = error or RunnerUnavailableError("runner transport disconnected")
        for request_id, future in list(self._pending.items()):
            self._pending.pop(request_id, None)
            if not future.done():
                future.set_exception(error)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.fail_all(RunnerUnavailableError("runner transport closed"))
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        self._mark_disconnected()
        await self._websocket.close()

    def close_sync(self, *, timeout: float = 5.0) -> None:
        loop = self._owner_loop
        if loop is None or loop.is_closed():
            self._closed = True
            self._mark_disconnected()
            return
        if self._on_owner_loop():
            raise RunnerTransportError("synchronous Runner close cannot run on the transport event loop")
        future = asyncio.run_coroutine_threadsafe(self._close_from_control_plane(), loop)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RunnerUnavailableError("runner synchronous close timed out") from exc

    async def _close_from_control_plane(self) -> None:
        """Close the socket without racing the handler's reader waiter.

        ``wait_closed()`` may be awaiting the receive task from another coroutine
        on the same event loop. Cancelling that task here lets the waiter return
        and tear down the event loop before a cross-thread close future resolves.
        Closing the socket instead causes the reader to finish naturally, so the
        synchronous caller can observe a bounded, completed shutdown.
        """

        if self._closed:
            return
        self._closed = True
        self.fail_all(RunnerUnavailableError("runner transport closed"))
        self._mark_disconnected()
        await self._websocket.close()

    async def _receive_loop(self) -> None:
        try:
            while not self._closed:
                payload = decode_message(await self._websocket.recv())
                message_type = payload.get("type")
                if message_type == "rpc_response":
                    request_id, result, error = parse_rpc_response(payload)
                    if error is not None:
                        message = error.get("message")
                        if not isinstance(message, str) or not message:
                            message = "remote runner returned an error"
                        code = error.get("code")
                        if not isinstance(code, str) or not code:
                            code = "RUNNER_REMOTE_ERROR"
                        details = error.get("details")
                        self.resolve_response(
                            request_id,
                            error=RunnerRemoteError(
                                message,
                                code=code,
                                retryable=bool(error.get("retryable", False)),
                                details=details if isinstance(details, dict) else None,
                            ),
                        )
                    else:
                        self.resolve_response(request_id, result=result)
                    continue
                if message_type == "heartbeat":
                    heartbeat = RunnerHeartbeat.from_payload(payload)
                    if heartbeat.runner_id != self.runner_id or heartbeat.instance_id != self.instance_id:
                        raise RunnerProtocolError("heartbeat identity does not match authenticated runner")
                    self._snapshot = self._registry.heartbeat(heartbeat)
                    continue
                if message_type == "event":
                    self._handle_event(payload)
                    continue
                if message_type == "job_inventory":
                    self._handle_job_inventory(payload)
                    continue
                if message_type == "disconnect":
                    await self._handle_remote_disconnect(payload)
                    return
                raise RunnerProtocolError("unsupported runner message type")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self._closed = True
                self._mark_disconnected()
                self.fail_all(RunnerUnavailableError(str(exc) or "runner transport disconnected"))
                await self._websocket.close()

    def _handle_event(self, payload: dict[str, Any]) -> None:
        event = RunnerEvent.from_payload(payload)
        if event.runner_id != self.runner_id or event.instance_id != self.instance_id:
            raise RunnerProtocolError("runner event identity does not match authenticated runner")
        if event.workspace_id is not None and event.workspace_id not in self._snapshot.workspace_ids:
            raise RunnerProtocolError("runner event workspace is not advertised by this runner")
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise RunnerProtocolError("runner event queue is full") from exc

    async def _handle_remote_disconnect(self, payload: dict[str, Any]) -> None:
        message = RunnerDisconnect.from_payload(payload)
        if message.runner_id != self.runner_id or message.instance_id != self.instance_id:
            raise RunnerProtocolError("runner disconnect identity does not match authenticated runner")
        self._closed = True
        self.fail_all(RunnerUnavailableError("runner disconnected gracefully"))
        self._mark_disconnected()
        await self._websocket.send(
            encode_message(disconnect_ack_payload(self.runner_id, self.instance_id))
        )
        await self._websocket.close()

    def _handle_job_inventory(self, payload: dict[str, Any]) -> None:
        if (
            payload.get("runner_id") != self.runner_id
            or payload.get("instance_id") != self.instance_id
        ):
            raise RunnerProtocolError("runner job inventory identity does not match authenticated runner")
        if self._reconciler is None:
            return
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list) or len(raw_jobs) > 1024:
            raise RunnerProtocolError("runner job inventory must be a bounded array")
        inventory = [JobInventoryItem.from_payload(item) for item in raw_jobs]
        self._reconciler.reconcile(
            self.runner_id,
            inventory,
            runner_instance_id=self.instance_id,
        )

    def _mark_disconnected(self) -> None:
        snapshot = self._registry.get(self.runner_id)
        disconnected_current_instance = False
        if snapshot is not None and snapshot.connected and snapshot.instance_id == self.instance_id:
            try:
                self._snapshot = self._registry.disconnect(self.runner_id, self.instance_id)
                disconnected_current_instance = True
            except RunnerConnectionError:
                pass
        if self._reconciler is not None and disconnected_current_instance:
            self._reconciler.mark_runner_disconnected(
                self.runner_id,
                self.instance_id,
            )
