from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

from coding_tools_mcp.json_utils import strict_json_loads
from coding_tools_mcp.upstream import (
    BaseUpstreamClient,
    UpstreamServerConfig,
    normalize_tool_result,
)
from tests.compliance.test_upstream_gateway import FakeUpstreamClient, build_manager
from coding_tools_mcp.upstream_result import (
    RESULT_INLINE_MAX,
    budget_tool_result,
    result_json_bytes,
)


class RequestBackedClient(BaseUpstreamClient):
    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        if method != "tools/call":
            raise AssertionError(method)
        return {"structuredContent": {"message": "raw"}}

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        raise AssertionError((method, params))


class UpstreamResultBudgetTests(unittest.TestCase):
    def test_result_json_bytes_ignores_lower_python_integer_string_limit(self) -> None:
        original_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(1_000)
            value = 10**1_000 + 7
            encoded = result_json_bytes(
                {
                    "content": [],
                    "structuredContent": {"value": value},
                    "isError": False,
                }
            )
            reparsed = strict_json_loads(encoded)
            self.assertEqual(reparsed["structuredContent"]["value"], value)
        finally:
            sys.set_int_max_str_digits(original_limit)

    def test_unpaired_surrogates_are_preserved_as_json_escapes(self) -> None:
        result = {
            "content": [{"type": "text", "text": "high:\ud800 low:\udc00 emoji:😀"}],
            "structuredContent": {"high": "\ud800", "low": "\udc00"},
            "isError": False,
        }

        encoded = result_json_bytes(result)
        decoded = encoded.decode("utf-8")
        reparsed = json.loads(decoded)

        self.assertIn(b"\\ud800", encoded)
        self.assertIn(b"\\udc00", encoded)
        self.assertEqual(reparsed, result)
        self.assertEqual(budget_tool_result(result), result)

    def test_manager_preserves_surrogate_result_instead_of_protocol_error(self) -> None:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        result = {
            "content": [{"type": "text", "text": "value:\ud800"}],
            "structuredContent": {"value": "\udc00"},
            "isError": False,
        }
        try:
            with patch.object(client, "call_tool_raw", return_value=result):
                returned = manager.call_tool("remote__search", {"q": "surrogate"})
            self.assertEqual(returned, result)
            self.assertFalse(returned["isError"])
            self.assertIn(b"\\ud800", result_json_bytes(returned))
            self.assertIn(b"\\udc00", result_json_bytes(returned))
        finally:
            manager.close()

    def test_small_result_is_returned_without_shape_changes(self) -> None:
        result = {
            "content": [
                {"type": "text", "text": "small"},
                {"type": "resource_link", "uri": "https://example.test/item"},
            ],
            "structuredContent": {"ok": True, "items": [1, 2, 3]},
            "isError": False,
            "_meta": {"trace": "kept"},
        }

        budgeted = budget_tool_result(result)

        self.assertIs(budgeted, result)
        self.assertEqual(budgeted, result)
        self.assertNotIn("_truncated", budgeted["structuredContent"])

    def test_raw_client_call_and_manager_normalization_responsibilities_are_separate(self) -> None:
        config = UpstreamServerConfig(
            alias="raw",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = RequestBackedClient(config, "2025-11-25")

        raw = client.call_tool_raw("sample", {})
        normalized = client.call_tool("sample", {})

        self.assertNotIn("content", raw)
        self.assertNotIn("isError", raw)
        self.assertEqual(normalized["content"], [])
        self.assertIs(normalized["isError"], False)
        self.assertEqual(normalized["structuredContent"], {"message": "raw"})

    def test_missing_content_normalizes_to_empty_without_copying_structured_text(self) -> None:
        normalized = normalize_tool_result(
            {
                "structuredContent": {"text": "structured-only"},
                "isError": False,
            }
        )

        budgeted = budget_tool_result(normalized)

        self.assertEqual(budgeted["content"], [])
        self.assertEqual(
            budgeted["structuredContent"],
            {"text": "structured-only"},
        )

    def test_multiple_text_blocks_share_one_envelope_budget(self) -> None:
        block_text = "abc界" * 2_000
        result = {
            "content": [
                {"type": "text", "text": block_text}
                for _ in range(80)
            ],
            "structuredContent": {"summary": "z" * 100_000},
            "isError": False,
        }
        original_bytes = len(result_json_bytes(result))

        budgeted = budget_tool_result(result)

        self.assertLessEqual(len(result_json_bytes(budgeted)), RESULT_INLINE_MAX)
        self.assertTrue(budgeted["structuredContent"]["_truncated"])
        self.assertEqual(
            budgeted["structuredContent"]["_original_bytes"],
            original_bytes,
        )
        output_text_bytes = sum(
            len(block.get("text", "").encode("utf-8"))
            for block in budgeted["content"]
            if isinstance(block, dict)
        )
        self.assertLess(output_text_bytes, 80 * len(block_text.encode("utf-8")))
        self.assertNotIn("_result_handle", json.dumps(budgeted))

    def test_binary_resource_and_unknown_blocks_are_content_aware(self) -> None:
        uri = "https://example.test/resource/42"
        result = {
            "content": [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": "IMAGE_PAYLOAD_" * 20_000,
                },
                {
                    "type": "audio",
                    "mimeType": "audio/wav",
                    "data": "AUDIO_PAYLOAD_" * 20_000,
                },
                {
                    "type": "blob",
                    "mimeType": "application/octet-stream",
                    "blob": "BLOB_PAYLOAD_" * 20_000,
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": uri,
                        "mimeType": "text/plain",
                        "text": "resource text " * 20_000,
                        "blob": "RESOURCE_BLOB_" * 20_000,
                    },
                },
                {
                    "type": "vendor_extension",
                    "payload": "UNKNOWN_PAYLOAD_" * 20_000,
                },
            ],
            "isError": True,
        }

        budgeted = budget_tool_result(result)
        serialized = json.dumps(budgeted, ensure_ascii=False)

        self.assertLessEqual(len(result_json_bytes(budgeted)), RESULT_INLINE_MAX)
        self.assertIs(budgeted["isError"], True)
        self.assertNotIn("IMAGE_PAYLOAD_", serialized)
        self.assertNotIn("AUDIO_PAYLOAD_", serialized)
        self.assertNotIn("BLOB_PAYLOAD_", serialized)
        self.assertNotIn("RESOURCE_BLOB_", serialized)
        self.assertNotIn("UNKNOWN_PAYLOAD_", serialized)
        self.assertIn("image content omitted", serialized)
        self.assertIn("audio content omitted", serialized)
        self.assertIn("blob content omitted", serialized)
        self.assertIn("vendor_extension content omitted", serialized)
        resource_blocks = [
            block
            for block in budgeted["content"]
            if isinstance(block, dict) and block.get("type") == "resource"
        ]
        self.assertEqual(len(resource_blocks), 1)
        resource = resource_blocks[0]["resource"]
        self.assertEqual(resource["uri"], uri)
        self.assertNotIn("blob", resource)
        self.assertIn("[truncated]", resource["text"])
        self.assertTrue(budgeted["structuredContent"]["_truncated"])
        self.assertNotIn("_result_handle", serialized)

    def test_structured_content_is_recursively_limited_and_metadata_is_created(self) -> None:
        nested: object = "leaf" * 50_000
        for index in range(20):
            nested = {f"level_{index}": [nested, "sibling" * 10_000]}
        result = {
            "content": [{"type": "text", "text": "x" * 200_000}],
            "structuredContent": {
                "nested": nested,
                "many": ["item" * 5_000 for _ in range(1_000)],
            },
            "isError": False,
        }

        budgeted = budget_tool_result(result)

        self.assertLessEqual(len(result_json_bytes(budgeted)), RESULT_INLINE_MAX)
        self.assertTrue(budgeted["structuredContent"]["_truncated"])
        self.assertGreater(budgeted["structuredContent"]["_original_bytes"], RESULT_INLINE_MAX)
        self.assertNotIn("_result_handle", json.dumps(budgeted))

    def test_result_handle_is_budgeted_inside_the_final_manager_envelope(self) -> None:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(
            config,
            "2025-11-25",
            tools=[
                {
                    "name": "near_limit",
                    "inputSchema": {"type": "object"},
                    "annotations": {"readOnlyHint": True},
                }
            ],
        )
        manager = build_manager([config], [client])
        raw = {
            "content": [
                {"type": "vendor_extension", "payload": "x" * 3_952}
                for _ in range(27)
            ]
            + [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": "z" * 30_000,
                }
            ],
            "structuredContent": {},
            "isError": False,
        }
        self.assertGreater(len(result_json_bytes(raw)), RESULT_INLINE_MAX)
        try:
            with patch.object(client, "call_tool_raw", return_value=raw):
                budgeted = manager.call_tool(
                    "remote__near_limit",
                    {},
                    result_owner="owner",
                    store_overflow=True,
                )
            structured = budgeted["structuredContent"]
            self.assertLessEqual(len(result_json_bytes(budgeted)), RESULT_INLINE_MAX)
            self.assertIsInstance(structured.get("_result_handle"), str)
            self.assertEqual(
                structured.get("_result_fetch_tool"),
                "upstream_result_fetch",
            )
        finally:
            manager.close()

    def test_oversized_result_without_structured_content_gets_only_phase_one_metadata(self) -> None:
        result = {
            "content": [{"type": "text", "text": "x" * 300_000}],
            "isError": True,
        }

        budgeted = budget_tool_result(result)

        self.assertLessEqual(len(result_json_bytes(budgeted)), RESULT_INLINE_MAX)
        self.assertIs(budgeted["isError"], True)
        self.assertEqual(
            set(budgeted["structuredContent"]),
            {"_truncated", "_original_bytes"},
        )
        self.assertTrue(budgeted["structuredContent"]["_truncated"])


if __name__ == "__main__":
    unittest.main()
