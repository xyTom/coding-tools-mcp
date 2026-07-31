from __future__ import annotations

import json
import unittest

from coding_tools_mcp.upstream import classify_risk
from coding_tools_mcp.upstream_sanitize import (
    MAX_DEFINITION_BYTES,
    MAX_SCHEMA_ENUM_ITEMS,
    MAX_SCHEMA_PROPERTIES,
    raw_schema_digest,
    sanitize_definition,
    schema_digest,
)


class UpstreamSanitizerTests(unittest.TestCase):
    def test_invalid_top_level_fields_and_nested_property_schemas_are_contained(self) -> None:
        raw = {
            "name": "remote__search",
            "title": 17,
            "description": "\x00  Search\n remote\titems.  ",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "bad": "not-a-schema",
                    "nullable": {
                        "type": ["string", "null", "string", "unsupported"],
                        "description": "\x07 nullable\n value ",
                    },
                },
                "required": ["bad", "missing", 7],
                "$defs": {"ignored": {"type": "string"}},
            },
            "outputSchema": "not-an-object",
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": "false",
                "unknownHint": True,
            },
            "serverInstructions": "must not escape containment",
        }

        public = sanitize_definition(raw)

        self.assertNotIn("title", public)
        self.assertEqual(public["description"], "Search remote items.")
        self.assertNotIn("outputSchema", public)
        self.assertNotIn("serverInstructions", public)
        self.assertNotIn("$defs", public["inputSchema"])
        self.assertEqual(public["inputSchema"]["properties"]["bad"], {})
        self.assertEqual(
            public["inputSchema"]["properties"]["nullable"]["type"],
            ["string", "null"],
        )
        self.assertEqual(
            public["inputSchema"]["properties"]["nullable"]["description"],
            "nullable value",
        )
        self.assertEqual(public["inputSchema"]["required"], ["bad"])
        self.assertEqual(public["annotations"], {"readOnlyHint": True})

    def test_ref_nodes_degrade_without_leaving_dangling_references(self) -> None:
        public = sanitize_definition(
            {
                "name": "remote__lookup",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"$ref": "#/$defs/Query"},
                    },
                    "$defs": {"Query": {"type": "string"}},
                },
            }
        )

        query = public["inputSchema"]["properties"]["query"]
        self.assertNotIn("$ref", query)
        self.assertEqual(query["type"], "object")
        self.assertTrue(query["additionalProperties"])

    def test_output_schema_is_recursively_sanitized(self) -> None:
        public = sanitize_definition(
            {
                "name": "remote__export",
                "inputSchema": {"type": "object"},
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "item": {
                            "type": "object",
                            "properties": {
                                "value": {"$ref": "#/$defs/Value"},
                                "invalid": 7,
                            },
                        },
                    },
                    "$defs": {"Value": {"type": "string"}},
                },
            }
        )

        output = public["outputSchema"]
        self.assertNotIn("$defs", output)
        value = output["properties"]["item"]["properties"]["value"]
        self.assertNotIn("$ref", value)
        self.assertTrue(value["additionalProperties"])
        self.assertEqual(
            output["properties"]["item"]["properties"]["invalid"],
            {},
        )

    def test_schema_limits_and_final_definition_budget_are_hard(self) -> None:
        properties = {
            f"property_{index}": {
                "type": "string",
                "description": "描述" * 1_000,
                "enum": [f"value-{item}-" + ("x" * 700) for item in range(80)],
            }
            for index in range(70)
        }
        public = sanitize_definition(
            {
                "name": "remote__huge",
                "title": "T" * 2_000,
                "description": "D" * 10_000,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": properties,
                },
            }
        )
        encoded = json.dumps(
            public,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertLess(len(encoded), MAX_DEFINITION_BYTES)
        self.assertEqual(
            public["inputSchema"],
            {"type": "object", "additionalProperties": True},
        )

        moderate = sanitize_definition(
            {
                "name": "remote__moderate",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        f"p{index}": {
                            "type": "string",
                            "enum": list(range(100)),
                        }
                        for index in range(MAX_SCHEMA_PROPERTIES + 5)
                    },
                },
            }
        )
        moderate_properties = moderate["inputSchema"].get("properties", {})
        self.assertLessEqual(len(moderate_properties), MAX_SCHEMA_PROPERTIES)
        for schema in moderate_properties.values():
            self.assertLessEqual(len(schema.get("enum", [])), MAX_SCHEMA_ENUM_ITEMS)

    def test_public_digest_tracks_public_schema_not_raw_only_metadata(self) -> None:
        raw_a = {
            "name": "remote__search",
            "inputSchema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "$defs": {"unused": {"type": "string"}},
            },
        }
        raw_b = {
            "name": "remote__search",
            "inputSchema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "$defs": {"unused": {"type": "integer"}},
                "x-private": "changed",
            },
        }
        public_a = sanitize_definition(raw_a)
        public_b = sanitize_definition(raw_b)

        self.assertEqual(public_a, public_b)
        self.assertEqual(schema_digest(public_a), schema_digest(public_b))
        self.assertNotEqual(raw_schema_digest(raw_a), raw_schema_digest(raw_b))
        self.assertRegex(schema_digest(public_a), r"^[0-9a-f]{32}$")

    def test_invalid_input_schema_gets_loose_public_schema_and_no_raw_digest(self) -> None:
        raw = {
            "name": "remote__invalid",
            "inputSchema": "not-an-object",
        }
        public = sanitize_definition(raw)

        self.assertEqual(
            public["inputSchema"],
            {"type": "object", "additionalProperties": True},
        )
        self.assertIsNone(raw_schema_digest(raw))
        self.assertRegex(schema_digest(public), r"^[0-9a-f]{32}$")

    def test_risk_policy_precedes_annotations_and_unknown_defaults_mutating(self) -> None:
        readonly = {
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
            }
        }
        destructive = {
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": True,
            }
        }

        self.assertEqual(classify_risk(readonly, {}, "search"), "readonly")
        self.assertEqual(classify_risk(destructive, {}, "search"), "mutating")
        self.assertEqual(classify_risk({}, {}, "unknown"), "mutating")
        self.assertEqual(
            classify_risk(readonly, {"search": "mutating"}, "search"),
            "mutating",
        )
        self.assertEqual(
            classify_risk(destructive, {"search": "readonly"}, "search"),
            "readonly",
        )
        self.assertEqual(
            classify_risk(readonly, {"search": "invalid"}, "search"),
            "readonly",
        )


if __name__ == "__main__":
    unittest.main()
