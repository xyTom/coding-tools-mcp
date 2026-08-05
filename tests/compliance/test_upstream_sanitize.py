from __future__ import annotations

import json
import sys
import unittest

from coding_tools_mcp.json_utils import strict_json_bytes
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
    def test_canonical_json_handles_large_integers_and_unpaired_surrogates(self) -> None:
        original_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(1_000)
            minimum = 10**1_000 + 7
            raw = {
                "name": "remote__large",
                "description": "surrogate:\ud800",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "integer", "minimum": minimum}
                    },
                },
            }
            public = sanitize_definition(raw)
            self.assertEqual(
                public["inputSchema"]["properties"]["value"]["minimum"],
                minimum,
            )
            self.assertRegex(schema_digest(public), r"^[0-9a-f]{32}$")
            self.assertRegex(raw_schema_digest(raw) or "", r"^[0-9a-f]{32}$")
            encoded = strict_json_bytes(public, sort_keys=True)
            self.assertIn(b"\\ud800", encoded)
        finally:
            sys.set_int_max_str_digits(original_limit)

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
        self.assertNotIn("type", query)
        self.assertNotIn("additionalProperties", query)
        self.assertIn("reference omitted", query["description"].lower())

    def test_unknown_ref_branches_degrade_safely_inside_combinators(self) -> None:
        public = sanitize_definition(
            {
                "name": "remote__combined_refs",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "all_value": {
                            "allOf": [
                                {"$ref": "#/$defs/Value"},
                                {"type": "string"},
                            ]
                        },
                        "any_value": {
                            "anyOf": [
                                {"$ref": "#/$defs/Value"},
                                {"type": "string"},
                            ]
                        },
                        "one_value": {
                            "oneOf": [
                                {"$ref": "#/$defs/Value"},
                                {"type": "string"},
                            ]
                        },
                    },
                    "$defs": {"Value": {"type": "string"}},
                },
            }
        )
        properties = public["inputSchema"]["properties"]
        self.assertEqual(properties["all_value"]["allOf"], [{"type": "string"}])
        self.assertNotIn("anyOf", properties["any_value"])
        self.assertNotIn("oneOf", properties["one_value"])

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
        self.assertNotIn("type", value)
        self.assertNotIn("additionalProperties", value)
        self.assertIn("reference omitted", value["description"].lower())
        self.assertEqual(
            output["properties"]["item"]["properties"]["invalid"],
            {},
        )

    def test_top_level_input_ref_degrades_to_loose_object(self) -> None:
        public = sanitize_definition(
            {
                "name": "remote__root_ref",
                "inputSchema": {"$ref": "#/$defs/Input"},
                "$defs": {"Input": {"type": "string"}},
            }
        )
        self.assertEqual(public["inputSchema"]["type"], "object")
        self.assertTrue(public["inputSchema"]["additionalProperties"])

    def test_untrusted_pattern_is_removed_from_public_schema(self) -> None:
        public = sanitize_definition(
            {
                "name": "remote__pattern",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "pattern": "(a+)+$"}
                    },
                },
            }
        )
        value = public["inputSchema"]["properties"]["value"]
        self.assertNotIn("pattern", value)

    def test_not_is_dropped_when_its_only_assertion_was_untrusted_pattern(self) -> None:
        public = sanitize_definition(
            {
                "name": "remote__negated_pattern",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "not": {"pattern": "(a+)+$"},
                        }
                    },
                },
            }
        )
        value = public["inputSchema"]["properties"]["value"]
        self.assertNotIn("not", value)
        self.assertNotIn("pattern", repr(value))

    def test_restrictive_cardinality_limits_are_dropped_not_truncated(self) -> None:
        public = sanitize_definition(
            {
                "name": "remote__cardinality",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "enum_value": {"enum": list(range(51))},
                        "any_value": {
                            "anyOf": [{"const": value} for value in range(11)]
                        },
                        "one_value": {
                            "oneOf": [{"const": value} for value in range(11)]
                        },
                        "all_value": {
                            "allOf": [{"type": "integer"} for _ in range(11)]
                        },
                    },
                },
            }
        )
        properties = public["inputSchema"]["properties"]
        self.assertNotIn("enum", properties["enum_value"])
        self.assertNotIn("anyOf", properties["any_value"])
        self.assertNotIn("oneOf", properties["one_value"])
        self.assertEqual(len(properties["all_value"]["allOf"]), 10)

    def test_truncated_properties_drop_additional_properties_restrictions(self) -> None:
        properties = {f"p{index}": {"type": "integer"} for index in range(41)}
        for additional in (False, {"type": "string"}):
            with self.subTest(additional=additional):
                public = sanitize_definition(
                    {
                        "name": "remote__properties",
                        "inputSchema": {
                            "type": "object",
                            "properties": properties,
                            "additionalProperties": additional,
                        },
                    }
                )
                public_input = public["inputSchema"]
                self.assertEqual(len(public_input["properties"]), 40)
                self.assertNotIn("p40", public_input["properties"])
                self.assertNotIn("additionalProperties", public_input)

    def test_mutated_restrictive_values_are_dropped(self) -> None:
        long_value = "x" * 10_000
        public = sanitize_definition(
            {
                "name": "remote__long_values",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "enum_value": {"enum": [long_value]},
                        "const_value": {"const": long_value},
                    },
                },
            }
        )
        properties = public["inputSchema"]["properties"]
        self.assertNotIn("enum", properties["enum_value"])
        self.assertNotIn("const", properties["const_value"])

    def test_nonmonotonic_combinators_drop_partially_sanitized_constraints(self) -> None:
        long_value = "x" * 10_000
        public = sanitize_definition(
            {
                "name": "remote__nonmonotonic",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "negated": {
                            "not": {"type": "string", "const": long_value}
                        },
                        "exclusive": {
                            "oneOf": [
                                {"type": "string", "enum": [long_value]},
                                {"type": "string", "enum": ["short"]},
                            ]
                        },
                    },
                },
            }
        )
        properties = public["inputSchema"]["properties"]
        self.assertNotIn("not", properties["negated"])
        self.assertNotIn("oneOf", properties["exclusive"])

    def test_deep_parseable_schema_cannot_escape_sanitizer(self) -> None:
        nested: dict[str, object] = {"type": "string"}
        for _ in range(400):
            nested = {"not": nested}
        raw = {
            "name": "remote__deep",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "value": {"oneOf": [nested]},
                },
            },
        }

        public = sanitize_definition(raw)
        value_schema = public["inputSchema"]["properties"]["value"]
        self.assertNotIn("oneOf", value_schema)
        raw_digest = raw_schema_digest(raw)
        self.assertTrue(raw_digest is None or len(raw_digest) == 32)

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
        public_input = public["inputSchema"]
        self.assertEqual(public_input["type"], "object")
        self.assertLessEqual(len(public_input.get("properties", {})), MAX_SCHEMA_PROPERTIES)
        self.assertNotIn("additionalProperties", public_input)
        for property_schema in public_input.get("properties", {}).values():
            self.assertNotIn("enum", property_schema)

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
