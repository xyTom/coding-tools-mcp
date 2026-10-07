"""Destination policy and real local transport tests, not native sandbox acceptance.

Protocol tests use a test-only TCP ingress because some restricted executors
cannot create Unix sockets. UnixProxyTests repeats the protocol cases with the
production Unix ingress when available. Loopback targets replace DNS and the
public/local-address predicates only in permitted-tunnel tests; production refusal is
tested independently. Neither transport fixture proves native OS enforcement.
"""
from __future__ import annotations

import os
import socket
import stat
import threading
import time
import unittest
from unittest.mock import patch

from coding_tools_mcp.network_proxy import ControlledProxy, _is_host_local_address, _is_public_address, parse_destination


def _read_header(stream: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = stream.recv(65536)
        if not chunk:
            raise AssertionError(f"Connection closed before response: {data!r}")
        data += chunk
    head, remainder = data.split(b"\r\n\r\n", 1)
    return head, remainder


class DestinationTests(unittest.TestCase):
    def test_exact_normalization(self):
        self.assertEqual(parse_destination("EXAMPLE.com.:443").authority, "example.com:443")
        self.assertEqual(parse_destination("bücher.example:443").authority, "xn--bcher-kva.example:443")
        self.assertEqual(parse_destination("[2606:4700:4700::1111]:443").authority, "[2606:4700:4700::1111]:443")
        self.assertEqual(parse_destination("8.8.8.8:0053").port, 53)

    def test_malformed_destinations(self):
        for value in ("", "*", "*.example.com:443", "https://example.com:443", "example.com",
                      "example.com:0", "example.com:65536", "example.com:-1", "example.com:+443",
                      "example.com:４４３", "example.com:443/", "x@y:443", "x:443#y", "x:443?y",
                      "[::1%lo]:443", "::1:443", "[::1]443", "example.com:443\r\n", ".:443",
                      "-example.com:443", "example..com:443", "example.com..:443", "a_b.example:443"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_destination(value)

    def test_non_global_and_transition_addresses_denied(self):
        for address in ("127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.0.1", "192.168.0.1",
                        "169.254.169.254", "100.64.0.1", "192.0.2.1", "192.0.0.8", "192.88.99.2", "198.18.0.1", "224.0.0.1",
                        "255.255.255.255", "::", "::1", "fc00::1", "fe80::1", "ff02::1", "2001:db8::1",
                        "::ffff:127.0.0.1", "::ffff:8.8.8.8", "64:ff9b::7f00:1", "64:ff9b:1::a00:1",
                        "2002:7f00:1::", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "100:0:0:1::1",
                        "3fff::1", "5f00::1", "not-an-address"):
            with self.subTest(address=address):
                self.assertFalse(_is_public_address(address))
        for address in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2001:4860:4860::8888"):
            self.assertTrue(_is_public_address(address), address)

    def test_host_local_address_check_rejects_without_connecting(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
            self.assertTrue(_is_host_local_address(stream, ("127.0.0.1", 443)))
            self.assertEqual(stream.getsockname()[0], "127.0.0.1")
            with self.assertRaises(OSError):
                stream.getpeername()

    def test_empty_policy_or_bad_limits_refused(self):
        with self.assertRaises(ValueError):
            ControlledProxy(())
        for kwargs in ({"max_connections": 0}, {"header_timeout": 0}, {"idle_timeout": float("nan")}, {"connect_timeout": 9999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ControlledProxy(("example.com:443",), **kwargs)


class ProxyProtocolTests(unittest.TestCase):
    def proxy(self, destinations=("allowed.example:443",), **kwargs):
        # Test-only TCP ingress. Production start() never exposes a TCP listener.
        proxy = ControlledProxy(destinations, **kwargs)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        listener.settimeout(0.2)
        proxy._listener = listener
        self._endpoints = getattr(self, "_endpoints", {})
        self._endpoints[proxy] = listener.getsockname()
        proxy._thread = threading.Thread(target=proxy._accept, daemon=True)
        proxy._thread.start()
        self.addCleanup(proxy.close)
        return proxy

    def client(self, proxy):
        stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(stream.close)
        stream.settimeout(2)
        stream.connect(self._endpoints[proxy])
        return stream

    def request(self, proxy, request):
        client = self.client(proxy)
        client.sendall(request)
        return _read_header(client)[0]

    @staticmethod
    def answer(host="127.0.0.1", port=443):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
        return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)

    def echo_fixture(self):
        """A reachable local TCP fixture; no Internet or native sandbox claim."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.1)
        self.addCleanup(listener.close)
        stop = threading.Event()
        active = []

        def serve():
            while not stop.is_set():
                try:
                    stream, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                active.append(stream)
                stream.settimeout(1)
                try:
                    while not stop.is_set():
                        data = stream.recv(16384)
                        if not data:
                            stream.shutdown(socket.SHUT_WR)
                            break
                        stream.sendall(data)
                except OSError:
                    pass
                finally:
                    stream.close()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()

        def finish():
            stop.set()
            listener.close()
            for stream in active:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
            worker.join(timeout=2)

        self.addCleanup(finish)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1) as baseline:
            baseline.sendall(b"reachable before test")
            self.assertEqual(baseline.recv(64), b"reachable before test")
        return port

    def test_unlisted_host_and_port_denied_without_dns(self):
        proxy = self.proxy()
        for authority in ("denied.example:443", "allowed.example:80", "sub.allowed.example:443", "127.0.0.1:443"):
            with self.subTest(authority=authority), patch("coding_tools_mcp.network_proxy.socket.getaddrinfo") as resolve:
                response = self.request(proxy, f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode())
                self.assertIn(b"403 Forbidden", response)
                resolve.assert_not_called()

    def test_invalid_connect_headers_rejected(self):
        proxy = self.proxy()
        requests = (
            b"GET https://allowed.example/ HTTP/1.1\r\n\r\n",
            b"CONNECT allowed.example:443 HTTP/2.0\r\n\r\n",
            b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: other.example:443\r\n\r\n",
            b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\nHost: allowed.example:443\r\n\r\n",
            b"CONNECT allowed.example:443 HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
            b"CONNECT allowed.example:443 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"CONNECT allowed.example:443 HTTP/1.1\r\nProxy-Authorization: Basic unsafe\r\n\r\n",
            b"CONNECT allowed.example:443 HTTP/1.1\r\n malformed: value\r\n\r\n",
            b"CONNECT allowed.example:443 HTTP/1.1\r\nX-Test: bad\x00header\r\n\r\n",
        )
        for request in requests:
            with self.subTest(request=request), patch("coding_tools_mcp.network_proxy.socket.getaddrinfo") as resolve:
                self.assertIn(b"400 Bad Request", self.request(proxy, request))
                resolve.assert_not_called()

    def test_private_mixed_or_invalid_dns_answers_denied(self):
        proxy = self.proxy()
        unsafe = [
            [self.answer()], [self.answer("::1")],
            [self.answer("8.8.8.8"), self.answer()],
            [self.answer("8.8.8.8", 80)], [],
            [self.answer("8.8.8.8")] * 17,
            [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700:4700::1111", 443, 0, 2))],
            [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", ("8.8.8.8", 443))],
        ]
        for answers in unsafe:
            with self.subTest(answers=answers), patch("coding_tools_mcp.network_proxy.socket.getaddrinfo", return_value=answers):
                response = self.request(proxy, b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
                self.assertIn(b"403 Forbidden", response)

    def test_public_but_host_local_target_is_rejected(self):
        port = self.echo_fixture()
        proxy = self.proxy((f"allowed.example:{port}",))
        # Force only global classification; leave the actual host-local guard.
        with patch("coding_tools_mcp.network_proxy._is_public_address", return_value=True), patch(
            "coding_tools_mcp.network_proxy.socket.getaddrinfo", return_value=[self.answer(port=port)]
        ):
            response = self.request(proxy, f"CONNECT allowed.example:{port} HTTP/1.1\r\n\r\n".encode())
            self.assertIn(b"403 Forbidden", response)

    def test_dns_failure_fail_closed(self):
        proxy = self.proxy()
        with patch("coding_tools_mcp.network_proxy.socket.getaddrinfo", side_effect=socket.gaierror("unavailable")):
            response = self.request(proxy, b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
            self.assertIn(b"502 Bad Gateway", response)

    def test_real_tunnel_pins_checked_address_and_preserves_early_payload(self):
        port = self.echo_fixture()
        authority = f"allowed.example:{port}"
        proxy = self.proxy((authority,))
        # Intentional unit-only override: never enabled by a production option.
        with patch("coding_tools_mcp.network_proxy._is_host_local_address", return_value=False), patch("coding_tools_mcp.network_proxy._is_public_address", return_value=True) as public, patch(
            "coding_tools_mcp.network_proxy.socket.getaddrinfo", return_value=[self.answer(port=port)]
        ) as resolve:
            client = self.client(proxy)
            client.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode() + b"early payload")
            head, echo = _read_header(client)
            self.assertIn(b"200 Connection Established", head)
            while len(echo) < len(b"early payload"):
                echo += client.recv(64)
            self.assertEqual(echo, b"early payload")
            client.sendall(b"after connect")
            self.assertEqual(client.recv(64), b"after connect")
            resolve.assert_called_once_with("allowed.example", port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            public.assert_called_once_with("127.0.0.1")
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(64), b"")

    def test_proxy_close_revokes_existing_tunnel_and_listener(self):
        port = self.echo_fixture()
        proxy = self.proxy((f"allowed.example:{port}",))
        with patch("coding_tools_mcp.network_proxy._is_host_local_address", return_value=False), patch("coding_tools_mcp.network_proxy._is_public_address", return_value=True), patch(
            "coding_tools_mcp.network_proxy.socket.getaddrinfo", return_value=[self.answer(port=port)]
        ):
            client = self.client(proxy)
            client.sendall(f"CONNECT allowed.example:{port} HTTP/1.0\r\n\r\n".encode())
            self.assertIn(b"200", _read_header(client)[0])
            proxy.close()
            self.assertEqual(client.recv(64), b"")
            self.assertFalse(proxy.healthy)
            with self.assertRaises(OSError):
                self.client(proxy)

    def test_connect_failure_does_not_report_established(self):
        dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        proxy = self.proxy((f"allowed.example:{port}",))
        with patch("coding_tools_mcp.network_proxy._is_host_local_address", return_value=False), patch("coding_tools_mcp.network_proxy._is_public_address", return_value=True), patch(
            "coding_tools_mcp.network_proxy.socket.getaddrinfo", return_value=[self.answer(port=port)]
        ):
            response = self.request(proxy, f"CONNECT allowed.example:{port} HTTP/1.0\r\n\r\n".encode())
            self.assertIn(b"502 Bad Gateway", response)
            self.assertNotIn(b"200", response)

    def test_partial_header_timeout_and_connection_limit(self):
        proxy = self.proxy(max_connections=1, header_timeout=0.2)
        first = self.client(proxy)
        first.sendall(b"CONNECT ")
        # Wait until accept has reserved its slot, without relying on sleeps.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with proxy._lock:
                if first.fileno() >= 0 and proxy._sockets:
                    break
            time.sleep(0.005)
        second = self.client(proxy)
        self.assertEqual(second.recv(1), b"")
        response = _read_header(first)[0]
        self.assertIn(b"408 Request Timeout", response)

    def test_dns_timeout_and_global_capacity_fail_closed(self):
        started = threading.Event()
        finish = threading.Event()
        finished = threading.Event()

        def stalled_lookup(*args, **kwargs):
            started.set()
            finish.wait(2)
            finished.set()
            return [self.answer("8.8.8.8")]

        proxy = self.proxy(connect_timeout=0.3)
        with patch("coding_tools_mcp.network_proxy._DNS_SLOTS", threading.BoundedSemaphore(1)), patch(
            "coding_tools_mcp.network_proxy.socket.getaddrinfo", side_effect=stalled_lookup
        ) as resolve:
            try:
                first = self.client(proxy)
                first.sendall(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
                self.assertTrue(started.wait(1))
                response = self.request(proxy, b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
                self.assertIn(b"503 Service Unavailable", response)
                self.assertIn(b"504 Gateway Timeout", _read_header(first)[0])
                resolve.assert_called_once()
            finally:
                finish.set()
                self.assertTrue(finished.wait(1))

    def test_large_duplex_stream_is_not_truncated(self):
        port = self.echo_fixture()
        proxy = self.proxy((f"allowed.example:{port}",))
        with patch("coding_tools_mcp.network_proxy._is_host_local_address", return_value=False), patch("coding_tools_mcp.network_proxy._is_public_address", return_value=True), patch(
            "coding_tools_mcp.network_proxy.socket.getaddrinfo", return_value=[self.answer(port=port)]
        ):
            client = self.client(proxy)
            client.sendall(f"CONNECT allowed.example:{port} HTTP/1.0\r\n\r\n".encode())
            self.assertIn(b"200", _read_header(client)[0])
            payload = b"bounded binary stream\x00" * 32768
            errors = []

            def write():
                try:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                except OSError as exc:
                    errors.append(exc)

            worker = threading.Thread(target=write, daemon=True)
            worker.start()
            echoed = bytearray()
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                echoed += chunk
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(echoed, payload)

    def test_oversized_header_rejected(self):
        proxy = self.proxy()
        response = self.request(proxy, b"CONNECT allowed.example:443 HTTP/1.1\r\nX-Large: " + b"a" * 17000 + b"\r\n\r\n")
        self.assertIn(b"431", response)


class UnixProxyTests(ProxyProtocolTests):
    @classmethod
    def setUpClass(cls):
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except (AttributeError, OSError) as exc:
            message = f"Unix-domain sockets unavailable in test host: {exc}"
            if os.environ.get("CODING_TOOLS_SANDBOX_REQUIRE_NATIVE") == "1":
                raise AssertionError(message) from exc
            raise unittest.SkipTest(message) from exc
        probe.close()

    def proxy(self, destinations=("allowed.example:443",), **kwargs):
        proxy = ControlledProxy(destinations, **kwargs).start()
        self.addCleanup(proxy.close)
        return proxy

    def client(self, proxy):
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(stream.close)
        stream.settimeout(2)
        stream.connect(str(proxy.socket_path))
        return stream

    def test_private_listener_and_single_start(self):
        proxy = self.proxy()
        self.assertTrue(proxy.healthy)
        self.assertEqual(stat.S_IMODE(proxy.socket_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(proxy.socket_path.parent.stat().st_mode), 0o700)
        with self.assertRaises(RuntimeError):
            proxy.start()
        path = proxy.socket_path
        proxy.close()
        self.assertFalse(proxy.healthy)
        self.assertFalse(path.parent.exists())
        with self.assertRaises(RuntimeError):
            proxy.start()


if __name__ == "__main__":
    unittest.main()
