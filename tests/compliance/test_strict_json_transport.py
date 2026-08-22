from __future__ import annotations

import http.client
import io
import json
import os
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any, cast

from coding_tools_mcp.json_utils import (
    MAX_JSON_INTEGER_DIGITS,
    strict_json_bytes,
    strict_json_dumps,
    strict_json_loads,
)
from coding_tools_mcp.server import AuthorizationContext, MCPHandler, Runtime, RuntimeHTTPServer
from coding_tools_mcp.transport_stdio import serve_stdio
from coding_tools_mcp.upstream import (
    MAX_RESPONSE_BYTES,
    StdioUpstreamClient,
    UpstreamError,
    UpstreamManager,
    decode_http_rpc_response,
)

from tests.compliance.mcp_client import StdioMCPClient


_DEEP_JSON_VALUE = "[" * 2_000 + "0" + "]" * 2_000


def _rpc_request_with_raw_value(request_id: int, raw_value: str) -> str:
    return (
        '{"jsonrpc":"2.0","id":'
        + str(request_id)
        + ',"method":"tools/list","params":{"value":'
        + raw_value
        + '}}'
    )


def _upstream_response_with_raw_value(request_id: int, raw_value: str) -> bytes:
    return (
        '{"jsonrpc":"2.0","id":'
        + str(request_id)
        + ',"result":{"value":'
        + raw_value
        + '}}'
    ).encode("utf-8")


class _Headers(dict[str, str]):
    def get_content_type(self) -> str:
        return self.get("Content-Type", "")


class StrictJSONTransportTests(unittest.TestCase):
    def test_strict_loader_bounds_integer_digits_and_float_overflow(self) -> None:
        accepted = strict_json_loads(
            '{"value":' + ('9' * MAX_JSON_INTEGER_DIGITS) + '}'
        )
        self.assertEqual(len(str(accepted["value"])), MAX_JSON_INTEGER_DIGITS)

        with self.assertRaises(ValueError):
            strict_json_loads(
                '{"value":' + ('9' * (MAX_JSON_INTEGER_DIGITS + 1)) + '}'
            )
        self.assertEqual(strict_json_loads('{"value":1e308}')["value"], 1e308)
        for raw in ("1e309", "-1e999999", "NaN", "Infinity", "-Infinity"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json_loads('{"value":' + raw + '}')

    def test_project_integer_limit_ignores_lower_python_global_limit(self) -> None:
        original_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(1_000)
            parsed = strict_json_loads('{"value":' + ('9' * 1_001) + '}')
            self.assertGreater(parsed["value"].bit_length(), 3_000)
        finally:
            sys.set_int_max_str_digits(original_limit)

    def test_strict_encoder_owns_integer_float_and_surrogate_contract(self) -> None:
        original_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(1_000)
            value = 10**1_000 + 7
            encoded = strict_json_bytes({"value": value, "text": "high:\ud800"})
            reparsed = strict_json_loads(encoded)
            self.assertEqual(reparsed["value"], value)
            self.assertEqual(reparsed["text"], "high:\ud800")
            self.assertIn(b"\\ud800", encoded)
            boundary = 10 ** (MAX_JSON_INTEGER_DIGITS - 1)
            boundary_encoded = strict_json_bytes({"value": boundary})
            self.assertEqual(strict_json_loads(boundary_encoded)["value"], boundary)
            with self.assertRaises(ValueError):
                strict_json_dumps({"value": 10**MAX_JSON_INTEGER_DIGITS})
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    strict_json_dumps({"value": value})
        finally:
            sys.set_int_max_str_digits(original_limit)

    def test_strict_loader_rejects_deep_nesting_and_non_utf8_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "nesting"):
            strict_json_loads(_DEEP_JSON_VALUE)

        self.assertEqual(strict_json_loads(b'{"value":1}'), {"value": 1})
        document = '{"value":1}'
        for encoding in ("utf-16", "utf-16-le", "utf-32", "utf-32-le"):
            with self.subTest(encoding=encoding), self.assertRaises(
                (UnicodeDecodeError, ValueError)
            ):
                strict_json_loads(document.encode(encoding))

    def test_stdio_parse_error_does_not_terminate_the_request_loop(self) -> None:
        bad_integer = (
            '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"n":'
            + ('9' * (MAX_JSON_INTEGER_DIGITS + 1))
            + '}}\n'
        )
        bad_float = (
            '{"jsonrpc":"2.0","id":2,"method":"tools/list",'
            '"params":{"n":1e309}}\n'
        )
        bad_depth = _rpc_request_with_raw_value(3, _DEEP_JSON_VALUE) + "\n"
        good = (
            '{"jsonrpc":"2.0","id":4,"method":"initialize","params":'
            '{"clientInfo":{"name":"test","version":"1"}}}\n'
        )
        sink = io.StringIO()
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), transport="stdio")
            code = serve_stdio(
                runtime,
                input_stream=io.StringIO(bad_integer + bad_float + bad_depth + good),
                output_stream=sink,
            )
        self.assertEqual(code, 0)
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual([item["error"]["code"] for item in responses[:3]], [-32700] * 3)
        self.assertEqual(responses[3]["id"], 4)
        self.assertIn("result", responses[3])

    def test_production_stdio_uses_raw_utf8_pipes_and_recovers_after_bad_bytes(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            home = root / "home"
            workspace.mkdir()
            (home / "AppData" / "Roaming").mkdir(parents=True)
            (home / "AppData" / "Local").mkdir(parents=True)
            (workspace / "测试.txt").write_text("UTF-8响应测试\n", encoding="utf-8")

            env = os.environ.copy()
            env["PYTHONUTF8"] = "0"
            env.pop("PYTHONIOENCODING", None)
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
            env["APPDATA"] = str(home / "AppData" / "Roaming")
            env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
            current_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                str(repository_root)
                if not current_pythonpath
                else str(repository_root) + os.pathsep + current_pythonpath
            )

            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "coding_tools_mcp",
                    "--workspace",
                    str(workspace),
                    "--stdio",
                ],
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=False,
            )
            self.assertIsNotNone(process.stdin)
            self.assertIsNotNone(process.stdout)
            self.assertIsNotNone(process.stderr)
            stdin = cast(Any, process.stdin)
            stdout = cast(Any, process.stdout)
            stderr = cast(Any, process.stderr)
            stdout_lines: queue.Queue[bytes] = queue.Queue()
            stderr_chunks: list[bytes] = []

            def drain_stdout() -> None:
                for raw_line in stdout:
                    stdout_lines.put(raw_line)

            def drain_stderr() -> None:
                stderr_chunks.extend(stderr.readlines())

            stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            def send(
                payload: dict[str, Any],
                *,
                ensure_ascii: bool = False,
            ) -> None:
                raw = (
                    json.dumps(
                        payload,
                        ensure_ascii=ensure_ascii,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
                stdin.write(raw)
                stdin.flush()

            def read_response() -> tuple[bytes, dict[str, Any]]:
                try:
                    raw_line = stdout_lines.get(timeout=15)
                except queue.Empty as exc:
                    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
                    raise AssertionError(
                        f"timed out waiting for stdio response; stderr={stderr_text!r}"
                    ) from exc
                self.assertTrue(raw_line.endswith(b"\n"), raw_line)
                self.assertFalse(raw_line.endswith(b"\r\n"), raw_line)
                decoded = raw_line.decode("utf-8")
                parsed = json.loads(decoded)
                self.assertIsInstance(parsed, dict)
                return raw_line, cast(dict[str, Any], parsed)

            try:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "测试", "version": "1"},
                        },
                    }
                )
                _, initialized = read_response()
                self.assertEqual(initialized["id"], 1)
                self.assertIn("result", initialized)

                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    }
                )
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "read_file",
                            "arguments": {"path": "测试.txt"},
                        },
                    }
                )
                raw_read, read_result = read_response()
                self.assertEqual(read_result["id"], 2)
                self.assertNotIn("error", read_result)
                self.assertIn("测试.txt", raw_read.decode("utf-8"))
                self.assertIn("UTF-8响应测试", raw_read.decode("utf-8"))
                self.assertIn("测试.txt".encode("utf-8"), raw_read)

                stdin.write(
                    b'{"jsonrpc":"2.0","id":3,"method":"tools/list",'
                    b'"params":{"bad":"\xff"}}\n'
                )
                stdin.flush()
                _, parse_error = read_response()
                self.assertIsNone(parse_error.get("id"))
                self.assertEqual(parse_error["error"]["code"], -32700)

                for request_id, surrogate, escaped in (
                    (4, "\ud800", b"\\ud800"),
                    (5, "\udc00", b"\\udc00"),
                ):
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "tools/call",
                            "params": {
                                "name": surrogate,
                                "arguments": {},
                            },
                        },
                        ensure_ascii=True,
                    )
                    raw_error, error = read_response()
                    self.assertEqual(error["id"], request_id)
                    self.assertEqual(error["error"]["code"], -32602)
                    self.assertIn(escaped, raw_error)

                emoji = "😀"
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": emoji,
                            "arguments": {},
                        },
                    }
                )
                raw_emoji, emoji_error = read_response()
                self.assertEqual(emoji_error["id"], 6)
                self.assertEqual(emoji_error["error"]["code"], -32602)
                self.assertIn(emoji.encode("utf-8"), raw_emoji)

                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/list",
                        "params": {},
                    }
                )
                _, tools = read_response()
                self.assertEqual(tools["id"], 7)
                self.assertIn("result", tools)
            finally:
                try:
                    stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
                stdout_thread.join(timeout=3)
                stderr_thread.join(timeout=3)
                for stream in (stdout, stderr):
                    try:
                        stream.close()
                    except OSError:
                        pass

            stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            self.assertEqual(process.returncode, 0, stderr_text)

    def test_compliance_stdio_client_explicitly_decodes_utf8(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            home = root / "home"
            workspace.mkdir()
            (home / "AppData" / "Roaming").mkdir(parents=True)
            (home / "AppData" / "Local").mkdir(parents=True)
            (workspace / "测试.txt").write_text("客户端 UTF-8 😀\n", encoding="utf-8")
            env = {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
            }
            with patch.dict(os.environ, env, clear=False):
                with StdioMCPClient(workspace) as client:
                    result = client.call_tool("read_file", {"path": "测试.txt"})
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertIn("测试.txt", serialized)
            self.assertIn("客户端 UTF-8 😀", serialized)

    def test_http_parse_error_is_structured_and_connection_remains_usable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_runtime = Runtime(root, transport="http")

            def runtime_factory(context: AuthorizationContext) -> Runtime:
                return Runtime(root, transport="http", authorization_context=context)

            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                control_runtime,
                runtime_factory,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=5
            )
            try:
                bad_payloads = (
                    _rpc_request_with_raw_value(1, '9' * (MAX_JSON_INTEGER_DIGITS + 1)).encode("utf-8"),
                    _rpc_request_with_raw_value(2, "1e309").encode("utf-8"),
                    _rpc_request_with_raw_value(3, _DEEP_JSON_VALUE).encode("utf-8"),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 4,
                            "method": "initialize",
                            "params": {"clientInfo": {"name": "test", "version": "1"}},
                        }
                    ).encode("utf-16"),
                )
                for bad in bad_payloads:
                    connection.request(
                        "POST",
                        "/mcp",
                        body=bad,
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    error = json.loads(response.read())
                    self.assertEqual(error["error"]["code"], -32700)

                good = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "initialize",
                        "params": {"clientInfo": {"name": "test", "version": "1"}},
                    }
                ).encode("utf-8")
                connection.request(
                    "POST",
                    "/mcp",
                    body=good,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                initialized = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(initialized["id"], 5)
                self.assertIn("result", initialized)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_upstream_http_and_sse_map_strict_json_failures_to_protocol_error(self) -> None:
        bad_values = (
            ('9' * (MAX_JSON_INTEGER_DIGITS + 1)),
            "1e309",
            _DEEP_JSON_VALUE,
        )
        for raw_value in bad_values:
            payload = _upstream_response_with_raw_value(1, raw_value)
            for content_type, body in (
                ("application/json", payload),
                ("text/event-stream", b"data: " + payload + b"\n\n"),
            ):
                with self.subTest(raw_value=raw_value[:20], content_type=content_type):
                    with self.assertRaises(UpstreamError) as raised:
                        decode_http_rpc_response(body, content_type, expected_id=1)
                    self.assertEqual(raised.exception.code, "UPSTREAM_PROTOCOL_ERROR")

        utf16 = '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'.encode("utf-16")
        with self.assertRaises(UpstreamError) as raised:
            decode_http_rpc_response(utf16, "application/json", expected_id=1)
        self.assertEqual(raised.exception.code, "UPSTREAM_PROTOCOL_ERROR")

    def test_upstream_stdio_reader_survives_bad_json_and_reads_next_response(self) -> None:
        bad_integer = (
            '{"jsonrpc":"2.0","id":1,"result":{"value":'
            + ('9' * (MAX_JSON_INTEGER_DIGITS + 1))
            + '}}\n'
        )
        bad_depth = _upstream_response_with_raw_value(2, _DEEP_JSON_VALUE).decode("utf-8") + "\n"
        good = '{"jsonrpc":"2.0","id":3,"result":{"ok":true}}\n'
        client = cast(StdioUpstreamClient, object.__new__(StdioUpstreamClient))
        fabricated = cast(Any, client)
        fabricated.process = SimpleNamespace(stdout=io.StringIO(bad_integer + bad_depth + good))
        fabricated._responses = queue.Queue()

        client._read_stdout()

        first = client._responses.get_nowait()
        second = client._responses.get_nowait()
        third = client._responses.get_nowait()
        self.assertIsInstance(first, UpstreamError)
        self.assertIsInstance(second, UpstreamError)
        first_error = cast(UpstreamError, first)
        second_error = cast(UpstreamError, second)
        self.assertEqual(first_error.code, "UPSTREAM_PROTOCOL_ERROR")
        self.assertEqual(second_error.code, "UPSTREAM_PROTOCOL_ERROR")
        self.assertIsInstance(third, dict)
        self.assertEqual(cast(dict[str, Any], third)["id"], 3)

    def test_upstream_stdio_reader_rejects_oversized_frame_and_recovers(self) -> None:
        oversized = (
            b'{"jsonrpc":"2.0","id":1,"result":{"value":"'
            + (b"x" * (MAX_RESPONSE_BYTES + 1))
            + b'"}}\n'
        )
        good = b'{"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n'
        text_pipe = io.TextIOWrapper(
            io.BytesIO(oversized + good),
            encoding="utf-8",
            errors="strict",
            newline="\n",
        )
        client = cast(StdioUpstreamClient, object.__new__(StdioUpstreamClient))
        fabricated = cast(Any, client)
        fabricated.process = SimpleNamespace(stdout=text_pipe)
        fabricated._responses = queue.Queue()

        client._read_stdout()

        first = client._responses.get_nowait()
        second = client._responses.get_nowait()
        self.assertIsInstance(first, UpstreamError)
        oversized_error = cast(UpstreamError, first)
        self.assertEqual(oversized_error.code, "UPSTREAM_RESPONSE_TOO_LARGE")
        self.assertEqual(oversized_error.category, "protocol")
        self.assertFalse(oversized_error.retryable)
        self.assertIsInstance(second, dict)
        self.assertEqual(cast(dict[str, Any], second)["id"], 2)
        text_pipe.close()

    def test_upstream_stdio_stderr_drain_survives_non_utf8_windows_bytes(self) -> None:
        text_pipe = io.TextIOWrapper(
            io.BytesIO(b"\xa8\nFastMCP diagnostic\r\n"),
            encoding="utf-8",
            errors="strict",
            newline="",
        )
        client = cast(StdioUpstreamClient, object.__new__(StdioUpstreamClient))
        fabricated = cast(Any, client)
        fabricated.process = SimpleNamespace(stderr=text_pipe)
        fabricated._stderr_lines = []
        fabricated._stderr_lock = threading.Lock()

        client._read_stderr()

        self.assertGreaterEqual(len(client._stderr_lines), 2)
        self.assertIn("FastMCP diagnostic", client._stderr_lines[-1])
        text_pipe.close()

    def test_upstream_manager_discards_and_closes_failed_stdio_client(self) -> None:
        class _Client:
            closed = False

            def close(self) -> None:
                self.closed = True

        manager = cast(UpstreamManager, object.__new__(UpstreamManager))
        fabricated = cast(Any, manager)
        failed = _Client()
        fabricated._clients = {"origin": failed}
        fabricated._client_locks = {"origin": threading.Lock()}
        fabricated._lifecycle_condition = threading.Condition(threading.Lock())

        manager._discard_client("origin", cast(Any, failed))

        self.assertNotIn("origin", manager._clients)
        self.assertTrue(failed.closed)

    def test_upstream_stdio_writer_uses_binary_utf8_with_surrogate_fallback(self) -> None:
        raw_pipe = io.BytesIO()
        text_pipe = io.TextIOWrapper(
            raw_pipe,
            encoding="utf-8",
            errors="strict",
            newline="\n",
            write_through=True,
        )
        client = cast(StdioUpstreamClient, object.__new__(StdioUpstreamClient))
        fabricated = cast(Any, client)
        fabricated.process = SimpleNamespace(
            stdin=text_pipe,
            poll=lambda: None,
        )

        client._write(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "high:\ud800 low:\udc00 emoji:😀"},
            }
        )

        encoded = raw_pipe.getvalue()
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertFalse(encoded.endswith(b"\r\n"))
        self.assertIn(b"\\ud800", encoded)
        self.assertIn(b"\\udc00", encoded)
        parsed = strict_json_loads(encoded.rstrip(b"\n"))
        self.assertEqual(
            parsed["params"]["name"],
            "high:\ud800 low:\udc00 emoji:😀",
        )
        text_pipe.close()

    def test_upstream_stdio_writer_maps_closed_pipe_to_retryable_disconnect(self) -> None:
        text_pipe = io.TextIOWrapper(
            io.BytesIO(),
            encoding="utf-8",
            errors="strict",
            newline="\n",
            write_through=True,
        )
        text_pipe.close()
        client = cast(StdioUpstreamClient, object.__new__(StdioUpstreamClient))
        fabricated = cast(Any, client)
        fabricated.process = SimpleNamespace(
            stdin=text_pipe,
            poll=lambda: None,
        )

        with self.assertRaises(UpstreamError) as raised:
            client._write(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "closed-pipe"},
                }
            )

        self.assertEqual(raised.exception.code, "UPSTREAM_DISCONNECTED")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.category, "runtime")

    def test_admin_and_dcr_json_bodies_use_the_strict_loader(self) -> None:
        bodies = (
            b'{"value":1e309}',
            ('{"value":' + _DEEP_JSON_VALUE + '}').encode("utf-8"),
            b'{"value":1}'.decode("utf-8").encode("utf-16"),
        )

        class _Registry:
            def register(self, metadata: object) -> object:
                raise AssertionError(f"invalid metadata reached registry: {metadata!r}")

        for body in bodies:
            with self.subTest(body_prefix=body[:20]):
                admin_responses: list[tuple[dict[str, Any], int]] = []
                admin = cast(
                    Any,
                    SimpleNamespace(
                        command="POST",
                        headers=_Headers(
                            {
                                "Content-Type": "application/json",
                                "Content-Length": str(len(body)),
                            }
                        ),
                        rfile=io.BytesIO(body),
                        send_json=lambda payload, status=200, **_: admin_responses.append(
                            (payload, status)
                        ),
                    ),
                )
                self.assertIsNone(MCPHandler._read_admin_json(admin))
                self.assertEqual(admin_responses[0][1], 400)
                self.assertEqual(
                    admin_responses[0][0]["error"]["code"], "invalid_json"
                )

                dcr_responses: list[tuple[dict[str, Any], int]] = []
                dcr = cast(
                    Any,
                    SimpleNamespace(
                        runtime=SimpleNamespace(
                            oauth_config=SimpleNamespace(registry=_Registry())
                        ),
                        headers=_Headers({"Content-Type": "application/json"}),
                        _read_oauth_body=lambda body=body: body,
                        send_json=lambda payload, status=200, **_: dcr_responses.append(
                            (payload, status)
                        ),
                    ),
                )
                MCPHandler.handle_oauth_register(dcr)
                self.assertEqual(dcr_responses[0][1], 400)
                self.assertEqual(
                    dcr_responses[0][0]["error"], "invalid_client_metadata"
                )


if __name__ == "__main__":
    unittest.main()
