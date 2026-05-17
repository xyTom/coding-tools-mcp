from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

from tests.compliance.mcp_client import FORBIDDEN_TOOL_NAMES, FORBIDDEN_TOOL_TERMS, MCPClient, MCPError, REQUIRED_TOOLS
from tests.compliance.test_support import ComplianceTestCase


class MCPContractTests(ComplianceTestCase):
    def test_initialize_succeeds_and_tools_list_is_available(self) -> None:
        tools = self.client.list_tools()
        self.assertIsInstance(tools, list)
        self.assertGreater(len(tools), 0)

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
        old_trace = os.environ.get("CODEX_TOOL_RUNTIME_TRACE")
        os.environ["CODEX_TOOL_RUNTIME_TRACE"] = "1"
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
                os.environ.pop("CODEX_TOOL_RUNTIME_TRACE", None)
            else:
                os.environ["CODEX_TOOL_RUNTIME_TRACE"] = old_trace

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
        self.assertEqual(body.get("error", {}).get("code"), -32600)
        self.assertIn("Unsupported MCP protocol version", body.get("error", {}).get("message", ""))

    def test_stdio_transport_uses_newline_delimited_json_rpc_only(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codex_tool_runtime_mcp",
                "--workspace",
                str(self.workspace.root),
                "--stdio",
            ],
            cwd=str(self.workspace.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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

            self.stdio_send(process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            self.assert_no_stdio_response(process)

            listed = self.stdio_rpc(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
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
        self.assertIsNotNone(process.stdout)
        readable, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(readable, "stdio server did not produce a JSON-RPC response")
        line = process.stdout.readline()
        self.assertTrue(line.endswith("\n"), f"stdio response must be newline-delimited: {line!r}")
        self.assertEqual(line.count("\n"), 1, f"stdio response must be one JSON-RPC message per line: {line!r}")
        response = json.loads(line)
        self.assertEqual(response.get("jsonrpc"), "2.0")
        self.assertEqual(response.get("id"), payload.get("id"))
        self.assertNotIn("error", response, f"unexpected stdio JSON-RPC error: {response!r}")
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
