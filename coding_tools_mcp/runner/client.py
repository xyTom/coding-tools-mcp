"""Production Runner peer for outbound authenticated WebSocket connections."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import os
import secrets
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..agent_backends import CodexAppServerBackend, CodexAppServerConfig
from ..repo_fingerprint import build_repo_fingerprint
from ..semantic import LspSemanticBackend
from ..validation import ValidationBackend
from ..workspace_binding import WorkspaceBinding
from ..workspace_catalog import WorkspaceEntry
from ..workspace_host import LocalWorkspaceHost
from .capabilities import RunnerCapabilityError, RunnerCapabilityHost
from .protocol import (
    RUNNER_PROTOCOL_VERSION,
    RunnerDisconnect,
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
    ) -> None:
        if hello.runner_id != config.runner_id or hello.instance_id != config.instance_id:
            raise ValueError("Runner hello identity must match client configuration")
        self.config = config
        self.hello = hello
        self.router = router
        self._stop = asyncio.Event()
        self._websocket: AsyncSocketWebSocket | None = None
        self._heartbeat_sequence = 0
        self._owner_loop: asyncio.AbstractEventLoop | None = None

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
        try:
            await websocket.send(encode_message(self.hello.message_payload()))
            acknowledgement = decode_message(await websocket.recv())
            self._validate_hello_ack(acknowledgement)
            heartbeat = asyncio.create_task(self._heartbeat_loop(websocket))
            await self._serve_rpc(websocket)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
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

    def __init__(self, workspaces: Iterable[WorkspaceEntry]) -> None:
        entries = tuple(workspaces)
        if not entries:
            raise ValueError("Runner requires at least one Workspace")
        self.workspaces = {entry.id: entry for entry in entries}
        if len(self.workspaces) != len(entries):
            raise ValueError("Runner Workspace ids must be unique")
        for entry in entries:
            if entry.target != "local" or not isinstance(entry.root, Path):
                raise ValueError("Runner-local Workspace entries must use local Path roots")

        self.mcp_sessions = RunnerMcpSessionHost(self._runtime_factory)
        self.capabilities = RunnerCapabilityHost(
            agent_backend_factory=self._agent_backend_factory,
            semantic_backend_factory=self._semantic_backend_factory,
            validation_backend_factory=self._validation_backend_factory,
            fingerprint_factory=self._fingerprint_factory,
        )
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
            capabilities=("agent", "mcp", "semantic", "validation"),
            workspaces=tuple(
                WorkspaceInventoryItem(entry.id, entry.name, str(entry.root))
                for entry in self.workspaces.values()
                if entry.enabled
            ),
        )

    def close(self) -> None:
        self.capabilities.shutdown()
        self.mcp_sessions.shutdown()

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
        return Runtime(
            entry.root,
            workspace_binding=WorkspaceBinding(entry.id, entry.root, "runner"),
            transport="http",
        )

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coding Tools MCP remote Runner")
    parser.add_argument("--server", required=True, help="Control Plane Runner WebSocket URL (wss://.../runner/ws)")
    parser.add_argument("--runner-id", required=True)
    parser.add_argument("--workspace", action="append", required=True, metavar="ID=PATH")
    parser.add_argument("--credential-file")
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
        application = LocalRunnerApplication(workspaces)
        peer = RunnerPeer(
            config,
            application.hello(
                runner_id=config.runner_id,
                instance_id=config.instance_id,
                credential=config.credential,
            ),
            application.router,
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
    "load_runner_credential",
    "main",
    "parse_workspace_spec",
    "validate_runner_url",
]
