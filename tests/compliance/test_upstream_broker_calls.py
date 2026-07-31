from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.server import JsonRpcError, Runtime, TOOL_REGISTRY
from coding_tools_mcp.upstream import UpstreamServerConfig
from tests.compliance.test_upstream_gateway import FakeUpstreamClient, build_manager


READ_TOOL = {
    "name": "complex_read",
    "description": "Validate a complex public schema.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "safe"]},
            "kind": {"type": "string", "const": "query"},
            "count": {"type": "integer", "minimum": 1, "maximum": 5},
            "ratio": {"type": "number", "minimum": 0.25, "maximum": 0.75},
            "labels": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"type": "string", "minLength": 2, "maxLength": 8},
            },
            "choice": {
                "oneOf": [
                    {"type": "string", "enum": ["alpha"]},
                    {"type": "integer", "minimum": 10, "maximum": 20},
                ]
            },
        },
        "required": ["mode", "kind", "count", "ratio", "labels", "choice"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}

MUTATING_TOOL = {
    "name": "complex_write",
    "description": "Mutate remote state.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "const": "write"},
            "payload": {"type": "string", "minLength": 1, "maxLength": 20},
        },
        "required": ["action", "payload"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": True},
}

LOOSE_TOOL = {
    "name": "loose_read",
    "description": "Accept arbitrary object arguments.",
    "inputSchema": {"type": "object", "additionalProperties": True},
    "annotations": {"readOnlyHint": True},
}


class UpstreamBrokerCallTests(unittest.TestCase):
    def build_runtime(self, *, fake_readonly: bool = False) -> tuple[Runtime, FakeUpstreamClient]:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode="broker",
        )
        client = FakeUpstreamClient(
            config,
            "2025-11-25",
            tools=[READ_TOOL, MUTATING_TOOL, LOOSE_TOOL],
        )
        manager = build_manager([config], [client])
        self.temp = TemporaryDirectory()
        runtime = Runtime(
            Path(self.temp.name),
            permission_mode="dangerous" if fake_readonly else "safe",
            fake_readonly_annotations=fake_readonly,
            upstream_manager=manager,
        )
        return runtime, client

    def tearDown(self) -> None:
        temp = getattr(self, "temp", None)
        if temp is not None:
            temp.cleanup()

    def valid_read_arguments(self) -> dict[str, object]:
        return {
            "mode": "fast",
            "kind": "query",
            "count": 3,
            "ratio": 0.5,
            "labels": ["ab"],
            "choice": "alpha",
        }

    def test_fixed_call_tools_and_real_annotations(self) -> None:
        readonly = TOOL_REGISTRY["upstream_tool_call"]
        mutating = TOOL_REGISTRY["upstream_tool_call_mutating"]
        self.assertTrue(readonly.read_only)
        self.assertFalse(readonly.idempotent)
        self.assertTrue(readonly.open_world)
        self.assertFalse(mutating.read_only)
        self.assertTrue(mutating.destructive)
        self.assertTrue(mutating.open_world)

    def test_readonly_call_passthrough_has_no_double_envelope(self) -> None:
        runtime, client = self.build_runtime()
        digest = runtime.upstream_manager.state.all_tools[
            "remote__complex_read"
        ].public_schema_digest
        result = runtime.call_tool(
            "upstream_tool_call",
            {
                "name": "remote__complex_read",
                "arguments": self.valid_read_arguments(),
                "schema_digest": digest,
            },
        )
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["remote_name"], "complex_read")
        self.assertNotIn("structuredContent", result["structuredContent"])
        self.assertEqual(client.calls, [("complex_read", self.valid_read_arguments())])
        runtime.close()

    def test_readonly_and_mutating_routes_reject_the_wrong_risk(self) -> None:
        runtime, client = self.build_runtime()
        write_digest = runtime.upstream_manager.state.all_tools[
            "remote__complex_write"
        ].public_schema_digest
        wrong_read = runtime.call_tool(
            "upstream_tool_call",
            {
                "name": "remote__complex_write",
                "arguments": {"action": "write", "payload": "x"},
            },
        )
        self.assertEqual(
            wrong_read["structuredContent"]["error"]["code"],
            "UPSTREAM_TOOL_NOT_READONLY",
        )
        wrong_mutating = runtime.call_tool(
            "upstream_tool_call_mutating",
            {
                "name": "remote__complex_read",
                "arguments": self.valid_read_arguments(),
                "schema_digest": write_digest,
            },
        )
        self.assertEqual(
            wrong_mutating["structuredContent"]["error"]["code"],
            "UPSTREAM_TOOL_NOT_MUTATING",
        )
        self.assertEqual(client.calls, [])
        runtime.close()

    def test_mutating_digest_is_required_and_stale_digest_is_rejected(self) -> None:
        runtime, client = self.build_runtime()
        with self.assertRaises(JsonRpcError):
            runtime.call_tool(
                "upstream_tool_call_mutating",
                {
                    "name": "remote__complex_write",
                    "arguments": {"action": "write", "payload": "x"},
                },
            )
        stale = runtime.call_tool(
            "upstream_tool_call_mutating",
            {
                "name": "remote__complex_write",
                "arguments": {"action": "write", "payload": "x"},
                "schema_digest": "0" * 32,
            },
        )
        self.assertEqual(
            stale["structuredContent"]["error"]["code"],
            "UPSTREAM_SCHEMA_CHANGED",
        )
        self.assertEqual(client.calls, [])
        runtime.close()

    def test_mutating_call_with_current_digest_is_passthrough(self) -> None:
        runtime, client = self.build_runtime()
        digest = runtime.upstream_manager.state.all_tools[
            "remote__complex_write"
        ].public_schema_digest
        result = runtime.call_tool(
            "upstream_tool_call_mutating",
            {
                "name": "remote__complex_write",
                "arguments": {"action": "write", "payload": "updated"},
                "schema_digest": digest,
            },
        )
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["remote_name"], "complex_write")
        self.assertEqual(client.calls[0][0], "complex_write")
        runtime.close()

    def test_public_schema_required_type_enum_const_bounds_and_oneof(self) -> None:
        runtime, client = self.build_runtime()
        cases = [
            ({}, "required"),
            ({**self.valid_read_arguments(), "mode": "invalid"}, "one of"),
            ({**self.valid_read_arguments(), "kind": "mutate"}, "equal"),
            ({**self.valid_read_arguments(), "count": 0}, ">="),
            ({**self.valid_read_arguments(), "ratio": 0.9}, "<="),
            ({**self.valid_read_arguments(), "labels": []}, "at least"),
            ({**self.valid_read_arguments(), "labels": ["a"]}, "shorter"),
            ({**self.valid_read_arguments(), "choice": 5}, "exactly one"),
            ({**self.valid_read_arguments(), "extra": True}, "recognized"),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                result = runtime.call_tool(
                    "upstream_tool_call",
                    {"name": "remote__complex_read", "arguments": arguments},
                )
                self.assertTrue(result["isError"])
                self.assertEqual(
                    result["structuredContent"]["error"]["code"],
                    "UPSTREAM_ARGUMENTS_INVALID",
                )
                self.assertIn(message, result["structuredContent"]["error"]["message"])
        self.assertEqual(client.calls, [])
        runtime.close()

    def test_loose_schema_accepts_arbitrary_object(self) -> None:
        runtime, client = self.build_runtime()
        arguments = {"nested": {"anything": [1, 2, 3]}, "flag": True}
        result = runtime.call_tool(
            "upstream_tool_call",
            {"name": "remote__loose_read", "arguments": arguments},
        )
        self.assertFalse(result["isError"])
        self.assertEqual(client.calls, [("loose_read", arguments)])
        runtime.close()

    def test_upstream_is_error_is_preserved(self) -> None:
        runtime, client = self.build_runtime()
        upstream_result = {
            "content": [{"type": "text", "text": "remote denied"}],
            "structuredContent": {"remote_error": True},
            "isError": True,
        }
        with patch.object(client, "call_tool_raw", return_value=copy.deepcopy(upstream_result)):
            result = runtime.call_tool(
                "upstream_tool_call",
                {
                    "name": "remote__complex_read",
                    "arguments": self.valid_read_arguments(),
                },
            )
        self.assertEqual(result, upstream_result)
        runtime.close()

    def test_fake_readonly_annotation_does_not_change_risk_route(self) -> None:
        runtime, client = self.build_runtime(fake_readonly=True)
        definitions = {tool["name"]: tool for tool in runtime.list_tools()["tools"]}
        self.assertTrue(
            definitions["upstream_tool_call_mutating"]["annotations"]["readOnlyHint"]
        )
        result = runtime.call_tool(
            "upstream_tool_call",
            {
                "name": "remote__complex_write",
                "arguments": {"action": "write", "payload": "x"},
            },
        )
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "UPSTREAM_TOOL_NOT_READONLY",
        )
        self.assertEqual(client.calls, [])
        runtime.close()

    def test_invalid_digest_format_is_rejected_before_remote_call(self) -> None:
        runtime, client = self.build_runtime()
        with self.assertRaises(JsonRpcError):
            runtime.call_tool(
                "upstream_tool_call_mutating",
                {
                    "name": "remote__complex_write",
                    "arguments": {"action": "write", "payload": "x"},
                    "schema_digest": "ABC",
                },
            )
        self.assertEqual(client.calls, [])
        runtime.close()

    def test_broker_errors_are_not_double_wrapped(self) -> None:
        runtime, _client = self.build_runtime()
        result = runtime.call_tool(
            "upstream_tool_call",
            {"name": "remote__missing", "arguments": {}},
        )
        encoded = json.dumps(result)
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "UPSTREAM_TOOL_NOT_FOUND",
        )
        self.assertEqual(encoded.count("UPSTREAM_TOOL_NOT_FOUND"), 1)
        runtime.close()


if __name__ == "__main__":
    unittest.main()
