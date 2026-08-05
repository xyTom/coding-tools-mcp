from __future__ import annotations

import argparse
import copy
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
os.environ.setdefault("CODING_TOOLS_MCP_TELEMETRY", "off")

import coding_tools_mcp.upstream as upstream_module  # noqa: E402
from coding_tools_mcp.server import Runtime, TOOL_REGISTRY  # noqa: E402
from coding_tools_mcp.upstream import (  # noqa: E402
    BaseUpstreamClient,
    UpstreamConfigSnapshot,
    UpstreamManager,
    UpstreamServerConfig,
    load_upstream_config_snapshot,
)
from coding_tools_mcp.upstream_result import RESULT_INLINE_MAX, result_json_bytes  # noqa: E402
from coding_tools_mcp.upstream_search import (  # noqa: E402
    CatalogSearchIndex,
    ToolSearchFilters,
    UpstreamToolCatalogEntry,
)

BROKER_TOOLS = {
    "upstream_tool_search",
    "upstream_tool_describe",
    "upstream_tool_call",
    "upstream_tool_call_mutating",
    "upstream_result_fetch",
}

RELEASE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_repositories",
        "title": "Repository Search",
        "description": "Search remote source repositories.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "$defs": {"raw_only": {"type": "string"}},
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
        "x-raw-canary": "must-not-leak",
    },
    {
        "name": "create_issue",
        "title": "Issue Creation",
        "description": "Create an issue on the remote service.",
        "inputSchema": {
            "type": "object",
            "properties": {"title": {"type": "string", "minLength": 1}},
            "required": ["title"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "export_archive",
        "title": "Archive Export",
        "description": "Export a large archive result.",
        "inputSchema": {"type": "object", "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]

SEARCH_FIXTURES: list[tuple[str, str, str, str]] = [
    ("repo_search", "Repository Search", "搜索仓库", "Search source repositories and code."),
    ("issue_create", "Issue Creation", "创建问题", "Create a remote issue."),
    ("branch_list", "Branch Listing", "列出分支", "List repository branches."),
    ("pull_request_merge", "Pull Request Merge", "合并请求", "Merge a pull request."),
    ("file_read", "File Reading", "读取文件", "Read file content."),
    ("file_write", "File Writing", "写入文件", "Write file content."),
    ("file_delete", "File Deletion", "删除文件", "Delete a file."),
    ("directory_list", "Directory Listing", "列出目录", "List directory entries."),
    ("database_query", "Database Query", "查询数据库", "Query database rows."),
    ("service_deploy", "Service Deployment", "部署服务", "Deploy a service release."),
    ("container_logs", "Container Logs", "容器日志", "Read container logs."),
    ("page_screenshot", "Page Screenshot", "网页截图", "Capture a page screenshot."),
    ("browser_navigate", "Browser Navigation", "浏览器导航", "Navigate a browser page."),
    ("element_click", "Element Click", "点击元素", "Click a page element."),
    ("literature_search", "Literature Search", "检索文献", "Search papers and literature."),
    ("citation_export", "Citation Export", "导出引用", "Export citation records."),
    ("collection_list", "Collection Listing", "列出收藏", "List library collections."),
    ("annotation_search", "Annotation Search", "搜索批注", "Search annotations and notes."),
    ("nmr_analyze", "NMR Analysis", "核磁分析", "Analyze NMR spectroscopy data."),
    ("spectrum_integrate", "Spectrum Integration", "谱图积分", "Integrate spectrum peaks."),
    ("artifact_download", "Artifact Download", "下载制品", "Download a build artifact."),
    ("server_status", "Server Status", "服务器状态", "Read server health status."),
    ("table_schema", "Table Schema", "表结构", "Inspect a database table schema."),
    ("record_fetch", "Record Fetch", "获取记录", "Fetch one record."),
    ("record_update", "Record Update", "修改记录", "Update one record."),
    ("record_create", "Record Creation", "创建记录", "Create one record."),
    ("record_remove", "Record Removal", "删除记录", "Remove one record."),
    ("git_commit", "Git Commit", "提交代码", "Commit source changes."),
    ("git_push", "Git Push", "推送提交", "Push commits to a remote."),
    ("git_checkout", "Git Checkout", "切换分支", "Checkout a Git branch."),
]

CUSTOM_SYNONYMS: dict[str, tuple[str, ...]] = {
    "仓库": ("repository", "repo"),
    "问题": ("issue",),
    "请求": ("request",),
    "日志": ("logs", "log"),
    "网页": ("page", "web"),
    "元素": ("element",),
    "导出": ("export",),
    "核磁": ("nmr",),
    "谱图": ("spectrum",),
    "积分": ("integrate", "integration"),
    "状态": ("status",),
    "结构": ("schema",),
    "记录": ("record",),
    "推送": ("push",),
    "切换": ("checkout",),
    "制品": ("artifact",),
}


class ReleaseClient(BaseUpstreamClient):
    def __init__(self, config: UpstreamServerConfig, marker: str) -> None:
        super().__init__(config, "2025-11-25")
        self.marker = marker
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "initialize":
            return {}
        if method == "tools/list":
            return {"tools": copy.deepcopy(RELEASE_TOOLS)}
        if method != "tools/call" or not isinstance(params, dict):
            raise AssertionError(f"Unexpected release validation request: {method}")
        name = str(params.get("name"))
        arguments = params.get("arguments")
        arguments_dict = copy.deepcopy(arguments) if isinstance(arguments, dict) else {}
        self.calls.append((name, arguments_dict))
        if name == "export_archive":
            return {
                "content": [{"type": "text", "text": "Archive export completed."}],
                "structuredContent": {
                    "ok": True,
                    "marker": self.marker,
                    "archive": "数据🙂" * 100_000,
                },
                "isError": False,
            }
        return {
            "content": [{"type": "text", "text": f"called {name}"}],
            "structuredContent": {
                "ok": True,
                "marker": self.marker,
                "remote_name": name,
                "arguments": arguments_dict,
            },
            "isError": False,
        }

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        del method, params


def build_manager(snapshot: UpstreamConfigSnapshot, client: ReleaseClient) -> UpstreamManager:
    with patch.object(upstream_module, "build_client", return_value=client):
        return UpstreamManager(
            snapshot.configs,
            reserved_names=set(TOOL_REGISTRY),
            custom_synonyms=snapshot.custom_synonyms,
        )


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[position]


def build_search_entries(count: int | None = None) -> dict[str, UpstreamToolCatalogEntry]:
    entries: dict[str, UpstreamToolCatalogEntry] = {}
    total = count if count is not None else len(SEARCH_FIXTURES)
    for index in range(total):
        name, english, chinese, description = SEARCH_FIXTURES[index % len(SEARCH_FIXTURES)]
        suffix = "" if count is None else f"_{index:03d}"
        public_name = f"fixture__{name}{suffix}"
        entries[public_name] = UpstreamToolCatalogEntry(
            public_name=public_name,
            server_alias="fixture",
            remote_name=f"{name}{suffix}",
            title=f"{english} / {chinese}",
            description=f"{description} Synthetic catalog item {index}.",
            tags=(name.split("_")[0], "release"),
            argument_names=("query", "name"),
            effective_risk="readonly" if index % 3 else "mutating",
            public_schema_digest=f"{index:032x}"[-32:],
        )
    return entries


def validate_search_quality_and_performance() -> dict[str, Any]:
    quality_index = CatalogSearchIndex(CUSTOM_SYNONYMS)
    quality_entries = build_search_entries()
    quality_index.build(quality_entries)

    fixture_results: list[dict[str, Any]] = []
    top1_hits = 0
    top5_hits = 0
    for index, (name, english, chinese, _description) in enumerate(SEARCH_FIXTURES):
        query = english.lower() if index % 2 == 0 else chinese
        expected = f"fixture__{name}"
        results = quality_index.search(query, ToolSearchFilters(limit=5))
        names = [result.public_name for result in results]
        top1 = names[0] if names else None
        top1_hits += int(top1 == expected)
        top5_hits += int(expected in names)
        fixture_results.append(
            {
                "query": query,
                "expected": expected,
                "top1": top1,
                "top5": names,
            }
        )

    if top5_hits != len(SEARCH_FIXTURES):
        raise AssertionError(f"Search top-5 recall regressed: {top5_hits}/{len(SEARCH_FIXTURES)}")

    performance_entries = build_search_entries(500)
    build_times: list[float] = []
    for _ in range(25):
        started = time.perf_counter()
        index = CatalogSearchIndex(CUSTOM_SYNONYMS)
        index.build(performance_entries)
        build_times.append((time.perf_counter() - started) * 1000.0)

    performance_index = CatalogSearchIndex(CUSTOM_SYNONYMS)
    performance_index.build(performance_entries)
    queries = [english.lower() if i % 2 == 0 else chinese for i, (_, english, chinese, _) in enumerate(SEARCH_FIXTURES)]
    search_times: list[float] = []
    for iteration in range(600):
        query = queries[iteration % len(queries)]
        started = time.perf_counter()
        performance_index.search(query, ToolSearchFilters(limit=5))
        search_times.append((time.perf_counter() - started) * 1000.0)

    return {
        "fixture_count": len(SEARCH_FIXTURES),
        "top1_hits": top1_hits,
        "top1_rate": round(top1_hits / len(SEARCH_FIXTURES), 4),
        "top5_hits": top5_hits,
        "top5_rate": round(top5_hits / len(SEARCH_FIXTURES), 4),
        "fixtures": fixture_results,
        "performance": {
            "catalog_size": len(performance_entries),
            "build_iterations": len(build_times),
            "build_ms_p50": round(statistics.median(build_times), 4),
            "build_ms_p95": round(percentile(build_times, 0.95), 4),
            "search_iterations": len(search_times),
            "search_ms_p50": round(statistics.median(search_times), 4),
            "search_ms_p95": round(percentile(search_times, 0.95), 4),
        },
    }


def validate_runtime_smoke() -> dict[str, Any]:
    if not BROKER_TOOLS.issubset(TOOL_REGISTRY):
        raise AssertionError("The five fixed Broker tools are not all registered.")

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        config_path = root / "mcp-servers.json"
        direct_document = {
            "servers": {
                "release": {
                    "transport": "streamable_http",
                    "url": "http://127.0.0.1/release",
                    "expose_mode": "direct",
                    "tags": ["release", "smoke"],
                }
            },
            "tool_search": {"custom_synonyms": {"仓库": ["repository", "repo"]}},
        }
        broker_document = copy.deepcopy(direct_document)
        broker_document["servers"]["release"]["expose_mode"] = "broker"
        broker_document["servers"]["release"]["pinned_tools"] = ["search_repositories"]

        config_path.write_text(json.dumps(direct_document, ensure_ascii=False), encoding="utf-8")
        direct_snapshot = load_upstream_config_snapshot(config_path)
        direct_client = ReleaseClient(direct_snapshot.configs[0], "direct-runtime")
        direct_manager = build_manager(direct_snapshot, direct_client)
        direct_workspace = root / "direct"
        direct_workspace.mkdir()
        direct_runtime = Runtime(direct_workspace, upstream_manager=direct_manager, transport="http")
        direct_initialized = direct_runtime.initialize({"name": "release-check", "version": "1"})
        direct_names_before = {item["name"] for item in direct_runtime.list_tools()["tools"]}
        direct_report = direct_manager.status_payload()["exposure_report"]

        config_path.write_text(json.dumps(broker_document, ensure_ascii=False), encoding="utf-8")
        broker_snapshot = load_upstream_config_snapshot(config_path)
        broker_client = ReleaseClient(broker_snapshot.configs[0], "broker-runtime")
        broker_manager = build_manager(broker_snapshot, broker_client)
        broker_workspace = root / "broker"
        broker_workspace.mkdir()
        broker_runtime = Runtime(broker_workspace, upstream_manager=broker_manager, transport="http")
        broker_initialized = broker_runtime.initialize({"name": "release-check", "version": "1"})
        broker_names = {item["name"] for item in broker_runtime.list_tools()["tools"]}
        broker_report = broker_manager.status_payload()["exposure_report"]

        direct_names_after = {item["name"] for item in direct_runtime.list_tools()["tools"]}
        if direct_names_after != direct_names_before:
            raise AssertionError("Existing direct Runtime changed after persisted config update.")
        if direct_initialized["capabilities"]["tools"]["listChanged"]:
            raise AssertionError("Direct Runtime advertised listChanged=true.")
        if broker_initialized["capabilities"]["tools"]["listChanged"]:
            raise AssertionError("Broker Runtime advertised listChanged=true.")
        if not {"release__search_repositories", "release__create_issue", "release__export_archive"}.issubset(direct_names_before):
            raise AssertionError("Legacy direct Runtime did not expose all upstream tools.")
        if "release__search_repositories" not in broker_names:
            raise AssertionError("Pinned Broker tool was not directly exposed.")
        if {"release__create_issue", "release__export_archive"} & broker_names:
            raise AssertionError("Broker-only tools leaked into tools/list.")

        search_read = broker_runtime.call_tool("upstream_tool_search", {"query": "search repositories", "limit": 5})
        read_name = search_read["structuredContent"]["results"][0]["name"]
        describe_read = broker_runtime.call_tool("upstream_tool_describe", {"name": read_name})
        describe_read_payload = describe_read["structuredContent"]
        read_call = broker_runtime.call_tool(
            "upstream_tool_call",
            {
                "name": read_name,
                "arguments": {"query": "stable catalog"},
                "schema_digest": describe_read_payload["schema_digest"],
            },
        )
        if read_call.get("isError"):
            raise AssertionError("Readonly Broker smoke call failed.")

        search_mutating = broker_runtime.call_tool("upstream_tool_search", {"query": "create issue", "limit": 5})
        mutating_name = search_mutating["structuredContent"]["results"][0]["name"]
        describe_mutating = broker_runtime.call_tool("upstream_tool_describe", {"name": mutating_name})
        mutating_digest = describe_mutating["structuredContent"]["schema_digest"]
        mutating_call = broker_runtime.call_tool(
            "upstream_tool_call_mutating",
            {
                "name": mutating_name,
                "arguments": {"title": "Release validation"},
                "schema_digest": mutating_digest,
            },
        )
        if mutating_call.get("isError"):
            raise AssertionError("Mutating Broker smoke call failed.")

        search_large = broker_runtime.call_tool("upstream_tool_search", {"query": "export archive", "limit": 5})
        large_name = search_large["structuredContent"]["results"][0]["name"]
        describe_large = broker_runtime.call_tool("upstream_tool_describe", {"name": large_name})
        large_result = broker_runtime.call_tool(
            "upstream_tool_call",
            {
                "name": large_name,
                "arguments": {},
                "schema_digest": describe_large["structuredContent"]["schema_digest"],
            },
        )
        envelope_bytes = len(result_json_bytes(large_result))
        if envelope_bytes > RESULT_INLINE_MAX:
            raise AssertionError(f"Final envelope exceeded budget: {envelope_bytes}")
        large_meta = large_result.get("structuredContent", {})
        handle = large_meta.get("_result_handle")
        if not isinstance(handle, str) or not handle:
            raise AssertionError("Oversized Broker result did not return a result handle.")
        fetched = broker_runtime.call_tool(
            "upstream_result_fetch",
            {"handle": handle, "offset": 0, "limit": 32_000},
        )
        fetched_payload = fetched["structuredContent"]
        if not fetched_payload.get("text") or fetched_payload.get("offset") != 0:
            raise AssertionError("Result fetch did not return the first Unicode page.")

        second_workspace = root / "second-session"
        second_workspace.mkdir()
        second_runtime = Runtime(second_workspace, upstream_manager=broker_manager, transport="http")
        cross_session = second_runtime.call_tool(
            "upstream_result_fetch",
            {"handle": handle, "offset": 0, "limit": 100},
        )
        cross_code = cross_session["structuredContent"]["error"]["code"]
        if cross_code != "UPSTREAM_RESULT_NOT_FOUND":
            raise AssertionError(f"Cross-session handle leaked: {cross_code}")

        public_outputs = json.dumps(
            {
                "search": search_read,
                "describe": describe_read,
                "tools": broker_runtime.list_tools(),
            },
            ensure_ascii=False,
        )
        if "x-raw-canary" in public_outputs or "must-not-leak" in public_outputs or '"$defs"' in public_outputs:
            raise AssertionError("Raw upstream definition leaked into a public path.")
        if hasattr(broker_manager, "start_server") or hasattr(broker_manager, "stop_server"):
            raise AssertionError("Dynamic manager lifecycle API was introduced.")

        result = {
            "fixed_local_tool_count": len(TOOL_REGISTRY),
            "fixed_broker_tools": sorted(BROKER_TOOLS),
            "direct_runtime": {
                "tools_list_count": len(direct_names_before),
                "upstream_direct_count": direct_report["direct"]["count"],
                "upstream_direct_definition_bytes": direct_report["direct"]["definition_bytes"],
                "catalog_count": direct_report["catalog"]["count"],
                "broker_only_count": direct_report["catalog"]["broker_only_count"],
            },
            "broker_runtime": {
                "tools_list_count": len(broker_names),
                "upstream_direct_count": broker_report["direct"]["count"],
                "upstream_direct_definition_bytes": broker_report["direct"]["definition_bytes"],
                "catalog_count": broker_report["catalog"]["count"],
                "broker_only_count": broker_report["catalog"]["broker_only_count"],
            },
            "old_runtime_unchanged_after_write": direct_names_after == direct_names_before,
            "list_changed": False,
            "readonly_call": read_name,
            "mutating_call": mutating_name,
            "large_result": {
                "tool": large_name,
                "final_envelope_bytes": envelope_bytes,
                "budget_bytes": RESULT_INLINE_MAX,
                "handle_present": True,
                "fetch_codepoints": len(fetched_payload["text"]),
                "fetch_eof": bool(fetched_payload["eof"]),
            },
            "cross_session_fetch_error": cross_code,
            "raw_definition_leak": False,
            "dynamic_manager_lifecycle": False,
        }

        second_runtime.close()
        broker_runtime.close()
        direct_runtime.close()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the stable-catalog upstream Broker release contract.")
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    args = parser.parse_args()

    report = {
        "schema_version": 1,
        "status": "pass",
        "stable_catalog_adaptation": "complete",
        "phase_1_defensive_controls": "complete",
        "phase_2_broker": "complete",
        "dynamic_list_changed_profile_activation": "not_implemented",
        "runtime_smoke": validate_runtime_smoke(),
        "search_validation": validate_search_quality_and_performance(),
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
