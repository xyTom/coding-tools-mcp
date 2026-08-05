from __future__ import annotations

import copy
import inspect
import json
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import Any, cast

from coding_tools_mcp import upstream as upstream_module
from coding_tools_mcp.json_utils import strict_json_bytes, strict_json_loads
from coding_tools_mcp.server import (
    AuthorizationContext,
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    TOOL_REGISTRY,
    build_parser,
    json_response_payload,
    load_upstream_startup,
    server_card_payload,
)
from coding_tools_mcp.upstream import (
    BaseUpstreamClient,
    HttpUpstreamClient,
    UpstreamConfigError,
    UpstreamError,
    UpstreamManager,
    UpstreamServerConfig,
    UpstreamStatus,
    base_upstream_environment,
    _rpc_result,
    decode_http_rpc_response,
    error_payload,
    load_upstream_config_snapshot,
    normalize_tool_result,
    parse_server_config,
    resolve_env_config,
    upstream_error_result,
)
from coding_tools_mcp.upstream_result import RESULT_INLINE_MAX, result_json_bytes
from coding_tools_mcp.upstream_result_store import ResultStore
from coding_tools_mcp.upstream_search import ToolSearchFilters
from coding_tools_mcp.workspace_binding import WorkspaceBinding


REMOTE_TOOLS = [
    {
        "name": "search",
        "title": "Remote Search",
        "description": "Search remote repositories.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "$defs": {"query": {"type": "string"}},
        },
        "outputSchema": {
            "type": "object",
            "properties": {"hits": {"type": "array"}},
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue on the remote service.",
        "inputSchema": {"type": "object", "additionalProperties": True},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


def nested_dict_with_levels(levels: int) -> dict[str, Any]:
    """Build exactly `levels` dict containers; the leaf is a scalar."""

    if levels < 1:
        raise ValueError("levels must be at least 1")
    value: Any = "leaf"
    for _ in range(levels):
        value = {"next": value}
    return cast(dict[str, Any], value)


def rpc_error_with_total_levels(total_levels: int) -> dict[str, Any]:
    """Build a JSON-RPC error with exactly `total_levels` container levels."""

    if total_levels < 1:
        raise ValueError("total_levels must be at least 1")
    data: Any = "leaf"
    for _ in range(total_levels - 1):
        data = {"next": data}
    return {
        "code": -32001,
        "message": "boundary",
        "data": data,
    }


class FakeUpstreamClient(BaseUpstreamClient):
    def __init__(
        self,
        config: UpstreamServerConfig,
        protocol_version: str,
        *,
        tools: list[dict[str, object]] | None = None,
        behavior: str = "success",
        marker: str = "remote",
    ) -> None:
        super().__init__(config, protocol_version)
        self.tools = copy.deepcopy(tools if tools is not None else REMOTE_TOOLS)
        self.behavior = behavior
        self.marker = marker
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.close_calls = 0

    def initialize(self) -> None:
        return None

    def list_tools(self) -> list[dict[str, object]]:
        return copy.deepcopy(self.tools)

    def call_tool_raw(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, copy.deepcopy(arguments)))
        if self.behavior == "timeout":
            raise UpstreamError(
                "UPSTREAM_TIMEOUT",
                "Timed out waiting for upstream MCP server.",
                retryable=True,
            )
        if self.behavior == "disconnect":
            raise UpstreamError(
                "UPSTREAM_DISCONNECTED",
                "Upstream MCP server disconnected.",
                retryable=True,
            )
        if self.behavior == "rpc_error":
            raise UpstreamError(
                "UPSTREAM_RPC_ERROR",
                "Remote tool rejected the request.",
                category="upstream",
                details={"method": "tools/call", "rpc_error": {"code": -32001, "message": "denied"}},
            )
        if self.behavior == "protocol":
            raise UpstreamError(
                "UPSTREAM_PROTOCOL_ERROR",
                "Upstream response envelope was invalid.",
                category="protocol",
            )
        return {
            "content": [
                {"type": "text", "text": f"called {name}"},
                {"type": "resource_link", "uri": "https://remote.example/item/1", "name": "item"},
            ],
            "structuredContent": {
                "ok": True,
                "marker": self.marker,
                "remote_name": name,
                "arguments": copy.deepcopy(arguments),
            },
            "isError": False,
        }

    def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        raise AssertionError("Fake client does not use raw request().")

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        raise AssertionError("Fake client does not use raw notify().")

    def _close_transport(self) -> None:
        self.close_calls += 1


class DeepDiscoveryClient(BaseUpstreamClient):
    """Exercise the production BaseUpstreamClient.list_tools boundary."""

    def __init__(
        self,
        config: UpstreamServerConfig,
        tools: list[dict[str, object]],
    ) -> None:
        super().__init__(config, "2025-11-25")
        self.tools = tools
        self.close_calls = 0

    def initialize(self) -> None:
        return None

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del params
        if method != "tools/list":
            raise AssertionError(method)
        return {"tools": self.tools}

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        del method, params
        raise AssertionError("Deep discovery client does not send notifications.")

    def _close_transport(self) -> None:
        self.close_calls += 1


def build_manager(
    configs: list[UpstreamServerConfig],
    clients: list[FakeUpstreamClient],
    *,
    reserved_names: set[str] | None = None,
    custom_synonyms: dict[str, tuple[str, ...]] | None = None,
    result_store: ResultStore | None = None,
) -> UpstreamManager:
    pending = list(clients)

    def fake_build_client(
        config: UpstreamServerConfig,
        protocol_version: str,
        secret_resolver: object | None = None,
    ) -> FakeUpstreamClient:
        del secret_resolver
        if not pending:
            raise AssertionError(f"Unexpected client creation for {config.alias}")
        client = pending.pop(0)
        self_config = client.config
        if self_config.alias != config.alias:
            raise AssertionError(f"Expected {self_config.alias}, got {config.alias}")
        return client

    with patch.object(upstream_module, "build_client", side_effect=fake_build_client):
        manager = UpstreamManager(
            configs,
            reserved_names=reserved_names or set(TOOL_REGISTRY),
            custom_synonyms=custom_synonyms,
            result_store=result_store,
        )
    return manager


class UpstreamGatewayTests(unittest.TestCase):
    def test_runtime_exposes_sanitized_schema_annotations_and_structured_content(self) -> None:
        with TemporaryDirectory() as tmp:
            config = UpstreamServerConfig(
                alias="github",
                transport="streamable_http",
                url="http://127.0.0.1/mcp",
            )
            client = FakeUpstreamClient(config, "2025-11-25")
            manager = build_manager([config], [client])
            runtime = Runtime(Path(tmp), upstream_manager=manager)

            definitions = {item["name"]: item for item in runtime.list_tools()["tools"]}
            remote = definitions["github__search"]
            original = REMOTE_TOOLS[0]

            self.assertIn("server_info", definitions)
            self.assertEqual(remote["title"], original["title"])
            self.assertEqual(remote["description"], original["description"])
            self.assertEqual(
                remote["inputSchema"],
                {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            )
            self.assertNotIn("$defs", remote["inputSchema"])
            self.assertEqual(remote["outputSchema"], original["outputSchema"])
            self.assertEqual(remote["annotations"], original["annotations"])
            registered = manager.state.all_tools["github__search"]
            self.assertIn("$defs", registered.raw_definition["inputSchema"])
            self.assertNotIn("$defs", registered.public_definition["inputSchema"])
            self.assertEqual(registered.effective_risk, "readonly")
            self.assertRegex(registered.public_schema_digest, r"^[0-9a-f]{32}$")
            self.assertRegex(registered.raw_schema_digest or "", r"^[0-9a-f]{32}$")
            frozen_public = cast(Any, registered.public_definition)
            frozen_required = cast(Any, frozen_public["inputSchema"]["required"])
            with self.assertRaises(TypeError):
                frozen_public["title"] = "changed"
            with self.assertRaises(TypeError):
                frozen_required.append("other")
            with self.assertRaises(TypeError):
                dict.__setitem__(frozen_public, "bypass", True)
            with self.assertRaises(TypeError):
                list.__setitem__(frozen_required, 0, "bypass")
            copied_public = copy.deepcopy(registered.public_definition)
            copied_public["title"] = "changed"
            self.assertNotEqual(copied_public["title"], registered.public_definition["title"])

            result = runtime.call_tool("github__search", {"q": "mcp"})

            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["remote_name"], "search")
            self.assertEqual(result["content"][1]["type"], "resource_link")
            self.assertEqual(client.calls, [("search", {"q": "mcp"})])
            runtime.close()
            self.assertTrue(client.closed)

    def test_deep_schema_discovery_reaches_sanitizer_without_recursive_copy(self) -> None:
        nested: dict[str, object] = {"type": "string"}
        for _ in range(500):
            nested = {"not": nested}
        tool: dict[str, object] = {
            "name": "deep_schema",
            "description": "A deeply nested but parseable Schema.",
            "inputSchema": {
                "type": "object",
                "properties": {"value": nested},
            },
            "annotations": {"readOnlyHint": True},
        }
        config = UpstreamServerConfig(
            alias="deep",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = DeepDiscoveryClient(config, [tool])

        manager = build_manager([config], [client])  # type: ignore[list-item]

        self.assertEqual(manager.tool_names(), ["deep__deep_schema"])
        registered = manager.state.all_tools["deep__deep_schema"]
        self.assertEqual(registered.public_definition["name"], "deep__deep_schema")
        public_value = registered.public_definition["inputSchema"]["properties"]["value"]
        self.assertIsInstance(public_value, Mapping)
        self.assertLessEqual(len(repr(public_value)), 2_000)
        self.assertRegex(registered.raw_schema_digest or "", r"^[0-9a-f]{32}$")

        copied_raw = copy.deepcopy(registered.raw_definition)
        copied_public = copy.deepcopy(registered.public_definition)
        self.assertIsInstance(copied_raw, dict)
        self.assertIsInstance(copied_public, dict)
        raw_value = copied_raw["inputSchema"]["properties"]["value"]
        for _ in range(500):
            self.assertIsInstance(raw_value, dict)
            raw_value = raw_value["not"]
        self.assertEqual(raw_value, {"type": "string"})
        copied_raw["description"] = "mutable export"
        copied_public["description"] = "mutable export"
        self.assertNotEqual(
            copied_raw["description"],
            registered.raw_definition["description"],
        )
        self.assertNotEqual(
            copied_public["description"],
            registered.public_definition["description"],
        )
        self.assertTrue(manager.statuses["deep"].initialized)
        manager.close()
        self.assertEqual(client.close_calls, 1)

    def test_nested_namespace_is_stable_and_local_names_remain_reserved(self) -> None:
        nested = copy.deepcopy(REMOTE_TOOLS[0])
        nested["name"] = "inner__search"
        config = UpstreamServerConfig(
            alias="outer",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25", tools=[nested])
        manager = build_manager([config], [client])
        self.assertEqual(manager.tool_names(), ["outer__inner__search"])

        collision_client = FakeUpstreamClient(config, "2025-11-25", tools=[nested])
        with self.assertRaisesRegex(UpstreamConfigError, "namespace collision"):
            build_manager(
                [config],
                [collision_client],
                reserved_names={"outer__inner__search"},
            )
        self.assertTrue(collision_client.closed)

    def test_registry_is_not_published_partially_when_later_config_collides(self) -> None:
        first_config = UpstreamServerConfig(
            alias="first",
            transport="streamable_http",
            url="http://127.0.0.1/first",
        )
        second_config = UpstreamServerConfig(
            alias="outer",
            transport="streamable_http",
            url="http://127.0.0.1/outer",
        )
        nested = copy.deepcopy(REMOTE_TOOLS[0])
        nested["name"] = "inner__search"
        first_client = FakeUpstreamClient(first_config, "2025-11-25", tools=[REMOTE_TOOLS[0]])
        second_client = FakeUpstreamClient(second_config, "2025-11-25", tools=[nested])

        with self.assertRaisesRegex(UpstreamConfigError, "namespace collision"):
            build_manager(
                [first_config, second_config],
                [first_client, second_client],
                reserved_names={"outer__inner__search"},
            )

        self.assertEqual(first_client.close_calls, 1)
        self.assertEqual(second_client.close_calls, 1)

    def test_registry_state_is_read_only_and_close_is_idempotent(self) -> None:
        config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        state = manager.state

        self.assertEqual(state.direct_tool_names, ("github__search", "github__create_issue"))
        self.assertEqual(list(state.all_tools), list(state.direct_tool_names))
        self.assertEqual(list(state.clients), ["github"])
        self.assertEqual(list(state.catalog), list(state.direct_tool_names))
        self.assertIsNotNone(state.search_index)
        with self.assertRaises(TypeError):
            state.all_tools["other"] = state.all_tools["github__search"]  # type: ignore[index]
        with self.assertRaises(TypeError):
            state.clients["other"] = client  # type: ignore[index]
        with self.assertRaises(TypeError):
            state.catalog["other"] = {}  # type: ignore[index]

        manager.close()
        manager.close()

        self.assertEqual(client.close_calls, 1)
        self.assertEqual(manager.tool_names(), ["github__search", "github__create_issue"])
        closed_result = manager.call_tool("github__search", {"q": "after-close"})
        self.assertEqual(closed_result["structuredContent"]["error"]["code"], "UPSTREAM_NOT_AVAILABLE")

    def test_invalid_remote_metadata_degrades_without_leaking_raw_values(self) -> None:
        invalid_tool = {
            "name": "invalid_metadata",
            "title": 42,
            "description": ["not", "text"],
            "inputSchema": "not-an-object",
            "outputSchema": ["not", "an", "object"],
            "annotations": "not-an-object",
            "privateInstructions": "do not expose",
        }
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25", tools=[invalid_tool])
        manager = build_manager([config], [client])

        public = manager.tool_definitions()[0]
        registered = manager.state.all_tools["remote__invalid_metadata"]
        self.assertEqual(
            public,
            {
                "name": "remote__invalid_metadata",
                "inputSchema": {"type": "object", "additionalProperties": True},
            },
        )
        self.assertEqual(registered.raw_definition["title"], 42)
        self.assertIn("privateInstructions", registered.raw_definition)
        self.assertNotIn("privateInstructions", registered.public_definition)
        self.assertEqual(registered.effective_risk, "mutating")
        self.assertIsNone(registered.raw_schema_digest)
        manager.close()

    def test_direct_upstream_call_enforces_the_final_result_budget(self) -> None:
        config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        raw_result = {
            "content": [
                {"type": "text", "text": "direct-output-" * 30_000},
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": "direct-image-payload-" * 20_000,
                },
            ],
            "isError": True,
        }

        with patch.object(client, "call_tool_raw", return_value=raw_result) as raw_call:
            result = manager.call_tool("github__search", {"q": "large"})

        raw_call.assert_called_once_with("search", {"q": "large"})
        self.assertLessEqual(len(result_json_bytes(result)), RESULT_INLINE_MAX)
        self.assertIs(result["isError"], True)
        self.assertTrue(result["structuredContent"]["_truncated"])
        self.assertNotIn("_result_handle", json.dumps(result))
        self.assertNotIn("direct-image-payload-", json.dumps(result))
        manager.close()

    def test_large_upstream_error_envelope_is_budgeted(self) -> None:
        config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        failure = UpstreamError(
            "UPSTREAM_RPC_ERROR",
            "remote failure " * 20_000,
            category="upstream",
            details={"rpc_error": {"data": "error-payload-" * 30_000}},
        )

        with patch.object(client, "call_tool_raw", side_effect=failure):
            result = manager.call_tool("github__search", {"q": "large-error"})

        self.assertLessEqual(len(result_json_bytes(result)), RESULT_INLINE_MAX)
        self.assertIs(result["isError"], True)
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "UPSTREAM_RPC_ERROR",
        )
        self.assertTrue(result["structuredContent"]["_truncated"])
        self.assertNotIn("_result_handle", json.dumps(result))
        manager.close()

    def test_local_tool_policy_sets_effective_risk_without_rewriting_annotations(self) -> None:
        config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            tool_policy={"search": "mutating", "create_issue": "readonly"},
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])

        search = manager.state.all_tools["github__search"]
        create_issue = manager.state.all_tools["github__create_issue"]
        self.assertEqual(search.effective_risk, "mutating")
        self.assertEqual(create_issue.effective_risk, "readonly")
        self.assertEqual(
            search.public_definition["annotations"],
            REMOTE_TOOLS[0]["annotations"],
        )
        self.assertEqual(
            create_issue.public_definition["annotations"],
            REMOTE_TOOLS[1]["annotations"],
        )
        manager.close()

    def test_fake_readonly_never_rewrites_upstream_annotations(self) -> None:
        with TemporaryDirectory() as tmp:
            config = UpstreamServerConfig(
                alias="github",
                transport="streamable_http",
                url="http://127.0.0.1/mcp",
            )
            client = FakeUpstreamClient(config, "2025-11-25")
            manager = build_manager([config], [client])
            runtime = Runtime(
                Path(tmp),
                permission_mode="dangerous",
                fake_readonly_annotations=True,
                upstream_manager=manager,
            )
            definitions = {item["name"]: item for item in runtime.list_tools()["tools"]}

            self.assertTrue(definitions["exec_command"]["annotations"]["readOnlyHint"])
            self.assertEqual(
                definitions["github__create_issue"]["annotations"],
                REMOTE_TOOLS[1]["annotations"],
            )
            self.assertEqual(
                manager.state.all_tools["github__create_issue"].effective_risk,
                "mutating",
            )
            card = server_card_payload(runtime)
            self.assertIn("github__create_issue", card["tools"]["readOnlyHintFalse"])
            runtime.close()

    def test_http_client_roundtrips_against_strict_project_server(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_runtime = Runtime(root, transport="http")

            def runtime_factory(context: AuthorizationContext) -> Runtime:
                return Runtime(
                    root,
                    transport="http",
                    authorization_context=context,
                )

            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                control_runtime,
                runtime_factory,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            config = UpstreamServerConfig(
                alias="self",
                transport="streamable_http",
                url=f"http://127.0.0.1:{port}/mcp",
            )
            client = HttpUpstreamClient(config, "2025-11-25")
            try:
                client.initialize()
                self.assertIsNotNone(client.session_id)
                tools = client.list_tools()
                self.assertTrue(any(tool.get("name") == "server_info" for tool in tools))
            finally:
                client.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_local_json_response_rejects_nonfinite_numbers(self) -> None:
        with self.assertRaises(ValueError):
            json_response_payload({"value": float("nan")})

    def test_nonfinite_upstream_json_is_rejected(self) -> None:
        payload = b'{"jsonrpc":"2.0","id":1,"result":{"value":NaN}}'
        with self.assertRaises(UpstreamError) as raised:
            decode_http_rpc_response(
                payload,
                "application/json",
                expected_id=1,
            )
        self.assertEqual(raised.exception.code, "UPSTREAM_PROTOCOL_ERROR")

        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        bad_result = {
            "content": [],
            "structuredContent": {"value": float("nan")},
            "isError": False,
        }
        with patch.object(client, "call_tool_raw", return_value=bad_result):
            result = manager.call_tool("remote__search", {"q": "nan"})
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "UPSTREAM_PROTOCOL_ERROR",
        )
        manager.close()

    def test_http_transport_maps_timeout_disconnect_and_connection_failure(self) -> None:
        config = UpstreamServerConfig(
            alias="http",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = HttpUpstreamClient(config, "2025-11-25")
        cases = (
            (TimeoutError("timed out"), "UPSTREAM_TIMEOUT"),
            (upstream_module.RemoteDisconnected("closed"), "UPSTREAM_DISCONNECTED"),
            (upstream_module.urllib.error.URLError("refused"), "UPSTREAM_CONNECTION_FAILED"),
        )
        for failure, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with patch.object(
                    upstream_module.urllib.request,
                    "urlopen",
                    side_effect=failure,
                ):
                    with self.assertRaises(UpstreamError) as caught:
                        client.request("tools/list", {})
                self.assertEqual(caught.exception.code, expected_code)
                self.assertTrue(caught.exception.retryable)

    def test_timeout_disconnect_rpc_and_protocol_errors_are_structured(self) -> None:
        for behavior, expected_code, expected_category in (
            ("timeout", "UPSTREAM_TIMEOUT", "runtime"),
            ("disconnect", "UPSTREAM_DISCONNECTED", "runtime"),
            ("rpc_error", "UPSTREAM_RPC_ERROR", "upstream"),
            ("protocol", "UPSTREAM_PROTOCOL_ERROR", "protocol"),
        ):
            with self.subTest(behavior=behavior):
                config = UpstreamServerConfig(
                    alias=behavior,
                    transport="streamable_http",
                    url="http://127.0.0.1/mcp",
                )
                client = FakeUpstreamClient(config, "2025-11-25", behavior=behavior)
                manager = build_manager([config], [client])

                result = manager.call_tool(f"{behavior}__search", {"q": "mcp"})

                self.assertTrue(result["isError"])
                structured = result["structuredContent"]
                self.assertFalse(structured["ok"])
                self.assertEqual(structured["error"]["code"], expected_code)
                self.assertEqual(structured["error"]["category"], expected_category)
                self.assertEqual(structured["upstream_alias"], behavior)

    def test_snapshot_and_list_changed_false_remain_true_after_remote_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            config = UpstreamServerConfig(
                alias="github",
                transport="streamable_http",
                url="http://127.0.0.1/mcp",
            )
            client = FakeUpstreamClient(config, "2025-11-25")
            runtime = Runtime(Path(tmp), upstream_manager=build_manager([config], [client]))
            first = runtime.list_tools()

            client.tools.clear()
            client.tools.append(
                {
                    "name": "new_after_initialize",
                    "inputSchema": {"type": "object"},
                    "annotations": {"readOnlyHint": True},
                }
            )
            second = runtime.list_tools()
            initialized = runtime.initialize({"name": "test", "version": "1"})

            self.assertEqual(first, second)
            self.assertFalse(initialized["capabilities"]["tools"]["listChanged"])
            self.assertNotIn("github__new_after_initialize", runtime.exposed_tool_names())
            self.assertFalse(hasattr(runtime.upstream_manager, "start_server"))
            self.assertFalse(hasattr(runtime.upstream_manager, "stop_server"))
            runtime.close()

    def test_enable_state_and_allowlist_are_applied_before_initialization(self) -> None:
        enabled = UpstreamServerConfig(
            alias="enabled",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            include_tools=("search",),
        )
        disabled = UpstreamServerConfig(
            alias="disabled",
            transport="streamable_http",
            enabled=False,
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(enabled, "2025-11-25")
        manager = build_manager([enabled, disabled], [client])

        self.assertEqual(manager.tool_names(), ["enabled__search"])
        status = manager.status_payload()
        disabled_status = next(item for item in status["servers"] if item["alias"] == "disabled")
        self.assertFalse(disabled_status["initialized"])
        self.assertEqual(status["tool_count"], 1)
        self.assertTrue(status["snapshot_immutable"])
        self.assertEqual(status["remote_capability_boundary"], "upstream_server")

    def test_two_runtimes_do_not_share_upstream_client_state_or_session_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = UpstreamServerConfig(
                alias="remote",
                transport="streamable_http",
                url="http://127.0.0.1/mcp",
            )
            first_client = FakeUpstreamClient(config, "2025-11-25", marker="first")
            second_client = FakeUpstreamClient(config, "2025-11-25", marker="second")
            first_binding = WorkspaceBinding("workspace-a", root, "oauth")
            second_binding = WorkspaceBinding("workspace-a", root, "oauth")
            first_context = AuthorizationContext("oauth")
            second_context = AuthorizationContext("oauth")
            first = Runtime(
                root,
                workspace_binding=first_binding,
                authorization_context=first_context,
                upstream_manager=build_manager([config], [first_client]),
            )
            second = Runtime(
                root,
                workspace_binding=second_binding,
                authorization_context=second_context,
                upstream_manager=build_manager([config], [second_client]),
            )
            first_key = first.session_authorization_key()
            second_key = second.session_authorization_key()

            first_result = first.call_tool("remote__search", {"q": "one"})
            second_result = second.call_tool("remote__search", {"q": "two"})

            self.assertEqual(first_result["structuredContent"]["marker"], "first")
            self.assertEqual(second_result["structuredContent"]["marker"], "second")
            self.assertEqual(first_client.calls, [("search", {"q": "one"})])
            self.assertEqual(second_client.calls, [("search", {"q": "two"})])
            self.assertEqual(first.session_authorization_key(), first_key)
            self.assertEqual(second.session_authorization_key(), second_key)
            first.close()
            second.close()

    def test_startup_loads_default_and_explicit_gateway_config_before_runtime(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            default_path = root / "mcp-servers.json"
            default_path.write_text(
                json.dumps(
                    {
                        "servers": {
                            "default": {
                                "transport": "streamable_http",
                                "url": "http://127.0.0.1/default",
                                "enabled": False,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                upstream_module.os.environ,
                {"CODING_TOOLS_MCP_UPSTREAM_CONFIG": ""},
                clear=False,
            ):
                args = build_parser().parse_args([])
                snapshot = load_upstream_startup(args, root)
            self.assertEqual(snapshot.configs[0].alias, "default")

            explicit_path = root / "explicit.json"
            explicit_path.write_text(
                json.dumps(
                    {
                        "servers": {
                            "explicit": {
                                "transport": "streamable_http",
                                "url": "http://127.0.0.1/explicit",
                                "enabled": False,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            args = build_parser().parse_args(["--upstream-config", str(explicit_path)])
            explicit = load_upstream_startup(args, root)
            self.assertEqual(explicit.configs[0].alias, "explicit")

            missing = build_parser().parse_args(
                ["--upstream-config", str(root / "missing.json")]
            )
            with self.assertRaises(UpstreamConfigError):
                load_upstream_startup(missing, root)

    def test_config_is_strict_and_contains_no_runtime_profile_control(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp-servers.json"
            path.write_text(
                json.dumps(
                    {
                        "servers": {
                            "github": {
                                "transport": "streamable_http",
                                "url": "http://127.0.0.1/mcp",
                                "enabled": True,
                                "include_tools": ["search"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            snapshot = load_upstream_config_snapshot(path)

        self.assertEqual(snapshot.configs[0].include_tools, ("search",))
        self.assertNotIn("tool_profile", inspect.getsource(upstream_module))
        self.assertNotIn("tool_profile", inspect.signature(UpstreamManager.tool_names).parameters)
        self.assertNotIn("tool_profile", inspect.signature(Runtime).parameters)
        with self.assertRaises(UpstreamConfigError):
            parse_server_config(
                "bad__alias",
                {"transport": "streamable_http", "url": "http://127.0.0.1/mcp"},
            )
        with self.assertRaisesRegex(UpstreamConfigError, "include and exclude"):
            parse_server_config(
                "github",
                {
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1/mcp",
                    "include_tools": ["search"],
                    "exclude_tools": ["search"],
                },
            )
        with self.assertRaisesRegex(UpstreamConfigError, "expose_mode"):
            parse_server_config(
                "github",
                {
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1/mcp",
                    "expose_mode": "live",
                },
            )
        with self.assertRaisesRegex(UpstreamConfigError, "tool_policy"):
            parse_server_config(
                "github",
                {
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1/mcp",
                    "tool_policy": {"search": "sometimes"},
                },
            )

    def test_config_snapshot_parses_exposure_catalog_and_synonyms(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp-servers.json"
            path.write_text(
                json.dumps(
                    {
                        "servers": {
                            "github": {
                                "transport": "streamable_http",
                                "url": "http://127.0.0.1/mcp",
                                "expose_mode": "broker",
                                "pinned_tools": ["search"],
                                "tags": ["code", "remote"],
                                "tool_policy": {"create_issue": "readonly"},
                            }
                        },
                        "tool_search": {
                            "custom_synonyms": {"仓库": ["search", "repository"]}
                        },
                    }
                ),
                encoding="utf-8",
            )
            snapshot = load_upstream_config_snapshot(path)

        config = snapshot.configs[0]
        self.assertEqual(config.expose_mode, "broker")
        self.assertEqual(config.pinned_tools, ("search",))
        self.assertEqual(config.tags, ("code", "remote"))
        self.assertEqual(config.tool_policy, {"create_issue": "readonly"})
        self.assertEqual(
            snapshot.custom_synonyms,
            {"仓库": ("search", "repository")},
        )

    def test_catalog_and_exposure_are_frozen_in_one_runtime_snapshot(self) -> None:
        direct_config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            tags=("code",),
        )
        direct_client = FakeUpstreamClient(direct_config, "2025-11-25")
        direct_manager = build_manager(
            [direct_config],
            [direct_client],
            custom_synonyms={"仓库": ("search",)},
        )
        self.assertEqual(
            direct_manager.tool_names(),
            ["github__search", "github__create_issue"],
        )
        self.assertEqual(len(direct_manager.catalog_entries()), 2)
        self.assertEqual(direct_manager.status_payload()["catalog_tool_count"], 2)
        self.assertIsNotNone(direct_manager.state.search_index)
        assert direct_manager.state.search_index is not None
        search_results = direct_manager.state.search_index.search(
            "仓库",
            ToolSearchFilters(server="github", limit=5),
        )
        self.assertEqual(search_results[0].tool_id, "github__search")
        self.assertEqual(search_results[0].tags, ("code",))

        broker_config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode="broker",
            pinned_tools=("search",),
            tags=("code", "broker"),
            tool_policy={"create_issue": "readonly"},
        )
        broker_client = FakeUpstreamClient(broker_config, "2025-11-25")
        broker_manager = build_manager([broker_config], [broker_client])
        self.assertEqual(broker_manager.tool_names(), ["github__search"])
        self.assertEqual(set(broker_manager.state.all_tools), {
            "github__search",
            "github__create_issue",
        })
        self.assertEqual(set(broker_manager.state.catalog), set(broker_manager.state.all_tools))
        self.assertEqual(
            broker_manager.catalog_entry("github__create_issue").effective_risk,  # type: ignore[union-attr]
            "readonly",
        )
        with TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), upstream_manager=broker_manager)
            exposed = {definition["name"] for definition in runtime.list_tools()["tools"]}
            self.assertIn("github__search", exposed)
            self.assertNotIn("github__create_issue", exposed)
            runtime.close()
        direct_manager.close()

    def test_broker_only_collision_is_checked_against_all_tools(self) -> None:
        config = UpstreamServerConfig(
            alias="github",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
            expose_mode="broker",
            pinned_tools=("search",),
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        with self.assertRaisesRegex(UpstreamConfigError, "collision"):
            build_manager(
                [config],
                [client],
                reserved_names={"github__create_issue"},
            )
        self.assertTrue(client.closed)

    def test_old_runtime_snapshot_is_unchanged_after_config_rewrite(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "mcp-servers.json"
            direct_document = {
                "servers": {
                    "github": {
                        "transport": "streamable_http",
                        "url": "http://127.0.0.1/mcp",
                    }
                }
            }
            path.write_text(json.dumps(direct_document), encoding="utf-8")
            old_snapshot = load_upstream_config_snapshot(path)
            old_config = old_snapshot.configs[0]
            old_manager = build_manager(
                list(old_snapshot.configs),
                [FakeUpstreamClient(old_config, "2025-11-25")],
                custom_synonyms=dict(old_snapshot.custom_synonyms),
            )
            old_runtime = Runtime(root, upstream_manager=old_manager)

            broker_document = {
                "servers": {
                    "github": {
                        "transport": "streamable_http",
                        "url": "http://127.0.0.1/mcp",
                        "expose_mode": "broker",
                        "pinned_tools": ["search"],
                    }
                }
            }
            path.write_text(json.dumps(broker_document), encoding="utf-8")
            new_snapshot = load_upstream_config_snapshot(path)
            new_config = new_snapshot.configs[0]
            new_manager = build_manager(
                list(new_snapshot.configs),
                [FakeUpstreamClient(new_config, "2025-11-25")],
                custom_synonyms=dict(new_snapshot.custom_synonyms),
            )
            new_runtime = Runtime(root, upstream_manager=new_manager)

            old_names = {item["name"] for item in old_runtime.list_tools()["tools"]}
            new_names = {item["name"] for item in new_runtime.list_tools()["tools"]}
            self.assertIn("github__create_issue", old_names)
            self.assertNotIn("github__create_issue", new_names)
            self.assertIn("github__search", old_names)
            self.assertIn("github__search", new_names)
            old_runtime.close()
            new_runtime.close()

    def test_stdio_base_environment_does_not_inherit_server_secrets(self) -> None:
        with patch.dict(
            upstream_module.os.environ,
            {
                "PATH": "synthetic-path",
                "HOME": "synthetic-home",
                "CODING_TOOLS_MCP_OAUTH_PASSWORD": "secret-canary",
                "UNRELATED_SECRET": "another-secret",
            },
            clear=True,
        ):
            env = base_upstream_environment()
        self.assertEqual(env["PATH"], "synthetic-path")
        self.assertEqual(env["HOME"], "synthetic-home")
        self.assertNotIn("CODING_TOOLS_MCP_OAUTH_PASSWORD", env)
        self.assertNotIn("UNRELATED_SECRET", env)
        self.assertNotIn("secret-canary", repr(env))
        with self.assertRaisesRegex(UpstreamConfigError, "secret_ref"):
            resolve_env_config({"TOKEN": {"secret_ref": "gateway/token"}})

    def test_json_rpc_envelopes_and_sse_are_validated_strictly(self) -> None:
        invalid = [
            None,
            {"jsonrpc": "1.0", "id": 7, "result": {}},
            {"jsonrpc": "2.0", "id": 8, "result": {}},
            {"jsonrpc": "2.0", "id": True, "result": {}},
            {"jsonrpc": "2.0", "id": 7.0, "result": {}},
            {"jsonrpc": "2.0", "id": "7", "result": {}},
            {"jsonrpc": "2.0", "id": 7, "result": {}, "error": {"message": "both"}},
            {"jsonrpc": "2.0", "id": 7},
            {"jsonrpc": "2.0", "id": 7, "error": {"code": -1}},
            {"jsonrpc": "2.0", "id": 7, "error": {"message": "missing code"}},
            {"jsonrpc": "2.0", "id": 7, "error": {"code": "-1", "message": "string code"}},
            {"jsonrpc": "2.0", "id": 7, "error": {"code": True, "message": "boolean code"}},
            {"jsonrpc": "2.0", "id": 7, "error": {"code": -1.0, "message": "float code"}},
        ]
        for envelope in invalid:
            with self.subTest(envelope=envelope):
                with self.assertRaises(UpstreamError) as caught:
                    _rpc_result(envelope, 7, "tools/call")
                self.assertEqual(caught.exception.code, "UPSTREAM_PROTOCOL_ERROR")

        with self.assertRaises(UpstreamError) as boolean_id_caught:
            _rpc_result(
                {"jsonrpc": "2.0", "id": True, "result": {}},
                1,
                "tools/call",
            )
        self.assertEqual(
            boolean_id_caught.exception.code,
            "UPSTREAM_PROTOCOL_ERROR",
        )

        with self.assertRaises(UpstreamError) as caught:
            _rpc_result(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "error": {"code": -32001, "message": "remote denied", "data": {"reason": "policy"}},
                },
                7,
                "tools/call",
            )
        self.assertEqual(caught.exception.code, "UPSTREAM_RPC_ERROR")
        self.assertEqual(caught.exception.details["rpc_error"]["data"]["reason"], "policy")

        raw_deep_error = (
            '{"jsonrpc":"2.0","id":7,"error":{"code":-32002,'
            '"message":"deep remote error","data":'
            + '{"next":' * 497
            + '{"reason":"deep"}'
            + '}' * 497
            + '}}'
        )
        parsed_deep_error = strict_json_loads(raw_deep_error)
        self.assertIsInstance(parsed_deep_error, dict)
        with self.assertRaises(UpstreamError) as deep_caught:
            _rpc_result(parsed_deep_error, 7, "tools/call")
        self.assertEqual(deep_caught.exception.code, "UPSTREAM_PROTOCOL_ERROR")
        self.assertFalse(deep_caught.exception.retryable)
        self.assertEqual(deep_caught.exception.details, {"method": "tools/call"})

        sse = (
            b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            b'data: {"jsonrpc":"2.0","id":7,"result":{"tools":[]}}\n\n'
        )
        parsed = decode_http_rpc_response(sse, "text/event-stream", expected_id=7)
        self.assertEqual(parsed["id"], 7)

        exact_id_sse = (
            b'data: {"jsonrpc":"2.0","id":true,"result":{"wrong":true}}\n\n'
            b'data: {"jsonrpc":"2.0","id":1,"result":{"right":true}}\n\n'
        )
        exact_id = decode_http_rpc_response(
            exact_id_sse,
            "text/event-stream",
            expected_id=1,
        )
        self.assertIs(type(exact_id["id"]), int)
        self.assertEqual(exact_id["result"], {"right": True})
        with self.assertRaises(UpstreamError) as raised:
            decode_http_rpc_response(b"not-json", "application/json", expected_id=7)
        self.assertEqual(raised.exception.code, "UPSTREAM_PROTOCOL_ERROR")

    def test_deep_error_details_are_bounded_during_gateway_mapping(self) -> None:
        deep: dict[str, object] = {"reason": "deep"}
        for _ in range(497):
            deep = {"next": deep}
        exc = UpstreamError(
            "UPSTREAM_RPC_ERROR",
            "remote denied",
            category="upstream",
            details={"rpc_error": {"data": deep}},
        )

        mapped = error_payload(exc)
        self.assertEqual(
            mapped["details"],
            {"_omitted": True, "_reason": "invalid_or_excessive_nesting"},
        )
        status_payload = UpstreamStatus(
            alias="deep",
            transport="streamable_http",
            enabled=True,
            error=mapped,
        ).payload()
        self.assertEqual(status_payload["error"]["code"], "UPSTREAM_RPC_ERROR")
        self.assertEqual(status_payload["error"]["message"], "remote denied")
        self.assertEqual(status_payload["error"]["category"], "upstream")
        self.assertIs(status_payload["error"]["retryable"], False)
        self.assertEqual(
            status_payload["error"]["details"],
            {"_omitted": True, "_reason": "invalid_or_excessive_nesting"},
        )
        strict_json_bytes(status_payload)
        result = upstream_error_result(
            exc.code,
            exc.message,
            category=exc.category,
            retryable=exc.retryable,
            details=exc.details,
            alias="deep",
            tool_name="deep__tool",
        )
        error = result["structuredContent"]["error"]
        self.assertEqual(
            error["details"],
            {"_omitted": True, "_reason": "invalid_or_excessive_nesting"},
        )
        self.assertFalse(error["retryable"])

    def test_rpc_error_container_depth_boundaries_across_gateway_and_status(self) -> None:
        for total_levels in (63, 64, 65):
            with self.subTest(total_levels=total_levels):
                with self.assertRaises(UpstreamError) as caught:
                    _rpc_result(
                        {
                            "jsonrpc": "2.0",
                            "id": 7,
                            "error": rpc_error_with_total_levels(total_levels),
                        },
                        7,
                        "tools/call",
                    )

                expected_code = (
                    "UPSTREAM_RPC_ERROR"
                    if total_levels <= 64
                    else "UPSTREAM_PROTOCOL_ERROR"
                )
                self.assertEqual(caught.exception.code, expected_code)
                self.assertFalse(caught.exception.retryable)

                mapped = error_payload(caught.exception)
                self.assertEqual(mapped["code"], expected_code)
                strict_json_bytes(mapped)

                status = UpstreamStatus(
                    alias="deep",
                    transport="streamable_http",
                    enabled=True,
                    error=mapped,
                )
                status_payload = status.payload()
                status_error = status_payload["error"]
                self.assertEqual(status_error["code"], expected_code)
                self.assertEqual(
                    status_error["category"],
                    "upstream" if total_levels <= 64 else "protocol",
                )
                self.assertIs(status_error["retryable"], False)
                strict_json_bytes(status_payload)

                gateway_result = upstream_error_result(
                    caught.exception.code,
                    caught.exception.message,
                    category=caught.exception.category,
                    retryable=caught.exception.retryable,
                    details=caught.exception.details,
                    alias="deep",
                    tool_name="deep__tool",
                )
                result_json_bytes(gateway_result)

                if total_levels <= 64:
                    self.assertEqual(status_error["message"], "boundary")
                    self.assertNotIn("_omitted", status_error)
                    self.assertNotIn("_omitted", status_error["details"])
                    self.assertNotIn("_omitted", mapped["details"])
                    cursor: Any = status_error["details"]["rpc_error"]["data"]
                    for _ in range(total_levels - 1):
                        cursor = cursor["next"]
                    self.assertEqual(cursor, "leaf")
                else:
                    self.assertEqual(status_error["code"], "UPSTREAM_PROTOCOL_ERROR")
                    self.assertEqual(status_error["category"], "protocol")
                    self.assertIs(status_error["retryable"], False)
                    self.assertIsInstance(status_error.get("details"), dict)

    def test_result_container_depth_uses_root_as_level_one(self) -> None:
        for total_levels in (63, 64):
            with self.subTest(total_levels=total_levels):
                result = normalize_tool_result(
                    {
                        "content": [],
                        "structuredContent": nested_dict_with_levels(total_levels - 1),
                    }
                )
                result_json_bytes(result)
        with self.assertRaises(UpstreamError) as caught:
            normalize_tool_result(
                {
                    "content": [],
                    "structuredContent": nested_dict_with_levels(64),
                }
            )
        self.assertEqual(caught.exception.code, "UPSTREAM_PROTOCOL_ERROR")

    def test_error_detail_shared_container_scope_is_per_top_level_value(self) -> None:
        shared = {"value": "same"}
        mapped = error_payload(
            UpstreamError(
                "UPSTREAM_RPC_ERROR",
                "shared across top-level values",
                category="upstream",
                details={"left": shared, "right": shared},
            )
        )
        self.assertEqual(mapped["details"]["left"], mapped["details"]["right"])
        self.assertIsNot(mapped["details"]["left"], mapped["details"]["right"])
        strict_json_bytes(mapped)

        internally_shared = {"left": shared, "right": shared}
        omitted = error_payload(
            UpstreamError(
                "UPSTREAM_RPC_ERROR",
                "shared inside one detail value",
                category="upstream",
                details={"rpc_error": internally_shared},
            )
        )
        self.assertEqual(
            omitted["details"],
            {"_omitted": True, "_reason": "invalid_or_excessive_nesting"},
        )
        strict_json_bytes(omitted)

    def test_invalid_json_scalars_in_rpc_error_details_are_omitted_by_manager(self) -> None:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        try:
            invalid_values = (
                ("nan", float("nan")),
                ("infinity", float("inf")),
                ("over_limit_integer", 10**4300),
            )
            for label, invalid_value in invalid_values:
                with self.subTest(label=label):
                    rpc_error = UpstreamError(
                        "UPSTREAM_RPC_ERROR",
                        "invalid scalar",
                        category="upstream",
                        details={
                            "method": "tools/call",
                            "rpc_error": {
                                "code": -32005,
                                "message": "invalid scalar",
                                "data": invalid_value,
                            },
                        },
                    )
                    with patch.object(
                        client,
                        "call_tool_raw",
                        side_effect=rpc_error,
                    ):
                        result = manager.call_tool("remote__search", {"q": "shape"})
                    result_json_bytes(result)
                    self.assertEqual(
                        result["structuredContent"]["error"]["details"],
                        {
                            "_omitted": True,
                            "_reason": "invalid_or_excessive_nesting",
                        },
                    )
        finally:
            manager.close()

    def test_content_boundary_does_not_serialize_structured_content_as_text(self) -> None:
        result = normalize_tool_result(
            {
                "structuredContent": {"answer": 42},
                "isError": False,
            }
        )
        self.assertEqual(result["content"], [])
        self.assertEqual(result["structuredContent"], {"answer": 42})
        with self.assertRaisesRegex(UpstreamError, "content"):
            normalize_tool_result({"content": "not-an-aray"})
        with self.assertRaisesRegex(UpstreamError, "structuredContent"):
            normalize_tool_result(
                {
                    "content": [],
                    "structuredContent": [],
                    "isError": False,
                }
            )

    def test_deep_structured_content_maps_to_gateway_protocol_error(self) -> None:
        structured: dict[str, object] = {}
        cursor = structured
        for _ in range(500):
            child: dict[str, object] = {}
            cursor["next"] = child
            cursor = child
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        with patch.object(
            client,
            "call_tool_raw",
            return_value={
                "content": [],
                "structuredContent": structured,
                "isError": False,
            },
        ):
            result = manager.call_tool("remote__search", {"q": "depth"})
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "UPSTREAM_PROTOCOL_ERROR")
        self.assertEqual(error["category"], "protocol")
        self.assertFalse(error["retryable"])
        manager.close()

    def test_invalid_structured_content_maps_to_gateway_protocol_error(self) -> None:
        config = UpstreamServerConfig(
            alias="remote",
            transport="streamable_http",
            url="http://127.0.0.1/mcp",
        )
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        with patch.object(
            client,
            "call_tool_raw",
            return_value={
                "content": [],
                "structuredContent": [],
                "isError": False,
            },
        ):
            result = manager.call_tool("remote__search", {"q": "shape"})
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "UPSTREAM_PROTOCOL_ERROR",
        )
        self.assertFalse(result["structuredContent"]["error"]["retryable"])
        manager.close()


if __name__ == "__main__":
    unittest.main()
