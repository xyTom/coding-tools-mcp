from __future__ import annotations

import threading
import unittest
from collections import Counter
from typing import Any
from unittest.mock import patch

from coding_tools_mcp import upstream as upstream_module
from coding_tools_mcp.upstream import (
    BaseUpstreamClient,
    UpstreamCatalogTemplate,
    UpstreamConfigSnapshot,
    UpstreamManager,
    UpstreamServerConfig,
    build_upstream_catalog_template,
)


TOOL = {
    "name": "search",
    "title": "Search",
    "description": "Synthetic immutable discovery tool.",
    "inputSchema": {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True},
}


class DiscoveryClient(BaseUpstreamClient):
    def __init__(self, config: UpstreamServerConfig, counters: Counter[str]) -> None:
        super().__init__(config, "2025-11-25")
        self.counters = counters

    def initialize(self) -> None:
        self.counters[f"discover_init:{self.config.alias}"] += 1

    def list_tools(self) -> list[dict[str, Any]]:
        self.counters[f"discover_list:{self.config.alias}"] += 1
        return [dict(TOOL)]

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del method, params
        raise AssertionError("DiscoveryClient request path is not used")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        del method, params
        raise AssertionError("DiscoveryClient notify path is not used")

    def _close_transport(self) -> None:
        self.counters[f"discover_close:{self.config.alias}"] += 1


class LiveClient(BaseUpstreamClient):
    def __init__(
        self,
        config: UpstreamServerConfig,
        counters: Counter[str],
        *,
        initialize_entered: threading.Event | None = None,
        initialize_release: threading.Event | None = None,
    ) -> None:
        super().__init__(config, "2025-11-25")
        self.counters = counters
        self.initialize_entered = initialize_entered
        self.initialize_release = initialize_release

    def initialize(self) -> None:
        self.counters[f"live_init:{self.config.alias}"] += 1
        if self.initialize_entered is not None:
            self.initialize_entered.set()
        if self.initialize_release is not None:
            self.initialize_release.wait(timeout=3)

    def list_tools(self) -> list[dict[str, Any]]:
        raise AssertionError("lazy live client must not rediscover tools/list")

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method != "tools/call":
            raise AssertionError(method)
        self.counters[f"tools_call:{self.config.alias}"] += 1
        return {
            "content": [{"type": "text", "text": self.config.alias}],
            "structuredContent": {"alias": self.config.alias, "params": params or {}},
            "isError": False,
        }

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        del method, params
        raise AssertionError("LiveClient notify path is not used")

    def _close_transport(self) -> None:
        self.counters[f"live_close:{self.config.alias}"] += 1


def configs(*, tag: str = "v1") -> tuple[UpstreamServerConfig, ...]:
    return (
        UpstreamServerConfig(
            alias="alpha",
            transport="streamable_http",
            url="http://127.0.0.1/alpha",
            expose_mode="broker",
            tags=(tag,),
        ),
        UpstreamServerConfig(
            alias="beta",
            transport="streamable_http",
            url="http://127.0.0.1/beta",
            expose_mode="broker",
            tags=(tag,),
        ),
    )


def build_template(counters: Counter[str], *, tag: str = "v1") -> UpstreamCatalogTemplate:
    snapshot = UpstreamConfigSnapshot(configs=configs(tag=tag))

    def discovery_factory(
        config: UpstreamServerConfig,
        protocol_version: str,
        secret_resolver: object | None = None,
    ) -> DiscoveryClient:
        del protocol_version, secret_resolver
        counters[f"discover_create:{config.alias}"] += 1
        return DiscoveryClient(config, counters)

    with patch.object(upstream_module, "build_client", side_effect=discovery_factory):
        return build_upstream_catalog_template(snapshot, reserved_names={"read_file"})


class UpstreamLazyCatalogTests(unittest.TestCase):
    def test_one_discovery_template_serves_one_hundred_runtime_managers_without_live_clients(self) -> None:
        counters: Counter[str] = Counter()
        template = build_template(counters)
        self.assertEqual(counters["discover_create:alpha"], 1)
        self.assertEqual(counters["discover_create:beta"], 1)
        self.assertEqual(counters["discover_close:alpha"], 1)
        self.assertEqual(counters["discover_close:beta"], 1)

        live_creates = 0

        def unexpected_live_factory(*_args: object, **_kwargs: object) -> LiveClient:
            nonlocal live_creates
            live_creates += 1
            raise AssertionError("constructing a Runtime manager must remain lazy")

        with patch.object(upstream_module, "build_client", side_effect=unexpected_live_factory):
            managers = [UpstreamManager.from_template(template) for _ in range(100)]
        try:
            self.assertEqual(live_creates, 0)
            self.assertTrue(all(manager.live_client_count() == 0 for manager in managers))
            self.assertTrue(all(manager.has_tool("alpha__search") for manager in managers))
            self.assertTrue(all(manager.has_tool("beta__search") for manager in managers))
        finally:
            for manager in managers:
                manager.close()

    def test_first_use_initializes_only_requested_alias(self) -> None:
        counters: Counter[str] = Counter()
        template = build_template(counters)

        def live_factory(
            config: UpstreamServerConfig,
            protocol_version: str,
            secret_resolver: object | None = None,
        ) -> LiveClient:
            del protocol_version, secret_resolver
            counters[f"live_create:{config.alias}"] += 1
            return LiveClient(config, counters)

        manager = UpstreamManager.from_template(template)
        with patch.object(upstream_module, "build_client", side_effect=live_factory):
            result = manager.call_tool("alpha__search", {"q": "first"})
        self.assertFalse(result["isError"])
        self.assertEqual(counters["live_create:alpha"], 1)
        self.assertEqual(counters["live_init:alpha"], 1)
        self.assertEqual(counters["live_create:beta"], 0)
        self.assertEqual(manager.live_client_count(), 1)
        manager.close()

    def test_concurrent_first_use_single_flights_per_runtime_alias(self) -> None:
        counters: Counter[str] = Counter()
        template = build_template(counters)
        entered = threading.Event()
        release = threading.Event()

        def live_factory(
            config: UpstreamServerConfig,
            protocol_version: str,
            secret_resolver: object | None = None,
        ) -> LiveClient:
            del protocol_version, secret_resolver
            counters[f"live_create:{config.alias}"] += 1
            return LiveClient(
                config,
                counters,
                initialize_entered=entered,
                initialize_release=release,
            )

        manager = UpstreamManager.from_template(template)
        results: list[dict[str, Any]] = []

        def call(index: int) -> None:
            results.append(manager.call_tool("alpha__search", {"q": str(index)}))

        with patch.object(upstream_module, "build_client", side_effect=live_factory):
            first = threading.Thread(target=call, args=(1,))
            second = threading.Thread(target=call, args=(2,))
            first.start()
            self.assertTrue(entered.wait(timeout=2))
            second.start()
            release.set()
            first.join(timeout=3)
            second.join(timeout=3)
        self.assertEqual(counters["live_create:alpha"], 1)
        self.assertEqual(counters["live_init:alpha"], 1)
        self.assertEqual(counters["tools_call:alpha"], 2)
        self.assertEqual(len(results), 2)
        manager.close()

    def test_live_clients_and_result_stores_remain_runtime_isolated(self) -> None:
        counters: Counter[str] = Counter()
        template = build_template(counters)
        created: list[LiveClient] = []

        def live_factory(
            config: UpstreamServerConfig,
            protocol_version: str,
            secret_resolver: object | None = None,
        ) -> LiveClient:
            del protocol_version, secret_resolver
            client = LiveClient(config, counters)
            created.append(client)
            return client

        first = UpstreamManager.from_template(template)
        second = UpstreamManager.from_template(template)
        with patch.object(upstream_module, "build_client", side_effect=live_factory):
            first.call_tool("alpha__search", {})
            second.call_tool("alpha__search", {})
        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        self.assertIsNot(first.result_store, second.result_store)
        first.close()
        still_open = second.call_tool("alpha__search", {"q": "still-open"})
        self.assertFalse(still_open["isError"])
        self.assertEqual(still_open["structuredContent"]["alias"], "alpha")
        second.close()

    def test_template_catalog_is_frozen_and_old_runtime_does_not_drift_after_new_revision(self) -> None:
        counters: Counter[str] = Counter()
        old_template = build_template(counters, tag="old")
        new_template = build_template(counters, tag="new")
        old = UpstreamManager.from_template(old_template)
        new = UpstreamManager.from_template(new_template)
        old_entry = old.catalog_entry("alpha__search")
        new_entry = new.catalog_entry("alpha__search")
        assert old_entry is not None and new_entry is not None
        self.assertEqual(old_entry.tags, ("old",))
        self.assertEqual(new_entry.tags, ("new",))
        with self.assertRaises(TypeError):
            old.state.catalog["replacement"] = old_entry  # type: ignore[index]
        self.assertEqual(old.catalog_entry("alpha__search").tags, ("old",))  # type: ignore[union-attr]
        old.close()
        new.close()


if __name__ == "__main__":
    unittest.main()
