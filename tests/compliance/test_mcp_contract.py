from __future__ import annotations

import base64
import hashlib
import json
import http.client
import os
import select
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from coding_tools_mcp.server import MAX_HTTP_REQUEST_BYTES, MAX_JSON_RPC_BATCH_ITEMS
from tests.compliance.mcp_client import (
    FORBIDDEN_TOOL_NAMES,
    FORBIDDEN_TOOL_TERMS,
    MCPClient,
    MCPError,
    REQUIRED_TOOLS,
    ROOT,
    default_server_command,
    free_port,
)
from tests.compliance.test_support import ComplianceTestCase


class MCPContractTests(ComplianceTestCase):
    def test_initialize_succeeds_and_tools_list_is_available(self) -> None:
        tools = self.client.list_tools()
        self.assertIsInstance(tools, list)
        self.assertGreater(len(tools), 0)

    def test_advertised_logging_capability_accepts_set_level(self) -> None:
        result = self.client.rpc("logging/setLevel", {"level": "debug"})
        self.assertEqual(result, {})

        with self.assertRaises(MCPError) as cm:
            self.client.rpc("logging/setLevel", {"level": "verbose"})
        self.assertEqual(cm.exception.error.get("code"), -32602)
        self.assertIn("logging level", cm.exception.error.get("message", ""))

    def test_tools_list_contains_all_required_p0_tools(self) -> None:
        names = {tool.get("name") for tool in self.client.list_tools()}
        missing = sorted(set(REQUIRED_TOOLS) - names)
        self.assertEqual(missing, [], f"missing required P0 tools: {missing}")

    def test_fresh_http_clients_can_retrieve_stable_tool_catalog(self) -> None:
        first = canonical_tool_catalog(self.client.list_tools())
        second = canonical_tool_catalog(self.client.list_tools())
        self.assertEqual(second, first, "tools/list must be stable across repeated calls")

        with MCPClient(self.workspace.root, url=self.client.url) as sibling:
            sibling_catalog = canonical_tool_catalog(sibling.list_tools())

        self.assertEqual(sibling_catalog, first, "fresh MCP clients must be able to retrieve tools")
        self.assertEqual(len(first), len({tool["name"] for tool in first}), "tool names must be unique")
        self.assertTrue({tool["name"] for tool in first} >= set(REQUIRED_TOOLS))

    def test_tools_list_excludes_forbidden_product_layer_tools(self) -> None:
        names = {str(tool.get("name", "")) for tool in self.client.list_tools()}
        exact_forbidden = sorted(names & FORBIDDEN_TOOL_NAMES)
        self.assertEqual(exact_forbidden, [], f"forbidden tools exposed: {exact_forbidden}")
        term_hits = [
            name
            for name in names
            for term in FORBIDDEN_TOOL_TERMS
            if term in name.lower()
        ]
        self.assertEqual(term_hits, [], f"product-layer tool terms exposed: {sorted(term_hits)}")

    def test_each_tool_has_valid_basic_json_schema(self) -> None:
        for tool in self.client.list_tools():
            with self.subTest(tool=tool.get("name")):
                self.assertIsInstance(tool.get("name"), str)
                self.assertIsInstance(tool.get("title"), str)
                self.assertIsInstance(tool.get("description"), str)
                schema = tool.get("inputSchema")
                self.assert_schema_object(schema)
                output_schema = tool.get("outputSchema")
                self.assert_schema_object(output_schema)
                self.assertIn("ok", output_schema.get("required", []))

    def test_tool_annotations_match_mcp_sdk_hint_shape(self) -> None:
        expected = {
            "server_info": (True, False, True, False),
            "get_default_cwd": (True, False, True, False),
            "set_default_cwd": (True, False, True, False),
            "read_file": (True, False, True, False),
            "list_dir": (True, False, True, False),
            "list_files": (True, False, True, False),
            "search_text": (True, False, True, False),
            "apply_patch": (False, True, False, False),
            "exec_command": (False, True, False, True),
            "write_stdin": (False, False, False, False),
            "kill_session": (False, True, False, False),
            "git_status": (True, False, True, False),
            "git_diff": (True, False, True, False),
            "git_log": (True, False, True, False),
            "git_show": (True, False, True, False),
            "git_blame": (True, False, True, False),
            "request_permissions": (True, False, False, False),
            "view_image": (True, False, True, False),
        }
        for tool in self.client.list_tools():
            name = str(tool.get("name"))
            annotations = tool.get("annotations")
            with self.subTest(tool=name):
                self.assertIsInstance(annotations, dict)
                self.assertIsInstance(annotations.get("title"), str)
                read_only, destructive, idempotent, open_world = expected[name]
                self.assertEqual(annotations.get("readOnlyHint"), read_only)
                self.assertEqual(annotations.get("destructiveHint"), destructive)
                self.assertEqual(annotations.get("idempotentHint"), idempotent)
                self.assertEqual(annotations.get("openWorldHint"), open_world)

    def test_success_and_failure_paths_return_structured_tool_results(self) -> None:
        success = self.client.call_tool("read_file", {"path": "src/math.js"})
        payload = self.assert_tool_success(success)
        self.assertTrue(payload or self.tool_text(success))
        self.assert_content_text_mirrors_structured_content(success)

        failure = self.assert_denied_or_permission_required("read_file", {"path": "../outside-secret.txt"})
        self.assertTrue(failure)

    def test_tool_error_result_has_mcp_error_shape_and_mirrored_text(self) -> None:
        result = self.client.call_tool("read_file", {"path": "../outside-secret.txt"})
        self.assertTrue(result.get("isError"), f"expected tool error, got {result!r}")
        payload = self.assert_content_text_mirrors_structured_content(result)
        self.assertIs(payload.get("ok"), False)
        error = payload.get("error")
        self.assertIsInstance(error, dict)
        self.assertIsInstance(error.get("code"), str)
        self.assertIsInstance(error.get("message"), str)
        self.assertIn(error.get("category"), {"validation", "security", "permission", "runtime", "not_found", "internal"})
        self.assertIsInstance(error.get("retryable"), bool)
        self.assertIsInstance(error.get("details"), dict)

    def test_unknown_tool_returns_standard_json_rpc_error_or_tool_error(self) -> None:
        try:
            result = self.client.call_tool("definitely_not_a_tool", {})
        except MCPError as exc:
            self.assertIn(exc.error.get("code"), {-32601, -32602, -32000})
            self.assertIsInstance(exc.error.get("message"), str)
            return
        self.assertTrue(result.get("isError"), f"unknown tool must not succeed: {result!r}")

    def test_server_does_not_write_debug_logs_to_stdout(self) -> None:
        stdout = self.client.stdout_snapshot()
        self.assertEqual(stdout, "", f"server must log to stderr, not stdout: {stdout!r}")

    def test_trace_logs_are_structured_redacted_and_stderr_only(self) -> None:
        old_trace = os.environ.get("CODING_TOOLS_MCP_TRACE")
        os.environ["CODING_TOOLS_MCP_TRACE"] = "1"
        try:
            with MCPClient(self.workspace.root) as traced:
                traced.call_tool(
                    "request_permissions",
                    {
                        "tool_name": "exec_command",
                        "permission": "sensitive_env",
                        "reason": "trace redaction check",
                        "arguments": {"token": "COMPLIANCE_SHOULD_NOT_LEAK"},
                    },
                )
                stderr = traced.stderr_snapshot()
                stdout = traced.stdout_snapshot()
        finally:
            if old_trace is None:
                os.environ.pop("CODING_TOOLS_MCP_TRACE", None)
            else:
                os.environ["CODING_TOOLS_MCP_TRACE"] = old_trace

        self.assertEqual(stdout, "", f"trace logs must not pollute stdout: {stdout!r}")
        events = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
        trace_events = [event for event in events if event.get("event") == "tool_call"]
        self.assertTrue(trace_events, f"expected structured tool_call trace in stderr: {stderr!r}")
        event = trace_events[-1]
        self.assertEqual(event.get("tool"), "request_permissions")
        self.assertFalse(event.get("ok"))
        self.assertEqual(event.get("error_code"), "ELICITATION_UNSUPPORTED")
        serialized = json.dumps(event, sort_keys=True)
        self.assertNotIn("COMPLIANCE_SHOULD_NOT_LEAK", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_http_rejects_unsupported_protocol_version_header(self) -> None:
        request = urllib.request.Request(
            str(self.client.url),
            data=b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}',
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "1900-01-01",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(cm.exception.code, 400)
        body = json.loads(cm.exception.read().decode("utf-8"))
        self.assertIsNone(body.get("id"))
        self.assertEqual(body.get("error", {}).get("code"), -32600)
        self.assertIn("Unsupported MCP protocol version", body.get("error", {}).get("message", ""))

    def test_http_rejects_non_json_content_type(self) -> None:
        status, body = self.raw_http_post(
            b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}',
            content_type="text/plain; charset=utf-8",
        )
        self.assertEqual(status, 415)
        self.assertEqual(body.get("error", {}).get("code"), -32600)
        self.assertIn("Content-Type", body.get("error", {}).get("message", ""))

    def test_http_rejects_invalid_and_oversized_content_length(self) -> None:
        invalid_status, invalid_body = self.raw_http_post(
            b"",
            content_length="invalid",
        )
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_body.get("error", {}).get("code"), -32600)
        self.assertIn("Content-Length", invalid_body.get("error", {}).get("message", ""))

        oversized_status, oversized_body = self.raw_http_post(
            b"",
            content_length=MAX_HTTP_REQUEST_BYTES + 1,
        )
        self.assertEqual(oversized_status, 413)
        self.assertEqual(oversized_body.get("error", {}).get("code"), -32600)
        self.assertEqual(oversized_body.get("error", {}).get("data", {}).get("max_bytes"), MAX_HTTP_REQUEST_BYTES)

    def test_http_rejects_oversized_json_rpc_batches(self) -> None:
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": "ping", "params": {}}
            for i in range(MAX_JSON_RPC_BATCH_ITEMS + 1)
        ]
        status, body = self.raw_http_post(json.dumps(payload).encode("utf-8"))
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error", {}).get("code"), -32600)
        self.assertEqual(body.get("error", {}).get("data", {}).get("max_items"), MAX_JSON_RPC_BATCH_ITEMS)

    def test_http_origin_policy_requires_exact_loopback_host(self) -> None:
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
        for origin in ("http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"):
            with self.subTest(origin=origin):
                status, response = self.raw_http_post(body, headers={"Origin": origin})
                self.assertEqual(status, 200)
                self.assertEqual(response.get("result"), {})

        denied_origins = (
            "http://localhost.evil.example",
            "http://127.0.0.1.evil.example",
            "https://example.com",
            "null",
        )
        for origin in denied_origins:
            with self.subTest(origin=origin):
                status, response = self.raw_http_post(body, headers={"Origin": origin})
                self.assertEqual(status, 403)
                self.assertIsNone(response.get("id"))
                self.assertEqual(response.get("error", {}).get("code"), -32600)
                self.assertIn("Origin denied", response.get("error", {}).get("message", ""))

    def test_http_rejects_unknown_session_id_header(self) -> None:
        self.assertIsNotNone(self.client.session_id)
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
        accepted_status, accepted = self.raw_http_post(body, headers={"Mcp-Session-Id": str(self.client.session_id)})
        self.assertEqual(accepted_status, 200)
        self.assertEqual(accepted.get("result"), {})

        rejected_status, rejected = self.raw_http_post(body, headers={"Mcp-Session-Id": "not-the-current-session"})
        self.assertEqual(rejected_status, 404)
        self.assertEqual(rejected.get("error", {}).get("code"), -32001)
        self.assertIn("Unknown MCP session", rejected.get("error", {}).get("message", ""))

    def test_http_discovery_endpoints_return_server_card_metadata(self) -> None:
        self.assertIsNotNone(self.client.url)
        parsed = urllib.parse.urlparse(str(self.client.url))
        base = f"{parsed.scheme}://{parsed.netloc}"
        for path in ("/mcp", "/.well-known/mcp.json", "/.well-known/mcp/server-card.json"):
            with self.subTest(path=path):
                request = urllib.request.Request(base + path, method="GET")
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.assertEqual(body.get("protocolVersion"), "2025-06-18")
                self.assertEqual(body.get("server", {}).get("name"), "coding-tools-mcp")
                self.assertEqual(body.get("transport", {}).get("endpoint"), "/mcp")
                self.assertEqual(body.get("auth", {}).get("type"), "none")
                self.assertIn("toolProfile", body)
                self.assertIn("tools", body)

        head = urllib.request.Request(base + "/mcp", method="HEAD")
        with urllib.request.urlopen(head, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")

    def test_bearer_auth_rejects_missing_or_wrong_token_and_accepts_valid_token(self) -> None:
        port = free_port()
        token = "test-token-remote-mcp"
        cmd = default_server_command(self.workspace.root, port) + ["--auth-token", token]
        process = subprocess.Popen(
            cmd,
            cwd=str(self.workspace.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.server_process_env(),
            text=True,
            start_new_session=True,
        )
        url = f"http://127.0.0.1:{port}/mcp"
        well_known = f"http://127.0.0.1:{port}/.well-known/mcp.json"
        try:
            deadline = time.time() + 10
            while True:
                try:
                    with urllib.request.urlopen(well_known, timeout=1) as response:
                        metadata = json.loads(response.read().decode("utf-8"))
                    break
                except Exception:
                    if time.time() >= deadline:
                        raise
                    time.sleep(0.1)
            self.assertEqual(metadata.get("auth", {}).get("type"), "bearer")

            missing_status, missing = self.raw_post_to_auth_server(url, token=None)
            self.assertEqual(missing_status, 401)
            self.assertEqual(missing.get("error", {}).get("message"), "Unauthorized")

            wrong_status, wrong = self.raw_post_to_auth_server(url, token="wrong")
            self.assertEqual(wrong_status, 401)
            self.assertEqual(wrong.get("error", {}).get("message"), "Unauthorized")

            ok_status, ok = self.raw_post_to_auth_server(url, token=token)
            self.assertEqual(ok_status, 200)
            self.assertEqual(ok.get("result"), {})

            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                card = json.loads(response.read().decode("utf-8"))
            self.assertEqual(card.get("auth", {}).get("type"), "bearer")
        finally:
            self.stop_process(process)

    def test_oauth_public_client_pkce_flow_succeeds(self) -> None:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = self.oauth_server_env(
            CODING_TOOLS_MCP_OAUTH_PASSWORD="test-password",
            CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET=bytes(range(32)).hex(),
        )
        process = self.start_oauth_server(port, env)
        try:
            metadata = self.wait_for_json(f"{base_url}/.well-known/oauth-authorization-server")
            self.assertEqual(metadata.get("issuer"), base_url)
            self.assertEqual(metadata.get("token_endpoint_auth_methods_supported"), ["none"])

            forwarded_headers = {"X-Forwarded-Host": "example.trycloudflare.com", "X-Forwarded-Proto": "https"}
            forwarded_status, _, forwarded_body = self.raw_base_http_request(
                base_url,
                "GET",
                "/.well-known/oauth-authorization-server",
                headers=forwarded_headers,
            )
            self.assertEqual(forwarded_status, 200)
            forwarded_metadata = json.loads(forwarded_body)
            self.assertEqual(forwarded_metadata.get("issuer"), "https://example.trycloudflare.com")

            forwarded_verifier = "e" * 43
            forwarded_code = self.oauth_authorization_code(
                base_url,
                "mcp-cli",
                "test-password",
                forwarded_verifier,
                headers=forwarded_headers,
            )
            forwarded_token_status, forwarded_token = self.oauth_token_request(
                base_url,
                "mcp-cli",
                forwarded_code,
                forwarded_verifier,
                headers=forwarded_headers,
            )
            self.assertEqual(forwarded_token_status, 200)
            forwarded_ok_status, forwarded_ok = self.raw_post_to_auth_server(
                f"{base_url}/mcp",
                token=forwarded_token.get("access_token"),
                headers=forwarded_headers,
            )
            self.assertEqual(forwarded_ok_status, 200)
            self.assertEqual(forwarded_ok.get("result"), {})

            verifier = "a" * 43
            code = self.oauth_authorization_code(base_url, "mcp-cli", "test-password", verifier)
            token_status, token_response = self.oauth_token_request(base_url, "mcp-cli", code, verifier)
            self.assertEqual(token_status, 200)
            access_token = token_response.get("access_token")
            self.assertIsInstance(access_token, str)

            ok_status, ok = self.raw_post_to_auth_server(f"{base_url}/mcp", token=access_token)
            self.assertEqual(ok_status, 200)
            self.assertEqual(ok.get("result"), {})

            bad_code = self.oauth_authorization_code(base_url, "mcp-cli", "test-password", verifier)
            bad_status, bad = self.oauth_token_request(base_url, "mcp-cli", bad_code, "b" * 43)
            self.assertEqual(bad_status, 400)
            self.assertEqual(bad.get("error"), "invalid_grant")
        finally:
            self.stop_process(process)

    def test_oauth_and_static_bearer_dual_credentials_both_accepted(self) -> None:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        static_token = "test-token-dual-auth"
        env = self.oauth_server_env(
            CODING_TOOLS_MCP_OAUTH_PASSWORD="test-password",
            CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET=bytes(range(32)).hex(),
        )
        process = self.start_oauth_server(port, env, extra_args=["--auth-token", static_token])
        try:
            metadata = self.wait_for_json(f"{base_url}/.well-known/mcp.json")
            self.assertEqual(metadata.get("auth", {}).get("type"), "oauth2")

            stderr = self.process_stderr_snapshot(process)
            self.assertIn(
                "Auth: dual credentials enabled — both static bearer token and OAuth 2.1 access tokens will be accepted.",
                stderr,
            )
            self.assertIn("oauth2 + bearer enabled (server_url=dynamic request URL)", stderr)
            self.assertNotIn("--auth-token is ignored", stderr)

            static_status, static_response = self.raw_post_to_auth_server(f"{base_url}/mcp", token=static_token)
            self.assertEqual(static_status, 200)
            self.assertEqual(static_response.get("result"), {})

            verifier = "c" * 43
            code = self.oauth_authorization_code(base_url, "claude-desktop", "test-password", verifier)
            token_status, token_response = self.oauth_token_request(base_url, "claude-desktop", code, verifier)
            self.assertEqual(token_status, 200)
            oauth_status, oauth_response = self.raw_post_to_auth_server(
                f"{base_url}/mcp",
                token=token_response.get("access_token"),
            )
            self.assertEqual(oauth_status, 200)
            self.assertEqual(oauth_response.get("result"), {})

            wrong_status, wrong = self.raw_post_to_auth_server(f"{base_url}/mcp", token="wrong")
            self.assertEqual(wrong_status, 401)
            self.assertEqual(wrong.get("error", {}).get("message"), "Unauthorized")
        finally:
            self.stop_process(process)

    def test_oauth_token_endpoint_rejects_mismatched_client_id_when_restricted(self) -> None:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = self.oauth_server_env(
            CODING_TOOLS_MCP_OAUTH_CLIENT_ID="trusted-client",
            CODING_TOOLS_MCP_OAUTH_PASSWORD="test-password",
            CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET=bytes(range(32)).hex(),
        )
        process = self.start_oauth_server(port, env)
        try:
            self.wait_for_json(f"{base_url}/.well-known/oauth-authorization-server")

            verifier = "d" * 43
            challenge = self.pkce_challenge(verifier)
            query = urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": "attacker-client",
                    "redirect_uri": "http://127.0.0.1/callback",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
            denied_status, _, _ = self.raw_base_http_request(base_url, "GET", f"/oauth/authorize?{query}")
            self.assertGreaterEqual(denied_status, 400)
            self.assertLess(denied_status, 500)

            code = self.oauth_authorization_code(base_url, "trusted-client", "test-password", verifier)
            token_status, token_response = self.oauth_token_request(base_url, "attacker-client", code, verifier)
            self.assertEqual(token_status, 400)
            self.assertIn(token_response.get("error"), {"invalid_client", "invalid_grant"})
        finally:
            self.stop_process(process)

    def test_http_pre_dispatch_errors_include_null_json_rpc_id(self) -> None:
        cases = [
            (
                "unknown endpoint",
                b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}',
                {"path": "/not-mcp"},
                404,
                -32601,
            ),
            ("non_object_request", b"1", {}, 400, -32600),
        ]
        for name, body, kwargs, status_code, error_code in cases:
            with self.subTest(name=name):
                status, response = self.raw_http_post(body, **kwargs)
                self.assertEqual(status, status_code)
                self.assertIsNone(response.get("id"))
                self.assertEqual(response.get("error", {}).get("code"), error_code)

    def test_http_rejects_malformed_json_rpc_envelopes_and_params(self) -> None:
        cases = [
            ({"id": 1, "method": "ping", "params": {}}, -32600),
            ({"jsonrpc": "2.0", "id": True, "method": "ping", "params": {}}, -32600),
            ({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": []}, -32602),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "initialize",
                    "params": {"protocolVersion": "1900-01-01"},
                },
                -32602,
            ),
        ]
        for payload, code in cases:
            with self.subTest(payload=payload):
                response = self.raw_post(payload)
                self.assertEqual(response.get("jsonrpc"), "2.0")
                self.assertEqual(response.get("error", {}).get("code"), code)

    def test_http_rejects_tools_before_initialize(self) -> None:
        process, url = self.start_raw_http_server()
        try:
            self.wait_for_ping(url)
            response = self.raw_post_to(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            self.assertEqual(response.get("error", {}).get("code"), -32002)
            self.assertIn("not initialized", response.get("error", {}).get("message", "").lower())
        finally:
            self.stop_process(process)

    def test_initialize_with_newer_client_protocol_negotiates_server_version(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "newer-sdk", "version": "1.0"},
            },
        }
        response = self.raw_post(
            payload
        )
        self.assertEqual(response.get("result", {}).get("protocolVersion"), "2025-06-18")

        header_response = self.raw_post_to(
            str(self.client.url),
            payload,
            protocol_version="2025-11-25",
        )
        self.assertEqual(header_response.get("result", {}).get("protocolVersion"), "2025-06-18")

    def test_http_rejects_older_protocol_version_header(self) -> None:
        status, response = self.raw_http_post(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "older-header", "version": "1.0"},
                    },
                }
            ).encode("utf-8"),
            headers={"MCP-Protocol-Version": "2024-01-01"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(response.get("error", {}).get("code"), -32600)

    def test_initialize_rejects_older_client_protocol(self) -> None:
        response = self.raw_post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-01-01",
                    "capabilities": {},
                    "clientInfo": {"name": "older-sdk", "version": "1.0"},
                },
            }
        )
        self.assertEqual(response.get("error", {}).get("code"), -32602)

    def test_stdio_transport_uses_newline_delimited_json_rpc_only(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "coding_tools_mcp",
                "--workspace",
                str(self.workspace.root),
                "--stdio",
            ],
            cwd=str(self.workspace.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.server_process_env(),
            text=True,
            start_new_session=True,
        )
        try:
            initialize = self.stdio_rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "contract-stdio-test", "version": "0.1"},
                    },
                },
            )
            result = initialize.get("result")
            self.assertIsInstance(result, dict)
            self.assertEqual(result.get("protocolVersion"), "2025-06-18")
            self.assertIn("tools", result.get("capabilities", {}))
            self.assertIn("logging", result.get("capabilities", {}))

            logging_level = self.stdio_rpc(
                process,
                {"jsonrpc": "2.0", "id": 2, "method": "logging/setLevel", "params": {"level": "debug"}},
            )
            self.assertEqual(logging_level.get("result"), {})

            self.stdio_send(process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            self.assert_no_stdio_response(process)

            listed = self.stdio_rpc(process, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
            tools = listed.get("result", {}).get("tools")
            self.assertIsInstance(tools, list)
            self.assertTrue({tool.get("name") for tool in tools} >= set(REQUIRED_TOOLS))
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def test_stdio_rejects_preinitialize_calls_and_accepts_cancel_notification(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "coding_tools_mcp",
                "--workspace",
                str(self.workspace.root),
                "--stdio",
            ],
            cwd=str(self.workspace.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.server_process_env(),
            text=True,
            start_new_session=True,
        )
        try:
            rejected = self.stdio_rpc_allow_error(
                process,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            self.assertEqual(rejected.get("error", {}).get("code"), -32002)

            initialize = self.stdio_rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x"}},
                },
            )
            self.assertEqual(initialize.get("result", {}).get("protocolVersion"), "2025-06-18")

            self.stdio_send(
                process,
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"session_id": "missing"}},
            )
            self.assert_no_stdio_response(process)
        finally:
            self.stop_process(process)

    def assert_content_text_mirrors_structured_content(self, result: dict[str, Any]) -> dict[str, Any]:
        structured = result.get("structuredContent")
        self.assertIsInstance(structured, dict, f"structuredContent must be an object: {result!r}")
        content = result.get("content")
        self.assertIsInstance(content, list, f"content must be a list: {result!r}")
        text_items = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        self.assertTrue(text_items, f"content must include a text mirror: {result!r}")
        mirrored = json.loads(str(text_items[0]))
        self.assertEqual(mirrored, structured)
        return structured

    def stdio_send(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        self.assertIsNotNone(process.stdin)
        line = json.dumps(payload, separators=(",", ":"))
        self.assertNotIn("\n", line)
        process.stdin.write(line + "\n")
        process.stdin.flush()

    def stdio_rpc(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> dict[str, Any]:
        self.stdio_send(process, payload)
        response = self.stdio_read_response(process, payload)
        self.assertNotIn("error", response, f"unexpected stdio JSON-RPC error: {response!r}")
        return response

    def stdio_rpc_allow_error(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> dict[str, Any]:
        self.stdio_send(process, payload)
        return self.stdio_read_response(process, payload)

    def stdio_read_response(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> dict[str, Any]:
        self.assertIsNotNone(process.stdout)
        readable, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(readable, "stdio server did not produce a JSON-RPC response")
        line = process.stdout.readline()
        self.assertTrue(line.endswith("\n"), f"stdio response must be newline-delimited: {line!r}")
        self.assertEqual(line.count("\n"), 1, f"stdio response must be one JSON-RPC message per line: {line!r}")
        response = json.loads(line)
        self.assertEqual(response.get("jsonrpc"), "2.0")
        self.assertEqual(response.get("id"), payload.get("id"))
        return response

    def assert_no_stdio_response(self, process: subprocess.Popen[str]) -> None:
        self.assertIsNotNone(process.stdout)
        readable, _, _ = select.select([process.stdout], [], [], 0.2)
        self.assertFalse(readable, "stdio notification must not produce a JSON-RPC response")

    def assert_schema_object(self, schema: Any) -> None:
        self.assertIsInstance(schema, dict, f"inputSchema must be an object, got {schema!r}")
        self.assertEqual(schema.get("type"), "object", f"inputSchema.type must be object: {schema!r}")
        self.assertIsInstance(schema.get("properties", {}), dict)
        self.assert_schema_node(schema)

    def assert_schema_node(self, node: Any) -> None:
        if isinstance(node, dict):
            if "type" in node:
                allowed = {"array", "boolean", "integer", "null", "number", "object", "string"}
                value = node["type"]
                if isinstance(value, list):
                    self.assertTrue(set(value) <= allowed, f"invalid schema type list: {value!r}")
                else:
                    self.assertIn(value, allowed, f"invalid schema type: {value!r}")
            for key in ("properties", "$defs", "definitions"):
                for child in node.get(key, {}).values():
                    self.assert_schema_node(child)
            if "items" in node:
                self.assert_schema_node(node["items"])
            for key in ("anyOf", "oneOf", "allOf"):
                for child in node.get(key, []):
                    self.assert_schema_node(child)

    def raw_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.assertIsNotNone(self.client.url)
        return self.raw_post_to(str(self.client.url), payload)

    def raw_post_to(self, url: str, payload: Any, *, protocol_version: str = "2025-06-18") -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": protocol_version,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def raw_http_post(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        content_length: int | str | None = None,
        headers: dict[str, str] | None = None,
        path: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self.assertIsNotNone(self.client.url)
        parsed = urllib.parse.urlparse(str(self.client.url))
        self.assertEqual(parsed.scheme, "http")
        self.assertIsNotNone(parsed.hostname)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            connection.putrequest("POST", path or parsed.path or "/mcp")
            connection.putheader("Accept", "application/json, text/event-stream")
            connection.putheader("Content-Type", content_type)
            if not headers or "MCP-Protocol-Version" not in headers:
                connection.putheader("MCP-Protocol-Version", "2025-06-18")
            connection.putheader("Content-Length", str(len(body) if content_length is None else content_length))
            for name, value in (headers or {}).items():
                connection.putheader(name, value)
            connection.endheaders()
            if body:
                connection.send(body)
            response = connection.getresponse()
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body)
        finally:
            connection.close()

    def raw_post_to_auth_server(
        self,
        url: str,
        *,
        token: str | None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        parsed = urllib.parse.urlparse(url)
        self.assertIsNotNone(parsed.hostname)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
        try:
            connection.putrequest("POST", parsed.path or "/mcp")
            connection.putheader("Accept", "application/json, text/event-stream")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("MCP-Protocol-Version", "2025-06-18")
            connection.putheader("Content-Length", str(len(body)))
            if token is not None:
                connection.putheader("Authorization", f"Bearer {token}")
            for name, value in (headers or {}).items():
                connection.putheader(name, value)
            connection.endheaders()
            connection.send(body)
            response = connection.getresponse()
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body)
        finally:
            connection.close()

    def oauth_server_env(self, **overrides: str) -> dict[str, str]:
        env = self.server_process_env()
        for name in (
            "CODING_TOOLS_MCP_OAUTH_CLIENT_ID",
            "CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET",
            "CODING_TOOLS_MCP_OAUTH_PASSWORD",
            "CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET",
            "CODING_TOOLS_MCP_OAUTH_TOKEN_TTL",
            "CODING_TOOLS_MCP_SERVER_URL",
            "CODING_TOOLS_MCP_AUTH_TOKEN",
            "CODING_TOOLS_MCP_OAUTH_MODE",
        ):
            env.pop(name, None)
        env.update(overrides)
        return env

    def start_oauth_server(
        self,
        port: int,
        env: dict[str, str],
        *,
        extra_args: list[str] | None = None,
    ) -> subprocess.Popen[str]:
        cmd = default_server_command(self.workspace.root, port) + ["--oauth-mode"] + (extra_args or [])
        return subprocess.Popen(
            cmd,
            cwd=str(self.workspace.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            start_new_session=True,
        )

    def wait_for_json(self, url: str) -> dict[str, Any]:
        deadline = time.time() + 10
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - startup retry records last failure
                last_error = exc
                time.sleep(0.1)
        raise AssertionError(f"server did not return JSON from {url}: {last_error!r}")

    def pkce_challenge(self, verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def oauth_authorization_code(
        self,
        base_url: str,
        client_id: str,
        password: str,
        verifier: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> str:
        redirect_uri = "http://127.0.0.1/callback"
        challenge = self.pkce_challenge(verifier)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "test-state",
            }
        )
        get_status, _, get_body = self.raw_base_http_request(
            base_url,
            "GET",
            f"/oauth/authorize?{query}",
            headers=headers,
        )
        self.assertEqual(get_status, 200, get_body)
        self.assertIn("Redirect URI", get_body)

        form = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "test-state",
                "password": password,
            }
        ).encode("utf-8")
        request_headers = {**(headers or {}), "Content-Type": "application/x-www-form-urlencoded"}
        status, response_headers, _ = self.raw_base_http_request(
            base_url,
            "POST",
            "/oauth/authorize",
            body=form,
            headers=request_headers,
        )
        self.assertEqual(status, 302)
        location = response_headers.get("location", "")
        code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [""])[0]
        self.assertTrue(code, f"authorization redirect did not contain a code: {location!r}")
        return code

    def oauth_token_request(
        self,
        base_url: str,
        client_id: str,
        code: str,
        verifier: str,
        *,
        client_secret: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1/callback",
            "code_verifier": verifier,
            "client_id": client_id,
        }
        if client_secret is not None:
            params["client_secret"] = client_secret
        body = urllib.parse.urlencode(params).encode("utf-8")
        request_headers = {**(headers or {}), "Content-Type": "application/x-www-form-urlencoded"}
        status, _, response_body = self.raw_base_http_request(
            base_url,
            "POST",
            "/oauth/token",
            body=body,
            headers=request_headers,
        )
        return status, json.loads(response_body)

    def raw_base_http_request(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], str]:
        parsed = urllib.parse.urlparse(base_url)
        self.assertIsNotNone(parsed.hostname)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            return response.status, response_headers, response_body
        finally:
            connection.close()

    def start_raw_http_server(self) -> tuple[subprocess.Popen[str], str]:
        port = free_port()
        cmd = default_server_command(self.workspace.root, port)
        process = subprocess.Popen(
            cmd,
            cwd=str(self.workspace.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.server_process_env(),
            text=True,
            start_new_session=True,
        )
        return process, f"http://127.0.0.1:{port}/mcp"

    def server_process_env(self) -> dict[str, str]:
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(ROOT) if not existing_pythonpath else str(ROOT) + os.pathsep + existing_pythonpath
        return env

    def process_stderr_snapshot(self, process: subprocess.Popen[str]) -> str:
        if process.stderr is None:
            return ""
        chunks: list[str] = []
        while True:
            readable, _, _ = select.select([process.stderr], [], [], 0)
            if not readable:
                break
            chunk = os.read(process.stderr.fileno(), 4096).decode("utf-8", errors="replace")
            if not chunk:
                break
            chunks.append(chunk)
        return "".join(chunks)

    def wait_for_ping(self, url: str) -> None:
        deadline = time.time() + 10
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                self.raw_post_to(url, {"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}})
                return
            except Exception as exc:  # noqa: BLE001 - startup retry records last failure
                last_error = exc
                time.sleep(0.1)
        raise AssertionError(f"server did not accept ping: {last_error!r}")

    def stop_process(self, process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def canonical_tool_catalog(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for tool in tools:
        catalog.append(
            {
                "name": tool.get("name"),
                "inputSchema": tool.get("inputSchema"),
                "annotations": tool.get("annotations"),
            }
        )
    return sorted(catalog, key=lambda item: str(item["name"]))
