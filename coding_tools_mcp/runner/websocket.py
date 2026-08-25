"""Minimal RFC 6455 text/binary framing for the Runner transport.

This module deliberately implements only the WebSocket features required by
the versioned Runner JSON protocol.  It has no HTTP routing or Runner business
logic; server/client handshakes and message dispatch remain separate layers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import urllib.parse
from dataclasses import dataclass
from .protocol import DEFAULT_MAX_MESSAGE_BYTES


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketProtocolError(RuntimeError):
    pass


class WebSocketClosedError(WebSocketProtocolError):
    pass


def websocket_accept_value(client_key: str) -> str:
    if not isinstance(client_key, str) or not client_key.strip():
        raise WebSocketProtocolError("Sec-WebSocket-Key is required")
    try:
        decoded = base64.b64decode(client_key.strip(), validate=True)
    except ValueError as exc:
        raise WebSocketProtocolError("Sec-WebSocket-Key is invalid") from exc
    if len(decoded) != 16:
        raise WebSocketProtocolError("Sec-WebSocket-Key must decode to 16 bytes")
    digest = hashlib.sha1((client_key.strip() + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class BlockingWebSocket:
    """Thread-safe framed WebSocket over an already-upgraded socket."""

    def __init__(
        self,
        sock: socket.socket,
        *,
        client_side: bool,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        close_socket: bool = True,
    ) -> None:
        if max_message_bytes < 1:
            raise ValueError("max_message_bytes must be positive")
        self.sock = sock
        self.client_side = client_side
        self.max_message_bytes = max_message_bytes
        self.close_socket = close_socket
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False

    def send(self, message: str | bytes) -> None:
        if isinstance(message, str):
            payload = message.encode("utf-8")
            opcode = 0x1
        elif isinstance(message, bytes):
            payload = message
            opcode = 0x2
        else:
            raise TypeError("WebSocket messages must be str or bytes")
        if len(payload) > self.max_message_bytes:
            raise WebSocketProtocolError("WebSocket message exceeds configured limit")
        self._send_frame(opcode, payload)

    def recv(self) -> str | bytes:
        with self._recv_lock:
            while True:
                opcode, payload = self._recv_frame()
                if opcode == 0x8:
                    self._mark_closed()
                    try:
                        self._send_frame(0x8, payload[:125], allow_closed=True)
                    except (OSError, WebSocketProtocolError):
                        pass
                    raise WebSocketClosedError("WebSocket peer closed the connection")
                if opcode == 0x9:
                    self._send_frame(0xA, payload[:125])
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x1:
                    try:
                        return payload.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise WebSocketProtocolError("WebSocket text frame is not UTF-8") from exc
                if opcode == 0x2:
                    return payload
                raise WebSocketProtocolError("unsupported WebSocket opcode")

    def close(self) -> None:
        should_close = False
        with self._state_lock:
            if not self._closed:
                self._closed = True
                should_close = True
        if should_close:
            try:
                self._send_frame(0x8, b"", allow_closed=True)
            except OSError:
                pass
        if self.close_socket:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    def _mark_closed(self) -> None:
        with self._state_lock:
            self._closed = True

    def _send_frame(self, opcode: int, payload: bytes, *, allow_closed: bool = False) -> None:
        with self._send_lock:
            with self._state_lock:
                if self._closed and not allow_closed:
                    raise WebSocketClosedError("WebSocket is closed")
            first = 0x80 | (opcode & 0x0F)
            mask_bit = 0x80 if self.client_side else 0
            length = len(payload)
            header = bytearray([first])
            if length < 126:
                header.append(mask_bit | length)
            elif length <= 0xFFFF:
                header.append(mask_bit | 126)
                header.extend(struct.pack("!H", length))
            else:
                header.append(mask_bit | 127)
                header.extend(struct.pack("!Q", length))
            wire_payload = payload
            if self.client_side:
                mask = os.urandom(4)
                header.extend(mask)
                wire_payload = _apply_mask(payload, mask)
            self.sock.sendall(bytes(header) + wire_payload)

    def _recv_frame(self) -> tuple[int, bytes]:
        first, second = _recv_exact(self.sock, 2)
        fin = (first & 0x80) != 0
        opcode = first & 0x0F
        masked = (second & 0x80) != 0
        if not fin:
            raise WebSocketProtocolError("fragmented Runner WebSocket frames are unsupported")
        if opcode >= 0x8 and (second & 0x7F) > 125:
            raise WebSocketProtocolError("WebSocket control frame is too large")
        if self.client_side and masked:
            raise WebSocketProtocolError("server WebSocket frames must not be masked")
        if not self.client_side and not masked:
            raise WebSocketProtocolError("client WebSocket frames must be masked")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(self.sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(self.sock, 8))[0]
            if length & (1 << 63):
                raise WebSocketProtocolError("invalid WebSocket payload length")
        if length > self.max_message_bytes and opcode < 0x8:
            raise WebSocketProtocolError("WebSocket message exceeds configured limit")
        mask = _recv_exact(self.sock, 4) if masked else None
        payload = _recv_exact(self.sock, length)
        if mask is not None:
            payload = _apply_mask(payload, mask)
        return opcode, payload


class AsyncSocketWebSocket:
    """Async adapter that keeps blocking socket I/O off the transport event loop."""

    def __init__(self, framed: BlockingWebSocket) -> None:
        self.framed = framed

    async def send(self, message: str) -> None:
        await asyncio.to_thread(self.framed.send, message)

    async def recv(self) -> str | bytes:
        return await asyncio.to_thread(self.framed.recv)

    async def close(self) -> None:
        await asyncio.to_thread(self.framed.close)


@dataclass(frozen=True)
class WebSocketClientConnection:
    websocket: AsyncSocketWebSocket
    socket: socket.socket


def connect_websocket(
    url: str,
    *,
    timeout: float = 10.0,
    ssl_context: ssl.SSLContext | None = None,
) -> WebSocketClientConnection:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError("Runner WebSocket URL must use ws:// or wss://")
    if not parsed.hostname:
        raise ValueError("Runner WebSocket URL must include a host")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    raw = socket.create_connection((parsed.hostname, port), timeout=timeout)
    sock: socket.socket = raw
    try:
        if parsed.scheme == "wss":
            context = ssl_context or ssl.create_default_context()
            sock = context.wrap_socket(raw, server_hostname=parsed.hostname)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        host_header = parsed.hostname
        default_port = 443 if parsed.scheme == "wss" else 80
        if port != default_port:
            host_header = f"{host_header}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        sock.sendall(request)
        head = _recv_http_headers(sock)
        lines = head.split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise WebSocketProtocolError("Runner WebSocket upgrade was rejected")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        expected = websocket_accept_value(key)
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketProtocolError("Runner WebSocket accept header is invalid")
        framed = BlockingWebSocket(sock, client_side=True)
        return WebSocketClientConnection(AsyncSocketWebSocket(framed), sock)
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        if sock is not raw:
            try:
                raw.close()
            except OSError:
                pass
        raise


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    if length == 0:
        return b""
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise WebSocketClosedError("WebSocket socket closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _apply_mask(payload: bytes, mask: bytes) -> bytes:
    return bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def _recv_http_headers(sock: socket.socket, *, max_bytes: int = 32 * 1024) -> str:
    data = bytearray()
    marker = b"\r\n\r\n"
    while marker not in data:
        if len(data) >= max_bytes:
            raise WebSocketProtocolError("WebSocket handshake headers are too large")
        chunk = sock.recv(min(4096, max_bytes - len(data)))
        if not chunk:
            raise WebSocketClosedError("WebSocket handshake connection closed")
        data.extend(chunk)
    head, _marker, extra = bytes(data).partition(marker)
    if extra:
        # The peer must not send Runner frames until the HTTP upgrade is complete.
        raise WebSocketProtocolError("unexpected bytes after WebSocket handshake headers")
    try:
        return head.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise WebSocketProtocolError("WebSocket handshake headers are invalid") from exc


__all__ = [
    "AsyncSocketWebSocket",
    "BlockingWebSocket",
    "WebSocketClientConnection",
    "WebSocketClosedError",
    "WebSocketProtocolError",
    "connect_websocket",
    "websocket_accept_value",
]
