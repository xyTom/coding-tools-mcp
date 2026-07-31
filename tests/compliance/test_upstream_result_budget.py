from __future__ import annotations

import json
import unittest

from coding_tools_mcp.upstream import (
    BaseUpstreamClient,
    UpstreamServerConfig,
    normalize_tool_result,
)
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
