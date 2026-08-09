"""Codex App Server v2 adapter over the supported stdio JSONL transport."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .. import __version__
from ..json_utils import strict_json_bytes, strict_json_loads
from .base import AgentBackendError, AgentBackendEvent, BackendHealth, BackendThread, BackendTurn


MAX_MESSAGE_BYTES = 2_000_000
APPROVAL_METHODS = frozenset(
    {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
)
APPROVAL_DECISIONS = frozenset(
    {"accept", "acceptForSession", "decline", "cancel"}
)


@dataclass(frozen=True)
class CodexAppServerConfig:
    workspace_root: Path
    command: tuple[str, ...] = ("codex", "app-server", "--listen", "stdio://")
    request_timeout_seconds: float = 30.0
    max_message_bytes: int = MAX_MESSAGE_BYTES
    approval_policy: str | None = "on-request"
    approvals_reviewer: str | None = "user"

    @classmethod
    def create(
        cls,
        workspace_root: str | Path,
        *,
        command: tuple[str, ...] | None = None,
        request_timeout_seconds: float = 30.0,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
        approval_policy: str | None = "on-request",
        approvals_reviewer: str | None = "user",
    ) -> "CodexAppServerConfig":
        try:
            root = Path(workspace_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AgentBackendError(
                "AGENT_WORKSPACE_UNAVAILABLE",
                "Agent workspace root cannot be resolved.",
                details={"reason": str(exc)[:300]},
            ) from exc
        if not root.is_dir():
            raise AgentBackendError(
                "AGENT_WORKSPACE_UNAVAILABLE",
                "Agent workspace root must be an existing directory.",
            )
        chosen = tuple(command or cls.command)
        if not chosen or not all(isinstance(item, str) and item for item in chosen):
            raise AgentBackendError(
                "AGENT_BACKEND_CONFIG_INVALID",
                "Codex App Server command must contain non-empty strings.",
            )
        if request_timeout_seconds <= 0 or max_message_bytes < 1024:
            raise AgentBackendError(
                "AGENT_BACKEND_CONFIG_INVALID",
                "Codex App Server timeout/message limits are invalid.",
            )
        return cls(
            workspace_root=root,
            command=chosen,
            request_timeout_seconds=float(request_timeout_seconds),
            max_message_bytes=int(max_message_bytes),
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
        )


class CodexAppServerBackend:
    """Small synchronous client for the app-server v2 thread/turn lifecycle."""

    def __init__(self, config: CodexAppServerConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[bytes] | None = None
        self._initialized = False
        self._closed = False
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any] | AgentBackendError]] = {}
        self._events: queue.Queue[AgentBackendEvent] = queue.Queue(maxsize=2000)
        self._event_sequence = 0
        self._event_lock = threading.Lock()
        self._approvals_lock = threading.Lock()
        self._approvals: dict[str, tuple[int | str, str]] = {}

    @property
    def backend_kind(self) -> str:
        return "codex-app-server"

    def start(self) -> None:
        if self._initialized:
            return
        if self._closed:
            raise AgentBackendError("AGENT_BACKEND_CLOSED", "Agent backend is closed.")
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                list(self.config.command),
                cwd=self.config.workspace_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=flags,
            )
        except (OSError, ValueError) as exc:
            raise AgentBackendError(
                "AGENT_BACKEND_UNAVAILABLE",
                "Could not start Codex App Server.",
                retryable=True,
                details={"reason": str(exc)[:300]},
            ) from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            result = self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "coding_tools_mcp",
                        "title": "Coding Tools MCP Agent Platform",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
                ensure_started=False,
            )
            if not isinstance(result, dict):
                raise AgentBackendError(
                    "AGENT_BACKEND_PROTOCOL_ERROR",
                    "Codex App Server initialize result was not an object.",
                )
            self._notify("initialized")
            self._initialized = True
        except BaseException:
            self.close()
            raise

    def health(self) -> BackendHealth:
        try:
            self.start()
        except AgentBackendError as exc:
            return BackendHealth(False, self.backend_kind, error=exc.payload())
        process = self.process
        if process is None or process.poll() is not None:
            return BackendHealth(
                False,
                self.backend_kind,
                error={
                    "code": "AGENT_BACKEND_UNAVAILABLE",
                    "message": "Codex App Server is not running.",
                    "retryable": True,
                    "details": {},
                },
            )
        return BackendHealth(True, self.backend_kind)

    def create_thread(self, *, instructions: str | None = None) -> BackendThread:
        result = self._request_object("thread/start", self._thread_params(instructions))
        return self._thread_from_result(result, "thread/start")

    def resume_thread(
        self,
        thread_id: str,
        *,
        instructions: str | None = None,
    ) -> BackendThread:
        params = self._thread_params(instructions)
        params["threadId"] = self._identifier(thread_id, "thread_id")
        result = self._request_object("thread/resume", params)
        return self._thread_from_result(result, "thread/resume")

    def send_turn(self, thread_id: str, message: str) -> BackendTurn:
        thread_id = self._identifier(thread_id, "thread_id")
        if not isinstance(message, str) or not message.strip() or len(message) > 2_000_000:
            raise AgentBackendError(
                "AGENT_INPUT_INVALID",
                "Agent turn message must be non-empty bounded text.",
            )
        result = self._request_object(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": message}]},
        )
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AgentBackendError(
                "AGENT_BACKEND_PROTOCOL_ERROR",
                "Codex App Server turn/start result did not contain a turn id.",
            )
        return BackendTurn(str(turn["id"]), dict(turn))

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self._request_object(
            "turn/interrupt",
            {
                "threadId": self._identifier(thread_id, "thread_id"),
                "turnId": self._identifier(turn_id, "turn_id"),
            },
        )

    def approve(self, approval_id: str, decision: str) -> None:
        approval_id = self._identifier(approval_id, "approval_id")
        if decision not in APPROVAL_DECISIONS:
            raise AgentBackendError(
                "AGENT_APPROVAL_INVALID",
                "Unsupported approval decision.",
                details={"supported": sorted(APPROVAL_DECISIONS)},
            )
        with self._approvals_lock:
            pending = self._approvals.pop(approval_id, None)
        if pending is None:
            raise AgentBackendError(
                "AGENT_APPROVAL_NOT_FOUND",
                "Approval request is no longer pending.",
            )
        request_id, method = pending
        if method not in APPROVAL_METHODS:
            raise AgentBackendError("AGENT_APPROVAL_INVALID", "Server request is not an approval.")
        self._write({"id": request_id, "result": {"decision": decision}})

    def list_threads(self, *, limit: int = 50) -> list[BackendThread]:
        if type(limit) is not int or limit < 1:
            raise AgentBackendError("AGENT_INPUT_INVALID", "limit must be a positive integer.")
        result = self._request_object("thread/list", {"limit": min(limit, 200)})
        data = result.get("data")
        if not isinstance(data, list):
            raise AgentBackendError(
                "AGENT_BACKEND_PROTOCOL_ERROR",
                "Codex App Server thread/list result did not contain a data list.",
            )
        threads: list[BackendThread] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise AgentBackendError(
                    "AGENT_BACKEND_PROTOCOL_ERROR",
                    "Codex App Server thread/list returned an invalid thread.",
                )
            threads.append(BackendThread(str(item["id"]), dict(item)))
        return threads

    def close_thread(self, thread_id: str) -> None:
        self._request_object(
            "thread/unsubscribe",
            {"threadId": self._identifier(thread_id, "thread_id")},
        )

    def stream_events(self, *, timeout: float | None = None) -> Iterator[AgentBackendEvent]:
        if timeout is not None and timeout < 0:
            raise AgentBackendError("AGENT_INPUT_INVALID", "timeout cannot be negative.")
        while True:
            try:
                yield self._events.get(timeout=timeout)
            except queue.Empty:
                return

    def drain_events(self, *, limit: int = 100) -> list[AgentBackendEvent]:
        if type(limit) is not int or limit < 1:
            raise AgentBackendError("AGENT_INPUT_INVALID", "limit must be a positive integer.")
        result: list[AgentBackendEvent] = []
        for _ in range(min(limit, 1000)):
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                break
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    process.terminate()
                process.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except OSError:
                pass
        self._fail_pending(
            AgentBackendError(
                "AGENT_BACKEND_CLOSED",
                "Agent backend is closed.",
                retryable=True,
            )
        )

    def _thread_params(self, instructions: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": str(self.config.workspace_root)}
        if instructions:
            params["developerInstructions"] = instructions
        if self.config.approval_policy is not None:
            params["approvalPolicy"] = self.config.approval_policy
        if self.config.approvals_reviewer is not None:
            params["approvalsReviewer"] = self.config.approvals_reviewer
        return params

    @staticmethod
    def _thread_from_result(result: dict[str, Any], method: str) -> BackendThread:
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AgentBackendError(
                "AGENT_BACKEND_PROTOCOL_ERROR",
                f"Codex App Server {method} result did not contain a thread id.",
            )
        return BackendThread(str(thread["id"]), dict(thread))

    @staticmethod
    def _identifier(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
            raise AgentBackendError(
                "AGENT_INPUT_INVALID",
                f"{field} must be a non-empty bounded string.",
            )
        return value

    def _request_object(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = self._request(method, params)
        if not isinstance(result, dict):
            raise AgentBackendError(
                "AGENT_BACKEND_PROTOCOL_ERROR",
                f"Codex App Server {method} result was not an object.",
            )
        return result

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        ensure_started: bool = True,
    ) -> Any:
        if ensure_started and not self._initialized:
            self.start()
        request_id = self._next_request_id()
        responses: queue.Queue[dict[str, Any] | AgentBackendError] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = responses
        try:
            self._write({"method": method, "id": request_id, "params": params})
            deadline = time.monotonic() + self.config.request_timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentBackendError(
                        "AGENT_BACKEND_TIMEOUT",
                        "Timed out waiting for Codex App Server.",
                        retryable=True,
                        details={"method": method},
                    )
                process = self.process
                if process is None or (process.poll() is not None and responses.empty()):
                    raise self._process_exited_error()
                try:
                    response = responses.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                if isinstance(response, AgentBackendError):
                    raise response
                return self._parse_response(response, request_id, method)
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def _next_request_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _write(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            raise self._process_exited_error()
        if process.stdin is None:
            raise AgentBackendError(
                "AGENT_BACKEND_DISCONNECTED",
                "Codex App Server stdin is closed.",
                retryable=True,
            )
        try:
            encoded = strict_json_bytes(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ) + b"\n"
        except (TypeError, ValueError) as exc:
            raise AgentBackendError(
                "AGENT_BACKEND_PROTOCOL_ERROR",
                "Codex App Server request was not valid standard JSON.",
            ) from exc
        if len(encoded) > self.config.max_message_bytes:
            raise AgentBackendError(
                "AGENT_BACKEND_REQUEST_TOO_LARGE",
                "Codex App Server request exceeded the supported size.",
            )
        try:
            with self._write_lock:
                process.stdin.write(encoded)
                process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AgentBackendError(
                "AGENT_BACKEND_DISCONNECTED",
                "Codex App Server disconnected.",
                retryable=True,
            ) from exc

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        try:
            while True:
                raw = stream.readline(self.config.max_message_bytes + 2)
                if not raw:
                    return
                if len(raw) > self.config.max_message_bytes:
                    if not raw.endswith(b"\n"):
                        self._drain_line(stream)
                    self._fail_pending(
                        AgentBackendError(
                            "AGENT_BACKEND_RESPONSE_TOO_LARGE",
                            "Codex App Server response exceeded the supported size.",
                        )
                    )
                    continue
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = strict_json_loads(line)
                except (UnicodeError, ValueError):
                    self._fail_pending(
                        AgentBackendError(
                            "AGENT_BACKEND_PROTOCOL_ERROR",
                            "Codex App Server returned invalid JSON.",
                        )
                    )
                    continue
                if not isinstance(message, dict):
                    self._fail_pending(
                        AgentBackendError(
                            "AGENT_BACKEND_PROTOCOL_ERROR",
                            "Codex App Server message was not an object.",
                        )
                    )
                    continue
                self._route_message(message)
        except (OSError, ValueError):
            self._fail_pending(
                AgentBackendError(
                    "AGENT_BACKEND_DISCONNECTED",
                    "Codex App Server stdout disconnected.",
                    retryable=True,
                )
            )

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while process.stderr.readline():
                pass
        except (OSError, ValueError):
            return

    @staticmethod
    def _drain_line(stream: Any) -> None:
        while True:
            chunk = stream.readline(65_536)
            if not chunk or chunk.endswith(b"\n"):
                return

    def _route_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and isinstance(method, str):
            self._handle_server_request(request_id, method, message.get("params"))
            return
        if request_id is not None and ("result" in message or "error" in message):
            if type(request_id) is not int:
                return
            with self._pending_lock:
                target = self._pending.get(request_id)
            if target is not None:
                try:
                    target.put_nowait(message)
                except queue.Full:
                    pass
            return
        if isinstance(method, str):
            params = message.get("params")
            self._queue_event(
                "notification",
                method,
                dict(params) if isinstance(params, dict) else {},
            )

    def _handle_server_request(self, request_id: Any, method: str, params: Any) -> None:
        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            return
        normalized = dict(params) if isinstance(params, dict) else {}
        if method in APPROVAL_METHODS:
            candidate = normalized.get("approvalId")
            approval_id = (
                candidate
                if isinstance(candidate, str) and candidate
                else f"request-{request_id}"
            )
            with self._approvals_lock:
                if approval_id in self._approvals:
                    approval_id = f"request-{request_id}"
                self._approvals[approval_id] = (request_id, method)
            self._queue_event(
                "approval",
                method,
                normalized,
                approval_id=approval_id,
            )
            return
        self._queue_event("server_request", method, normalized)
        try:
            self._write(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "Client does not implement this server request.",
                    },
                }
            )
        except AgentBackendError:
            pass

    def _queue_event(
        self,
        kind: str,
        method: str,
        params: dict[str, Any],
        *,
        approval_id: str | None = None,
    ) -> None:
        with self._event_lock:
            self._event_sequence += 1
            sequence = self._event_sequence
        if self._events.full():
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
        try:
            self._events.put_nowait(
                AgentBackendEvent(sequence, kind, method, params, approval_id)
            )
        except queue.Full:
            pass

    @staticmethod
    def _parse_response(response: dict[str, Any], request_id: int, method: str) -> Any:
        if response.get("id") != request_id:
            raise AgentBackendError(
                "AGENT_BACKEND_PROTOCOL_ERROR",
                "Codex App Server response id did not match the request.",
                details={"method": method},
            )
        has_result = "result" in response
        has_error = "error" in response
        if has_result == has_error:
            raise AgentBackendError(
                "AGENT_BACKEND_PROTOCOL_ERROR",
                "Codex App Server response must contain exactly one of result or error.",
                details={"method": method},
            )
        if has_error:
            error = response.get("error")
            message = "Codex App Server request failed."
            code: Any = None
            if isinstance(error, dict):
                code = error.get("code")
                if isinstance(error.get("message"), str):
                    message = str(error["message"])[:1000]
            raise AgentBackendError(
                "AGENT_BACKEND_RPC_ERROR",
                message,
                retryable=code == -32001,
                details={"method": method, "rpc_code": code},
            )
        return response.get("result")

    def _process_exited_error(self) -> AgentBackendError:
        process = self.process
        return AgentBackendError(
            "AGENT_BACKEND_UNAVAILABLE",
            "Codex App Server is unavailable.",
            retryable=True,
            details={"returncode": process.poll() if process is not None else None},
        )

    def _fail_pending(self, error: AgentBackendError) -> None:
        with self._pending_lock:
            targets = list(self._pending.values())
        for target in targets:
            try:
                target.put_nowait(error)
            except queue.Full:
                pass


__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVAL_METHODS",
    "CodexAppServerBackend",
    "CodexAppServerConfig",
]
