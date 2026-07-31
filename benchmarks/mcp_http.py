#!/usr/bin/env python3
"""Small MCP-over-HTTP JSON-RPC client used by deterministic benchmarks.

The client intentionally depends only on the Python standard library so the
dogfood path can run before project packaging is complete.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class McpHttpError(RuntimeError):
    """Raised when the HTTP transport or JSON-RPC response fails."""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


@dataclass
class JsonRpcReply:
    status: int
    payload: dict[str, Any] | None
    headers: dict[str, str]


def connect_with_retry(
    endpoint: str,
    timeout_seconds: float,
    *,
    poll_interval: float = 0.1,
    request_timeout: float = 10.0,
    catch: tuple[type[BaseException], ...] = (McpHttpError,),
) -> tuple[McpHttpClient | None, dict[str, Any] | None, str | None]:
    """Poll an MCP endpoint until initialize() succeeds or the deadline passes.

    Returns (client, initialize_result, None) on success and
    (None, None, error_text) on failure. Shared by the benchmark entry points
    so the startup retry policy lives in one place.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() <= deadline:
        client = McpHttpClient(endpoint, timeout=request_timeout)
        try:
            return client, client.initialize(), None
        except catch as exc:
            last_error = exc
            time.sleep(poll_interval)
    return None, None, str(last_error) if last_error is not None else "startup timeout elapsed"


class McpHttpClient:
    """Minimal streamable-HTTP MCP client.

    It supports JSON responses and server-sent-event responses that contain a
    JSON-RPC payload in a ``data:`` line. This is enough for deterministic
    benchmark calls without pulling in an MCP SDK.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 30.0,
        protocol_version: str = "2025-11-25",
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.protocol_version = protocol_version
        self._next_id = 1
        self.session_id: str | None = None

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": "coding-tools-mcp-benchmark-runner",
                    "version": "0.1",
                },
            },
        )
        negotiated = result.get("protocolVersion")
        if isinstance(negotiated, str) and negotiated:
            self.protocol_version = negotiated
        self.notify("notifications/initialized", {})
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpHttpError("tools/list result did not contain a tools array", payload=result)
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params}, expect_reply=False)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        reply = self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
            expect_reply=True,
        )
        payload = reply.payload
        if payload is None:
            raise McpHttpError(f"{method} returned an empty response", status=reply.status)
        if payload.get("id") not in (request_id, None):
            raise McpHttpError(
                f"{method} returned mismatched JSON-RPC id {payload.get('id')!r}",
                status=reply.status,
                payload=payload,
            )
        if "error" in payload:
            raise McpHttpError(
                f"{method} JSON-RPC error: {payload['error']}",
                status=reply.status,
                payload=payload,
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpHttpError(f"{method} result was not an object", status=reply.status, payload=payload)
        return result

    def _post(self, payload: dict[str, Any], *, expect_reply: bool) -> JsonRpcReply:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.getcode()
                raw = response.read()
                response_headers = {k: v for k, v in response.headers.items()}
                self._capture_session_id(response_headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            response_headers = {k: v for k, v in exc.headers.items()}
            self._capture_session_id(response_headers)
            parsed = self._parse_body(raw, response_headers.get("Content-Type", ""))
            raise McpHttpError(
                f"HTTP {exc.code} from MCP endpoint",
                status=exc.code,
                payload=parsed or raw.decode("utf-8", errors="replace"),
            ) from exc
        except urllib.error.URLError as exc:
            raise McpHttpError(f"Could not connect to MCP endpoint: {exc.reason}") from exc

        if not expect_reply and status in (200, 202, 204):
            return JsonRpcReply(status=status, payload=None, headers=response_headers)
        parsed = self._parse_body(raw, response_headers.get("Content-Type", ""))
        if parsed is None:
            raise McpHttpError(
                "MCP endpoint returned a non-JSON response",
                status=status,
                payload=raw.decode("utf-8", errors="replace"),
            )
        return JsonRpcReply(status=status, payload=parsed, headers=response_headers)

    def _capture_session_id(self, headers: dict[str, str]) -> None:
        for key, value in headers.items():
            if key.lower() == "mcp-session-id" and value:
                self.session_id = value

    def _parse_body(self, raw: bytes, content_type: str) -> dict[str, Any] | None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        if "text/event-stream" in content_type or text.startswith("event:") or text.startswith("data:"):
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data or data == "[DONE]":
                    continue
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    return parsed
            return None
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return None
