from __future__ import annotations

import json
import sys
from typing import Any, Protocol, TextIO

from .protocol import dispatch_rpc, invalid_request_response, jsonrpc_error


class StdioRuntime(Protocol):
    protocol_version: str
    initialized: bool

    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def list_tools(self) -> dict[str, Any]: ...

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        request_id: str | int | None = None,
    ) -> dict[str, Any]: ...

    def cancel_request(self, request_id: str | int) -> None: ...

    def close(self) -> None: ...


def serve_stdio(
    runtime: StdioRuntime,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    source = input_stream or sys.stdin
    sink = output_stream or sys.stdout
    try:
        for line in source:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                response = jsonrpc_error(None, -32700, "Parse error")
            else:
                try:
                    response = (
                        dispatch_rpc(runtime, request)
                        if isinstance(request, dict)
                        else invalid_request_response()
                    )
                except Exception as exc:  # noqa: BLE001 - keep the stdio server alive
                    response = jsonrpc_error(None, -32603, str(exc))
            if response is not None:
                sink.write(
                    json.dumps(response, separators=(",", ":")) + "\n"
                )
                sink.flush()
    finally:
        runtime.close()
    return 0
