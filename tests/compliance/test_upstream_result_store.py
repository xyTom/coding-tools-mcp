from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.server import JsonRpcError, Runtime, TOOL_REGISTRY
from coding_tools_mcp.upstream import UpstreamServerConfig
from coding_tools_mcp.upstream_result import RESULT_INLINE_MAX, result_json_bytes
from coding_tools_mcp.upstream_result_store import (
    RESULT_FETCH_MAX_CODEPOINTS,
    ResultNotFound,
    ResultStore,
)
from tests.compliance.test_upstream_gateway import FakeUpstreamClient, build_manager


class ResultStoreTests(unittest.TestCase):
    def deterministic_store(self, **kwargs: object) -> ResultStore:
        counter = iter(range(100))
        return ResultStore(
            handle_factory=lambda: f"handle-{next(counter)}",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_single_owner_and_global_fifo_caps(self) -> None:
        single = self.deterministic_store(single_max_bytes=4)
        self.assertIsNone(single.store("12345", owner="a", server_alias="remote"))

        handles = self.deterministic_store(owner_max_handles=2)
        first = handles.store("a", owner="owner", server_alias="remote")
        second = handles.store("b", owner="owner", server_alias="remote")
        third = handles.store("c", owner="owner", server_alias="remote")
        self.assertEqual(handles.owner_handle_count("owner"), 2)
        with self.assertRaises(ResultNotFound):
            handles.fetch(str(first), owner="owner")
        self.assertEqual(handles.fetch(str(second), owner="owner").text, "b")
        self.assertEqual(handles.fetch(str(third), owner="owner").text, "c")

        owner_bytes = self.deterministic_store(owner_max_bytes=5)
        old = owner_bytes.store("aaa", owner="owner", server_alias="remote")
        new = owner_bytes.store("bbb", owner="owner", server_alias="remote")
        with self.assertRaises(ResultNotFound):
            owner_bytes.fetch(str(old), owner="owner")
        self.assertEqual(owner_bytes.fetch(str(new), owner="owner").text, "bbb")
        self.assertLessEqual(owner_bytes.owner_bytes("owner"), 5)

        global_store = self.deterministic_store(global_max_bytes=6)
        oldest = global_store.store("aaaa", owner="a", server_alias="remote")
        newest = global_store.store("bbbb", owner="b", server_alias="remote")
        with self.assertRaises(ResultNotFound):
            global_store.fetch(str(oldest), owner="a")
        self.assertEqual(global_store.fetch(str(newest), owner="b").text, "bbbb")
        self.assertLessEqual(global_store.total_bytes, 6)

    def test_ttl_cross_owner_and_unknown_are_the_same_not_found(self) -> None:
        clock = [100.0]
        store = self.deterministic_store(now=lambda: clock[0], ttl_seconds=5)
        handle = store.store("secret", owner="owner-a", server_alias="remote")
        for owner, candidate in (("owner-b", handle), ("owner-a", "missing")):
            with self.subTest(owner=owner, candidate=candidate):
                with self.assertRaisesRegex(ResultNotFound, "not found"):
                    store.fetch(str(candidate), owner=owner)
        clock[0] = 106.0
        with self.assertRaisesRegex(ResultNotFound, "not found"):
            store.fetch(str(handle), owner="owner-a")

    def test_fetch_uses_unicode_codepoint_offsets_and_clamps_limit(self) -> None:
        store = self.deterministic_store()
        text = "A😀中文B" + ("x" * (RESULT_FETCH_MAX_CODEPOINTS + 100))
        handle = store.store(text, owner="owner", server_alias="remote")
        page = store.fetch(str(handle), owner="owner", offset=1, limit=2)
        self.assertEqual(page.text, "😀中")
        self.assertEqual(page.offset, 1)
        self.assertEqual(page.next_offset, 3)
        self.assertFalse(page.eof)
        clamped = store.fetch(str(handle), owner="owner", offset=5, limit=999_999)
        self.assertEqual(len(clamped.text), RESULT_FETCH_MAX_CODEPOINTS)


class BrokerResultPagingTests(unittest.TestCase):
    def build_runtime(
        self,
        *,
        expose_mode: str = "broker",
        transport: str = "stdio",
        result_store: ResultStore | None = None,
    ) -> tuple[Runtime, FakeUpstreamClient]:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode=expose_mode,
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client], result_store=result_store)
        self.temp = TemporaryDirectory()
        runtime = Runtime(
            Path(self.temp.name),
            upstream_manager=manager,
            transport=transport,
        )
        return runtime, client

    def tearDown(self) -> None:
        temp = getattr(self, "temp", None)
        if temp is not None:
            temp.cleanup()

    def oversized_result(self, *, structured: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "content": [
                {
                    "type": "text",
                    "text": "ORIGINAL_CANARY😀中文" + ("x" * (RESULT_INLINE_MAX * 2)),
                },
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": "BINARY_ORIGINAL_CANARY" + ("A" * RESULT_INLINE_MAX),
                },
            ],
            "isError": False,
        }
        if structured:
            result["structuredContent"] = {"answer": "raw"}
        return result

    def test_broker_stores_raw_before_truncation_and_fetches_it(self) -> None:
        runtime, client = self.build_runtime()
        original = self.oversized_result()
        with patch.object(client, "call_tool_raw", return_value=copy.deepcopy(original)):
            result = runtime.call_tool(
                "upstream_tool_call",
                {"name": "remote__search", "arguments": {"q": "large"}},
            )
        self.assertLessEqual(len(result_json_bytes(result)), RESULT_INLINE_MAX)
        structured = result["structuredContent"]
        self.assertTrue(structured["_truncated"])
        handle = structured["_result_handle"]
        self.assertEqual(structured["_result_fetch_tool"], "upstream_result_fetch")
        self.assertNotIn("BINARY_ORIGINAL_CANARY", json.dumps(result))

        fetched = runtime.call_tool(
            "upstream_result_fetch",
            {"handle": handle, "offset": 0, "limit": 32000},
        )
        self.assertFalse(fetched["isError"])
        page = fetched["structuredContent"]
        self.assertIn("ORIGINAL_CANARY😀中文", page["text"])
        self.assertFalse(page["eof"])
        self.assertGreater(page["next_offset"], page["offset"])
        runtime.close()

    def test_missing_structured_content_still_gets_handle_metadata(self) -> None:
        runtime, client = self.build_runtime()
        with patch.object(
            client,
            "call_tool_raw",
            return_value=self.oversized_result(structured=False),
        ):
            result = runtime.call_tool(
                "upstream_tool_call",
                {"name": "remote__search", "arguments": {"q": "large"}},
            )
        self.assertIn("_result_handle", result["structuredContent"])
        self.assertTrue(result["structuredContent"]["_truncated"])
        runtime.close()

    def test_direct_upstream_and_missing_owner_do_not_store(self) -> None:
        runtime, client = self.build_runtime(expose_mode="direct")
        with patch.object(client, "call_tool_raw", return_value=self.oversized_result()):
            direct = runtime.call_tool("remote__search", {"q": "large"})
        self.assertNotIn("_result_handle", direct["structuredContent"])
        self.assertEqual(runtime.upstream_manager.result_store.total_bytes, 0)
        runtime.close()

        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode="broker",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        with patch.object(client, "call_tool_raw", return_value=self.oversized_result()):
            no_owner = manager.call_tool(
                "remote__search",
                {"q": "large"},
                result_owner=None,
                store_overflow=True,
            )
        self.assertNotIn("_result_handle", no_owner["structuredContent"])
        self.assertEqual(manager.result_store.total_bytes, 0)
        manager.close()

    def test_runtime_fetch_hides_cross_session_expired_and_unknown_handles(self) -> None:
        clock = [100.0]
        store = ResultStore(now=lambda: clock[0], ttl_seconds=5)
        runtime, _client = self.build_runtime(result_store=store)
        owner = runtime.current_result_owner()
        assert owner is not None
        handle = store.store("secret", owner=owner, server_alias="remote")
        other = store.store("other", owner="other-session", server_alias="remote")

        for candidate in (other, "missing"):
            with self.subTest(candidate=candidate):
                result = runtime.call_tool(
                    "upstream_result_fetch",
                    {"handle": candidate},
                )
                self.assertEqual(
                    result["structuredContent"]["error"]["code"],
                    "UPSTREAM_RESULT_NOT_FOUND",
                )
                self.assertNotIn("other-session", json.dumps(result))
        clock[0] = 106.0
        expired = runtime.call_tool("upstream_result_fetch", {"handle": handle})
        self.assertEqual(
            expired["structuredContent"]["error"]["code"],
            "UPSTREAM_RESULT_NOT_FOUND",
        )
        runtime.close()

    def test_fetch_schema_and_runtime_limit_cap(self) -> None:
        runtime, _client = self.build_runtime()
        owner = runtime.current_result_owner()
        assert owner is not None
        handle = runtime.upstream_manager.result_store.store(
            "x" * 40000,
            owner=owner,
            server_alias="remote",
        )
        fetched = runtime.call_tool(
            "upstream_result_fetch",
            {"handle": handle, "limit": RESULT_FETCH_MAX_CODEPOINTS},
        )
        self.assertEqual(
            len(fetched["structuredContent"]["text"]),
            RESULT_FETCH_MAX_CODEPOINTS,
        )
        with self.assertRaises(JsonRpcError):
            runtime.call_tool(
                "upstream_result_fetch",
                {"handle": handle, "limit": RESULT_FETCH_MAX_CODEPOINTS + 1},
            )
        runtime.close()

    def test_stdio_and_http_owners_are_runtime_scoped(self) -> None:
        stdio, _client = self.build_runtime(transport="stdio")
        self.assertTrue(str(stdio.current_result_owner()).startswith("stdio:"))
        first_stdio_owner = stdio.current_result_owner()
        stdio.close()

        http, _client = self.build_runtime(transport="http")
        self.assertEqual(http.current_result_owner(), http.http_session_id)
        self.assertNotEqual(first_stdio_owner, http.current_result_owner())
        http.close()

    def test_fixed_local_directory_contains_all_five_broker_tools(self) -> None:
        expected = {
            "upstream_tool_search",
            "upstream_tool_describe",
            "upstream_tool_call",
            "upstream_tool_call_mutating",
            "upstream_result_fetch",
        }
        self.assertTrue(expected.issubset(TOOL_REGISTRY))
        self.assertTrue(TOOL_REGISTRY["upstream_result_fetch"].read_only)
        self.assertTrue(TOOL_REGISTRY["upstream_result_fetch"].idempotent)


if __name__ == "__main__":
    unittest.main()
