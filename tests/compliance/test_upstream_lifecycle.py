from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from tempfile import TemporaryDirectory

from coding_tools_mcp import upstream as upstream_module
from coding_tools_mcp.server import Runtime
from coding_tools_mcp.upstream import (
    BaseUpstreamClient,
    HttpUpstreamClient,
    UpstreamError,
    UpstreamServerConfig,
)
from coding_tools_mcp.upstream_result import RESULT_INLINE_MAX
from coding_tools_mcp.upstream_result_store import ResultNotFound
from tests.compliance.test_upstream_gateway import FakeUpstreamClient, build_manager


BLOCKING_TOOL = {
    "name": "blocking_read",
    "description": "Wait until the test releases the active call.",
    "inputSchema": {"type": "object", "additionalProperties": True},
    "annotations": {"readOnlyHint": True},
}


class BlockingClient(BaseUpstreamClient):
    def __init__(self, config: UpstreamServerConfig) -> None:
        super().__init__(config, "2025-11-25")
        self.started = threading.Event()
        self.release = threading.Event()
        self.transport_closed = threading.Event()
        self.close_calls = 0

    def initialize(self) -> None:
        return None

    def list_tools(self) -> list[dict[str, object]]:
        return [dict(BLOCKING_TOOL)]

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if method != "tools/call":
            raise AssertionError(method)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release call")
        return {
            "content": [{"type": "text", "text": "completed"}],
            "structuredContent": {"completed": True, "params": params or {}},
            "isError": False,
        }

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        raise AssertionError(method)

    def _close_transport(self) -> None:
        self.close_calls += 1
        self.transport_closed.set()


class UpstreamLifecycleTests(unittest.TestCase):
    def config(self, alias: str = "remote") -> UpstreamServerConfig:
        return UpstreamServerConfig(
            alias=alias,
            transport="streamable_http",
            url=f"http://127.0.0.1/{alias}",
            expose_mode="broker",
        )

    def test_started_call_completes_and_close_waits_for_lease(self) -> None:
        config = self.config()
        client = BlockingClient(config)
        manager = build_manager([config], [client])  # type: ignore[list-item]
        call_result: list[dict[str, object]] = []

        call_thread = threading.Thread(
            target=lambda: call_result.append(
                manager.call_tool("remote__blocking_read", {"value": 1})
            )
        )
        call_thread.start()
        self.assertTrue(client.started.wait(timeout=2))

        close_thread = threading.Thread(target=manager.close)
        close_thread.start()
        time.sleep(0.05)
        self.assertTrue(close_thread.is_alive(), "close must wait for the active call lease")
        self.assertFalse(client.transport_closed.is_set())

        client.release.set()
        call_thread.join(timeout=2)
        close_thread.join(timeout=2)
        self.assertFalse(call_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertFalse(call_result[0]["isError"])
        self.assertTrue(client.transport_closed.is_set())
        self.assertEqual(client.close_calls, 1)

    def test_close_waits_through_result_store_postprocessing(self) -> None:
        config = self.config()
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        entered = threading.Event()
        release = threading.Event()
        original_normalize = upstream_module.normalize_tool_result
        large_result = {
            "content": [{"type": "text", "text": "x" * (RESULT_INLINE_MAX + 4_096)}],
            "structuredContent": {"value": "large"},
            "isError": False,
        }

        def blocking_normalize(value: dict[str, object]) -> dict[str, object]:
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release postprocessing")
            return original_normalize(value)

        results: list[dict[str, object]] = []
        with patch.object(client, "call_tool_raw", return_value=large_result), patch.object(
            upstream_module,
            "normalize_tool_result",
            side_effect=blocking_normalize,
        ):
            call_thread = threading.Thread(
                target=lambda: results.append(
                    manager.call_tool(
                        "remote__search",
                        {"q": "race"},
                        result_owner="owner",
                        store_overflow=True,
                    )
                )
            )
            call_thread.start()
            self.assertTrue(entered.wait(timeout=2))
            close_thread = threading.Thread(target=manager.close)
            close_thread.start()
            time.sleep(0.05)
            self.assertTrue(close_thread.is_alive())
            self.assertEqual(manager.result_store.owner_handle_count("owner"), 0)
            release.set()
            call_thread.join(timeout=3)
            close_thread.join(timeout=3)

        self.assertFalse(call_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertIn("_result_handle", results[0]["structuredContent"])
        self.assertEqual(manager.result_store.owner_handle_count("owner"), 0)

    def test_one_hundred_calls_and_close_have_no_deadlock(self) -> None:
        config = self.config()
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        barrier = threading.Barrier(101)
        release = threading.Event()
        original_normalize = upstream_module.normalize_tool_result
        results: list[dict[str, object]] = []
        results_lock = threading.Lock()

        def blocking_normalize(value: dict[str, object]) -> dict[str, object]:
            barrier.wait(timeout=10)
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release calls")
            return original_normalize(value)

        def invoke(index: int) -> None:
            result = manager.call_tool("remote__search", {"q": str(index)})
            with results_lock:
                results.append(result)

        with patch.object(
            upstream_module,
            "normalize_tool_result",
            side_effect=blocking_normalize,
        ):
            threads = [threading.Thread(target=invoke, args=(index,)) for index in range(100)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=10)
            close_thread = threading.Thread(target=manager.close)
            close_thread.start()
            time.sleep(0.05)
            self.assertTrue(close_thread.is_alive())
            release.set()
            for thread in threads:
                thread.join(timeout=5)
            close_thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(len(results), 100)
        self.assertTrue(all(not result["isError"] for result in results))

    def test_close_prevents_new_calls_with_retryable_not_available(self) -> None:
        config = self.config()
        client = FakeUpstreamClient(config, "2025-11-25")
        manager = build_manager([config], [client])
        manager.close()
        manager.close()

        result = manager.call_tool("remote__search", {"q": "after-close"})
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "UPSTREAM_NOT_AVAILABLE")
        self.assertTrue(error["retryable"])
        self.assertEqual(client.close_calls, 1)
        real_client = HttpUpstreamClient(config, "2025-11-25")
        real_client.close()
        with self.assertRaises(UpstreamError) as raised:
            real_client.call_tool_raw("search", {"q": "direct-after-close"})
        self.assertEqual(raised.exception.code, "UPSTREAM_NOT_AVAILABLE")
        self.assertTrue(raised.exception.retryable)

    def test_two_runtimes_do_not_share_clients_catalog_sessions_or_result_store(self) -> None:
        first_config = self.config("remote")
        second_config = self.config("remote")
        first_client = FakeUpstreamClient(first_config, "2025-11-25", marker="first")
        second_client = FakeUpstreamClient(second_config, "2025-11-25", marker="second")
        first_manager = build_manager([first_config], [first_client])
        second_manager = build_manager([second_config], [second_client])

        self.assertIsNot(first_manager.state.clients["remote"], second_manager.state.clients["remote"])
        self.assertIsNot(first_manager.state.catalog, second_manager.state.catalog)
        self.assertIsNot(first_manager.state.search_index, second_manager.state.search_index)
        self.assertIsNot(first_manager.result_store, second_manager.result_store)

        first_owner = "first-owner"
        handle = first_manager.result_store.store(
            "first-only",
            owner=first_owner,
            server_alias="remote",
        )
        self.assertEqual(
            first_manager.result_store.fetch(str(handle), owner=first_owner).text,
            "first-only",
        )
        with self.assertRaises(ResultNotFound):
            second_manager.result_store.fetch(str(handle), owner=first_owner)

        first_http = HttpUpstreamClient(first_config, "2025-11-25")
        second_http = HttpUpstreamClient(second_config, "2025-11-25")
        first_http.session_id = "session-first"
        second_http.session_id = "session-second"
        self.assertNotEqual(first_http.session_id, second_http.session_id)
        first_http.close()
        second_http.close()

        with TemporaryDirectory() as first_tmp, TemporaryDirectory() as second_tmp:
            first_runtime = Runtime(Path(first_tmp), upstream_manager=first_manager, transport="http")
            second_runtime = Runtime(Path(second_tmp), upstream_manager=second_manager, transport="http")
            self.assertNotEqual(first_runtime.http_session_id, second_runtime.http_session_id)
            first_runtime.close()
            self.assertTrue(first_client.closed)
            self.assertFalse(second_client.closed)
            result = second_runtime.call_tool(
                "upstream_tool_call",
                {"name": "remote__search", "arguments": {"q": "still-open"}},
            )
            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["marker"], "second")
            second_runtime.close()

    def test_restart_is_old_runtime_close_then_new_runtime_construction(self) -> None:
        old_config = self.config("remote")
        old_client = FakeUpstreamClient(old_config, "2025-11-25", marker="old")
        old_manager = build_manager([old_config], [old_client])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_runtime = Runtime(root, upstream_manager=old_manager)
            old_catalog = old_manager.state.catalog
            old_runtime.close()

            new_config = UpstreamServerConfig(
                alias="remote",
                transport="streamable_http",
                url="http://127.0.0.1/new",
                expose_mode="broker",
                tags=("new",),
            )
            new_client = FakeUpstreamClient(new_config, "2025-11-25", marker="new")
            new_manager = build_manager([new_config], [new_client])
            new_runtime = Runtime(root, upstream_manager=new_manager)

            self.assertTrue(old_client.closed)
            self.assertFalse(new_client.closed)
            self.assertIsNot(old_catalog, new_manager.state.catalog)
            self.assertEqual(
                new_manager.catalog_entry("remote__search").tags,  # type: ignore[union-attr]
                ("new",),
            )
            result = new_runtime.call_tool(
                "upstream_tool_call",
                {"name": "remote__search", "arguments": {"q": "new"}},
            )
            self.assertEqual(result["structuredContent"]["marker"], "new")
            new_runtime.close()


if __name__ == "__main__":
    unittest.main()
