from __future__ import annotations

import sys
from io import TextIOBase
from typing import Any, BinaryIO, Protocol, TextIO, cast

from .json_utils import strict_json_bytes, strict_json_dumps, strict_json_loads
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


def _serialize_response(response: dict[str, Any]) -> tuple[str, bytes]:
    serialized = strict_json_dumps(
        response,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    try:
        return serialized, serialized.encode("utf-8")
    except UnicodeEncodeError:
        # JSON permits escaped surrogate code units in input. Python preserves
        # an unpaired surrogate in str, but standard UTF-8 cannot encode it.
        # Fall back to ASCII JSON escapes so one response cannot terminate the
        # stdio transport while ordinary Unicode still uses real UTF-8 bytes.
        serialized = strict_json_dumps(
            response,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return serialized, strict_json_bytes(
            response,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def serve_stdio(
    runtime: StdioRuntime,
    *,
    input_stream: TextIO | BinaryIO | None = None,
    output_stream: TextIO | BinaryIO | None = None,
) -> int:
    source: TextIO | BinaryIO = (
        input_stream
        if input_stream is not None
        else cast(BinaryIO, getattr(sys.stdin, "buffer", sys.stdin))
    )
    sink: TextIO | BinaryIO = (
        output_stream
        if output_stream is not None
        else cast(BinaryIO, getattr(sys.stdout, "buffer", sys.stdout))
    )
    try:
        for line in source:
            if not line.strip():
                continue
            response: dict[str, Any] | None
            try:
                request = strict_json_loads(line)
            except ValueError:
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
                serialized, encoded = _serialize_response(response)
                if isinstance(sink, TextIOBase):
                    cast(TextIO, sink).write(serialized + "\n")
                else:
                    cast(BinaryIO, sink).write(encoded + b"\n")
                sink.flush()
    finally:
        runtime.close()
    return 0
