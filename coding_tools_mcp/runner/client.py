"""Production Runner peer for outbound authenticated WebSocket connections."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import os
import queue
import secrets
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..agent_backends import CodexAppServerBackend, CodexAppServerConfig
from ..repo_fingerprint import build_repo_fingerprint
from ..semantic import LspSemanticBackend
from ..validation import ValidationBackend
from ..workspace_binding import WorkspaceBinding
from ..workspace_catalog import WorkspaceEntry
from ..workspace_host import LocalWorkspaceHost
from .capabilities import RunnerCapabilityError, RunnerCapabilityHost
from .jobs import JobInventoryItem, MAX_RUNNER_JOBS, RunnerJobInventoryRegistry
from .protocol import (
    RUNNER_PROTOCOL_VERSION,
    RunnerDisconnect,
    RunnerEvent,
    RunnerHello,
    RunnerProtocolError,
    WorkspaceInventoryItem,
    decode_message,
    encode_message,
    parse_rpc_request,
    rpc_response_payload,
)
from .routing import RemoteMcpRouteError, RunnerMcpRouter, RunnerMcpSessionHost
from .websocket import (
    AsyncSocketWebSocket,
    WebSocketClosedError,
    WebSocketProtocolError,
    connect_websocket,
)


RUNNER_CREDENTIAL_ENV = "CODING_TOOLS_MCP_RUNNER_CREDENTIAL"
MAX_RUNNER_PENDING_EVENTS = 1000


class RunnerClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunnerClientConfig:
    server_url: str
    runner_id: str
    instance_id: str
    credential: str
    heartbeat_seconds: float = 15.0
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if self.reconnect_initial_seconds <= 0 or self.reconnect_max_seconds <= 0:
            raise ValueError("reconnect delays must be positive")
        if self.reconnect_initial_seconds > self.reconnect_max_seconds:
            raise ValueError("reconnect_initial_seconds cannot exceed reconnect_max_seconds")


class RunnerPeer:
    """One Runner process identity with reconnect-safe outbound RPC serving."""

    def __init__(
        self,
        config: RunnerClientConfig,
        hello: RunnerHello,
        router: RunnerMcpRouter,
        *,
        job_inventory_provider: Callable[[], Iterable[JobInventoryItem]] | None = None,
    ) -> None:
        if hello.runner_id != config.runner_id or hello.instance_id != config.instance_id:
            raise ValueError("Runner hello identity must match client configuration")
        self.config = config
        self.hello = hello
        self.router = router
        self._job_inventory_provider = job_inventory_provider
        self._stop = asyncio.Event()
        self._websocket: AsyncSocketWebSocket | None = None
        self._heartbeat_sequence = 0
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._events: queue.Queue[RunnerEvent] = queue.Queue(maxsize=MAX_RUNNER_PENDING_EVENTS)

    async def run_forever(self) -> None:
        delay = self.config.reconnect_initial_seconds
        while not self._stop.is_set():
            try:
                await self.run_once()
                delay = self.config.reconnect_initial_seconds
            except asyncio.CancelledError:
                raise
            except (
                OSError,
                RunnerClientError,
                RunnerProtocolError,
                WebSocketClosedError,
                WebSocketProtocolError,
            ):
                if self._stop.is_set():
                    return
                await asyncio.sleep(delay)
                delay = min(self.config.reconnect_max_seconds, delay * 2.0)

    async def run_once(self) -> None:
        self._owner_loop = asyncio.get_running_loop()
        connection = await asyncio.to_thread(connect_websocket, self.config.server_url)
        websocket = connection.websocket
        self._websocket = websocket
        heartbeat: asyncio.Task[None] | None = None
        event_sender: asyncio.Task[None] | None = None
        try:
            await websocket.send(encode_message(self.hello.message_payload()))
            acknowledgement = decode_message(await websocket.recv())
            self._validate_hello_ack(acknowledgement)
            await self._send_job_inventory(websocket)
            event_sender = asyncio.create_task(self._event_sender_loop(websocket))
            heartbeat = asyncio.create_task(self._heartbeat_loop(websocket))
            await self._serve_rpc(websocket)
        finally:
            for task in (heartbeat, event_sender):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._websocket = None
            await websocket.close()

    def stop_sync(self, reason: str = "runner stopping", *, timeout: float = 5.0) -> None:
        loop = self._owner_loop
        if loop is None or loop.is_closed():
            self._stop.set()
            return
        future = asyncio.run_coroutine_threadsafe(self.stop(reason), loop)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RunnerClientError("Runner stop acknowledgement timed out.") from exc

    async def stop(self, reason: str = "runner stopping") -> None:
        self._stop.set()
        websocket = self._websocket
        if websocket is None:
            return
        try:
            await websocket.send(
                encode_message(
                    RunnerDisconnect(
                        self.config.runner_id,
                        self.config.instance_id,
                        reason=reason[:256],
                    ).message_payload()
                )
            )
        except (OSError, WebSocketClosedError):
            pass

    def publish_event(self, event: RunnerEvent) -> None:
        if not isinstance(event, RunnerEvent):
            raise RunnerClientError("Runner event is invalid.")
        if (
            event.runner_id != self.config.runner_id
            or event.instance_id != self.config.instance_id
        ):
            raise RunnerClientError("Runner event identity does not match this peer.")
        if event.workspace_id is not None:
            advertised = {item.workspace_id for item in self.hello.workspaces}
            if event.workspace_id not in advertised:
                raise RunnerClientError("Runner event Workspace is not advertised by this peer.")
        try:
            self._events.put_nowait(event)
        except queue.Full as exc:
            raise RunnerClientError("Runner event queue is full.") from exc

    async def _serve_rpc(self, websocket: AsyncSocketWebSocket) -> None:
        while not self._stop.is_set():
            payload = decode_message(await websocket.recv())
            message_type = payload.get("type")
            if message_type == "rpc_request":
                request_id, workspace_id, method, params = parse_rpc_request(payload)
                try:
                    result = await asyncio.to_thread(
                        self.router.dispatch,
                        workspace_id,
                        method,
                        params,
                    )
                    response = rpc_response_payload(request_id, result=result)
                except (RunnerCapabilityError, RemoteMcpRouteError) as exc:
                    response = rpc_response_payload(
                        request_id,
                        error={
                            "code": exc.code,
                            "message": str(exc),
                            "retryable": bool(getattr(exc, "retryable", False)),
                            "details": dict(getattr(exc, "details", {}) or {}),
                        },
                    )
                except Exception:
                    # Unknown backend exceptions are deliberately redacted at
                    # this trust boundary; local logs may contain diagnostics.
                    response = rpc_response_payload(
                        request_id,
                        error={
                            "code": "RUNNER_RPC_FAILED",
                            "message": "Runner RPC failed.",
                            "retryable": False,
                        },
                    )
                await websocket.send(encode_message(response))
                continue
            if message_type == "disconnect_ack":
                if (
                    payload.get("runner_id") != self.config.runner_id
                    or payload.get("instance_id") != self.config.instance_id
                ):
                    raise RunnerProtocolError("disconnect acknowledgement identity mismatch")
                return
            raise RunnerProtocolError("unsupported Control Plane Runner message type")

    async def _send_job_inventory(self, websocket: AsyncSocketWebSocket) -> None:
        provider = self._job_inventory_provider
        if provider is None:
            return
        items = tuple(provider())
        if len(items) > MAX_RUNNER_JOBS:
            raise RunnerClientError("Runner job inventory exceeds the bounded limit.")
        if not all(isinstance(item, JobInventoryItem) for item in items):
            raise RunnerClientError("Runner job inventory provider returned an invalid item.")
        await websocket.send(
            encode_message(
                {
                    "type": "job_inventory",
                    "runner_id": self.config.runner_id,
                    "instance_id": self.config.instance_id,
                    "jobs": [item.payload() for item in items],
                }
            )
        )

    async def _heartbeat_loop(self, websocket: AsyncSocketWebSocket) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.config.heartbeat_seconds)
            if self._stop.is_set():
                return
            self._heartbeat_sequence += 1
            await websocket.send(
                encode_message(
                    {
                        "type": "heartbeat",
                        "runner_id": self.config.runner_id,
                        "instance_id": self.config.instance_id,
                        "sequence": self._heartbeat_sequence,
                    }
                )
            )

    async def _event_sender_loop(self, websocket: AsyncSocketWebSocket) -> None:
        while not self._stop.is_set():
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            await websocket.send(encode_message(event.message_payload()))

    def _validate_hello_ack(self, payload: dict[str, Any]) -> None:
        if payload.get("type") != "hello_ack":
            raise RunnerProtocolError("Control Plane did not acknowledge Runner hello")
        if payload.get("protocol_version") != RUNNER_PROTOCOL_VERSION:
            raise RunnerProtocolError("Control Plane Runner protocol version mismatch")
        if payload.get("runner_id") != self.config.runner_id:
            raise RunnerProtocolError("Control Plane Runner id acknowledgement mismatch")
        if payload.get("instance_id") != self.config.instance_id:
            raise RunnerProtocolError("Control Plane Runner instance acknowledgement mismatch")


class LocalRunnerApplication:
    """Runner-local composition of MCP/Agent/Semantic/Validation capabilities."""

    def __init__(
        self,
        workspaces: Iterable[WorkspaceEntry],
        *,
        upstream_manager_factory: Callable[[], Any] | None = None,
    ) -> None:
        entries = tuple(workspaces)
        if not entries:
            raise ValueError("Runner requires at least one Workspace")
        self.workspaces = {entry.id: entry for entry in entries}
        if len(self.workspaces) != len(entries):
            raise ValueError("Runner Workspace ids must be unique")
        for entry in entries:
            if entry.target != "local" or not isinstance(entry.root, Path):
                raise ValueError("Runner-local Workspace entries must use local Path roots")
        self._upstream_manager_factory = upstream_manager_factory

        self.mcp_sessions = RunnerMcpSessionHost(self._runtime_factory)
        self.capabilities = RunnerCapabilityHost(
            agent_backend_factory=self._agent_backend_factory,
            semantic_backend_factory=self._semantic_backend_factory,
            validation_backend_factory=self._validation_backend_factory,
            fingerprint_factory=self._fingerprint_factory,
        )
        self.jobs = RunnerJobInventoryRegistry()
        self.router = RunnerMcpRouter(self.mcp_sessions, self.capabilities.dispatch)

    def hello(
        self,
        *,
        runner_id: str,
        instance_id: str,
        credential: str,
    ) -> RunnerHello:
        return RunnerHello(
            runner_id=runner_id,
            instance_id=instance_id,
            credential=credential,
            capabilities=("agent", "jobs", "mcp", "semantic", "validation"),
            workspaces=tuple(
                WorkspaceInventoryItem(entry.id, entry.name, str(entry.root))
                for entry in self.workspaces.values()
                if entry.enabled
            ),
        )

    def close(self) -> None:
        self.capabilities.shutdown()
        self.mcp_sessions.shutdown()

    def job_inventory(self) -> tuple[JobInventoryItem, ...]:
        """Merge explicit Runner jobs with Runtime-derived ExecSession jobs."""

        merged = {item.job_id: item for item in self.jobs.snapshot()}
        for item in self.mcp_sessions.job_inventory():
            existing = merged.get(item.job_id)
            if existing is not None and existing != item:
                raise RunnerClientError("Runner job inventory contains conflicting job identities.")
            merged[item.job_id] = item
        if len(merged) > MAX_RUNNER_JOBS:
            raise RunnerClientError("Runner job inventory exceeds the bounded limit.")
        return tuple(merged[job_id] for job_id in sorted(merged))

    def _entry(self, workspace_id: str) -> WorkspaceEntry:
        try:
            entry = self.workspaces[workspace_id]
        except KeyError as exc:
            raise RemoteMcpRouteError(
                "RUNNER_WORKSPACE_NOT_FOUND",
                "Runner Workspace is not configured.",
            ) from exc
        if not entry.enabled:
            raise RemoteMcpRouteError(
                "RUNNER_WORKSPACE_NOT_FOUND",
                "Runner Workspace is disabled.",
            )
        return entry

    def _runtime_factory(self, workspace_id: str, authorization_digest: str) -> Any:
        del authorization_digest
        from ..server import Runtime

        entry = self._entry(workspace_id)
        upstream_manager = (
            self._upstream_manager_factory()
            if self._upstream_manager_factory is not None
            else None
        )
        try:
            return Runtime(
                entry.root,
                workspace_binding=WorkspaceBinding(entry.id, entry.root, "runner"),
                upstream_manager=upstream_manager,
                transport="http",
            )
        except BaseException:
            if upstream_manager is not None:
                upstream_manager.close()
            raise

    def _agent_backend_factory(self, workspace_id: str, backend_kind: str) -> CodexAppServerBackend:
        entry = self._entry(workspace_id)
        if backend_kind != "codex-app-server":
            raise RunnerCapabilityError(
                "AGENT_BACKEND_UNAVAILABLE",
                "Unsupported Runner Agent backend.",
            )
        return CodexAppServerBackend(CodexAppServerConfig.create(entry.root))

    def _semantic_backend_factory(self, workspace_id: str) -> LspSemanticBackend:
        return LspSemanticBackend(self._entry(workspace_id).root)

    def _validation_backend_factory(self, workspace_id: str) -> ValidationBackend:
        entry = self._entry(workspace_id)
        return LocalWorkspaceHost(entry).get_validation_backend()

    def _fingerprint_factory(self, workspace_id: str) -> dict[str, Any]:
        return build_repo_fingerprint(self._entry(workspace_id).root)


def parse_workspace_spec(spec: str) -> WorkspaceEntry:
    workspace_id, separator, raw_root = spec.partition("=")
    if not separator or not workspace_id.strip() or not raw_root.strip():
        raise ValueError("--workspace must use ID=PATH")
    root = Path(raw_root.strip()).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Runner Workspace root must be a directory")
    return WorkspaceEntry(
        workspace_id.strip(),
        workspace_id.strip(),
        root,
        True,
        False,
    )


def load_runner_credential(path: str | None) -> str:
    if path:
        value = Path(path).expanduser().read_text(encoding="utf-8").strip()
    else:
        value = os.environ.get(RUNNER_CREDENTIAL_ENV, "").strip()
    if not value:
        raise RunnerClientError(
            f"Runner credential is required via {RUNNER_CREDENTIAL_ENV} or --credential-file."
        )
    if len(value) > 512:
        raise RunnerClientError("Runner credential is invalid.")
    return value


def validate_runner_url(url: str, *, allow_insecure_ws: bool) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "wss":
        return url
    if parsed.scheme != "ws" or not allow_insecure_ws:
        raise RunnerClientError("Runner connection must use wss:// (or explicit --allow-insecure-ws).")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RunnerClientError("Insecure ws:// Runner connections are restricted to loopback hosts.")
    return url


def build_runner_upstream_manager_factory(
    upstream_config: str | None,
) -> Callable[[], Any] | None:
    """Build one Runner-local immutable catalog template and per-Runtime clients."""

    from ..secret_vault import SecretVault, SecretVaultError
    from ..settings_store import default_settings_dir
    from ..upstream import UpstreamConfigError
    from ..server import (
        ENV_PREFIX,
        SERVER_SECRET_VAULT_FILENAME,
        build_upstream_manager,
        discover_upstream_catalog_template,
        load_upstream_startup,
        upstream_secret_resolver,
    )

    namespace = argparse.Namespace(upstream_config=upstream_config)
    config_dir = default_settings_dir()
    try:
        snapshot = load_upstream_startup(namespace, config_dir)
        if not snapshot.configs:
            return None
        vault = SecretVault(
            config_dir / SERVER_SECRET_VAULT_FILENAME,
            os.environ.get(f"{ENV_PREFIX}_SECRETS_KEY"),
        )
        secret_resolver = upstream_secret_resolver(snapshot, vault)
        template = discover_upstream_catalog_template(
            snapshot,
            secret_resolver=secret_resolver,
        )
    except (OSError, ValueError, UpstreamConfigError, SecretVaultError) as exc:
        raise RunnerClientError(f"Runner upstream configuration is unavailable: {exc}") from exc

    return lambda: build_upstream_manager(
        snapshot,
        secret_resolver=secret_resolver,
        catalog_template=template,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coding Tools MCP remote Runner")
    parser.add_argument("--server", required=True, help="Control Plane Runner WebSocket URL (wss://.../runner/ws)")
    parser.add_argument("--runner-id", required=True)
    parser.add_argument("--workspace", action="append", required=True, metavar="ID=PATH")
    parser.add_argument("--credential-file")
    parser.add_argument(
        "--upstream-config",
        help="Runner-local upstream Gateway config; defaults to local server config/env when present",
    )
    parser.add_argument("--heartbeat-seconds", type=float, default=15.0)
    parser.add_argument("--reconnect-initial-seconds", type=float, default=1.0)
    parser.add_argument("--reconnect-max-seconds", type=float, default=30.0)
    parser.add_argument("--allow-insecure-ws", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        server_url = validate_runner_url(args.server, allow_insecure_ws=args.allow_insecure_ws)
        credential = load_runner_credential(args.credential_file)
        workspaces = tuple(parse_workspace_spec(item) for item in args.workspace)
        instance_id = f"instance-{secrets.token_hex(12)}"
        config = RunnerClientConfig(
            server_url=server_url,
            runner_id=args.runner_id,
            instance_id=instance_id,
            credential=credential,
            heartbeat_seconds=args.heartbeat_seconds,
            reconnect_initial_seconds=args.reconnect_initial_seconds,
            reconnect_max_seconds=args.reconnect_max_seconds,
        )
        application = LocalRunnerApplication(
            workspaces,
            upstream_manager_factory=build_runner_upstream_manager_factory(args.upstream_config),
        )
        peer = RunnerPeer(
            config,
            application.hello(
                runner_id=config.runner_id,
                instance_id=config.instance_id,
                credential=config.credential,
            ),
            application.router,
            job_inventory_provider=application.job_inventory,
        )
    except (OSError, ValueError, RunnerClientError, RunnerProtocolError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(peer.run_forever())
    except KeyboardInterrupt:
        return 130
    finally:
        application.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LocalRunnerApplication",
    "RUNNER_CREDENTIAL_ENV",
    "RunnerClientConfig",
    "RunnerClientError",
    "RunnerPeer",
    "build_runner_upstream_manager_factory",
    "load_runner_credential",
    "main",
    "parse_workspace_spec",
    "validate_runner_url",
]
