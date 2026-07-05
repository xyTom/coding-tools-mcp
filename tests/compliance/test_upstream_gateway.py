from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import upstream as upstream_module
from coding_tools_mcp.server import Runtime, server_card_payload
from coding_tools_mcp.upstream import (
    BaseUpstreamClient,
    UpstreamConfigError,
    UpstreamManager,
    UpstreamServerConfig,
    parse_server_config,
)


class FakeUpstreamClient(BaseUpstreamClient):
    def __init__(self, config: UpstreamServerConfig, protocol_version: str) -> None:
        super().__init__(config, protocol_version)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def initialize(self) -> None:
        return None

    def list_tools(self) -> list[dict[str, object]]:
        return [
            {
                "name": "search",
                "description": "Search remote repositories.",
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "create_issue",
                "description": "Create an issue.",
                "inputSchema": {"type": "object"},
                "annotations": {"destructiveHint": True},
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, arguments))
        return {
            "content": [{"type": "text", "text": f"called {name}"}],
            "structuredContent": {"ok": True, "remote_name": name, "arguments": arguments},
            "isError": False,
        }

    def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        raise AssertionError("Fake client does not use raw request().")

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        raise AssertionError("Fake client does not use raw notify().")

    def close(self) -> None:
        self.closed = True

    def health_payload(self) -> dict[str, object]:
        return {"transport": self.config.transport, "running": not self.closed}

    def logs_payload(self, *, max_lines: int = 200) -> dict[str, object]:
        return {"lines": ["fake stderr"], "truncated": False, "max_lines": max_lines}


class UpstreamGatewayTests(unittest.TestCase):
    def test_runtime_lists_and_calls_namespaced_upstream_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = UpstreamManager.empty()
            config = UpstreamServerConfig(alias="github", transport="streamable_http", url="http://127.0.0.1/mcp")
            client = FakeUpstreamClient(config, manager.protocol_version)
            manager.configs = [config]
            manager.clients["github"] = client
            manager.tools = {}
            for tool in client.list_tools():
                remote_name = str(tool["name"])
                public_name = f"github__{remote_name}"
                manager.tools[public_name] = manager_tool(config.alias, public_name, remote_name, tool)

            runtime = Runtime(Path(tmp), upstream_manager=manager)
            tool_names = {tool["name"] for tool in runtime.list_tools()["tools"]}

            self.assertIn("server_info", tool_names)
            self.assertIn("github__search", tool_names)
            self.assertIn("github__create_issue", tool_names)

            result = runtime.call_tool("github__search", {"q": "mcp"})

            self.assertFalse(result.get("isError"))
            self.assertEqual(result.get("structuredContent", {}).get("remote_name"), "search")
            self.assertEqual(client.calls, [("search", {"q": "mcp"})])

    def test_runtime_preserves_nested_upstream_tool_names(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = UpstreamManager.empty()
            config = UpstreamServerConfig(alias="outer", transport="streamable_http", url="http://127.0.0.1/mcp")
            client = FakeUpstreamClient(config, manager.protocol_version)
            nested_tool = {
                "name": "inner__search",
                "description": "Search through a nested upstream gateway.",
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                "annotations": {"readOnlyHint": True},
            }
            public_name = "outer__inner__search"
            manager.configs = [config]
            manager.clients["outer"] = client
            manager.tools[public_name] = manager_tool(config.alias, public_name, "inner__search", nested_tool)

            runtime = Runtime(Path(tmp), upstream_manager=manager)
            tool_names = {tool["name"] for tool in runtime.list_tools()["tools"]}

            self.assertIn(public_name, tool_names)
            result = runtime.call_tool(public_name, {"q": "mcp"})

            self.assertFalse(result.get("isError"))
            self.assertEqual(result.get("structuredContent", {}).get("remote_name"), "inner__search")
            self.assertEqual(client.calls, [("inner__search", {"q": "mcp"})])

    def test_read_only_profile_hides_non_read_only_upstream_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = fake_manager()
            runtime = Runtime(Path(tmp), tool_profile="read-only", upstream_manager=manager)

            tool_names = set(runtime.exposed_tool_names())

            self.assertIn("github__search", tool_names)
            self.assertNotIn("github__create_issue", tool_names)

    def test_compat_readonly_all_marks_upstream_tools_read_only(self) -> None:
        manager = fake_manager()

        definitions = manager.tool_definitions(tool_profile="compat-readonly-all")

        self.assertTrue(definitions)
        self.assertTrue(all(tool["annotations"].get("readOnlyHint") is True for tool in definitions))
        self.assertTrue(all(tool["annotations"].get("destructiveHint") is False for tool in definitions))

    def test_parse_server_config_rejects_reserved_alias_separator(self) -> None:
        with self.assertRaises(UpstreamConfigError):
            parse_server_config("bad__alias", {"transport": "streamable_http", "url": "http://127.0.0.1/mcp"})

    def test_server_info_reports_upstream_status(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = fake_manager()
            runtime = Runtime(Path(tmp), upstream_manager=manager)

            upstream = runtime.server_info_payload()["upstream"]

            self.assertEqual(upstream["server_count"], 1)
            self.assertEqual(upstream["tool_count"], 2)

    def test_server_card_includes_upstream_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), upstream_manager=fake_manager())

            card = server_card_payload(runtime)

            self.assertIn("github__search", card["tools"]["names"])
            self.assertIn("github__search", card["tools"]["readOnlyHintTrue"])

    def test_upstream_manager_lifecycle_health_and_logs(self) -> None:
        config = UpstreamServerConfig(alias="github", transport="stdio", command="uvx", args=("mcp-github",))
        clients: list[FakeUpstreamClient] = []

        def fake_build_client(
            config: UpstreamServerConfig,
            protocol_version: str,
            secret_resolver: object | None = None,
        ) -> FakeUpstreamClient:
            client = FakeUpstreamClient(config, protocol_version)
            clients.append(client)
            return client

        with patch.object(upstream_module, "build_client", side_effect=fake_build_client):
            manager = UpstreamManager([config])

            self.assertIn("github__search", manager.tool_names(tool_profile="full"))
            self.assertEqual(manager.health_payload()["running_count"], 1)
            self.assertEqual(manager.logs_payload("github")["servers"][0]["logs"]["lines"], ["fake stderr"])

            filtered = manager.health_payload("github")
            self.assertEqual(filtered["server_count"], 1)
            self.assertEqual(filtered["servers"][0]["alias"], "github")

            missing = manager.health_payload("missing")
            self.assertEqual(missing["servers"][0]["error"]["code"], "UPSTREAM_NOT_FOUND")

            stopped = manager.stop_server("github")
            self.assertTrue(stopped["ok"])
            self.assertTrue(clients[0].closed)
            self.assertNotIn("github__search", manager.tool_names(tool_profile="full"))
            self.assertEqual(manager.health_payload()["running_count"], 0)

            restarted = manager.start_server("github")
            self.assertTrue(restarted["ok"])
            self.assertIn("github__search", manager.tool_names(tool_profile="full"))
            self.assertEqual(manager.health_payload()["running_count"], 1)


def fake_manager() -> UpstreamManager:
    manager = UpstreamManager.empty()
    config = UpstreamServerConfig(alias="github", transport="streamable_http", url="http://127.0.0.1/mcp")
    client = FakeUpstreamClient(config, manager.protocol_version)
    manager.configs = [config]
    manager.clients["github"] = client
    manager.statuses["github"] = type(
        "Status",
        (),
        {"payload": lambda self: {"alias": "github", "transport": "streamable_http", "enabled": True, "initialized": True, "tool_count": 2}, "initialized": True},
    )()
    for tool in client.list_tools():
        remote_name = str(tool["name"])
        public_name = f"github__{remote_name}"
        manager.tools[public_name] = manager_tool(config.alias, public_name, remote_name, tool)
    return manager


def manager_tool(alias: str, public_name: str, remote_name: str, definition: dict[str, object]):
    from coding_tools_mcp.upstream import UpstreamTool, namespaced_tool_definition

    return UpstreamTool(
        public_name=public_name,
        remote_name=remote_name,
        definition=namespaced_tool_definition(alias, public_name, definition),
    )


if __name__ == "__main__":
    unittest.main()
