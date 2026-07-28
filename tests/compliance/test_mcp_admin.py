from __future__ import annotations

import io
import json
import os
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from coding_tools_mcp.admin import McpAdminManager, McpManagementError
from coding_tools_mcp.chat_cli import main as chat_cli_main
from coding_tools_mcp.server import (
    AdminUIHandler,
    MCPHandler,
    OAuthConfig,
    Runtime,
    RuntimeHTTPServer,
    RuntimePolicy,
    ShellEnvPolicy,
    _create_oauth_token,
    _decode_oauth_token,
    _resolve_oauth_token_secret,
    admin_console_html,
    apply_startup_settings,
    build_parser,
    build_runtime,
    read_server_settings,
)
from coding_tools_mcp.settings_store import default_settings_dir
from coding_tools_mcp.upstream import UpstreamManager, parse_server_config, resolve_env_config
from coding_tools_mcp.webui import ADMIN_CSS, ADMIN_HTML, ADMIN_JS, WEBUI_DIST, admin_asset_response


class McpAdminConfigTests(unittest.TestCase):
    def test_install_requires_apply_before_writing_config(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            manager = McpAdminManager(config_path, protocol_version="2025-06-18")
            spec = filesystem_spec()

            plan = manager.install_server(spec)

            self.assertTrue(plan["dry_run"])
            self.assertTrue(plan["apply_required"])
            self.assertFalse(config_path.exists())

            applied = manager.install_server(spec, apply_changes=True)

            self.assertFalse(applied["dry_run"])
            self.assertTrue(applied["applied"])
            document = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(document["servers"]["filesystem"]["command"], "uvx")
            self.assertTrue(config_path.with_suffix(".json.audit.jsonl").exists())

    def test_templates_render_to_install_and_update_plans(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")

            templates = manager.template_list()
            template_ids = {template["id"] for template in templates["templates"]}
            self.assertIn("filesystem", template_ids)
            self.assertIn("playwright", template_ids)

            rendered = manager.render_template("filesystem", variables={"alias": "fs", "workspace": "G:/LLM"})
            self.assertEqual(rendered["config"]["alias"], "fs")
            self.assertEqual(rendered["config"]["args"], ["mcp-server-filesystem", "G:/LLM"])
            self.assertEqual(rendered["plan"]["action"], "install")

            manager.install_server(rendered["config"], apply_changes=True)
            update = manager.render_template("filesystem", variables={"alias": "fs", "workspace": "G:/LLM/project"})
            self.assertEqual(update["plan"]["action"], "update")
            self.assertIn("args", update["plan"]["changes"]["changed"])

            with self.assertRaises(McpManagementError):
                manager.render_template("missing-template")

    def test_update_enable_disable_and_remove_are_planned_then_applied(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")
            manager.install_server(filesystem_spec(), apply_changes=True)

            update_plan = manager.update_server("filesystem", {"args": ["mcp-server-filesystem", "G:/LLM/project"]})
            self.assertTrue(update_plan["dry_run"])
            self.assertIn("args", update_plan["changes"]["changed"])

            manager.update_server(
                "filesystem",
                {"args": ["mcp-server-filesystem", "G:/LLM/project"]},
                apply_changes=True,
            )
            disable_plan = manager.set_server_enabled("filesystem", False)
            self.assertEqual(disable_plan["action"], "disable")

            manager.set_server_enabled("filesystem", False, apply_changes=True)
            catalog = manager.catalog_list()
            self.assertFalse(catalog["servers"][0]["config"]["enabled"])
            self.assertFalse(catalog["servers"][0]["config_raw"]["enabled"])
            self.assertEqual(catalog["servers"][0]["config_raw"]["alias"], "filesystem")

            remove_plan = manager.remove_server("filesystem")
            self.assertEqual(remove_plan["action"], "remove")
            manager.remove_server("filesystem", apply_changes=True)
            self.assertEqual(manager.catalog_list()["server_count"], 0)

    def test_stdio_shell_commands_are_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")

            with self.assertRaises(McpManagementError):
                manager.plan_server(
                    {
                        "alias": "bad",
                        "transport": "stdio",
                        "command": "cmd",
                        "args": ["/c", "echo hi"],
                    }
                )

            with self.assertRaises(McpManagementError):
                manager.plan_server(
                    {
                        "alias": "bad2",
                        "transport": "stdio",
                        "command": "uvx",
                        "args": ["mcp-server-filesystem", "G:/LLM | more"],
                    }
                )

    def test_env_refs_parse_without_revealing_secret_values(self) -> None:
        parsed = parse_server_config(
            "filesystem",
            {
                "transport": "stdio",
                "command": "uvx",
                "args": ["mcp-server-filesystem", "G:/LLM"],
                "env": {"TOKEN": {"secret_ref": "github_token"}, "CACHE": {"env_ref": "CACHE_DIR"}},
            },
        )

        self.assertEqual(parsed.env["TOKEN"], {"secret_ref": "github_token"})
        with self.assertRaises(Exception):
            resolve_env_config(parsed.env)

    def test_reload_uses_saved_config_without_starting_disabled_stdio_processes(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")
            spec = {**filesystem_spec(), "enabled": False}
            manager.install_server(spec, apply_changes=True)

            upstream = manager.reload_upstreams()
            status = upstream.status_payload()

            self.assertEqual(status["server_count"], 1)
            self.assertEqual(status["initialized_count"], 0)

    def test_invalid_upstream_config_does_not_prevent_manager_startup(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            document = {
                "servers": {
                    "codegraph": {
                        "transport": "stdio",
                        "command": "cmd.exe",
                        "args": ["/c", "codegraph", "serve", "--mcp"],
                    },
                    "filesystem": {**filesystem_spec(), "enabled": False},
                }
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")

            upstream = UpstreamManager.from_config_file(str(config_path))
            status = upstream.status_payload()
            by_alias = {item["alias"]: item for item in status["servers"]}

            self.assertEqual(status["server_count"], 2)
            self.assertIn("codegraph", by_alias)
            self.assertIn("filesystem", by_alias)
            self.assertEqual(by_alias["codegraph"]["error"]["category"], "configuration")
            self.assertFalse(by_alias["codegraph"]["error"]["retryable"])
            self.assertEqual(by_alias["filesystem"]["tool_count"], 0)

    def test_malformed_upstream_config_is_reported_as_config_status(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            config_path.write_text("{", encoding="utf-8")

            upstream = UpstreamManager.from_config_file(str(config_path))
            status = upstream.status_payload()

            self.assertEqual(status["server_count"], 1)
            self.assertEqual(status["servers"][0]["alias"], "__config__")
            self.assertEqual(status["servers"][0]["error"]["category"], "configuration")
            self.assertFalse(status["servers"][0]["error"]["retryable"])

    def test_secret_vault_requires_key_and_stores_encrypted_values(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            with patch.dict(os.environ, {}, clear=True):
                manager = McpAdminManager(config_path, protocol_version="2025-06-18")
                with self.assertRaises(McpManagementError):
                    manager.secret_set("github_token", "plain-token")

            with patch.dict(os.environ, {"CODING_TOOLS_MCP_SECRETS_KEY": "test-master-key"}, clear=True):
                manager = McpAdminManager(config_path, protocol_version="2025-06-18")
                manager.secret_set("github_token", "plain-token")
                vault_text = config_path.with_suffix(".json.secrets.json").read_text(encoding="utf-8")

                self.assertNotIn("plain-token", vault_text)
                self.assertEqual(manager.secret_list()["secrets"], ["github_token"])
                self.assertEqual(manager.secret_vault.get_secret("github_token"), "plain-token")
                self.assertTrue(manager.secret_delete("github_token")["deleted"])

    def test_runtime_hides_admin_tools_unless_admin_context_is_provided(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")
            runtime = Runtime(Path(tmp), admin_token="admin-token", admin_manager=manager)

            normal_tools = {tool["name"] for tool in runtime.list_tools()["tools"]}
            admin_tools = {tool["name"] for tool in runtime.list_tools(include_admin=True)["tools"]}

            self.assertNotIn("mcp_catalog_list", normal_tools)
            self.assertIn("mcp_catalog_list", admin_tools)
            self.assertIn("mcp_template_list", admin_tools)
            self.assertIn("mcp_server_health", admin_tools)
            self.assertIn("record_chat_transcript", normal_tools)
            self.assertIn("record_chat_message", normal_tools)
            self.assertIn("recall_chat_context", normal_tools)
            self.assertIn("list_chat_projects", normal_tools)
            self.assertIn("list_chat_conversations", normal_tools)
            self.assertIn("recall_project_context", normal_tools)
            self.assertIn("mcp_chat_context", admin_tools)
            self.assertIn("mcp_chat_projects", admin_tools)
            self.assertIn("mcp_chat_record_context", admin_tools)
            self.assertIn("mcp_chat_update_context", admin_tools)
            self.assertIn("mcp_chat_delete_context", admin_tools)
            self.assertIn("mcp_chat_recall", admin_tools)
            self.assertIn("mcp_chat_project_recall", admin_tools)
            self.assertIn("mcp_chat_context_export", admin_tools)
            self.assertIn("mcp_codex_sessions_preview", admin_tools)
            self.assertIn("mcp_codex_sessions_import", admin_tools)
            self.assertIn("mcp_codex_sessions_sync", admin_tools)
            self.assertIn("mcp_chat_clear", admin_tools)
            with self.assertRaises(Exception):
                runtime.call_tool("mcp_catalog_list", {})
            result = runtime.call_tool("mcp_catalog_list", {}, admin=True)
            self.assertFalse(result["isError"])
            templates = runtime.call_tool("mcp_template_list", {}, admin=True)
            self.assertFalse(templates["isError"])
            self.assertGreater(templates["structuredContent"]["template_count"], 0)

    def test_runtime_persists_mcp_transcripts_and_exports_markdown(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")
            runtime = Runtime(Path(tmp), admin_token="admin-token", admin_manager=manager, config_dir=Path(tmp))
            session_id = "agent-session-1"

            runtime.record_mcp_http_access(
                session_id=session_id,
                method="POST",
                path="/mcp",
                rpc_method="tools/call",
                status=200,
                remote_addr="127.0.0.1",
                user_agent="test-agent",
                protocol_version="2025-06-18",
            )
            runtime.call_tool("get_default_cwd", {}, session_id=session_id)

            sessions = runtime.call_tool("mcp_transcript_sessions", {"limit": 10}, admin=True)["structuredContent"]
            self.assertGreaterEqual(sessions["session_count"], 1)
            self.assertIn(session_id, {item["session_id"] for item in sessions["sessions"]})

            export = runtime.call_tool(
                "mcp_transcript_export",
                {"session_id": session_id, "max_events": 20, "write_file": True},
                admin=True,
            )["structuredContent"]
            self.assertIn("MCP Session Transcript", export["markdown"])
            self.assertIn("get_default_cwd", export["markdown"])
            self.assertTrue(Path(export["path"]).exists())

    def test_runtime_persists_and_manages_chat_transcripts(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")
            runtime = Runtime(Path(tmp), admin_token="admin-token", admin_manager=manager, config_dir=Path(tmp))
            project_id = "coding-tools-mcp"
            project_fields = {
                "project_id": project_id,
                "project_name": "Coding Tools MCP",
                "project_path": "G:/LLM/coding-tools-mcp",
                "project_workspace": "G:/LLM",
            }

            recorded = runtime.call_tool(
                "record_chat_transcript",
                {
                    "conversation_id": "chat-2026-06-18",
                    "conversation_title": "备份测试会话",
                    "conversation_uid": "uid-chat-2026",
                    **project_fields,
                    "project_metadata": {"repo": "coding-tools-mcp"},
                    "source": "codex-test",
                    "messages": [
                        {"message_id": "u1", "role": "user", "timestamp": "2026-06-18T08:00:00Z", "content": "请记录完整聊天"},
                        {"message_id": "a1", "role": "assistant", "timestamp": "2026-06-18T08:00:01Z", "content": "已经同步到宿主机。"},
                    ],
                },
            )["structuredContent"]
            self.assertEqual(recorded["inserted_count"], 2)
            self.assertEqual(recorded["project_id"], project_id)

            context_recorded = runtime.call_tool(
                "mcp_chat_record_context",
                {
                    "conversation_id": "chat-2026-06-18",
                    **project_fields,
                    "entry_id": "checkpoint-1",
                    "kind": "checkpoint",
                    "timestamp": "2026-06-18T08:00:03Z",
                    "content": "Keep this as durable restore context, separate from full chat backups.",
                    "source": "context-test",
                },
                admin=True,
            )["structuredContent"]
            self.assertEqual(context_recorded["inserted_count"], 1)
            self.assertEqual(context_recorded["project_id"], project_id)

            conversations = runtime.call_tool("mcp_chat_conversations", {"limit": 10}, admin=True)["structuredContent"]
            self.assertEqual(conversations["conversation_count"], 1)
            conversation = conversations["conversations"][0]
            self.assertEqual(conversation["conversation_id"], "chat-2026-06-18")
            self.assertEqual(conversation["title"], "备份测试会话")
            self.assertEqual(conversation["unique_id"], "uid-chat-2026")
            self.assertEqual(conversation["date"], "2026-06-18")
            self.assertEqual(conversation["project_id"], project_id)
            self.assertEqual(conversation["project_name"], "Coding Tools MCP")
            self.assertEqual(conversation["project_path"], "G:/LLM/coding-tools-mcp")
            self.assertEqual(conversation["project_workspace"], "G:/LLM")
            self.assertEqual(conversation["context_entry_count"], 1)
            projects = runtime.call_tool("mcp_chat_projects", {"limit": 10}, admin=True)["structuredContent"]
            self.assertEqual(projects["project_count"], 1)
            project = projects["projects"][0]
            self.assertEqual(project["project_id"], project_id)
            self.assertEqual(project["name"], "Coding Tools MCP")
            self.assertEqual(project["path"], "G:/LLM/coding-tools-mcp")
            self.assertEqual(project["workspace"], "G:/LLM")
            self.assertEqual(project["conversation_count"], 1)
            self.assertEqual(project["message_count"], 2)
            self.assertEqual(project["context_entry_count"], 1)
            normal_projects = runtime.call_tool("list_chat_projects", {"limit": 10})["structuredContent"]
            self.assertEqual(normal_projects["projects"][0]["project_id"], project_id)
            normal_conversations = runtime.call_tool(
                "list_chat_conversations",
                {"limit": 10, "project_id": project_id},
            )["structuredContent"]
            self.assertEqual(normal_conversations["conversation_count"], 1)
            self.assertEqual(normal_conversations["conversations"][0]["conversation_id"], "chat-2026-06-18")
            status = runtime.admin_status_payload()
            self.assertEqual(status["chat_projects"]["projects"][0]["project_id"], project_id)
            filtered_by_project = runtime.call_tool(
                "mcp_chat_conversations",
                {"limit": 10, "project_id": project_id},
                admin=True,
            )["structuredContent"]
            self.assertEqual(filtered_by_project["conversation_count"], 1)
            filtered_by_project_query = runtime.call_tool(
                "mcp_chat_conversations",
                {"limit": 10, "query": "coding-tools-mcp"},
                admin=True,
            )["structuredContent"]
            self.assertEqual(filtered_by_project_query["conversation_count"], 1)
            filtered_by_uid = runtime.call_tool("mcp_chat_conversations", {"limit": 10, "query": "uid-chat-2026"}, admin=True)[
                "structuredContent"
            ]
            self.assertEqual(filtered_by_uid["conversation_count"], 1)
            filtered_by_title = runtime.call_tool("mcp_chat_conversations", {"limit": 10, "query": "备份测试"}, admin=True)[
                "structuredContent"
            ]
            self.assertEqual(filtered_by_title["conversation_count"], 1)
            filtered_empty = runtime.call_tool("mcp_chat_conversations", {"limit": 10, "query": "missing-conversation"}, admin=True)[
                "structuredContent"
            ]
            self.assertEqual(filtered_empty["conversation_count"], 0)

            messages = runtime.call_tool(
                "mcp_chat_messages",
                {"conversation_id": "chat-2026-06-18", "limit": 10},
                admin=True,
            )["structuredContent"]
            self.assertEqual(messages["message_count"], 2)
            assistant_message = next(item for item in messages["messages"] if item["role"] == "assistant")

            context_entries = runtime.call_tool(
                "mcp_chat_context",
                {"conversation_id": "chat-2026-06-18", "limit": 10},
                admin=True,
            )["structuredContent"]
            self.assertEqual(context_entries["entry_count"], 1)
            self.assertIn("durable restore context", context_entries["entries"][0]["content"])
            context_entry = context_entries["entries"][0]

            manual_context = runtime.call_tool(
                "mcp_chat_record_context",
                {
                    "conversation_id": "chat-2026-06-18",
                    **project_fields,
                    "entry_id": "webui-note-1",
                    "kind": "note",
                    "timestamp": "2026-06-18T08:00:04Z",
                    "content": "Manual WebUI note for reopening remote sessions.",
                    "source": "webui-test",
                },
                admin=True,
            )["structuredContent"]
            self.assertEqual(manual_context["inserted_count"], 1)
            self.assertEqual(manual_context["project_id"], project_id)

            updated_context = runtime.call_tool(
                "mcp_chat_update_context",
                {
                    "id": context_entry["id"],
                    "kind": "summary",
                    "content": "Updated durable restore context for WebUI.",
                    "source": "webui-test",
                },
                admin=True,
            )["structuredContent"]
            self.assertTrue(updated_context["updated"])

            updated = runtime.call_tool(
                "mcp_chat_update_message",
                {"id": assistant_message["id"], "role": "assistant", "content": "已经同步并可在 WebUI 编辑。"},
                admin=True,
            )["structuredContent"]
            self.assertTrue(updated["updated"])

            flat_recorded = runtime.call_tool(
                "record_chat_message",
                {
                    "conversation_id": "chat-2026-06-18",
                    **project_fields,
                    "message_id": "flat-a1",
                    "role": "assistant",
                    "timestamp": "2026-06-18T08:00:02Z",
                    "content": "扁平入口也能同步。",
                    "source": "flat-test",
                    "metadata_json": "{\"kind\":\"flat\"}",
                },
            )["structuredContent"]
            self.assertEqual(flat_recorded["inserted_count"], 1)
            self.assertEqual(flat_recorded["project_id"], project_id)

            recalled = runtime.call_tool(
                "recall_chat_context",
                {"conversation_id": "chat-2026-06-18", "max_messages": 10},
            )["structuredContent"]
            self.assertEqual(recalled["conversation_id"], "chat-2026-06-18")
            self.assertEqual(recalled["message_count"], 3)
            self.assertEqual(recalled["context_entry_count"], 2)
            self.assertIn("Updated durable restore context", recalled["context_text"])
            self.assertIn("Manual WebUI note", recalled["context_text"])
            self.assertIn("请记录完整聊天", recalled["chat_markdown"])
            self.assertIn("扁平入口也能同步。", recalled["chat_markdown"])
            self.assertEqual(len(recalled["messages"]), 3)

            admin_recalled = runtime.call_tool(
                "mcp_chat_recall",
                {"conversation_id": "chat-2026-06-18", "max_messages": 10, "max_context_entries": 10},
                admin=True,
            )["structuredContent"]
            self.assertEqual(admin_recalled["context_entry_count"], 2)
            self.assertIn("Manual WebUI note", admin_recalled["context_text"])

            project_recalled = runtime.call_tool(
                "recall_project_context",
                {"project_id": project_id, "max_conversations": 10, "max_messages": 10, "max_context_entries": 10},
            )["structuredContent"]
            self.assertEqual(project_recalled["project_id"], project_id)
            self.assertEqual(project_recalled["project"]["name"], "Coding Tools MCP")
            self.assertEqual(project_recalled["conversation_count"], 1)
            self.assertEqual(project_recalled["message_count"], 3)
            self.assertEqual(project_recalled["context_entry_count"], 2)
            self.assertIn("Manual WebUI note", project_recalled["context_text"])
            self.assertIn("扁平入口也能同步。", project_recalled["chat_markdown"])

            admin_project_recalled = runtime.call_tool(
                "mcp_chat_project_recall",
                {"project_id": project_id, "max_conversations": 10, "max_messages": 10, "max_context_entries": 10},
                admin=True,
            )["structuredContent"]
            self.assertEqual(admin_project_recalled["project_id"], project_id)
            self.assertEqual(admin_project_recalled["context_entry_count"], 2)

            runtime.call_tool(
                "record_chat_transcript",
                {
                    "conversation_id": "chat-extra",
                    "messages": [{"message_id": "u2", "role": "user", "timestamp": "2026-06-18T09:00:00Z", "content": "需要合并"}],
                },
            )
            merged = runtime.call_tool(
                "mcp_chat_merge",
                {"target_conversation_id": "chat-2026-06-18", "source_conversation_ids": ["chat-extra"]},
                admin=True,
            )["structuredContent"]
            self.assertEqual(merged["moved_count"], 1)

            export = runtime.call_tool(
                "mcp_chat_export",
                {"conversation_id": "chat-2026-06-18", "max_messages": 20, "write_file": True},
                admin=True,
            )["structuredContent"]
            self.assertIn("聊天备份记录：chat-2026-06-18", export["markdown"])
            self.assertIn("### 2026-06-18T08:00:02Z - 助手", export["markdown"])
            self.assertIn("- 消息 ID：`flat-a1`", export["markdown"])
            self.assertIn("已经同步并可在 WebUI 编辑。", export["markdown"])
            self.assertIn("扁平入口也能同步。", export["markdown"])
            self.assertIn("需要合并", export["markdown"])
            self.assertTrue(Path(export["path"]).exists())

            project_export = runtime.call_tool(
                "mcp_chat_export",
                {"project_id": project_id, "max_messages": 20, "write_file": False},
                admin=True,
            )["structuredContent"]
            self.assertEqual(project_export["project_id"], project_id)
            self.assertEqual(project_export["message_count"], 4)
            self.assertIn("聊天备份记录：项目 coding-tools-mcp", project_export["markdown"])
            self.assertIn("- 项目：`coding-tools-mcp`", project_export["markdown"])
            self.assertIn("- 项目路径：`G:/LLM/coding-tools-mcp`", project_export["markdown"])
            self.assertIn("需要合并", project_export["markdown"])

            context_export = runtime.call_tool(
                "mcp_chat_context_export",
                {"conversation_id": "chat-2026-06-18", "max_entries": 20, "write_file": True},
                admin=True,
            )["structuredContent"]
            self.assertIn("恢复上下文：chat-2026-06-18", context_export["markdown"])
            self.assertIn("Updated durable restore context", context_export["markdown"])
            self.assertIn("Manual WebUI note", context_export["markdown"])
            self.assertTrue(Path(context_export["path"]).exists())

            project_context_export = runtime.call_tool(
                "mcp_chat_context_export",
                {"project_id": project_id, "max_entries": 20, "write_file": False},
                admin=True,
            )["structuredContent"]
            self.assertEqual(project_context_export["project_id"], project_id)
            self.assertEqual(project_context_export["entry_count"], 2)
            self.assertIn("恢复上下文：项目 coding-tools-mcp", project_context_export["markdown"])
            self.assertIn("Manual WebUI note", project_context_export["markdown"])

            latest_context = runtime.call_tool(
                "mcp_chat_context",
                {"conversation_id": "chat-2026-06-18", "limit": 10},
                admin=True,
            )["structuredContent"]
            self.assertEqual(latest_context["entry_count"], 2)
            manual_entry = next(item for item in latest_context["entries"] if item["entry_id"] == "webui-note-1")
            deleted_context = runtime.call_tool("mcp_chat_delete_context", {"id": manual_entry["id"]}, admin=True)["structuredContent"]
            self.assertEqual(deleted_context["deleted_count"], 1)

            deleted_message = runtime.call_tool("mcp_chat_delete_message", {"id": assistant_message["id"]}, admin=True)["structuredContent"]
            self.assertEqual(deleted_message["deleted_count"], 1)
            deleted_conversation = runtime.call_tool(
                "mcp_chat_delete_conversation",
                {"conversation_id": "chat-2026-06-18"},
                admin=True,
            )["structuredContent"]
            self.assertGreaterEqual(deleted_conversation["deleted_count"], 1)
            cleared = runtime.call_tool("mcp_chat_clear", {}, admin=True)["structuredContent"]
            self.assertEqual(cleared["deleted_count"], 0)
            cleared_projects = runtime.call_tool("mcp_chat_projects", {"limit": 10}, admin=True)["structuredContent"]
            self.assertEqual(cleared_projects["project_count"], 0)

    def test_runtime_imports_codex_session_candidates(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")
            runtime = Runtime(Path(tmp), admin_token="admin-token", admin_manager=manager, config_dir=Path(tmp))
            session_root = Path(tmp) / "codex" / "sessions"
            session_root.mkdir(parents=True)
            session_path = session_root / "rollout-2026-06-25.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "timestamp": "2026-06-25T04:00:00Z",
                    "payload": {
                        "id": "codex-session-1",
                        "cwd": "G:/LLM/coding-tools-mcp",
                        "originator": "codex_desktop",
                    },
                },
                {"type": "user_message", "timestamp": "2026-06-25T04:00:01Z", "message": "请同步私有会话"},
                {
                    "type": "response_item",
                    "timestamp": "2026-06-25T04:00:02Z",
                    "payload": {
                        "type": "message",
                        "id": "assistant-1",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "已经导入。"}],
                    },
                },
            ]
            session_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records), encoding="utf-8")

            preview = runtime.call_tool(
                "mcp_codex_sessions_preview",
                {"roots": [str(session_root)], "limit": 10},
                admin=True,
            )["structuredContent"]
            self.assertEqual(preview["candidate_count"], 1)
            preview_text = json.dumps(preview, ensure_ascii=False)
            self.assertNotIn("请同步私有会话", preview_text)
            self.assertNotIn("已经导入", preview_text)
            candidate = preview["candidates"][0]
            self.assertEqual(candidate["message_count"], 2)
            self.assertEqual(candidate["project_id"], "coding-tools-mcp")
            self.assertEqual(candidate["source"], "codex-desktop")

            imported = runtime.call_tool(
                "mcp_codex_sessions_import",
                {"roots": [str(session_root)], "candidate_ids": [candidate["candidate_id"]]},
                admin=True,
            )["structuredContent"]
            self.assertEqual(imported["inserted_count"], 2)
            self.assertEqual(imported["duplicate_count"], 0)

            messages = runtime.call_tool(
                "mcp_chat_messages",
                {"conversation_id": candidate["conversation_id"], "limit": 10},
                admin=True,
            )["structuredContent"]
            self.assertEqual(messages["message_count"], 2)
            self.assertIn("请同步私有会话", {item["content"] for item in messages["messages"]})

            repeated = runtime.call_tool(
                "mcp_codex_sessions_sync",
                {"roots": [str(session_root)], "candidate_ids": [candidate["candidate_id"]]},
                admin=True,
            )["structuredContent"]
            self.assertEqual(repeated["inserted_count"], 0)
            self.assertEqual(repeated["duplicate_count"], 2)

            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    "\n"
                    + json.dumps(
                        {
                            "type": "response_item",
                            "timestamp": "2026-06-25T04:00:03Z",
                            "payload": {
                                "type": "message",
                                "id": "assistant-2",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "最新内容也已同步。"}],
                            },
                        },
                        ensure_ascii=False,
                    )
                )
            synced = runtime.call_tool(
                "mcp_codex_sessions_sync",
                {"roots": [str(session_root)], "candidate_ids": [candidate["candidate_id"]]},
                admin=True,
            )["structuredContent"]
            self.assertEqual(synced["inserted_count"], 1)
            self.assertEqual(synced["duplicate_count"], 2)

    def test_chat_cli_records_and_recalls_context(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "transcripts.sqlite3"
            class BytesStdin:
                def __init__(self, raw: bytes, *, encoding: str = "utf-8") -> None:
                    self.buffer = io.BytesIO(raw)
                    self.encoding = encoding

                def read(self) -> str:
                    raise AssertionError("chat_cli should prefer raw stdin bytes")

            payload = {
                "conversation_title": "Chat Backup Test",
                "conversation_uid": "uid-001",
                "project_id": "cli-project",
                "project_name": "CLI Project",
                "project_path": "G:/LLM/cli-project",
                "project_workspace": "G:/LLM",
                "project_metadata": {"origin": "stdin-json"},
                "message_id": "assistant-1",
                "role": "assistant",
                "timestamp": "2026-06-18T10:00:00Z",
                "content": "中文 CLI fallback can persist context.",
                "source": "cli-test",
            }
            stdout = io.StringIO()
            raw_stdin = json.dumps(payload, ensure_ascii=False).encode("gb18030")
            with patch("sys.stdin", BytesStdin(raw_stdin, encoding="utf-8")), redirect_stdout(stdout):
                exit_code = chat_cli_main(["--db-path", str(db_path), "record-message", "--stdin-json"])
            self.assertEqual(exit_code, 0)
            recorded = json.loads(stdout.getvalue())
            self.assertEqual(recorded["inserted_count"], 1)
            self.assertEqual(recorded["conversation_id"], "Chat-Backup-Test--uid-001")
            self.assertEqual(recorded["project_id"], "cli-project")

            surrogate_payload = {
                "conversation_title": "Chat Backup Test",
                "conversation_uid": "uid-001",
                "message_id": "assistant-surrogate",
                "role": "assistant",
                "timestamp": "2026-06-18T10:00:00Z",
                "content": "CLI fallback can sanitize surrogate." + "\udcb9",
                "source": "cli-test",
            }
            stdout = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps(surrogate_payload))), redirect_stdout(stdout):
                exit_code = chat_cli_main(["--db-path", str(db_path), "record-message", "--stdin-json"])
            self.assertEqual(exit_code, 0)
            surrogate_recorded = json.loads(stdout.getvalue())
            self.assertEqual(surrogate_recorded["inserted_count"], 1)

            context_payload = {
                "conversation_title": "Chat Backup Test",
                "conversation_uid": "uid-001",
                "project_id": "cli-project",
                "project_name": "CLI Project",
                "project_path": "G:/LLM/cli-project",
                "project_workspace": "G:/LLM",
                "entry_id": "checkpoint-1",
                "kind": "checkpoint",
                "timestamp": "2026-06-18T10:00:01Z",
                "content": "Use the CLI fallback and keep chat backups separate from restore context.",
                "source": "cli-test-context",
            }
            stdout = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps(context_payload))), redirect_stdout(stdout):
                exit_code = chat_cli_main(["--db-path", str(db_path), "record-context", "--stdin-json"])
            self.assertEqual(exit_code, 0)
            context_recorded = json.loads(stdout.getvalue())
            self.assertEqual(context_recorded["inserted_count"], 1)
            self.assertEqual(context_recorded["project_id"], "cli-project")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = chat_cli_main(
                    [
                        "--db-path",
                        str(db_path),
                        "recall-context",
                        "--conversation-id",
                        "Chat-Backup-Test--uid-001",
                        "--max-messages",
                        "10",
                        "--max-context-entries",
                        "10",
                    ]
                )
            self.assertEqual(exit_code, 0)
            recalled = json.loads(stdout.getvalue())
            self.assertEqual(recalled["conversation_id"], "Chat-Backup-Test--uid-001")
            self.assertEqual(recalled["message_count"], 2)
            self.assertEqual(recalled["context_entry_count"], 1)
            self.assertIn("chat backups separate from restore context", recalled["context_text"])
            self.assertIn("中文 CLI fallback can persist context.", recalled["chat_markdown"])
            self.assertIn("CLI fallback can persist context.", recalled["chat_markdown"])
            self.assertIn("�", recalled["chat_markdown"])

    def test_admin_console_parser_defaults_to_same_port_admin(self) -> None:
        args = build_parser().parse_args([])

        self.assertIsNone(args.host)
        self.assertIsNone(args.port)
        self.assertIsNone(args.config_dir)
        self.assertFalse(args.admin_ui)
        self.assertFalse(args.no_admin_ui)
        self.assertEqual(args.admin_host, "127.0.0.1")
        self.assertEqual(args.admin_port, 8766)

    def test_admin_console_html_uses_admin_api(self) -> None:
        html = admin_console_html()
        css = ADMIN_CSS.read_text(encoding="utf-8")
        js = ADMIN_JS.read_text(encoding="utf-8")

        self.assertTrue(WEBUI_DIST.is_dir())
        self.assertTrue(ADMIN_HTML.exists())
        self.assertTrue(ADMIN_CSS.exists())
        self.assertTrue(ADMIN_JS.exists())
        self.assertEqual(html, ADMIN_HTML.read_text(encoding="utf-8"))
        self.assertIn("/admin/assets/admin.css", html)
        self.assertIn("/admin/assets/admin.js", html)
        self.assertNotIn("<script>\n", html)
        self.assertNotIn("mcp_server_install", html)
        self.assertIn("MCP 管理台", html)
        self.assertIn("总览", html)
        self.assertIn("authGate", html)
        self.assertIn("result-surface", html)
        self.assertIn("result-block", html)
        self.assertIn("navToggle", html)
        self.assertIn("closeOutput", html)
        self.assertIn("outputBackdrop", html)
        self.assertIn("output-dialog", html)
        self.assertIn("见输出弹窗", html)
        self.assertIn("templateSelect", html)
        self.assertIn("validateServer", html)
        self.assertIn("校验格式", html)
        self.assertIn("serverEditorTitle", html)
        self.assertIn("exportAllTranscripts", html)
        self.assertIn("exportAllChatTranscripts", html)
        self.assertIn("exportAllChatContexts", html)
        self.assertIn("contextConversationId", html)
        self.assertIn("recallSelectedContext", html)
        self.assertIn("persistenceWorkspace", html)
        self.assertIn("conversation-workspace", html)
        self.assertIn("chatWorkspaceEmpty", html)
        self.assertIn("busyState", html)
        self.assertIn("dirtyBadge", html)
        self.assertIn("chatReadLimit", html)
        self.assertIn("reloadSelectedConversation", html)
        self.assertIn("deleteSelectedConversation", html)
        self.assertIn("copyOutput", html)
        self.assertIn("clearChatRecords", html)
        self.assertIn("mergeChatConversations", html)
        self.assertIn("chatSearch", html)
        self.assertIn("metricProjects", html)
        self.assertIn("chatProjectBadge", html)
        self.assertIn("chatRecordKind", html)
        self.assertIn("applyChatFilter", html)
        self.assertIn("showChatSource", html)
        self.assertIn("聊天备份", html)
        self.assertIn("syncJson", html)
        self.assertIn("wizardTransport", html)
        self.assertIn("httpSessions", html)
        self.assertIn("chatConversations", html)
        self.assertIn("chatMessages", html)
        self.assertIn("chatTrackTabs", html)
        self.assertIn("chatContextEntries", html)
        self.assertIn("chatReaderPanel", html)
        self.assertIn("chatReaderDialog", html)
        self.assertIn("codexSessionRoots", html)
        self.assertIn("previewCodexSessions", html)
        self.assertIn("syncAllCodexSessions", html)

        self.assertIn(".auth-panel { display:grid; grid-template-columns:1fr;", css)
        self.assertIn(".split.wide { grid-template-columns:1fr;", css)
        self.assertIn("conversation-row-selected", css)
        self.assertIn("codex-import-grid", css)

        self.assertIn("/api/admin/tool", js)
        self.assertIn("mcp_server_install", js)
        self.assertIn("mcp_server_update", js)
        self.assertIn("mcp_template_list", js)
        self.assertIn("mcp_server_health", js)
        self.assertIn("mcp_transcript_export", js)
        self.assertIn("mcp_chat_export", js)
        self.assertIn("mcp_chat_messages", js)
        self.assertIn("mcp_chat_context", js)
        self.assertIn("mcp_chat_record_context", js)
        self.assertIn("mcp_chat_update_context", js)
        self.assertIn("mcp_chat_delete_context", js)
        self.assertIn("mcp_chat_recall", js)
        self.assertIn("mcp_chat_context_export", js)
        self.assertIn("mcp_codex_sessions_preview", js)
        self.assertIn("mcp_codex_sessions_import", js)
        self.assertIn("mcp_codex_sessions_sync", js)
        self.assertIn("mcp_secret_set", js)
        self.assertIn("config_raw", js)
        self.assertIn("editServer", js)
        self.assertIn("保存编辑并重载", js)
        self.assertIn("confirmDirty", js)
        self.assertIn("beforeunload", js)
        self.assertIn("withBusy", js)
        self.assertIn("project-group-row", js)
        self.assertIn("UTC+8", js)
        self.assertIn("导出 MD", js)
        self.assertIn("scope:'admin'", js)
        self.assertIn("showChatReader", js)
        self.assertIn("已读取会话正文", js)
        self.assertIn("startupSettingsPayload", js)
        self.assertIn("allowed_origins:parseList", js)
        self.assertIn("renderCodexCandidates", js)

        css_asset = admin_asset_response("admin.css")
        js_asset = admin_asset_response("admin.js")
        self.assertIsNotNone(css_asset)
        self.assertIsNotNone(js_asset)
        self.assertIn("text/css", css_asset[1])
        self.assertIn("javascript", js_asset[1])
        self.assertIn("chatReaderBackdrop", html)
        self.assertIn("chat-reader-modal", html)
        self.assertIn("chat-reader-dialog", html)
        self.assertIn("closeChatReader", html)
        self.assertIn("execSessions", html)
        self.assertIn("mcpRequests", html)
        self.assertIn("MCP HTTP 会话", html)
        self.assertIn("sessionWorkspace", html)
        self.assertIn("sessionDefaultCwd", html)
        self.assertIn("settingsAllowedOrigins", html)
        self.assertIn("settingsOAuthServerUrl", html)
        self.assertIn("settingsOAuthTokenSecret", html)
        self.assertIn("generateOAuthTokenSecret", html)
        self.assertIn("httpSessionBadge", html)
        self.assertIn("execSessionBadge", html)
        self.assertIn("会话默认目录", html)
        self.assertIn("命令执行目录", html)
        self.assertIn("聊天持久化", html)
        self.assertIn("聊天记录", html)
        self.assertIn("完整消息文本", html)
        self.assertIn("恢复上下文", html)
        self.assertIn("一键清空", html)

    def test_config_dir_and_default_workspace_paths_are_resolved(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            args = build_parser().parse_args(["--workspace", str(workspace)])
            runtime = build_runtime(args, RuntimePolicy("safe", ShellEnvPolicy(), False))

            self.assertEqual(runtime.config_dir, default_settings_dir())
            self.assertEqual(runtime.upstream_config_path, default_settings_dir() / "mcp-servers.json")
            self.assertEqual(runtime.settings_path, default_settings_dir() / "server-settings.json")

            config_dir = Path(tmp) / "config"
            args = build_parser().parse_args(["--workspace", str(workspace), "--config-dir", str(config_dir)])
            runtime = build_runtime(args, RuntimePolicy("safe", ShellEnvPolicy(), False))
            self.assertEqual(runtime.config_dir, config_dir)
            self.assertEqual(runtime.upstream_config_path, config_dir / "mcp-servers.json")

            upstream = Path(tmp) / "custom.json"
            args = build_parser().parse_args(["--workspace", str(workspace), "--upstream-config", str(upstream)])
            runtime = build_runtime(args, RuntimePolicy("safe", ShellEnvPolicy(), False))
            self.assertEqual(runtime.config_dir, upstream.parent)
            self.assertEqual(runtime.upstream_config_path, upstream)

    def test_startup_settings_apply_tool_profile_when_cli_omits_it(self) -> None:
        args = build_parser().parse_args([])

        apply_startup_settings(args, {"tool_profile": "read-only"})

        self.assertEqual(args.tool_profile, "read-only")

    def test_runtime_status_and_hot_update_payloads(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp-servers.json"
            manager = McpAdminManager(config_path, protocol_version="2025-06-18")
            runtime = Runtime(
                Path(tmp),
                admin_manager=manager,
                config_dir=Path(tmp),
                upstream_config_path=config_path,
                settings_path=Path(tmp) / "server-settings.json",
                startup_settings={
                    "oauth_server_url": "https://mcp.example.com",
                    "oauth_token_secret": "11" * 32,
                    "auth_token": "mcp-secret",
                },
                server_host="127.0.0.1",
                server_port=8765,
                admin_ui_enabled=True,
                allowed_origins=("chrome-extension://currentextension",),
            )

            status = runtime.admin_status_payload(base_url="http://127.0.0.1:8765")
            self.assertTrue(status["ok"])
            self.assertEqual(status["config_paths"]["upstream_config"], str(config_path))
            self.assertIn("server_info", status)
            self.assertEqual(status["startup_settings"]["oauth_server_url"], "https://mcp.example.com")
            self.assertTrue(status["startup_settings"]["oauth_token_secret_configured"])
            self.assertTrue(status["startup_settings"]["auth_token_configured"])
            self.assertNotIn("oauth_token_secret", status["startup_settings"])
            self.assertNotIn("auth_token", status["startup_settings"])
            self.assertEqual(status["runtime"]["tool_profile"], "full")
            self.assertEqual(status["auth"]["allowed_origins"], ["chrome-extension://currentextension"])
            self.assertGreater(status["templates"]["template_count"], 0)

            runtime.record_mcp_http_access(
                session_id="session-a",
                method="POST",
                path="/mcp",
                rpc_method="tools/list",
                status=200,
                remote_addr="127.0.0.1",
                user_agent="unit-test",
                protocol_version="2025-06-18",
            )
            status = runtime.admin_status_payload(base_url="http://127.0.0.1:8765")
            self.assertEqual(status["runtime"]["http_session_count"], 1)
            self.assertEqual(status["http_sessions"][0]["session_id"], "session-a")
            self.assertEqual(status["http_sessions"][0]["last_rpc_method"], "tools/list")
            self.assertEqual(status["recent_mcp_requests"][0]["path"], "/mcp")

            result = runtime.apply_runtime_update({"auth_token": "mcp-token", "admin_token": "admin-token", "default_cwd": "."})
            self.assertTrue(result["ok"])
            self.assertEqual(runtime.auth_token, "mcp-token")
            self.assertEqual(runtime.admin_token, "admin-token")

            saved = runtime.save_startup_settings(
                {
                    "host": "0.0.0.0",
                    "port": 9123,
                    "workspace": str(Path(tmp)),
                    "tool_profile": "read-only",
                    "allowed_origins": [
                        "chrome-extension://KNGIAFGKDNLKGMEFDAFAIBKIBEGKCAEF",
                        "http://LOCALHOST:8181/",
                        "not-an-origin",
                    ],
                }
            )
            self.assertTrue(saved["ok"])
            self.assertTrue(saved["requires_restart"])
            self.assertEqual(saved["settings"]["tool_profile"], "read-only")
            self.assertEqual(
                saved["settings"]["allowed_origins"],
                [
                    "chrome-extension://kngiafgkdnlkgmefdafaibkibegkcaef",
                    "http://localhost:8181",
                ],
            )
            self.assertIn("uvx coding-tools-mcp", saved["restart_command"])
            self.assertIn("--host 0.0.0.0", saved["restart_command"])
            self.assertIn("--port 9123", saved["restart_command"])
            self.assertIn("--allowed-origin chrome-extension://kngiafgkdnlkgmefdafaibkibegkcaef", saved["restart_command"])
            self.assertNotIn("currentextension", saved["restart_command"])

    def test_oauth_tokens_preserve_admin_scope(self) -> None:
        cfg = OAuthConfig(None, None, "password", "http://127.0.0.1:8765", b"1" * 32)
        token = _create_oauth_token(cfg, "http://127.0.0.1:8765", scope="admin")
        claims = _decode_oauth_token(token, cfg, "http://127.0.0.1:8765")

        self.assertIsNotNone(claims)
        self.assertEqual(claims.get("scope"), "admin")

    def test_oauth_token_secret_is_persisted_for_restarts(self) -> None:
        with TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "server-settings.json"
            settings: dict[str, object] = {}
            with patch.dict(os.environ, {"CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET": ""}):
                token_secret = _resolve_oauth_token_secret(settings, settings_path)

            self.assertEqual(len(token_secret), 32)
            self.assertEqual(settings["oauth_token_secret"], token_secret.hex())
            persisted = read_server_settings(settings_path)
            self.assertEqual(persisted["oauth_token_secret"], token_secret.hex())

            with patch.dict(os.environ, {"CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET": ""}):
                restarted_secret = _resolve_oauth_token_secret(read_server_settings(settings_path), settings_path)

            self.assertEqual(restarted_secret, token_secret)

    def test_oauth_token_secret_env_overrides_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "server-settings.json"
            settings: dict[str, object] = {"oauth_token_secret": "11" * 32}
            env_secret = "22" * 32

            with patch.dict(os.environ, {"CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET": env_secret}):
                token_secret = _resolve_oauth_token_secret(settings, settings_path)

            self.assertEqual(token_secret, bytes.fromhex(env_secret))
            self.assertFalse(settings_path.exists())

    def test_same_port_admin_routes_are_served_by_mcp_handler(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")
            runtime = Runtime(Path(tmp), admin_token="admin-token", admin_manager=manager, admin_ui_enabled=True)
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                html = urllib.request.urlopen(f"{base_url}/admin", timeout=5).read().decode("utf-8")
                self.assertIn("MCP 管理台", html)
                self.assertIn("/admin/assets/admin.css", html)
                self.assertIn("/admin/assets/admin.js", html)

                with urllib.request.urlopen(f"{base_url}/admin/assets/admin.css", timeout=5) as response:
                    self.assertIn("text/css", response.headers.get("Content-Type", ""))
                    self.assertIn("app-shell", response.read().decode("utf-8"))
                with urllib.request.urlopen(f"{base_url}/admin/assets/admin.js", timeout=5) as response:
                    self.assertIn("javascript", response.headers.get("Content-Type", ""))
                    self.assertIn("mcp_server_install", response.read().decode("utf-8"))

                req = urllib.request.Request(f"{base_url}/api/admin/status", headers={"Authorization": "Bearer admin-token"})
                status = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
                self.assertTrue(status["ok"])
                self.assertEqual(status["server"]["admin_endpoint"], "/admin")

                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
                }
                mcp_req = urllib.request.Request(
                    f"{base_url}/mcp",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "admin-test"},
                )
                with urllib.request.urlopen(mcp_req, timeout=5) as response:
                    mcp_session_id = response.headers.get("Mcp-Session-Id")
                    self.assertIsNotNone(mcp_session_id)
                    self.assertIn("result", json.loads(response.read().decode("utf-8")))

                req = urllib.request.Request(f"{base_url}/api/admin/status", headers={"Authorization": "Bearer admin-token"})
                status = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
                self.assertEqual(status["runtime"]["http_session_count"], 1)
                self.assertEqual(status["http_sessions"][0]["session_id"], mcp_session_id)
                self.assertEqual(status["http_sessions"][0]["last_rpc_method"], "initialize")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_admin_console_handler_can_bind_as_separate_server(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = McpAdminManager(Path(tmp) / "mcp.json", protocol_version="2025-06-18")
            runtime = Runtime(Path(tmp), admin_token="admin-token", admin_manager=manager)
            server = RuntimeHTTPServer(("127.0.0.1", 0), AdminUIHandler, runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                self.assertIs(server.runtime, runtime)
                self.assertNotEqual(server.server_address[1], 0)
                html = urllib.request.urlopen(f"{base_url}/admin", timeout=5).read().decode("utf-8")
                self.assertIn("/admin/assets/admin.css", html)
                self.assertIn("/admin/assets/admin.js", html)
                with urllib.request.urlopen(f"{base_url}/admin/assets/admin.css", timeout=5) as response:
                    self.assertIn("text/css", response.headers.get("Content-Type", ""))
                with urllib.request.urlopen(f"{base_url}/admin/assets/admin.js", timeout=5) as response:
                    self.assertIn("javascript", response.headers.get("Content-Type", ""))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def filesystem_spec() -> dict[str, object]:
    return {
        "alias": "filesystem",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-filesystem", "G:/LLM"],
        "env": {"TOKEN": {"secret_ref": "github_token"}},
        "include_tools": ["read_file"],
    }


if __name__ == "__main__":
    unittest.main()
