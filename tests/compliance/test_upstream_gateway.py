from __future__ import annotations

import copy
import inspect
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp import upstream as upstream_module
from coding_tools_mcp.server import (
    AuthorizationContext,
    Runtime,
    TOOL_REGISTRY,
    build_parser,
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
    base_upstream_environment,
    _rpc_result,
    decode_http_rpc_response,
    load_upstream_config_snapshot,
    normalize_tool_result,
    parse_server_config,
    resolve_env_config,
)
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
        self.closed = False

    def initialize(self) -> None:
        return None

    def list_tools(self) -> list[dict[str, object]]:
        return copy.deepcopy(self.tools)

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
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

    def close(self) -> None:
        self.closed = True


def build_manager(
    configs: list[UpstreamServerConfig],
    clients: list[FakeUpstreamClient],
    *,
    reserved_names: set[str] | None = None,
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
        )
    return manager


class UpstreamGatewayTests(unittest.TestCase):
    def test_runtime_preserves_schema_annotations_and_structured_content(self) -> None:
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
            self.assertEqual(remote["inputSchema"], original["inputSchema"])
            self.assertEqual(remote["outputSchema"], original["outputSchema"])
            self.assertEqual(remote["annotations"], original["annotations"])

            result = runtime.call_tool("github__search", {"q": "mcp"})

            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["remote_name"], "search")
            self.assertEqual(result["content"][1]["type"], "resource_link")
            self.assertEqual(client.calls, [("search", {"q": "mcp"})])
            runtime.close()
            self.assertTrue(client.closed)

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

    def test_fake_readonly_never_rewrites_upstream_annotations(self) -> None:
        with TemporaryDirectory() as tmp:
            config = UpstreamServerConfig(
                alias="github",
                transport="streamable_http",
                url="http://127.0.0.1/mcp",
            )
            client = FakeUpstreamClient(config, "2025-11-25")
            runtime = Runtime(
                Path(tmp),
                permission_mode="dangerous",
                fake_readonly_annotations=True,
                upstream_manager=build_manager([config], [client]),
            )
            definitions = {item["name"]: item for item in runtime.list_tools()["tools"]}

            self.assertTrue(definitions["exec_command"]["annotations"]["readOnlyHint"])
            self.assertEqual(
                definitions["github__create_issue"]["annotations"],
                REMOTE_TOOLS[1]["annotations"],
            )
            card = server_card_payload(runtime)
            self.assertIn("github__create_issue", card["tools"]["readOnlyHintFalse"])
            runtime.close()

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
            {"jsonrpc": "2.0", "id": 7, "result": {}, "error": {"message": "both"}},
            {"jsonrpc": "2.0", "id": 7},
            {"jsonrpc": "2.0", "id": 7, "error": {"code": -1}},
        ]
        for envelope in invalid:
            with self.subTest(envelope=envelope):
                with self.assertRaises(UpstreamError) as caught:
                    _rpc_result(envelope, 7, "tools/call")
                self.assertEqual(caught.exception.code, "UPSTREAM_PROTOCOL_ERROR")

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

        sse = (
            b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            b'data: {"jsonrpc":"2.0","id":7,"result":{"tools":[]}}\n\n'
        )
        parsed = decode_http_rpc_response(sse, "text/event-stream", expected_id=7)
        self.assertEqual(parsed["id"], 7)
        with self.assertRaises(json.JSONDecodeError):
            decode_http_rpc_response(b"not-json", "application/json", expected_id=7)

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
            normalize_tool_result({"content": "not-an-array"})


if __name__ == "__main__":
    unittest.main()
