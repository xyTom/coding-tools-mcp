#!/usr/bin/env python3
"""Minimal HTTP/1.1 client for a Windows named pipe.

Used for local service/core control endpoints that deliberately expose HTTP
over ``\\.\pipe\...`` instead of a TCP listener.  It never touches the network.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import pywintypes
import win32file
import win32pipe


BROKEN_PIPE_ERRORS = {109, 232, 233}


def request(pipe: str, method: str, target: str, body: bytes = b"") -> bytes:
    win32pipe.WaitNamedPipe(pipe, 3000)
    handle = win32file.CreateFile(
        pipe,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )
    try:
        headers = [
            f"{method} {target} HTTP/1.1",
            "Host: localhost",
            "Connection: close",
            "Accept: application/json",
        ]
        if body:
            headers.extend(
                [
                    "Content-Type: application/json",
                    f"Content-Length: {len(body)}",
                ]
            )
        payload = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
        win32file.WriteFile(handle, payload)
        chunks: list[bytes] = []
        while True:
            try:
                _status, chunk = win32file.ReadFile(handle, 65536)
                if chunk:
                    chunks.append(chunk)
                else:
                    break
            except pywintypes.error as exc:
                if exc.winerror in BROKEN_PIPE_ERRORS:
                    break
                raise
        return b"".join(chunks)
    finally:
        win32file.CloseHandle(handle)


def _decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    cursor = 0
    while True:
        line_end = body.find(b"\r\n", cursor)
        if line_end < 0:
            raise RuntimeError("invalid chunked response: missing chunk-size terminator")
        raw_size = body[cursor:line_end].split(b";", 1)[0].strip()
        try:
            size = int(raw_size, 16)
        except ValueError as exc:
            raise RuntimeError("invalid chunked response: bad chunk size") from exc
        cursor = line_end + 2
        if size == 0:
            return bytes(decoded)
        end = cursor + size
        if end + 2 > len(body) or body[end:end + 2] != b"\r\n":
            raise RuntimeError("invalid chunked response: truncated chunk")
        decoded.extend(body[cursor:end])
        cursor = end + 2


def parse_response(raw: bytes) -> tuple[int, bytes]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise RuntimeError("named-pipe HTTP response has no header terminator")
    first = head.splitlines()[0].decode("ascii", errors="replace")
    parts = first.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeError(f"invalid HTTP status line: {first}")
    headers: dict[str, str] = {}
    for line in head.splitlines()[1:]:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.decode("ascii", errors="ignore").strip().lower()] = value.decode(
                "latin-1", errors="replace"
            ).strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _decode_chunked(body)
    return int(parts[1]), body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipe", required=True)
    parser.add_argument("--method", default="GET")
    parser.add_argument("--target", required=True)
    parser.add_argument("--json-body")
    parser.add_argument("--expect", type=int)
    args = parser.parse_args()
    body = b""
    if args.json_body is not None:
        parsed: Any = json.loads(args.json_body)
        body = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw = request(args.pipe, args.method.upper(), args.target, body)
    status, response_body = parse_response(raw)
    print(f"status={status}")
    if response_body:
        sys.stdout.buffer.write(response_body + b"\n")
    return 0 if args.expect is None or status == args.expect else 2


if __name__ == "__main__":
    raise SystemExit(main())
