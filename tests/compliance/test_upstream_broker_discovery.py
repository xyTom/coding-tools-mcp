from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp.server import Runtime, TOOL_REGISTRY, input_schemas
from coding_tools_mcp.upstream import UpstreamManager, UpstreamServerConfig
from tests.compliance.test_upstream_gateway import FakeUpstreamClient, build_manager


class UpstreamBrokerDiscoveryTests(unittest.TestCase):
    def test_fixed_discovery_tools_exist_with_empty_catalog(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), upstream_manager=UpstreamManager.empty())
            initialized = runtime.initialize({"name": "test", "version": "1"})
            names = {tool["name"] for tool in runtime.list_tools()["tools"]}

            self.assertIn("upstream_tool_search", names)
            self.assertIn("upstream_tool_describe", names)
            self.assertFalse(initialized["capabilities"]["tools"]["listChanged"])
            self.assertIn("upstream_tool_search", initialized["instructions"])

            search = runtime.call_tool("upstream_tool_search", {"query": "anything"})
            self.assertFalse(search["isError"])
            self.assertEqual(search["structuredContent"]["results"], [])
            self.assertEqual(search["structuredContent"]["count"], 0)

            describe = runtime.call_tool(
                "upstream_tool_describe",
                {"name": "missing__tool"},
            )
            self.assertTrue(describe["isError"])
            self.assertEqual(
                describe["structuredContent"]["error"]["code"],
                "UPSTREAM_TOOL_NOT_FOUND",
            )
            runtime.close()

    def test_discovery_tools_are_real_readonly_idempotent_fixed_entries(self) -> None:
        for name in ("upstream_tool_search", "upstream_tool_describe"):
            spec = TOOL_REGISTRY[name]
            self.assertTrue(spec.read_only)
            self.assertTrue(spec.idempotent)
            self.assertFalse(spec.destructive)
        self.assertNotIn("tool_profile", input_schemas()["upstream_tool_search"]["properties"])
        self.assertNotIn("tool_profile", input_schemas()["upstream_tool_describe"]["properties"])

    def test_search_and_describe_use_only_the_frozen_public_catalog(self) -> None:
        config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode="broker",
            tags=("code", "remote"),
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), upstream_manager=manager)
            names = {tool["name"] for tool in runtime.list_tools()["tools"]}
            self.assertNotIn("github__search", names)
            self.assertNotIn("github__create_issue", names)

            search = runtime.call_tool(
                "upstream_tool_search",
                {
                    "query": "search repositories",
                    "server": "github",
                    "read_only": True,
                    "tags": ["code"],
                    "name_prefix": "github__",
                    "limit": 5,
                },
            )
            payload = search["structuredContent"]
            self.assertFalse(search["isError"])
            self.assertEqual(payload["results"][0]["name"], "github__search")
            encoded_search = json.dumps(payload)
            self.assertNotIn("inputSchema", encoded_search)
            self.assertNotIn("public_definition", encoded_search)
            self.assertRegex(payload["results"][0]["schema_digest"], r"^[0-9a-f]{32}$")

            mutating_filtered = runtime.call_tool(
                "upstream_tool_search",
                {"query": "create issue", "read_only": True},
            )
            self.assertEqual(mutating_filtered["structuredContent"]["results"], [])

            describe = runtime.call_tool(
                "upstream_tool_describe",
                {"name": "github__search"},
            )
            description = describe["structuredContent"]
            self.assertFalse(describe["isError"])
            self.assertEqual(description["risk"], "readonly")
            self.assertEqual(description["tags"], ["code", "remote"])
            self.assertEqual(description["definition"]["name"], "github__search")
            self.assertNotIn("$defs", description["definition"]["inputSchema"])
            self.assertNotIn("raw_definition", json.dumps(description))
            self.assertRegex(description["schema_digest"], r"^[0-9a-f]{32}$")
            runtime.close()

    def test_two_runtimes_have_identical_fixed_local_catalog_with_different_upstreams(self) -> None:
        first_config = UpstreamServerConfig(
            alias="first",
            transport="streamable_http",
            url="http://127.0.0.1/first",
            expose_mode="broker",
        )
        second_config = UpstreamServerConfig(
            alias="second",
            transport="streamable_http",
            url="http://127.0.0.1/second",
            expose_mode="broker",
        )
        first = build_manager(
            [first_config],
            [FakeUpstreamClient(first_config, "2025-11-25")],
        )
        second = build_manager(
            [second_config],
            [FakeUpstreamClient(second_config, "2025-11-25")],
        )
        with TemporaryDirectory() as first_tmp, TemporaryDirectory() as second_tmp:
            first_runtime = Runtime(Path(first_tmp), upstream_manager=first)
            second_runtime = Runtime(Path(second_tmp), upstream_manager=second)
            first_names = {tool["name"] for tool in first_runtime.list_tools()["tools"]}
            second_names = {tool["name"] for tool in second_runtime.list_tools()["tools"]}
            self.assertEqual(first_names, second_names)
            self.assertEqual(first_names, set(TOOL_REGISTRY))

            first_result = first_runtime.call_tool(
                "upstream_tool_search",
                {"query": "search"},
            )
            second_result = second_runtime.call_tool(
                "upstream_tool_search",
                {"query": "search"},
            )
            self.assertEqual(
                first_result["structuredContent"]["results"][0]["server"],
                "first",
            )
            self.assertEqual(
                second_result["structuredContent"]["results"][0]["server"],
                "second",
            )
            first_runtime.close()
            second_runtime.close()

    def test_search_order_is_deterministic(self) -> None:
        config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode="broker",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), upstream_manager=manager)
            first = runtime.call_tool(
                "upstream_tool_search",
                {"query": "remote", "limit": 20},
            )["structuredContent"]["results"]
            second = runtime.call_tool(
                "upstream_tool_search",
                {"query": "remote", "limit": 20},
            )["structuredContent"]["results"]
            self.assertEqual(first, second)
            runtime.close()


if __name__ == "__main__":
    unittest.main()
