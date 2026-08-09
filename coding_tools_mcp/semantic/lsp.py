from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .base import SemanticBackend, SemanticPayload

DEFAULT_MAX_RESULTS = 200
DEFAULT_MAX_TEXT_CHARS = 4_000
DEFAULT_MAX_DOCUMENT_BYTES = 2 * 1_048_576
DEFAULT_MAX_LSP_MESSAGE_BYTES = 4 * 1_048_576
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_DIAGNOSTICS_WAIT_SECONDS = 0.5

_SAFE_LSP_ENV_NAMES = {
    "APPDATA",
    "CARGO_HOME",
    "COMSPEC",
    "GIT_CONFIG_GLOBAL",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "RUSTUP_HOME",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
}


@dataclass(frozen=True, slots=True)
class LspServerSpec:
    language: str
    language_id: str
    extensions: tuple[str, ...]
    candidates: tuple[tuple[str, ...], ...]
    language_ids: tuple[tuple[str, str], ...] = ()

    def language_id_for_path(self, path: Path) -> str:
        suffix = path.suffix.lower()
        for extension, language_id in self.language_ids:
            if extension.lower() == suffix:
                return language_id
        return self.language_id


DEFAULT_LSP_SERVER_SPECS: tuple[LspServerSpec, ...] = (
    LspServerSpec(
        language="python",
        language_id="python",
        extensions=(".py", ".pyi"),
        candidates=(
            ("basedpyright-langserver", "--stdio"),
            ("pyright-langserver", "--stdio"),
            ("pylsp",),
        ),
    ),
    LspServerSpec(
        language="typescript",
        language_id="typescript",
        extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        candidates=(("typescript-language-server", "--stdio"),),
        language_ids=(
            (".tsx", "typescriptreact"),
            (".js", "javascript"),
            (".jsx", "javascriptreact"),
            (".mjs", "javascript"),
            (".cjs", "javascript"),
        ),
    ),
    LspServerSpec(
        language="rust",
        language_id="rust",
        extensions=(".rs",),
        candidates=(("rust-analyzer",),),
    ),
)


class _SemanticFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = "error",
        language: str | None = None,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.language = language
        self.path = path
        self.details = details or {}


class _LspProtocolFailure(Exception):
    pass


@dataclass(slots=True)
class _PendingResponse:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: dict[str, Any] | None = None
    failure: str | None = None


def _safe_lsp_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in _SAFE_LSP_ENV_NAMES or upper.startswith("LC_"):
            env[str(name)] = str(value)
    return env


def _clip_text(value: Any, max_chars: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 1:
        return text[:max_chars], True
    return text[: max_chars - 1] + "…", True


def _normalize_position(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    line = value.get("line")
    character = value.get("character")
    if (
        not isinstance(line, int)
        or isinstance(line, bool)
        or not isinstance(character, int)
        or isinstance(character, bool)
        or line < 0
        or character < 0
    ):
        return None
    return {"line": line, "character": character}


def _normalize_range(value: Any) -> dict[str, dict[str, int]] | None:
    if not isinstance(value, dict):
        return None
    start = _normalize_position(value.get("start"))
    end = _normalize_position(value.get("end"))
    if start is None or end is None:
        return None
    return {"start": start, "end": end}


def _path_from_file_uri(uri: str) -> Path | None:
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme.lower() != "file":
        return None
    if parsed.netloc not in {"", "localhost"}:
        return None
    raw_path = urllib.request.url2pathname(parsed.path)
    if os.name == "nt" and raw_path.startswith("/") and len(raw_path) >= 3 and raw_path[2] == ":":
        raw_path = raw_path[1:]
    try:
        return Path(raw_path).resolve(strict=False)
    except OSError:
        return None


def _read_lsp_message(stream: Any, max_bytes: int) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        try:
            decoded = line.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise _LspProtocolFailure("LSP response contained a non-ASCII header.") from exc
        name, separator, value = decoded.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    raw_length = headers.get("content-length")
    if raw_length is None:
        raise _LspProtocolFailure("LSP response omitted Content-Length.")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise _LspProtocolFailure("LSP response had an invalid Content-Length.") from exc
    if length < 0 or length > max_bytes:
        raise _LspProtocolFailure(
            f"LSP response exceeded the configured {max_bytes}-byte message limit."
        )
    body = stream.read(length)
    if len(body) != length:
        raise _LspProtocolFailure("LSP response ended before the declared body length.")
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _LspProtocolFailure("LSP response was not valid UTF-8 JSON.") from exc
    if not isinstance(message, dict):
        raise _LspProtocolFailure("LSP response must be a JSON object.")
    return message


class _LspSession:
    def __init__(
        self,
        root: Path,
        language: str,
        command: tuple[str, ...],
        *,
        request_timeout: float,
        max_message_bytes: int,
    ) -> None:
        self.root = root
        self.language = language
        self.command = command
        self.request_timeout = request_timeout
        self.max_message_bytes = max_message_bytes
        self.process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingResponse] = {}
        self._request_id = 0
        self._initialized = False
        self._server_capabilities: dict[str, Any] = {}
        self._reader_failure: str | None = None
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._documents: dict[str, tuple[int, int, int]] = {}
        self._diagnostics_condition = threading.Condition()
        self._published_diagnostics: dict[str, list[Any]] = {}
        self._diagnostic_revisions: dict[str, int] = {}

    @property
    def initialized(self) -> bool:
        return self._initialized and self.alive

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return dict(self._server_capabilities)

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_lines)

    def start(self) -> None:
        if self.initialized:
            return
        if self.process is not None:
            self.abort()
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                list(self.command),
                cwd=str(self.root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_safe_lsp_environment(),
                shell=False,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise _LspProtocolFailure(f"Failed to start LSP server: {exc}") from exc
        self._reader_failure = None
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"coding-tools-lsp-reader-{self.language}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name=f"coding-tools-lsp-stderr-{self.language}",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        try:
            initialize_result = self._request_started(
                "initialize",
                {
                    "processId": os.getpid(),
                    "clientInfo": {"name": "coding-tools-mcp", "version": "0.3"},
                    "rootUri": self.root.as_uri(),
                    "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-16"]},
                        "textDocument": {
                            "definition": {"linkSupport": True},
                            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                            "publishDiagnostics": {"relatedInformation": True},
                            "references": {},
                            "diagnostic": {},
                        },
                        "workspace": {"workspaceFolders": True},
                    },
                },
            )
            if not isinstance(initialize_result, dict):
                raise _LspProtocolFailure("LSP initialize result must be an object.")
            capabilities = initialize_result.get("capabilities")
            if isinstance(capabilities, dict):
                self._server_capabilities = dict(capabilities)
            self._initialized = True
            self.notify("initialized", {})
        except Exception:
            self.abort()
            raise

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.start()
        return self._request_started(method, params)

    def _request_started(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self.alive:
            detail = self._reader_failure or "LSP process is not running."
            raise _LspProtocolFailure(detail)
        with self._pending_lock:
            self._request_id += 1
            request_id = self._request_id
            pending = _PendingResponse()
            self._pending[request_id] = pending
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        wait_seconds = self.request_timeout if timeout is None else timeout
        if not pending.event.wait(wait_seconds):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise _LspProtocolFailure(
                f"LSP request {method!r} timed out after {wait_seconds:.2f}s."
            )
        if pending.failure is not None:
            raise _LspProtocolFailure(pending.failure)
        if pending.error is not None:
            code = pending.error.get("code")
            message = pending.error.get("message") or "Unknown LSP error"
            raise _LspProtocolFailure(f"LSP request {method!r} failed ({code}): {message}")
        return pending.result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.alive:
            raise _LspProtocolFailure("LSP process is not running.")
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def sync_document(
        self,
        uri: str,
        language_id: str,
        text: str,
        signature: tuple[int, int],
    ) -> int:
        prior_revision = self.diagnostic_revision(uri)
        existing = self._documents.get(uri)
        if existing is None:
            version = 1
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": version,
                        "text": text,
                    }
                },
            )
            self._documents[uri] = (signature[0], signature[1], version)
            return prior_revision
        if existing[:2] == signature:
            return prior_revision
        version = existing[2] + 1
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
        )
        self._documents[uri] = (signature[0], signature[1], version)
        return prior_revision

    def diagnostic_revision(self, uri: str) -> int:
        with self._diagnostics_condition:
            return self._diagnostic_revisions.get(uri, 0)

    def wait_for_published_diagnostics(
        self,
        uri: str,
        after_revision: int,
        timeout: float,
    ) -> list[Any]:
        deadline = time.monotonic() + timeout
        with self._diagnostics_condition:
            while self._diagnostic_revisions.get(uri, 0) <= after_revision and self.alive:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._diagnostics_condition.wait(remaining)
            return list(self._published_diagnostics.get(uri, []))

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None and self._initialized:
            try:
                self._request_started("shutdown", {}, timeout=min(1.0, self.request_timeout))
            except _LspProtocolFailure:
                pass
            try:
                self.notify("exit", {})
            except _LspProtocolFailure:
                pass
        self._initialized = False
        if process.poll() is None:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
        self._close_pipes()

    def abort(self) -> None:
        process = self.process
        self._initialized = False
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        self._close_pipes()

    def _close_pipes(self) -> None:
        process = self.process
        if process is None:
            return
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise _LspProtocolFailure("LSP process is not running.")
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > self.max_message_bytes:
            raise _LspProtocolFailure(
                f"LSP request exceeded the configured {self.max_message_bytes}-byte message limit."
            )
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        try:
            with self._write_lock:
                process.stdin.write(frame)
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise _LspProtocolFailure(f"Failed to write to LSP process: {exc}") from exc

    def _reader_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        failure: str | None = None
        try:
            while True:
                message = _read_lsp_message(process.stdout, self.max_message_bytes)
                if message is None:
                    failure = "LSP process closed its stdout stream."
                    break
                self._dispatch_message(message)
        except _LspProtocolFailure as exc:
            failure = str(exc)
        except OSError as exc:
            failure = f"Failed reading LSP output: {exc}"
        if failure is not None:
            self._reader_failure = failure
            self._fail_pending(failure)
            with self._diagnostics_condition:
                self._diagnostics_condition.notify_all()

    def _stderr_loop(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = process.stderr.readline()
                if not line:
                    return
                text, _truncated = _clip_text(line.decode("utf-8", errors="replace").rstrip(), 1_000)
                if text:
                    self._stderr_lines.append(text)
        except OSError:
            return

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if isinstance(message_id, int) and ("result" in message or "error" in message):
            with self._pending_lock:
                pending = self._pending.pop(message_id, None)
            if pending is not None:
                pending.result = message.get("result")
                raw_error = message.get("error")
                pending.error = raw_error if isinstance(raw_error, dict) else None
                pending.event.set()
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        if "id" in message:
            self._handle_server_request(message_id, method, message.get("params"))
            return
        if method == "textDocument/publishDiagnostics":
            params = message.get("params")
            if not isinstance(params, dict):
                return
            uri = params.get("uri")
            diagnostics = params.get("diagnostics")
            if not isinstance(uri, str) or not isinstance(diagnostics, list):
                return
            with self._diagnostics_condition:
                self._published_diagnostics[uri] = list(diagnostics)
                self._diagnostic_revisions[uri] = self._diagnostic_revisions.get(uri, 0) + 1
                self._diagnostics_condition.notify_all()

    def _handle_server_request(self, message_id: Any, method: str, params: Any) -> None:
        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, dict) else None
            count = len(items) if isinstance(items, list) else 0
            self._send_response(message_id, [None] * count)
            return
        if method == "workspace/workspaceFolders":
            self._send_response(
                message_id,
                [{"uri": self.root.as_uri(), "name": self.root.name}],
            )
            return
        if method in {
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
        }:
            self._send_response(message_id, None)
            return
        if method == "workspace/applyEdit":
            self._send_response(
                message_id,
                {
                    "applied": False,
                    "failureReason": "coding-tools-mcp semantic backend does not allow LSP workspace edits",
                },
            )
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32601, "message": f"Unsupported LSP client method: {method}"},
            }
        )

    def _send_response(self, message_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": message_id, "result": result})

    def _fail_pending(self, failure: str) -> None:
        with self._pending_lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            pending.failure = failure
            pending.event.set()


@dataclass(frozen=True, slots=True)
class _DocumentContext:
    path: Path
    relative_path: str
    uri: str
    spec: LspServerSpec
    session: _LspSession
    prior_diagnostic_revision: int


class LspSemanticBackend(SemanticBackend):
    """Lazy, bounded LSP client scoped to one workspace root."""

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        server_specs: Sequence[LspServerSpec] = DEFAULT_LSP_SERVER_SPECS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        diagnostics_wait: float = DEFAULT_DIAGNOSTICS_WAIT_SECONDS,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
        max_message_bytes: int = DEFAULT_MAX_LSP_MESSAGE_BYTES,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if diagnostics_wait < 0:
            raise ValueError("diagnostics_wait must be non-negative")
        if max_results <= 0 or max_text_chars <= 0 or max_document_bytes <= 0 or max_message_bytes <= 0:
            raise ValueError("semantic output and message limits must be positive")
        self.root = root
        self.server_specs = tuple(server_specs)
        self.request_timeout = float(request_timeout)
        self.diagnostics_wait = float(diagnostics_wait)
        self.max_results = int(max_results)
        self.max_text_chars = int(max_text_chars)
        self.max_document_bytes = int(max_document_bytes)
        self.max_message_bytes = int(max_message_bytes)
        self._sessions: dict[str, _LspSession] = {}
        self._session_lock = threading.RLock()
        self._spec_by_extension: dict[str, LspServerSpec] = {}
        for spec in self.server_specs:
            for extension in spec.extensions:
                self._spec_by_extension[extension.lower()] = spec

    def semantic_status(self, path: str | None = None) -> SemanticPayload:
        selected_spec: LspServerSpec | None = None
        selected_path: str | None = None
        if path is not None:
            try:
                resolved = self._resolve_document_path(path)
                selected_path = resolved.relative_to(self.root).as_posix()
                selected_spec = self._spec_for_path(resolved)
            except _SemanticFailure as exc:
                return self._failure("semantic_status", exc)

        states: list[dict[str, Any]] = []
        for spec in self.server_specs:
            if selected_spec is not None and spec.language != selected_spec.language:
                continue
            command = self._resolve_server_command(spec)
            session = self._sessions.get(spec.language)
            running = bool(session is not None and session.initialized)
            if running:
                state = "ready"
            elif command is not None:
                state = "available"
            else:
                state = "unavailable"
            entry: dict[str, Any] = {
                "language": spec.language,
                "language_id": spec.language_id,
                "language_ids": {
                    extension: language_id for extension, language_id in spec.language_ids
                },
                "extensions": list(spec.extensions),
                "status": state,
                "running": running,
                "server": Path(command[0]).name if command is not None else None,
            }
            if command is None:
                entry["detail"] = "No configured LSP server executable was found on PATH."
                entry["candidates"] = [Path(candidate[0]).name for candidate in spec.candidates if candidate]
            elif session is not None and not session.alive and session.stderr_tail():
                entry["last_stderr"] = session.stderr_tail()[-3:]
            states.append(entry)

        overall = "unavailable"
        if any(item["status"] == "ready" for item in states):
            overall = "ready"
        elif any(item["status"] == "available" for item in states):
            overall = "available"
        payload: SemanticPayload = {
            "ok": True,
            "backend": "lsp",
            "capability": "semantic_status",
            "status": overall,
            "languages": states,
        }
        if selected_path is not None:
            payload["path"] = selected_path
        return payload

    def document_symbols(self, path: str) -> SemanticPayload:
        capability = "document_symbols"
        try:
            context = self._document_context(path)
            raw = context.session.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": context.uri}},
            )
            symbols, truncated, filtered = self._normalize_symbols(raw, context.relative_path)
            return self._success(
                capability,
                context,
                symbols,
                truncated=truncated,
                filtered=filtered,
            )
        except _SemanticFailure as exc:
            return self._failure(capability, exc)
        except _LspProtocolFailure as exc:
            return self._protocol_failure(capability, path, exc)

    def goto_definition(self, path: str, line: int, character: int) -> SemanticPayload:
        capability = "goto_definition"
        try:
            self._validate_position(line, character)
            context = self._document_context(path)
            raw = context.session.request(
                "textDocument/definition",
                self._position_request(context.uri, line, character),
            )
            locations, truncated, filtered = self._normalize_locations(raw)
            return self._success(
                capability,
                context,
                locations,
                truncated=truncated,
                filtered=filtered,
            )
        except _SemanticFailure as exc:
            return self._failure(capability, exc)
        except _LspProtocolFailure as exc:
            return self._protocol_failure(capability, path, exc)

    def find_references(self, path: str, line: int, character: int) -> SemanticPayload:
        capability = "find_references"
        try:
            self._validate_position(line, character)
            context = self._document_context(path)
            params = self._position_request(context.uri, line, character)
            params["context"] = {"includeDeclaration": True}
            raw = context.session.request("textDocument/references", params)
            locations, truncated, filtered = self._normalize_locations(raw)
            return self._success(
                capability,
                context,
                locations,
                truncated=truncated,
                filtered=filtered,
            )
        except _SemanticFailure as exc:
            return self._failure(capability, exc)
        except _LspProtocolFailure as exc:
            return self._protocol_failure(capability, path, exc)

    def document_diagnostics(self, path: str) -> SemanticPayload:
        capability = "document_diagnostics"
        try:
            context = self._document_context(path)
            raw_diagnostics: Any
            source: str
            if context.session.server_capabilities.get("diagnosticProvider"):
                raw = context.session.request(
                    "textDocument/diagnostic",
                    {
                        "textDocument": {"uri": context.uri},
                        "identifier": None,
                        "previousResultId": None,
                    },
                )
                raw_diagnostics = raw.get("items") if isinstance(raw, dict) else []
                source = "textDocument/diagnostic"
            else:
                raw_diagnostics = context.session.wait_for_published_diagnostics(
                    context.uri,
                    context.prior_diagnostic_revision,
                    self.diagnostics_wait,
                )
                source = "textDocument/publishDiagnostics"
            diagnostics, truncated = self._normalize_diagnostics(raw_diagnostics)
            payload = self._success(
                capability,
                context,
                diagnostics,
                truncated=truncated,
                filtered=0,
            )
            payload["diagnostic_source"] = source
            return payload
        except _SemanticFailure as exc:
            return self._failure(capability, exc)
        except _LspProtocolFailure as exc:
            return self._protocol_failure(capability, path, exc)

    def close(self) -> None:
        with self._session_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def _resolve_document_path(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise _SemanticFailure("SEMANTIC_INVALID_PATH", "path must be a non-empty string")
        pure = Path(raw_path)
        candidate = pure if pure.is_absolute() else self.root / pure
        try:
            boundary_probe = candidate.resolve(strict=False)
        except OSError as exc:
            raise _SemanticFailure(
                "SEMANTIC_PATH_ERROR",
                f"Could not resolve semantic document: {exc}",
                path=raw_path,
            ) from exc
        if not boundary_probe.is_relative_to(self.root):
            raise _SemanticFailure(
                "SEMANTIC_PATH_OUTSIDE_WORKSPACE",
                "Semantic document path escapes the configured workspace.",
                path=raw_path,
            )
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise _SemanticFailure(
                "SEMANTIC_NOT_FOUND",
                f"Semantic document not found: {raw_path}",
                path=raw_path,
            ) from exc
        except OSError as exc:
            raise _SemanticFailure(
                "SEMANTIC_PATH_ERROR",
                f"Could not resolve semantic document: {exc}",
                path=raw_path,
            ) from exc
        if not resolved.is_relative_to(self.root):
            raise _SemanticFailure(
                "SEMANTIC_PATH_OUTSIDE_WORKSPACE",
                "Semantic document path escapes the configured workspace.",
                path=raw_path,
            )
        if not resolved.is_file():
            raise _SemanticFailure(
                "SEMANTIC_NOT_A_FILE",
                "Semantic document path must refer to a file.",
                path=raw_path,
            )
        return resolved

    def _spec_for_path(self, path: Path) -> LspServerSpec:
        spec = self._spec_by_extension.get(path.suffix.lower())
        if spec is None:
            relative = path.relative_to(self.root).as_posix()
            raise _SemanticFailure(
                "SEMANTIC_LANGUAGE_UNSUPPORTED",
                f"No semantic language backend is configured for {path.suffix or 'this file type'}.",
                status="unavailable",
                path=relative,
            )
        return spec

    def _resolve_server_command(self, spec: LspServerSpec) -> tuple[str, ...] | None:
        for candidate in spec.candidates:
            if not candidate:
                continue
            executable = candidate[0]
            executable_path = Path(executable).expanduser()
            resolved: str | None = None
            if executable_path.is_absolute():
                if executable_path.is_file():
                    resolved = str(executable_path.resolve(strict=True))
            elif executable_path.parent == Path("."):
                resolved = shutil.which(executable)
            if resolved is not None:
                return (resolved, *candidate[1:])
        return None

    def _document_context(self, raw_path: str) -> _DocumentContext:
        path = self._resolve_document_path(raw_path)
        relative = path.relative_to(self.root).as_posix()
        spec = self._spec_for_path(path)
        command = self._resolve_server_command(spec)
        if command is None:
            raise _SemanticFailure(
                "SEMANTIC_LSP_UNAVAILABLE",
                f"No {spec.language} LSP server executable is available.",
                status="unavailable",
                language=spec.language,
                path=relative,
                details={
                    "candidates": [Path(candidate[0]).name for candidate in spec.candidates if candidate]
                },
            )
        session = self._session_for(spec, command)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise _SemanticFailure(
                "SEMANTIC_READ_FAILED",
                f"Could not read semantic document: {exc}",
                language=spec.language,
                path=relative,
            ) from exc
        if len(data) > self.max_document_bytes:
            raise _SemanticFailure(
                "SEMANTIC_DOCUMENT_TOO_LARGE",
                f"Semantic document exceeds the {self.max_document_bytes}-byte limit.",
                language=spec.language,
                path=relative,
            )
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _SemanticFailure(
                "SEMANTIC_NOT_UTF8",
                "Semantic documents must be UTF-8 text.",
                language=spec.language,
                path=relative,
            ) from exc
        try:
            stat = path.stat()
        except OSError as exc:
            raise _SemanticFailure(
                "SEMANTIC_STAT_FAILED",
                f"Could not stat semantic document: {exc}",
                language=spec.language,
                path=relative,
            ) from exc
        prior_revision = session.sync_document(
            path.as_uri(),
            spec.language_id_for_path(path),
            text,
            (stat.st_mtime_ns, stat.st_size),
        )
        return _DocumentContext(
            path=path,
            relative_path=relative,
            uri=path.as_uri(),
            spec=spec,
            session=session,
            prior_diagnostic_revision=prior_revision,
        )

    def _session_for(self, spec: LspServerSpec, command: tuple[str, ...]) -> _LspSession:
        with self._session_lock:
            existing = self._sessions.get(spec.language)
            if existing is not None and existing.command == command and existing.initialized:
                return existing
            if existing is not None:
                existing.abort()
            session = _LspSession(
                self.root,
                spec.language,
                command,
                request_timeout=self.request_timeout,
                max_message_bytes=self.max_message_bytes,
            )
            try:
                session.start()
            except _LspProtocolFailure as exc:
                details: dict[str, Any] = {}
                if session.stderr_tail():
                    details["stderr"] = session.stderr_tail()[-3:]
                raise _SemanticFailure(
                    "SEMANTIC_LSP_START_FAILED",
                    str(exc),
                    status="unavailable",
                    language=spec.language,
                    details=details,
                ) from exc
            self._sessions[spec.language] = session
            return session

    def _discard_session(self, language: str) -> None:
        with self._session_lock:
            session = self._sessions.pop(language, None)
        if session is not None:
            session.abort()

    def _normalize_workspace_location(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        uri = value.get("uri")
        raw_range = value.get("range")
        if not isinstance(uri, str):
            uri = value.get("targetUri")
            raw_range = value.get("targetSelectionRange") or value.get("targetRange")
        if not isinstance(uri, str):
            return None
        target = _path_from_file_uri(uri)
        if target is None or not target.is_relative_to(self.root):
            return None
        normalized_range = _normalize_range(raw_range)
        if normalized_range is None:
            return None
        return {
            "path": target.relative_to(self.root).as_posix(),
            "range": normalized_range,
        }

    def _normalize_locations(self, raw: Any) -> tuple[list[dict[str, Any]], bool, int]:
        if raw is None:
            return [], False, 0
        values = raw if isinstance(raw, list) else [raw]
        items: list[dict[str, Any]] = []
        filtered = 0
        truncated = False
        for value in values:
            normalized = self._normalize_workspace_location(value)
            if normalized is None:
                filtered += 1
                continue
            if len(items) >= self.max_results:
                truncated = True
                continue
            items.append(normalized)
        return items, truncated, filtered

    def _normalize_symbols(
        self,
        raw: Any,
        document_path: str,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        values = raw if isinstance(raw, list) else []
        items: list[dict[str, Any]] = []
        filtered = 0
        truncated = False

        def visit(value: Any, container_name: str | None, depth: int) -> None:
            nonlocal filtered, truncated
            if depth > 64 or not isinstance(value, dict):
                filtered += 1
                return
            if len(items) >= self.max_results:
                truncated = True
                return
            raw_location = value.get("location")
            if raw_location is not None:
                location = self._normalize_workspace_location(raw_location)
                if location is None:
                    filtered += 1
                    return
                symbol_range = location["range"]
                symbol_path = location["path"]
            else:
                symbol_range = _normalize_range(value.get("range"))
                if symbol_range is None:
                    filtered += 1
                    return
                symbol_path = document_path
            name, name_truncated = _clip_text(value.get("name"), min(self.max_text_chars, 512))
            item: dict[str, Any] = {
                "name": name,
                "kind": value.get("kind") if isinstance(value.get("kind"), int) else None,
                "path": symbol_path,
                "range": symbol_range,
            }
            selection_range = _normalize_range(value.get("selectionRange"))
            if selection_range is not None:
                item["selection_range"] = selection_range
            explicit_container = value.get("containerName")
            if isinstance(explicit_container, str) and explicit_container:
                item["container_name"] = _clip_text(explicit_container, 512)[0]
            elif container_name:
                item["container_name"] = container_name
            detail = value.get("detail")
            if isinstance(detail, str) and detail:
                detail_text, detail_truncated = _clip_text(detail, self.max_text_chars)
                item["detail"] = detail_text
                if detail_truncated:
                    item["detail_truncated"] = True
            if name_truncated:
                item["name_truncated"] = True
            items.append(item)
            children = value.get("children")
            if isinstance(children, list):
                for child in children:
                    visit(child, name, depth + 1)

        for value in values:
            visit(value, None, 0)
        return items, truncated, filtered

    def _normalize_diagnostics(self, raw: Any) -> tuple[list[dict[str, Any]], bool]:
        values = raw if isinstance(raw, list) else []
        items: list[dict[str, Any]] = []
        truncated = False
        for value in values:
            if not isinstance(value, dict):
                continue
            normalized_range = _normalize_range(value.get("range"))
            if normalized_range is None:
                continue
            if len(items) >= self.max_results:
                truncated = True
                continue
            message, message_truncated = _clip_text(value.get("message"), self.max_text_chars)
            item: dict[str, Any] = {
                "range": normalized_range,
                "message": message,
            }
            severity = value.get("severity")
            if isinstance(severity, int) and not isinstance(severity, bool):
                item["severity"] = severity
            code = value.get("code")
            if isinstance(code, (str, int)) and not isinstance(code, bool):
                item["code"] = _clip_text(code, 256)[0]
            source = value.get("source")
            if isinstance(source, str) and source:
                item["source"] = _clip_text(source, 256)[0]
            tags = value.get("tags")
            if isinstance(tags, list):
                item["tags"] = [tag for tag in tags[:16] if isinstance(tag, int) and not isinstance(tag, bool)]
            if message_truncated:
                item["message_truncated"] = True
            items.append(item)
        return items, truncated

    def _success(
        self,
        capability: str,
        context: _DocumentContext,
        items: list[dict[str, Any]],
        *,
        truncated: bool,
        filtered: int,
    ) -> SemanticPayload:
        return {
            "ok": True,
            "backend": "lsp",
            "capability": capability,
            "status": "ok",
            "language": context.spec.language,
            "path": context.relative_path,
            "items": items,
            "count": len(items),
            "truncated": truncated,
            "filtered_outside_workspace": filtered,
        }

    def _failure(self, capability: str, exc: _SemanticFailure) -> SemanticPayload:
        payload: SemanticPayload = {
            "ok": False,
            "backend": "lsp",
            "capability": capability,
            "status": exc.status,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "category": "capability" if exc.status == "unavailable" else "validation",
                "retryable": False,
                "details": exc.details,
            },
        }
        if exc.language is not None:
            payload["language"] = exc.language
        if exc.path is not None:
            payload["path"] = exc.path
        return payload

    def _protocol_failure(
        self,
        capability: str,
        path: str,
        exc: _LspProtocolFailure,
    ) -> SemanticPayload:
        language: str | None = None
        try:
            resolved = self._resolve_document_path(path)
            spec = self._spec_for_path(resolved)
            language = spec.language
            normalized_path = resolved.relative_to(self.root).as_posix()
            self._discard_session(spec.language)
        except _SemanticFailure:
            normalized_path = path
        payload: SemanticPayload = {
            "ok": False,
            "backend": "lsp",
            "capability": capability,
            "status": "error",
            "path": normalized_path,
            "error": {
                "code": "SEMANTIC_LSP_REQUEST_FAILED",
                "message": str(exc),
                "category": "runtime",
                "retryable": True,
            },
        }
        if language is not None:
            payload["language"] = language
        return payload

    @staticmethod
    def _validate_position(line: int, character: int) -> None:
        if (
            not isinstance(line, int)
            or isinstance(line, bool)
            or line < 0
            or not isinstance(character, int)
            or isinstance(character, bool)
            or character < 0
        ):
            raise _SemanticFailure(
                "SEMANTIC_INVALID_POSITION",
                "line and character must be non-negative integers",
            )

    @staticmethod
    def _position_request(uri: str, line: int, character: int) -> dict[str, Any]:
        return {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
