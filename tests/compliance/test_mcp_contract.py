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
from collections.abc import Callable
from typing import Any

from coding_tools_mcp.server import MAX_HTTP_REQUEST_BYTES
from tests.compliance.mcp_client import (
    FORBIDDEN_TOOL_NAMES,
    FORBIDDEN_TOOL_TERMS,
    MCPClient,
    MCPError,
    REQUIRED_TOOLS,
    default_server_command,
    free_port,
    safe_server_env,
    stream_snapshot,
)
from tests.compliance.test_support import ComplianceTestCase


class MCPContractTests(ComplianceTestCase):
    def test_initialize_succeeds_and_tools_list_is_available(self) -> None:
        tools = self.client.list_tools()
        self.assertIsInstance(tools, list)
        self.assertGreater(len(tools), 0)

    def test_unimplemented_logging_capability_is_not_advertised(self) -> None:
        with self.assertRaises(MCPError) as cm:
            self.client.rpc("logging/setLevel", {"level": "debug"})
        self.assertEqual(cm.exception.error.get("code"), -32601)

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

    def test_command_handles_have_no_legacy_session_aliases(self) -> None:
        tools = {str(tool.get("name")): tool for tool in self.client.list_tools()}
        self.assertIn("kill_command", tools)
        self.assertNotIn("kill_session", tools)
        for name in ("write_stdin", "kill_command"):
            schema = tools[name]["inputSchema"]
            properties = schema.get("properties", {})
            self.assertIn("command_id", properties)
            self.assertNotIn("session_id", properties)
            self.assertIn("command_id", schema.get("required", []))

        try:
            legacy = self.client.call_tool("write_stdin", {"session_id": "legacy", "chars": ""})
        except MCPError:
            pass
        else:
            self.assertTrue(legacy.get("isError"), legacy)

    def test_high_confusion_tools_include_model_ready_examples(self) -> None:
        tools = {str(tool.get("name")): tool for tool in self.client.list_tools()}
        expected_fragments = {
            "set_default_cwd": ("session", "workdir", '"path":"src"'),
            "apply_patch": ("*** Begin Patch", "*** Update File"),
            "exec_command": ("workdir", "command_id", '"yield_time_ms":30000'),
            "write_stdin": ("command_id", '"chars":""'),
            "kill_command": ("command_id", '"signal":"KILL"'),
            "read_output": ("command:abc:stdout", '"offset":0'),
        }
        for name, fragments in expected_fragments.items():
            description = str(tools[name].get("description", ""))
            for fragment in fragments:
                with self.subTest(tool=name, fragment=fragment):
                    self.assertIn(fragment, description)

    def test_http_sessions_isolate_cwd_but_share_workspace_commands(self) -> None:
        self.client.call_tool("set_default_cwd", {"path": "src"})
        with MCPClient(self.workspace.root, url=self.client.url) as sibling:
            sibling_cwd = self.assert_tool_success(sibling.call_tool("get_default_cwd", {}))
            self.assertEqual(sibling_cwd.get("default_cwd"), ".")
            sibling.call_tool("set_default_cwd", {"path": "test"})

            started = self.client.call_tool(
                "exec_command",
                {"cmd": "sleep 1", "timeout_ms": 5000, "yield_time_ms": 0},
            )
            payload = self.assert_tool_success(started)
            command_id = payload.get("command_id")
            self.assertIsInstance(command_id, str)
            polled = self.assert_tool_success(
                sibling.call_tool("write_stdin", {"command_id": command_id, "chars": "", "yield_time_ms": 0})
            )
            self.assertEqual(polled.get("command_id"), command_id)
            sibling.call_tool("kill_command", {"command_id": command_id, "signal": "KILL"})

        original_cwd = self.assert_tool_success(self.client.call_tool("get_default_cwd", {}))
        self.assertEqual(original_cwd.get("default_cwd"), "src")

    def test_http_session_delete_does_not_terminate_workspace_command(self) -> None:
        with MCPClient(self.workspace.root, url=self.client.url) as owner:
            started = self.assert_tool_success(
                owner.call_tool(
                    "exec_command",
                    {"cmd": "sleep 5", "timeout_ms": 10000, "yield_time_ms": 0},
                )
            )
            command_id = started.get("command_id")
            self.assertIsInstance(command_id, str)

        polled = self.assert_tool_success(
            self.client.call_tool("write_stdin", {"command_id": command_id, "chars": "", "yield_time_ms": 0})
        )
        self.assertEqual(polled.get("status"), "running")
        killed = self.assert_tool_success(
            self.client.call_tool("kill_command", {"command_id": command_id, "signal": "KILL"})
        )
        self.assertIn(killed.get("status"), {"killed", "exited"})

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
            "check_exec_environment": (True, False, True, False),
            "get_default_cwd": (True, False, True, False),
            "set_default_cwd": (False, False, True, False),
            "read_file": (True, False, True, False),
            "list_dir": (True, False, True, False),
            "list_files": (True, False, True, False),
            "search_text": (True, False, True, False),
            "apply_patch": (False, True, False, False),
            "exec_command": (False, True, False, True),
            "write_stdin": (False, False, False, False),
            "kill_command": (False, True, False, False),
            "read_output": (True, False, True, False),
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

    def test_success_and_failure_paths_return_structured_and_agent_readable_results(self) -> None:
        success = self.client.call_tool("read_file", {"path": "src/math.js"})
        payload = self.assert_tool_success(success)
        self.assertTrue(payload or self.tool_text(success))
        text = self.assert_content_text_is_agent_readable(success)
        self.assertIn("return a - b", text)
        self.assertNotEqual(text, json.dumps(payload, sort_keys=True))

        failure = self.assert_denied_or_permission_required("read_file", {"path": "../outside-secret.txt"})
        self.assertTrue(failure)

    def test_tool_error_result_has_mcp_error_shape_and_readable_text(self) -> None:
        result = self.client.call_tool("read_file", {"path": "../outside-secret.txt"})
        self.assertTrue(result.get("isError"), f"expected tool error, got {result!r}")
        text = self.assert_content_text_is_agent_readable(result)
        payload = result.get("structuredContent")
        self.assertIsInstance(payload, dict)
        self.assertIs(payload.get("ok"), False)
        error = payload.get("error")
        self.assertIsInstance(error, dict)
        self.assertIsInstance(error.get("code"), str)
        self.assertIsInstance(error.get("message"), str)
        self.assertIn(
            error.get("category"),
            {"validation", "security", "permission", "runtime", "not_found", "conflict", "internal"},
        )
        self.assertIsInstance(error.get("retryable"), bool)
        self.assertIsInstance(error.get("details"), dict)
        self.assertIn(str(error.get("code")), text)
        self.assertIn(str(error.get("message")), text)

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

    def test_http_rejects_json_rpc_batches(self) -> None:
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": "ping", "params": {}}
            for i in range(2)
        ]
        status, body = self.raw_http_post(json.dumps(payload).encode("utf-8"))
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error", {}).get("code"), -32600)
        self.assertIn("batch", body.get("error", {}).get("message", "").lower())

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
            "http://localhost:3000/path",
            "http://localhost:3000?query=1",
            "http://localhost@evil.example",
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
        self.assertEqual(rejected.get("id"), 1)
        self.assertEqual(rejected.get("error", {}).get("code"), -32001)
        self.assertIn("Unknown MCP session", rejected.get("error", {}).get("message", ""))

    def test_http_delete_terminates_only_the_selected_session(self) -> None:
        self.assertIsNotNone(self.client.url)
        with MCPClient(self.workspace.root, url=self.client.url) as sibling:
            sibling_session = str(sibling.session_id)
            parsed = urllib.parse.urlparse(str(self.client.url))
            base = f"{parsed.scheme}://{parsed.netloc}"
            status, _, body = self.raw_base_http_request(
                base,
                "DELETE",
                parsed.path or "/mcp",
                headers={
                    "Mcp-Session-Id": sibling_session,
                    "MCP-Protocol-Version": "2025-06-18",
                },
            )
            self.assertEqual(status, 200, body)
            rejected_status, rejected = self.raw_http_post(
                b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}',
                headers={"Mcp-Session-Id": sibling_session},
            )
            self.assertEqual(rejected_status, 404)
            self.assertEqual(rejected.get("error", {}).get("code"), -32001)

        still_alive = self.client.rpc("ping", {})
        self.assertEqual(still_alive, {})

    def test_http_discovery_endpoints_return_server_card_metadata(self) -> None:
        self.assertIsNotNone(self.client.url)
        parsed = urllib.parse.urlparse(str(self.client.url))
        base = f"{parsed.scheme}://{parsed.netloc}"
        for path in ("/.well-known/mcp.json", "/.well-known/mcp/server-card.json"):
            with self.subTest(path=path):
                request = urllib.request.Request(base + path, method="GET")
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.assertEqual(body.get("protocolVersion"), "2025-11-25")
                self.assertEqual(body.get("server", {}).get("name"), "coding-tools-mcp")
                self.assertEqual(body.get("transport", {}).get("endpoint"), "/mcp")
                self.assertEqual(body.get("auth", {}).get("type"), "none")
                self.assertIn("tools", body)

        for method in ("GET", "HEAD"):
            request = urllib.request.Request(base + "/mcp", method=method)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 405)

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

            request = urllib.request.Request(well_known, headers={"Authorization": f"Bearer {token}"}, method="GET")
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
            CODING_TOOLS_MCP_TRUST_PROXY_HEADERS="1",
        )
        process = self.start_oauth_server(port, env)
        try:
            metadata = self.wait_for_json(f"{base_url}/.well-known/oauth-authorization-server")
            self.assertEqual(metadata.get("issuer"), base_url)
            self.assertEqual(metadata.get("grant_types_supported"), ["authorization_code"])
            self.assertEqual(metadata.get("response_types_supported"), ["code"])
            self.assertEqual(
                set(metadata.get("token_endpoint_auth_methods_supported", [])),
                {"none", "client_secret_basic", "client_secret_post"},
            )
            self.assertEqual(metadata.get("registration_endpoint"), f"{base_url}/oauth/register")
            client_id = self.oauth_register_client(base_url, "MCP CLI")

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
                client_id,
                "test-password",
                forwarded_verifier,
                headers=forwarded_headers,
            )
            forwarded_token_status, forwarded_token = self.oauth_token_request(
                base_url,
                client_id,
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
            code = self.oauth_authorization_code(base_url, client_id, "test-password", verifier)
            token_status, token_response = self.oauth_token_request(base_url, client_id, code, verifier)
            self.assertEqual(token_status, 200)
            self.assertEqual(token_response.get("expires_in"), 24 * 60 * 60)
            access_token = token_response.get("access_token")
            self.assertIsInstance(access_token, str)

            ok_status, ok = self.raw_post_to_auth_server(f"{base_url}/mcp", token=access_token)
            self.assertEqual(ok_status, 200)
            self.assertEqual(ok.get("result"), {})

            bad_code = self.oauth_authorization_code(base_url, client_id, "test-password", verifier)
            bad_status, bad = self.oauth_token_request(base_url, client_id, bad_code, "b" * 43)
            self.assertEqual(bad_status, 400)
            self.assertEqual(bad.get("error"), "invalid_grant")
        finally:
            self.stop_process(process)

    def test_oauth_confidential_client_authentication_method_is_bound(self) -> None:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = self.oauth_server_env(
            CODING_TOOLS_MCP_OAUTH_PASSWORD="test-password",
            CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET=bytes(range(32)).hex(),
        )
        process = self.start_oauth_server(port, env)
        try:
            self.wait_for_json(f"{base_url}/.well-known/oauth-authorization-server")
            for index, method in enumerate(("client_secret_post", "client_secret_basic")):
                with self.subTest(method=method):
                    client_id, client_secret = self.oauth_register_confidential_client(
                        base_url,
                        f"Confidential client {index}",
                        method,
                    )
                    verifier = chr(ord("f") + index) * 43
                    code = self.oauth_authorization_code(
                        base_url,
                        client_id,
                        "test-password",
                        verifier,
                    )
                    wrong_method = (
                        "client_secret_basic"
                        if method == "client_secret_post"
                        else "client_secret_post"
                    )
                    wrong_status, wrong = self.oauth_token_request(
                        base_url,
                        client_id,
                        code,
                        verifier,
                        client_secret=client_secret,
                        client_auth_method=wrong_method,
                    )
                    self.assertEqual(wrong_status, 400)
                    self.assertEqual(wrong.get("error"), "invalid_client")

                    token_status, token = self.oauth_token_request(
                        base_url,
                        client_id,
                        code,
                        verifier,
                        client_secret=client_secret,
                        client_auth_method=method,
                    )
                    self.assertEqual(token_status, 200)
                    self.assertIsInstance(token.get("access_token"), str)
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

            client_id = self.oauth_register_client(base_url, "Claude Desktop")
            verifier = "c" * 43
            code = self.oauth_authorization_code(base_url, client_id, "test-password", verifier)
            token_status, token_response = self.oauth_token_request(base_url, client_id, code, verifier)
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

    def test_oauth_dynamic_registration_rejects_unsafe_and_unregistered_redirects(self) -> None:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = self.oauth_server_env(
            CODING_TOOLS_MCP_OAUTH_PASSWORD="test-password",
            CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET=bytes(range(32)).hex(),
        )
        process = self.start_oauth_server(port, env)
        try:
            self.wait_for_json(f"{base_url}/.well-known/oauth-authorization-server")
            unsafe_body = json.dumps(
                {
                    "redirect_uris": ["http://attacker.example/callback"],
                    "token_endpoint_auth_method": "none",
                }
            ).encode("utf-8")
            status, _, response_body = self.raw_base_http_request(
                base_url,
                "POST",
                "/oauth/register",
                body=unsafe_body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(response_body).get("error"), "invalid_client_metadata")

            malformed_body = json.dumps(
                {
                    "redirect_uris": ["http://127.0.0.1/callback"],
                    "grant_types": [["authorization_code"]],
                }
            ).encode("utf-8")
            malformed_status, malformed_headers, malformed_response = self.raw_base_http_request(
                base_url,
                "POST",
                "/oauth/register",
                body=malformed_body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(malformed_status, 400)
            self.assertEqual(json.loads(malformed_response).get("error"), "invalid_client_metadata")
            self.assertEqual(malformed_headers.get("cache-control"), "no-store")

            client_id = self.oauth_register_client(base_url, "Redirect Test")
            query = urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "https://attacker.example/callback",
                    "code_challenge": self.pkce_challenge("z" * 43),
                    "code_challenge_method": "S256",
                }
            )
            denied_status, _, denied_body = self.raw_base_http_request(
                base_url,
                "GET",
                f"/oauth/authorize?{query}",
            )
            self.assertEqual(denied_status, 400)
            self.assertIn("not registered", denied_body)
        finally:
            self.stop_process(process)

    def test_oauth_dynamic_registration_normalizes_unsupported_flow_metadata(self) -> None:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = self.oauth_server_env(
            CODING_TOOLS_MCP_OAUTH_PASSWORD="test-password",
            CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET=bytes(range(32)).hex(),
        )
        process = self.start_oauth_server(port, env)
        try:
            self.wait_for_json(f"{base_url}/.well-known/oauth-authorization-server")
            body = json.dumps(
                {
                    "client_name": "Refresh Token Compatibility Test",
                    "redirect_uris": ["http://127.0.0.1/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code", "token"],
                    "token_endpoint_auth_method": "none",
                }
            ).encode("utf-8")
            status, _, response_body = self.raw_base_http_request(
                base_url,
                "POST",
                "/oauth/register",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201, response_body)
            response = json.loads(response_body)
            self.assertEqual(response.get("grant_types"), ["authorization_code"])
            self.assertEqual(response.get("response_types"), ["code"])

            refresh_body = urllib.parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "client_id": response["client_id"],
                    "refresh_token": "not-issued",
                    "resource": base_url,
                }
            ).encode("utf-8")
            refresh_status, _, refresh_response = self.raw_base_http_request(
                base_url,
                "POST",
                "/oauth/token",
                body=refresh_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(refresh_status, 400)
            self.assertEqual(json.loads(refresh_response).get("error"), "unsupported_grant_type")

            unsupported_only_body = json.dumps(
                {
                    "redirect_uris": ["http://127.0.0.1/callback"],
                    "grant_types": ["refresh_token"],
                }
            ).encode("utf-8")
            unsupported_status, _, unsupported_response = self.raw_base_http_request(
                base_url,
                "POST",
                "/oauth/register",
                body=unsupported_only_body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(unsupported_status, 400)
            self.assertEqual(json.loads(unsupported_response).get("error"), "invalid_client_metadata")

            unsupported_response_type_body = json.dumps(
                {
                    "redirect_uris": ["http://127.0.0.1/callback"],
                    "grant_types": ["authorization_code"],
                    "response_types": ["token"],
                }
            ).encode("utf-8")
            unsupported_response_status, _, unsupported_response_body = self.raw_base_http_request(
                base_url,
                "POST",
                "/oauth/register",
                body=unsupported_response_type_body,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(unsupported_response_status, 400)
            self.assertEqual(json.loads(unsupported_response_body).get("error"), "invalid_client_metadata")
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

        status, response = self.raw_http_post(b"\xff")
        self.assertEqual(status, 400)
        self.assertIsNone(response.get("id"))
        self.assertEqual(response.get("error", {}).get("code"), -32700)

    def test_http_rejects_tools_before_initialize(self) -> None:
        process, url = self.start_raw_http_server()
        try:
            self.wait_for_ping(url)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.raw_post_to(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            response = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(response.get("error", {}).get("code"), -32002)
            self.assertIn("not initialized", response.get("error", {}).get("message", "").lower())
        finally:
            self.stop_process(process)

    def test_initialize_notification_is_rejected(self) -> None:
        status, response = self.raw_http_post(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "invalid-notification", "version": "1"},
                    },
                }
            ).encode("utf-8"),
            headers={"MCP-Protocol-Version": "2025-11-25"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(response.get("error", {}).get("code"), -32600)

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
        self.assertEqual(response.get("result", {}).get("protocolVersion"), "2025-11-25")

        header_response = self.raw_post_to(
            str(self.client.url),
            payload,
            protocol_version="2025-11-25",
        )
        self.assertEqual(header_response.get("result", {}).get("protocolVersion"), "2025-11-25")

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
            self.assertIsNotNone(process.stdin)
            process.stdin.write("{not-json}\n")
            process.stdin.flush()
            malformed = self.stdio_read_response(process, {})
            self.assertEqual(malformed.get("error", {}).get("code"), -32700)

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
            self.assertNotIn("logging", result.get("capabilities", {}))

            logging_level = self.stdio_rpc_allow_error(
                process,
                {"jsonrpc": "2.0", "id": 2, "method": "logging/setLevel", "params": {"level": "debug"}},
            )
            self.assertEqual(logging_level.get("error", {}).get("code"), -32601)

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
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "missing"}},
            )
            self.assert_no_stdio_response(process)
        finally:
            self.stop_process(process)

    def assert_content_text_is_agent_readable(self, result: dict[str, Any]) -> str:
        structured = result.get("structuredContent")
        self.assertIsInstance(structured, dict, f"structuredContent must be an object: {result!r}")
        content = result.get("content")
        self.assertIsInstance(content, list, f"content must be a list: {result!r}")
        text_items = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        self.assertTrue(text_items, f"content must include agent-readable text: {result!r}")
        return "\n".join(str(item) for item in text_items)

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
                connection.putheader("MCP-Protocol-Version", "2025-11-25")
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
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        request_headers.update(headers or {})
        status, _, response_body = self.raw_base_http_request(
            url,
            "POST",
            urllib.parse.urlparse(url).path or "/mcp",
            body=b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}',
            headers=request_headers,
        )
        return status, json.loads(response_body)

    def oauth_server_env(self, **overrides: str) -> dict[str, str]:
        env = self.server_process_env()
        for name in (
            "CODING_TOOLS_MCP_OAUTH_CLIENT_ID",
            "CODING_TOOLS_MCP_OAUTH_CLIENT_SECRET",
            "CODING_TOOLS_MCP_OAUTH_PASSWORD",
            "CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET",
            "CODING_TOOLS_MCP_OAUTH_TOKEN_TTL",
            "CODING_TOOLS_MCP_OAUTH_REDIRECT_URIS",
            "CODING_TOOLS_MCP_SERVER_URL",
            "CODING_TOOLS_MCP_AUTH_TOKEN",
            "CODING_TOOLS_MCP_OAUTH_MODE",
            "CODING_TOOLS_MCP_TRUST_PROXY_HEADERS",
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
        def probe() -> dict[str, Any]:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read().decode("utf-8"))

        return self.wait_for_server(probe, f"server did not return JSON from {url}")

    def pkce_challenge(self, verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def oauth_register(self, base_url: str, client_name: str, auth_method: str) -> dict[str, Any]:
        body = json.dumps(
            {
                "client_name": client_name,
                "redirect_uris": ["http://127.0.0.1/callback"],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": auth_method,
            }
        ).encode("utf-8")
        status, _, response_body = self.raw_base_http_request(
            base_url,
            "POST",
            "/oauth/register",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201, response_body)
        return json.loads(response_body)

    def oauth_register_client(self, base_url: str, client_name: str) -> str:
        response = self.oauth_register(base_url, client_name, "none")
        self.assertNotIn("client_secret", response)
        return str(response["client_id"])

    def oauth_register_confidential_client(
        self,
        base_url: str,
        client_name: str,
        auth_method: str,
    ) -> tuple[str, str]:
        response = self.oauth_register(base_url, client_name, auth_method)
        self.assertEqual(response.get("token_endpoint_auth_method"), auth_method)
        self.assertIsInstance(response.get("client_secret"), str)
        return str(response["client_id"]), str(response["client_secret"])

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
        resource = self.oauth_resource(base_url, headers)
        challenge = self.pkce_challenge(verifier)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "test-state",
                "resource": resource,
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
                "resource": resource,
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
        client_auth_method: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1/callback",
            "code_verifier": verifier,
            "client_id": client_id,
            "resource": self.oauth_resource(base_url, headers),
        }
        if client_secret is not None and client_auth_method != "client_secret_basic":
            params["client_secret"] = client_secret
        body = urllib.parse.urlencode(params).encode("utf-8")
        request_headers = {**(headers or {}), "Content-Type": "application/x-www-form-urlencoded"}
        if client_secret is not None and client_auth_method == "client_secret_basic":
            credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
            request_headers["Authorization"] = f"Basic {credentials}"
        status, _, response_body = self.raw_base_http_request(
            base_url,
            "POST",
            "/oauth/token",
            body=body,
            headers=request_headers,
        )
        return status, json.loads(response_body)

    def oauth_resource(self, base_url: str, headers: dict[str, str] | None) -> str:
        headers = headers or {}
        host = headers.get("X-Forwarded-Host")
        proto = headers.get("X-Forwarded-Proto")
        if host and proto:
            return f"{proto}://{host}"
        return base_url.rstrip("/")

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
        return safe_server_env()

    def process_stderr_snapshot(self, process: subprocess.Popen[str]) -> str:
        if process.stderr is None:
            return ""
        return stream_snapshot(process.stderr)

    def wait_for_server(self, probe: Callable[[], Any], description: str) -> Any:
        deadline = time.time() + 10
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                return probe()
            except Exception as exc:  # noqa: BLE001 - startup retry records last failure
                last_error = exc
                time.sleep(0.1)
        raise AssertionError(f"{description}: {last_error!r}")

    def wait_for_ping(self, url: str) -> None:
        self.wait_for_server(
            lambda: self.raw_post_to(url, {"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}}),
            "server did not accept ping",
        )

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
