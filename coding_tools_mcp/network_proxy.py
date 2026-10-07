"""Bounded, exact-destination CONNECT proxy for the strict Linux backend.

This service is a destination-policy boundary, not an OS network sandbox. The
native backend must make its Unix socket the only egress from an isolated
network namespace, and must deny command access to all other Unix sockets.
Proxy environment variables alone never establish that guarantee.

TLS passes through unchanged. A permitted endpoint can receive any information
readable by a command; destination rules are not content-loss prevention.
"""
from __future__ import annotations

import errno
import ipaddress
import re
import selectors
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_MAX_HEADER = 16 * 1024
_MAX_BUFFER = 64 * 1024
_MAX_ADDRESSES = 16
# getaddrinfo can block inside the host resolver. Bound outstanding resolver
# calls across all proxy instances; a command cannot accumulate daemon threads
# by repeatedly exiting while a DNS server is unresponsive.
_DNS_SLOTS = threading.BoundedSemaphore(8)
_ResolvedAddress = tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int] | tuple[str, int, int, int]]
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
# Conservative special-use exclusions supplement older supported Python
# ipaddress tables. Reviewed against IANA's registries (2025-10-09 edition):
# https://www.iana.org/assignments/iana-ipv4-special-registry/
# https://www.iana.org/assignments/iana-ipv6-special-registry/
# Entire protocol-assignment blocks are denied, including anycast exceptions.
_SPECIAL_V4 = tuple(ipaddress.IPv4Network(value) for value in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",
))
_SPECIAL_V6 = tuple(ipaddress.IPv6Network(value) for value in (
    "::/96", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64",
    "100:0:0:1::/64", "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20",
    "5f00::/16", "fc00::/7", "fe80::/10", "ff00::/8",
))


@dataclass(frozen=True)
class Destination:
    host: str
    port: int

    @property
    def authority(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


def parse_destination(value: str) -> Destination:
    """Parse one exact HOST:PORT grant or CONNECT authority; no wildcard/URL."""
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        raise ValueError("A destination must be an exact HOST:PORT pair.")
    if any(ch in value for ch in ("/", "\\", "@", "?", "#", "%", "\x00")):
        raise ValueError("URLs, userinfo, scoped addresses and paths are not destinations.")
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or value[end + 1:end + 2] != ":":
            raise ValueError("An IPv6 destination must use [ADDRESS]:PORT.")
        host, port_text = value[1:end], value[end + 2:]
        try:
            host = str(ipaddress.IPv6Address(host))
        except ValueError as exc:
            raise ValueError("Invalid IPv6 destination.") from exc
    else:
        if value.count(":") != 1:
            raise ValueError("A destination requires one explicit port.")
        host, port_text = value.rsplit(":", 1)
        host = host.removesuffix(".")
        try:
            host = str(ipaddress.IPv4Address(host))
        except ValueError:
            try:
                host = host.encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise ValueError("Invalid destination hostname.") from exc
            if not host or len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in host.split(".")):
                raise ValueError("Invalid destination hostname.")
    if not port_text.isascii() or not port_text.isdecimal() or len(port_text) > 5:
        raise ValueError("A destination port must be a decimal integer.")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("A destination port must be between 1 and 65535.")
    return Destination(host, port)


def _is_public_address(value: str) -> bool:
    """Reject non-global and transition addresses, including mapped private IPs."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or any((address.is_private, address.is_loopback, address.is_link_local,
                                     address.is_multicast, address.is_reserved, address.is_unspecified)):
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return not any(address in network for network in _SPECIAL_V4)
    if isinstance(address, ipaddress.IPv6Address):
        # Transition/translation addresses can reach a different IPv4 target.
        if address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None:
            return False
        if any(address in network for network in _SPECIAL_V6):
            return False
        if address.scope_id is not None:
            return False
    return True


def _is_host_local_address(stream: socket.socket, address: tuple[str, int] | tuple[str, int, int, int]) -> bool:
    """Detect locally owned public addresses before transmitting even a SYN.

    Linux normally refuses a nonlocal bind with EADDRNOTAVAIL. Hosts configured
    for nonlocal binding will conservatively refuse proxy destinations instead
    of weakening this check. Any other error also fails closed at the caller.
    """
    source = (address[0], 0, 0, 0) if len(address) == 4 else (address[0], 0)
    try:
        stream.bind(source)
    except OSError as exc:
        if exc.errno == errno.EADDRNOTAVAIL:
            return False
        raise
    return True


class _ProxyError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(reason)


class ControlledProxy:
    """Per-execution Unix-listening proxy with bounded concurrency and buffers.

    ``start()`` is synchronous: the returned socket is already listening.
    ``close()`` immediately revokes existing tunnels and future connections.
    The caller owns lifetime and must close it on launch failure or process exit.
    This is intentionally unavailable on platforms without Unix domain sockets.
    """

    def __init__(
        self,
        allowed_destinations: tuple[str, ...],
        *,
        max_connections: int = 64,
        header_timeout: float = 10.0,
        connect_timeout: float = 10.0,
        idle_timeout: float = 120.0,
    ) -> None:
        self.allowed_destinations = frozenset(parse_destination(value) for value in allowed_destinations)
        if not self.allowed_destinations:
            raise ValueError("Proxy mode requires at least one explicit destination.")
        if not 1 <= max_connections <= 1024:
            raise ValueError("Proxy concurrency must be between 1 and 1024.")
        if not all(0 < timeout <= 3600 for timeout in (header_timeout, connect_timeout, idle_timeout)):
            raise ValueError("Proxy timeouts must be positive and at most one hour.")
        self._header_timeout = header_timeout
        self._connect_timeout = connect_timeout
        self._idle_timeout = idle_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._sockets: set[socket.socket] = set()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._socket_path: Path | None = None

    @property
    def socket_path(self) -> Path:
        if self._socket_path is None:
            raise RuntimeError("The controlled proxy has not been started.")
        return self._socket_path

    @property
    def healthy(self) -> bool:
        return not self._closed.is_set() and self._thread is not None and self._thread.is_alive()

    def start(self) -> ControlledProxy:
        if self._listener is not None or self._closed.is_set():
            raise RuntimeError("A controlled proxy can only be started once.")
        try:
            self._directory = tempfile.TemporaryDirectory(prefix="ctmcp-proxy-")
            self._socket_path = Path(self._directory.name) / "proxy.sock"
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.bind(str(self._socket_path))
            self._socket_path.chmod(0o600)
            self._listener.listen(64)
            self._listener.settimeout(0.2)
            self._thread = threading.Thread(target=self._accept, name="ctmcp-proxy", daemon=True)
            self._thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            self._listener.close()
        with self._lock:
            sockets = list(self._sockets)
        for stream in sockets:
            self._close_socket(stream)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        if self._directory is not None:
            self._directory.cleanup()

    def __enter__(self) -> ControlledProxy:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _close_socket(stream: socket.socket) -> None:
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        stream.close()

    def _track(self, stream: socket.socket) -> bool:
        with self._lock:
            if self._closed.is_set():
                self._close_socket(stream)
                return False
            self._sockets.add(stream)
            return True

    def _forget(self, stream: socket.socket) -> None:
        self._close_socket(stream)
        with self._lock:
            self._sockets.discard(stream)

    def _accept(self) -> None:
        assert self._listener is not None
        try:
            while not self._closed.is_set():
                try:
                    client, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not self._slots.acquire(blocking=False):
                    client.close()
                    continue
                if not self._track(client):
                    self._slots.release()
                    break
                try:
                    threading.Thread(target=self._serve, args=(client,), name="ctmcp-tunnel", daemon=True).start()
                except BaseException:
                    self._forget(client)
                    self._slots.release()
                    raise
        finally:
            # An accept-loop failure revokes all existing tunnels too.
            self.close()

    def _read_connect(self, client: socket.socket) -> tuple[Destination, bytes]:
        deadline = time.monotonic() + self._header_timeout
        data = bytearray()
        while b"\r\n\r\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ProxyError(408, "Request Timeout")
            client.settimeout(remaining)
            try:
                chunk = client.recv(min(4096, _MAX_HEADER + 1 - len(data)))
            except socket.timeout as exc:
                raise _ProxyError(408, "Request Timeout") from exc
            if not chunk:
                raise _ProxyError(400, "Bad Request")
            data += chunk
            if len(data) > _MAX_HEADER:
                raise _ProxyError(431, "Request Header Fields Too Large")
        head, remainder = bytes(data).split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        try:
            method, authority, version = lines[0].decode("ascii").split(" ")
            if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ValueError("Only CONNECT is supported")
            destination = parse_destination(authority)
            seen: set[bytes] = set()
            if len(lines) > 65:
                raise ValueError("Too many headers")
            for line in lines[1:]:
                name, value = line.split(b":", 1)
                if _HEADER_NAME.fullmatch(name) is None or any(ch < 32 and ch != 9 or ch == 127 for ch in value):
                    raise ValueError("Invalid header")
                name = name.lower()
                if name in {b"content-length", b"transfer-encoding", b"proxy-authorization"} or name in seen:
                    raise ValueError("Unsupported or duplicate header")
                seen.add(name)
                if name == b"host" and parse_destination(value.decode("ascii").strip()) != destination:
                    raise ValueError("Conflicting destination")
        except (ValueError, UnicodeError) as exc:
            raise _ProxyError(400, "Bad Request") from exc
        if destination not in self.allowed_destinations:
            raise _ProxyError(403, "Forbidden")
        return destination, remainder

    def _resolve(self, destination: Destination, deadline: float) -> list[_ResolvedAddress]:
        slots = _DNS_SLOTS
        if not slots.acquire(blocking=False):
            raise _ProxyError(503, "Service Unavailable")
        done = threading.Event()
        answers: list[_ResolvedAddress] = []
        failures: list[Exception] = []

        def lookup() -> None:
            try:
                answers.extend(cast(list[_ResolvedAddress], socket.getaddrinfo(
                    destination.host, destination.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
                )))
            except Exception as exc:
                failures.append(exc)
            finally:
                slots.release()
                done.set()

        try:
            threading.Thread(target=lookup, name="ctmcp-dns", daemon=True).start()
        except BaseException:
            slots.release()
            raise
        while not done.is_set():
            remaining = deadline - time.monotonic()
            if self._closed.is_set() or remaining <= 0:
                raise _ProxyError(504, "Gateway Timeout")
            done.wait(min(0.1, remaining))
        if failures:
            raise _ProxyError(502, "Bad Gateway")
        return answers

    def _connect(self, destination: Destination) -> socket.socket:
        deadline = time.monotonic() + self._connect_timeout
        answers = self._resolve(destination, deadline)
        if not answers or len(answers) > _MAX_ADDRESSES:
            raise _ProxyError(403, "Forbidden")
        checked: list[tuple[socket.AddressFamily, tuple[str, int] | tuple[str, int, int, int]]] = []
        for family, kind, protocol, _, address in answers:
            if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM or protocol != socket.IPPROTO_TCP:
                raise _ProxyError(403, "Forbidden")
            # Reject the whole mixed DNS answer set, not just its unsafe entries.
            if not isinstance(address[0], str) or not _is_public_address(address[0]) or address[1] != destination.port:
                raise _ProxyError(403, "Forbidden")
            numeric = str(ipaddress.ip_address(address[0]))
            if family == socket.AF_INET6:
                if len(address) != 4 or address[2] != 0 or address[3] != 0 or ":" not in numeric:
                    raise _ProxyError(403, "Forbidden")
                pinned: tuple[str, int] | tuple[str, int, int, int] = (numeric, destination.port, 0, 0)
            else:
                if len(address) != 2 or ":" in numeric:
                    raise _ProxyError(403, "Forbidden")
                pinned = (numeric, destination.port)
            candidate = (family, pinned)
            if candidate not in checked:
                checked.append(candidate)
        for family, address in checked:
            if self._closed.is_set() or time.monotonic() >= deadline:
                break
            upstream = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            if not self._track(upstream):
                break
            try:
                upstream.settimeout(max(0.001, deadline - time.monotonic()))
                if _is_host_local_address(upstream, address):
                    raise _ProxyError(403, "Forbidden")
                # The hostname is never handed to connect/create_connection:
                # DNS cannot rebind between validation and the actual connect.
                upstream.connect(address)
                return upstream
            except _ProxyError:
                self._forget(upstream)
                raise
            except OSError:
                self._forget(upstream)
        raise _ProxyError(502, "Bad Gateway")

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        established = False
        try:
            destination, remainder = self._read_connect(client)
            upstream = self._connect(destination)
            client.settimeout(self._header_timeout)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            established = True
            self._relay(client, upstream, remainder)
        except _ProxyError as exc:
            if not established:
                self._send_error(client, exc.status, exc.reason)
        except (OSError, ValueError):
            if not established:
                self._send_error(client, 502, "Bad Gateway")
        finally:
            if upstream is not None:
                self._forget(upstream)
            self._forget(client)
            self._slots.release()

    @staticmethod
    def _send_error(client: socket.socket, status: int, reason: str) -> None:
        try:
            client.settimeout(0.2)
            client.sendall(f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode("ascii"))
        except OSError:
            pass

    def _relay(self, client: socket.socket, upstream: socket.socket, initial: bytes) -> None:
        streams = (client, upstream)
        pending = [bytearray(), bytearray(initial)]  # Bytes awaiting writes to each stream.
        readable = [True, True]
        write_closed = [False, False]
        for stream in streams:
            stream.setblocking(False)
        last_activity = time.monotonic()
        with selectors.DefaultSelector() as selector:
            while not self._closed.is_set():
                for index, stream in enumerate(streams):
                    peer = 1 - index
                    if not readable[peer] and not pending[index] and not write_closed[index]:
                        try:
                            stream.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        write_closed[index] = True
                    events = 0
                    if readable[index] and len(pending[peer]) < _MAX_BUFFER:
                        events |= selectors.EVENT_READ
                    if pending[index]:
                        events |= selectors.EVENT_WRITE
                    try:
                        selector.unregister(stream)
                    except KeyError:
                        pass
                    if events:
                        selector.register(stream, events, index)
                if not any(readable) and not any(pending):
                    return
                remaining = self._idle_timeout - (time.monotonic() - last_activity)
                if remaining <= 0:
                    return
                for key, events in selector.select(min(0.2, remaining)):
                    index = key.data
                    stream = streams[index]
                    peer = 1 - index
                    if events & selectors.EVENT_READ:
                        try:
                            chunk = stream.recv(min(16384, _MAX_BUFFER - len(pending[peer])))
                        except BlockingIOError:
                            continue
                        if chunk:
                            pending[peer] += chunk
                            last_activity = time.monotonic()
                        else:
                            readable[index] = False
                    if events & selectors.EVENT_WRITE:
                        try:
                            sent = stream.send(pending[index])
                        except BlockingIOError:
                            continue
                        if sent <= 0:
                            return
                        del pending[index][:sent]
                        last_activity = time.monotonic()
