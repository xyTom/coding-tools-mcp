from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock, patch

from coding_tools_mcp import telemetry
from coding_tools_mcp.protocol import dispatch_rpc
from coding_tools_mcp.server import Runtime
from coding_tools_mcp.telemetry import ERROR_EVENTS_PER_SESSION, SessionTelemetry

_ENV_KEYS = ("CODING_TOOLS_MCP_TELEMETRY", "DO_NOT_TRACK", "CI")


@contextlib.contextmanager
def scrubbed_env(**overrides: str) -> Iterator[None]:
    """Run with the telemetry-controlling variables removed, then overridden.

    The ambient environment (CI sets ``CI=true``; sandboxes may set
    ``CODING_TOOLS_MCP_*``) must never decide what these tests observe.
    """

    with patch.dict(os.environ):
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(overrides)
        yield


class _CapturingSender:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def enqueue(self, events: list[dict[str, object]], *, wake: bool = False) -> None:
        self.events.extend(events)

    def flush(self) -> None:
        pass


def _initialize(runtime: Runtime, client_name: str = "test-client") -> None:
    response = dispatch_rpc(
        runtime,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": client_name, "version": "9.9.9"}},
        },
    )
    assert response is not None and "error" not in response


class TelemetryModeTests(unittest.TestCase):
    def test_default_is_on(self) -> None:
        with scrubbed_env():
            self.assertEqual(telemetry.telemetry_mode(), "on")

    def test_env_switch_disables(self) -> None:
        for value in ("off", "0", "false", "no", "disabled"):
            with self.subTest(value=value), scrubbed_env(CODING_TOOLS_MCP_TELEMETRY=value):
                self.assertEqual(telemetry.telemetry_mode(), "off")

    def test_do_not_track_disables(self) -> None:
        with scrubbed_env(DO_NOT_TRACK="1"):
            self.assertEqual(telemetry.telemetry_mode(), "off")

    def test_do_not_track_overrides_explicit_on(self) -> None:
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="on", DO_NOT_TRACK="1"):
            self.assertEqual(telemetry.telemetry_mode(), "off")

    def test_ci_disables(self) -> None:
        with scrubbed_env(CI="true"):
            self.assertEqual(telemetry.telemetry_mode(), "off")

    def test_debug_mode(self) -> None:
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="debug"):
            self.assertEqual(telemetry.telemetry_mode(), "debug")


class OffMeansOffTests(unittest.TestCase):
    def test_disabled_session_never_reaches_the_sender(self) -> None:
        for overrides in ({"CODING_TOOLS_MCP_TELEMETRY": "off"}, {"DO_NOT_TRACK": "1"}, {"CI": "1"}):
            with self.subTest(overrides=overrides), scrubbed_env(**overrides):
                get_sender = Mock()
                with patch.object(telemetry, "_get_sender", get_sender):
                    with tempfile.TemporaryDirectory() as tmp:
                        runtime = Runtime(Path(tmp))
                        _initialize(runtime)
                        runtime.call_tool("get_default_cwd", {})
                        runtime.call_tool("read_file", {"path": "missing.txt"})
                        runtime.close()
                get_sender.assert_not_called()

    def test_post_sends_nothing_when_disabled(self) -> None:
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="off"):
            with patch.object(telemetry, "urlopen", Mock()) as opener:
                telemetry._post([{"event": "session_start"}])
            opener.assert_not_called()

    def test_debug_mode_prints_to_stderr_and_does_not_send(self) -> None:
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="debug"):
            stderr = io.StringIO()
            with patch.object(telemetry, "urlopen", Mock()) as opener:
                with contextlib.redirect_stderr(stderr):
                    telemetry._post([{"event": "session_start", "properties": {}}])
            opener.assert_not_called()
        output = stderr.getvalue()
        self.assertIn("telemetry (not sent):", output)
        self.assertIn("session_start", output)


def _run_probe_session() -> _CapturingSender:
    sender = _CapturingSender()
    with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
        with tempfile.TemporaryDirectory() as tmp:
            marker = "leakprobe-a8f3"
            workspace = Path(tmp) / marker
            workspace.mkdir()
            (workspace / f"{marker}.txt").write_text("leakprobe-content\n", encoding="utf-8")
            runtime = Runtime(workspace)
            _initialize(runtime, client_name="clientinfo-probe")
            runtime.call_tool("get_default_cwd", {})
            runtime.call_tool("read_file", {"path": f"{marker}-missing.txt"})
            runtime.call_tool("read_file", {"path": f"{marker}-missing.txt"})
            runtime.close()
    return sender


class SessionEventTests(unittest.TestCase):
    def test_payload_never_contains_paths_arguments_or_content(self) -> None:
        sender = _run_probe_session()
        serialized = json.dumps(sender.events)
        self.assertNotIn("leakprobe", serialized)
        self.assertNotIn("missing.txt", serialized)

    def test_session_events_carry_the_closed_schema(self) -> None:
        sender = _run_probe_session()
        by_name: dict[str, list[dict[str, object]]] = {}
        for event in sender.events:
            by_name.setdefault(str(event["event"]), []).append(event)
        self.assertEqual(len(by_name["session_start"]), 1)
        self.assertEqual(len(by_name["session_end"]), 1)
        self.assertEqual(len(by_name["tool_error"]), 2)

        properties = by_name["session_start"][0]["properties"]
        assert isinstance(properties, dict)
        self.assertEqual(properties["$process_person_profile"], False)
        self.assertEqual(properties["transport"], "stdio")
        self.assertEqual(properties["permission_mode"], "safe")
        # clientInfo values are enum-like labels, truncated, and expected here.
        self.assertEqual(properties["client_name"], "clientinfo-probe")

        errors = by_name["tool_error"]
        first = errors[0]["properties"]
        second = errors[1]["properties"]
        assert isinstance(first, dict) and isinstance(second, dict)
        self.assertEqual(first["tool"], "read_file")
        self.assertEqual(first["error_code"], "NOT_FOUND")
        self.assertEqual(first["consecutive_failures"], 1)
        self.assertEqual(second["consecutive_failures"], 2)

        summaries = {
            str(event["properties"]["tool"]): event["properties"]  # type: ignore[index]
            for event in by_name["tool_summary"]
        }
        self.assertEqual(summaries["read_file"]["calls"], 2)
        self.assertEqual(summaries["read_file"]["ok"], 0)
        self.assertEqual(summaries["read_file"]["err_NOT_FOUND"], 2)
        self.assertEqual(summaries["get_default_cwd"]["calls"], 1)
        self.assertEqual(summaries["get_default_cwd"]["ok"], 1)

        end = by_name["session_end"][0]["properties"]
        assert isinstance(end, dict)
        self.assertEqual(end["tool_calls"], 3)
        self.assertEqual(end["distinct_tools"], 2)
        self.assertEqual(end["errors_dropped"], 0)

    def test_sessions_without_initialize_emit_nothing(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                runtime.call_tool("get_default_cwd", {})
                runtime.call_tool("read_file", {"path": "missing.txt"})
                runtime.close()
        self.assertEqual(sender.events, [])

    def test_error_events_are_capped_and_drops_are_counted(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            session = SessionTelemetry(permission_mode="safe")
            session.record_session_start({"name": "cap"}, "2025-11-25")
            for _ in range(ERROR_EVENTS_PER_SESSION + 5):
                session.record_tool_call(
                    "apply_patch", ok=False, error_code="PATCH_CONTEXT_MISMATCH", duration_ms=5, truncated=False
                )
            session.finish()
        errors = [event for event in sender.events if event["event"] == "tool_error"]
        self.assertEqual(len(errors), ERROR_EVENTS_PER_SESSION)
        end = next(event for event in sender.events if event["event"] == "session_end")
        properties = end["properties"]
        assert isinstance(properties, dict)
        self.assertEqual(properties["errors_dropped"], 5)

    def test_duration_buckets_and_finish_is_idempotent(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            session = SessionTelemetry(permission_mode="safe")
            session.record_session_start(None, "2025-11-25")
            for duration in (50, 500, 5_000, 50_000):
                session.record_tool_call("exec_command", ok=True, error_code=None, duration_ms=duration, truncated=True)
            session.finish()
            session.finish()
        summaries = [event for event in sender.events if event["event"] == "tool_summary"]
        self.assertEqual(len(summaries), 1)
        properties = summaries[0]["properties"]
        assert isinstance(properties, dict)
        for bucket in ("dur_lt_100ms", "dur_lt_1s", "dur_lt_10s", "dur_gte_10s"):
            self.assertEqual(properties[bucket], 1)
        self.assertEqual(properties["truncated"], 4)
        self.assertEqual(len([event for event in sender.events if event["event"] == "session_end"]), 1)


class DocumentationDriftTests(unittest.TestCase):
    def test_documented_schema_matches_emitted_events(self) -> None:
        doc = (Path(__file__).resolve().parents[1] / "docs" / "telemetry.md").read_text(encoding="utf-8")
        emitted = {str(event["event"]) for event in _run_probe_session().events}
        self.assertEqual(emitted, {"session_start", "tool_error", "tool_summary", "session_end"})
        for name in emitted:
            self.assertIn(f"`{name}`", doc)
        self.assertIn(f"max {ERROR_EVENTS_PER_SESSION} per session", doc)


class InstallIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = telemetry._install_id
        telemetry._install_id = None

    def tearDown(self) -> None:
        telemetry._install_id = self._saved

    def test_install_id_is_random_stable_and_resettable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(Path, "home", return_value=Path(tmp)):
                first = telemetry.install_id()
                self.assertEqual(telemetry.install_id(), first)
                path = Path(tmp) / ".coding-tools-mcp" / "id"
                self.assertEqual(path.read_text(encoding="utf-8").strip(), first)
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

                telemetry._install_id = None
                path.unlink()
                second = telemetry.install_id()
                self.assertNotEqual(second, first)
                self.assertEqual(len(second), 32)


if __name__ == "__main__":
    unittest.main()
