from __future__ import annotations

import copy
import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import (
    JsonRpcError,
    Runtime,
    TOOL_REGISTRY,
    _json_schema_equal,
    validate_schema_value,
)
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
            "blocked": {"type": "string", "not": {"const": "forbidden"}},
            "unique_labels": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string"},
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


REF_TOOL = {
    "name": "ref_read",
    "description": "Accept a nested reference degraded to accept-any.",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"$ref": "#/$defs/query"}},
        "required": ["query"],
        "$defs": {"query": {"type": "string"}},
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}

ONEOF_REF_TOOL = {
    "name": "oneof_ref_read",
    "description": "Accept a oneOf containing an unresolved reference.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [
                    {"$ref": "#/$defs/value"},
                    {"type": "string"},
                ]
            }
        },
        "required": ["value"],
        "$defs": {"value": {"type": "string"}},
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}


ENUM_BUDGET_TOOL = {
    "name": "enum_budget_read",
    "description": "Accept values omitted from a contained oversized enum.",
    "inputSchema": {
        "type": "object",
        "properties": {"value": {"enum": list(range(51))}},
        "required": ["value"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}

PROPERTIES_BUDGET_TOOL = {
    "name": "properties_budget_read",
    "description": "Accept a declared property omitted by containment limits.",
    "inputSchema": {
        "type": "object",
        "properties": {f"p{index}": {"type": "integer"} for index in range(41)},
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}

PROPERTIES_SCHEMA_BUDGET_TOOL = {
    "name": "properties_schema_budget_read",
    "description": "Do not apply additionalProperties schema to omitted declared properties.",
    "inputSchema": {
        "type": "object",
        "properties": {f"p{index}": {"type": "integer"} for index in range(41)},
        "additionalProperties": {"type": "string"},
    },
    "annotations": {"readOnlyHint": True},
}

BRANCH_BUDGET_TOOL = {
    "name": "branch_budget_read",
    "description": "Accept values represented only by omitted combination branches.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "any_value": {"anyOf": [{"const": value} for value in range(11)]},
            "one_value": {"oneOf": [{"const": value} for value in range(11)]},
        },
        "required": ["any_value", "one_value"],
        "additionalProperties": False,
    },
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
            tools=[
                READ_TOOL,
                MUTATING_TOOL,
                LOOSE_TOOL,
                REF_TOOL,
                ONEOF_REF_TOOL,
                ENUM_BUDGET_TOOL,
                PROPERTIES_BUDGET_TOOL,
                PROPERTIES_SCHEMA_BUDGET_TOOL,
                BRANCH_BUDGET_TOOL,
            ],
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

    def test_search_digest_can_directly_authorize_mutating_route(self) -> None:
        runtime, client = self.build_runtime()
        search = runtime.call_tool(
            "upstream_tool_search",
            {"query": "mutate remote state", "read_only": False, "limit": 10},
        )
        result_entry = next(
            item
            for item in search["structuredContent"]["results"]
            if item["name"] == "remote__complex_write"
        )
        result = runtime.call_tool(
            "upstream_tool_call_mutating",
            {
                "name": result_entry["name"],
                "arguments": {"action": "write", "payload": "from-search"},
                "schema_digest": result_entry["schema_digest"],
            },
        )
        self.assertFalse(result["isError"])
        self.assertEqual(client.calls, [("complex_write", {"action": "write", "payload": "from-search"})])
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
            ({**self.valid_read_arguments(), "blocked": "forbidden"}, "forbidden schema"),
            ({**self.valid_read_arguments(), "unique_labels": ["x", "x"]}, "unique"),
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

    def test_json_schema_equality_distinguishes_boolean_from_number(self) -> None:
        with self.assertRaises(ToolFailure):
            validate_schema_value(True, {"const": 1}, path="value")
        with self.assertRaises(ToolFailure):
            validate_schema_value(True, {"enum": [1]}, path="value")

        validate_schema_value(
            [True, 1],
            {"type": "array", "uniqueItems": True},
            path="value",
        )
        with self.assertRaises(ToolFailure):
            validate_schema_value(
                [1, 1.0],
                {"type": "array", "uniqueItems": True},
                path="value",
            )
        with self.assertRaises(ToolFailure):
            validate_schema_value(
                [{"a": 1, "b": [2]}, {"b": [2.0], "a": 1.0}],
                {"type": "array", "uniqueItems": True},
                path="value",
            )

    def test_json_schema_numeric_equality_is_exact_beyond_decimal_context(self) -> None:
        for digits in (30, 100):
            with self.subTest(digits=digits):
                value = 10 ** (digits - 1) + 12345
                adjacent = value + 1
                self.assertFalse(_json_schema_equal(value, adjacent))
                validate_schema_value(value, {"const": value}, path="value")
                validate_schema_value(value, {"enum": [value]}, path="value")
                with self.assertRaises(ToolFailure):
                    validate_schema_value(adjacent, {"const": value}, path="value")
                with self.assertRaises(ToolFailure):
                    validate_schema_value(adjacent, {"enum": [value]}, path="value")

        scientific_value = 10**30
        self.assertTrue(_json_schema_equal(scientific_value, 1e30))
        validate_schema_value(1e30, {"const": scientific_value}, path="value")
        validate_schema_value(1e30, {"enum": [scientific_value]}, path="value")

        nested_left = 10**99 + 7
        nested_right = nested_left + 1
        validate_schema_value(
            [{"nested": [nested_left]}, {"nested": [nested_right]}],
            {"type": "array", "uniqueItems": True},
            path="value",
        )
        with self.assertRaises(ToolFailure):
            validate_schema_value(
                [{"nested": [scientific_value]}, {"nested": [1e30]}],
                {"type": "array", "uniqueItems": True},
                path="value",
            )

    def test_json_schema_large_integer_fingerprint_ignores_host_string_limit(self) -> None:
        original_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(1_000)
            value = 10**1_000 + 7
            adjacent = value + 1
            self.assertFalse(_json_schema_equal(value, adjacent))
            validate_schema_value(value, {"const": value}, path="value")
            with self.assertRaises(ToolFailure):
                validate_schema_value(adjacent, {"const": value}, path="value")
            with self.assertRaises(ToolFailure) as below_minimum:
                validate_schema_value(value - 1, {"minimum": value}, path="value")
            self.assertIn("must be >=", below_minimum.exception.message)
            with self.assertRaises(ToolFailure) as above_maximum:
                validate_schema_value(adjacent, {"maximum": value}, path="value")
            self.assertIn("must be <=", above_maximum.exception.message)
        finally:
            sys.set_int_max_str_digits(original_limit)

    def test_unique_items_ten_thousand_values_is_linear_time(self) -> None:
        started = time.perf_counter()
        validate_schema_value(
            list(range(10_000)),
            {"type": "array", "uniqueItems": True},
            path="value",
        )
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0, f"uniqueItems validation took {elapsed:.3f}s")

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
        annotations = definitions["upstream_tool_call_mutating"]["annotations"]
        self.assertFalse(annotations["readOnlyHint"])
        self.assertTrue(annotations["destructiveHint"])
        self.assertTrue(annotations["openWorldHint"])
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

    def test_broker_calls_omit_output_schema_for_true_passthrough(self) -> None:
        runtime, _client = self.build_runtime()
        definitions = {tool["name"]: tool for tool in runtime.list_tools()["tools"]}
        self.assertNotIn("outputSchema", definitions["upstream_tool_call"])
        self.assertNotIn("outputSchema", definitions["upstream_tool_call_mutating"])
        runtime.close()

    def test_content_only_upstream_result_remains_content_only(self) -> None:
        runtime, client = self.build_runtime()
        upstream_result = {
            "content": [{"type": "text", "text": "done"}],
            "isError": False,
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
        self.assertNotIn("structuredContent", result)
        runtime.close()

    def test_nested_ref_property_degrades_to_accept_any(self) -> None:
        runtime, client = self.build_runtime()
        result = runtime.call_tool(
            "upstream_tool_call",
            {"name": "remote__ref_read", "arguments": {"query": "abc"}},
        )
        self.assertFalse(result["isError"])
        self.assertEqual(client.calls, [("ref_read", {"query": "abc"})])
        runtime.close()

    def test_oneof_with_nested_ref_degrades_without_double_match_rejection(self) -> None:
        runtime, client = self.build_runtime()
        result = runtime.call_tool(
            "upstream_tool_call",
            {"name": "remote__oneof_ref_read", "arguments": {"value": "abc"}},
        )
        self.assertFalse(result["isError"])
        self.assertEqual(client.calls, [("oneof_ref_read", {"value": "abc"})])
        runtime.close()

    def test_schema_quantity_budgets_only_widen_real_broker_validation(self) -> None:
        runtime, client = self.build_runtime()
        cases = [
            ("enum_budget_read", {"value": 50}),
            ("properties_budget_read", {"p40": 40}),
            ("properties_schema_budget_read", {"p40": 40}),
            ("branch_budget_read", {"any_value": 10, "one_value": 10}),
        ]
        for remote_name, arguments in cases:
            with self.subTest(remote_name=remote_name):
                result = runtime.call_tool(
                    "upstream_tool_call",
                    {"name": f"remote__{remote_name}", "arguments": arguments},
                )
                self.assertFalse(result["isError"])

        self.assertEqual(client.calls, cases)

        enum_schema = runtime.upstream_manager.state.all_tools[
            "remote__enum_budget_read"
        ].public_definition["inputSchema"]["properties"]["value"]
        self.assertNotIn("enum", enum_schema)
        for tool_name in ("properties_budget_read", "properties_schema_budget_read"):
            public_input = runtime.upstream_manager.state.all_tools[
                f"remote__{tool_name}"
            ].public_definition["inputSchema"]
            self.assertEqual(len(public_input["properties"]), 40)
            self.assertNotIn("additionalProperties", public_input)
        branch_properties = runtime.upstream_manager.state.all_tools[
            "remote__branch_budget_read"
        ].public_definition["inputSchema"]["properties"]
        self.assertNotIn("anyOf", branch_properties["any_value"])
        self.assertNotIn("oneOf", branch_properties["one_value"])
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
