from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
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


LEGACY_PROTOCOL_VERSION = "2025-11-25"
MODERN_PROTOCOL_VERSION = "2026-07-28"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}


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


def _modern_request(
    runtime: Runtime,
    method: str,
    params: dict[str, object] | None = None,
    *,
    client_info: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Dispatch one 2026-07-28 request, which states its version per request."""

    meta = dict(MODERN_META)
    if client_info is not None:
        meta["io.modelcontextprotocol/clientInfo"] = client_info
    body = dict(params or {})
    body["_meta"] = meta
    return dispatch_rpc(runtime, {"jsonrpc": "2.0", "id": 7, "method": method, "params": body})


def _events_by_name(sender: _CapturingSender) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for event in sender.events:
        grouped.setdefault(str(event["event"]), []).append(event)
    return grouped


def _properties(event: dict[str, object]) -> dict[str, object]:
    properties = event["properties"]
    assert isinstance(properties, dict)
    return properties


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
                        runtime.call_tool("check_exec_environment", {})
                        runtime.call_tool("read_file", {"path": "missing.txt"})
                        runtime.close()
                get_sender.assert_not_called()

    def test_an_activating_request_writes_nothing_while_telemetry_is_off(self) -> None:
        """Off means no sender and no install id: building an event reads one."""

        saved_install_id = telemetry._install_id
        telemetry._install_id = None
        get_sender = Mock()
        try:
            with tempfile.TemporaryDirectory() as home:
                with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="off", HOME=home):
                    with patch.object(telemetry, "_get_sender", get_sender):
                        with tempfile.TemporaryDirectory() as tmp:
                            runtime = Runtime(Path(tmp))
                            dispatch_rpc(
                                runtime,
                                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                            )
                            runtime.close()
                get_sender.assert_not_called()
                self.assertFalse((Path(home) / ".coding-tools-mcp").exists(), "off must not create an install id")
        finally:
            telemetry._install_id = saved_install_id

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
            runtime.call_tool("check_exec_environment", {})
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
        by_name = _events_by_name(sender)
        self.assertEqual(len(by_name["session_start"]), 1)
        self.assertEqual(len(by_name["handshake"]), 1)
        self.assertEqual(len(by_name["session_end"]), 1)
        self.assertEqual(len(by_name["tool_error"]), 2)

        properties = _properties(by_name["session_start"][0])
        self.assertEqual(properties["$process_person_profile"], False)
        self.assertEqual(properties["transport"], "stdio")
        self.assertEqual(properties["permission_mode"], "safe")
        # One runtime serves every client of the workspace, so only the
        # handshake and the request that failed name a client at all.
        for event in sender.events:
            if event["event"] in {"handshake", "tool_error"}:
                continue
            with self.subTest(event=event["event"]):
                aggregate = _properties(event)
                for field in ("client_name", "client_version", "protocol_version"):
                    self.assertNotIn(field, aggregate)

        handshake = _properties(by_name["handshake"][0])
        self.assertEqual(handshake["client_name"], "clientinfo-probe")
        self.assertEqual(handshake["client_version"], "9.9.9")
        self.assertEqual(handshake["protocol_version"], LEGACY_PROTOCOL_VERSION)

        errors = by_name["tool_error"]
        first = _properties(errors[0])
        second = _properties(errors[1])
        self.assertEqual(first["tool"], "read_file")
        self.assertEqual(first["error_code"], "NOT_FOUND")
        self.assertEqual(first["consecutive_failures"], 1)
        self.assertEqual(second["consecutive_failures"], 2)
        # The failing calls were made straight against the runtime, so they
        # carry no request context and therefore no client identity.
        self.assertIsNone(first["client_name"])
        self.assertIsNone(first["client_version"])

        summaries = {str(_properties(event)["tool"]): _properties(event) for event in by_name["tool_summary"]}
        self.assertEqual(summaries["read_file"]["calls"], 2)
        self.assertEqual(summaries["read_file"]["ok"], 0)
        self.assertEqual(summaries["read_file"]["err_NOT_FOUND"], 2)
        self.assertEqual(summaries["check_exec_environment"]["calls"], 1)
        self.assertEqual(summaries["check_exec_environment"]["ok"], 1)
        self.assertNotIn("client_name", summaries["read_file"])

        end = _properties(by_name["session_end"][0])
        self.assertEqual(end["tool_calls"], 3)
        self.assertEqual(end["distinct_tools"], 2)
        self.assertEqual(end["errors_dropped"], 0)
        self.assertEqual(end["legacy_requests"], 1)
        self.assertEqual(end["modern_requests"], 0)
        self.assertEqual(end["discover_probes"], 0)
        for counter in ("evict_events", "evicted_bytes_total", "read_output_omitted_hits", "poll_omitted_hits"):
            self.assertEqual(end[counter], 0)

    def test_a_runtime_that_serves_no_request_emits_nothing(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                runtime.call_tool("check_exec_environment", {})
                runtime.call_tool("read_file", {"path": "missing.txt"})
                runtime.close()
        self.assertEqual(sender.events, [])

    def test_a_ping_only_runtime_emits_nothing(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                for request_id in (1, 2):
                    dispatch_rpc(runtime, {"jsonrpc": "2.0", "id": request_id, "method": "ping", "params": {}})
                _modern_request(runtime, "ping")
                runtime.close()
        self.assertEqual(sender.events, [], "an HTTP health probe must not create a session")

    def test_a_modern_client_that_never_handshakes_produces_a_session(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                # The very first request fails: activation happens before the
                # method runs, so its tool_error must not be lost.
                _modern_request(
                    runtime,
                    "tools/call",
                    {"name": "read_file", "arguments": {"path": "missing.txt"}},
                    client_info={"name": "modern-probe", "version": "2.0"},
                )
                _modern_request(runtime, "tools/list")
                runtime.close()

        by_name = _events_by_name(sender)
        self.assertEqual(len(by_name["session_start"]), 1)
        self.assertNotIn("handshake", by_name)
        self.assertEqual(len(by_name["tool_error"]), 1)
        error = _properties(by_name["tool_error"][0])
        self.assertEqual(error["tool"], "read_file")
        self.assertEqual(error["client_name"], "modern-probe")
        self.assertEqual(error["client_version"], "2.0")
        end = _properties(by_name["session_end"][0])
        self.assertEqual(end["modern_requests"], 2)
        self.assertEqual(end["legacy_requests"], 0)

    def test_discover_probes_are_counted_and_do_not_need_a_handshake(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                probe = dispatch_rpc(
                    runtime, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
                )
                assert probe is not None
                self.assertEqual(probe["error"]["code"], -32601)
                _initialize(runtime)
                runtime.close()

        by_name = _events_by_name(sender)
        self.assertEqual(len(by_name["session_start"]), 1)
        end = _properties(by_name["session_end"][0])
        self.assertEqual(end["discover_probes"], 1)
        self.assertEqual(end["legacy_requests"], 2)

    def test_self_reported_client_identity_is_sanitized(self) -> None:
        # A client names itself; the label is ours. Anything that would turn
        # one into free-form text, an address, or a path is dropped rather
        # than escaped, and a name is never long enough to be an identifier.
        cases = [
            (
                {
                    "name": "evil\r\nclient\u4e2d\x07" + "x" * 200,
                    "version": "1.0\n",
                    "secret": "must-not-travel",
                },
                "evilclient" + "x" * 30,
                "1.0",
            ),
            ({"name": "alice@example.com", "version": "2.0"}, "aliceexample.com", "2.0"),
            ({"name": "/home/alice/repo", "version": "3.0"}, "homealicerepo", "3.0"),
        ]
        for client_info, expected_name, expected_version in cases:
            with self.subTest(client_name=client_info["name"]):
                sender = _CapturingSender()
                with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
                    with tempfile.TemporaryDirectory() as tmp:
                        runtime = Runtime(Path(tmp))
                        _modern_request(
                            runtime,
                            "tools/call",
                            {"name": "read_file", "arguments": {"path": "missing.txt"}},
                            client_info=client_info,
                        )
                        runtime.close()

                serialized = json.dumps(sender.events)
                error = _properties(_events_by_name(sender)["tool_error"][0])
                self.assertEqual(error["client_name"], expected_name)
                self.assertEqual(error["client_version"], expected_version)
                self.assertNotIn("must-not-travel", serialized)
                for character in ("@", "/"):
                    self.assertNotIn(character, str(error["client_name"]))

    def test_every_handshake_is_recorded_but_the_session_starts_once(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                _initialize(runtime, client_name="first-connector")
                _initialize(runtime, client_name="second-connector")
                runtime.close()

        by_name = _events_by_name(sender)
        self.assertEqual(len(by_name["session_start"]), 1)
        self.assertEqual(
            [_properties(event)["client_name"] for event in by_name["handshake"]],
            ["first-connector", "second-connector"],
        )

    def test_output_retention_counters_travel_with_session_end(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                _initialize(runtime)
                runtime.command_manager.record_output_eviction("stdout", 512)
                runtime.command_manager.record_omitted_read("read_output")
                runtime.close()

        end = _properties(_events_by_name(sender)["session_end"][0])
        self.assertEqual(end["evict_events"], 1)
        self.assertEqual(end["evicted_bytes_total"], 512)
        self.assertEqual(end["read_output_omitted_hits"], 1)
        self.assertEqual(end["poll_omitted_hits"], 0)

    def test_error_events_are_capped_and_drops_are_counted(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            session = SessionTelemetry(permission_mode="safe")
            session.record_request("legacy", "tools/call")
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
            session.record_session_start(None, LEGACY_PROTOCOL_VERSION)
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


class FirstAppearanceLogTests(unittest.TestCase):
    """The one-line stderr notes an operator reads to see which era clients speak."""

    def setUp(self) -> None:
        self._saved = set(telemetry._first_seen)
        telemetry._first_seen.clear()

    def tearDown(self) -> None:
        telemetry._first_seen.clear()
        telemetry._first_seen.update(self._saved)

    def test_each_protocol_choice_is_logged_once_to_stderr(self) -> None:
        stderr = io.StringIO()
        # Logged for the operator, not for us: telemetry being off changes nothing.
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="off"), contextlib.redirect_stderr(stderr):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                _initialize(runtime)
                _initialize(runtime)
                _modern_request(runtime, "tools/list")
                _modern_request(runtime, "ping")
                for request_id in (1, 2):
                    dispatch_rpc(
                        runtime,
                        {"jsonrpc": "2.0", "id": request_id, "method": "server/discover", "params": {}},
                    )
                runtime.close()

        lines = [line for line in stderr.getvalue().splitlines() if line.startswith("coding-tools-mcp:")]
        self.assertEqual(
            lines,
            [
                f"coding-tools-mcp: legacy client handshake ({LEGACY_PROTOCOL_VERSION})",
                "coding-tools-mcp: modern client request (tools/list)",
                "coding-tools-mcp: server/discover probe",
            ],
        )

    def test_a_method_name_cannot_write_a_second_line_into_the_log(self) -> None:
        """The method comes off the wire, and the note is one line about it."""

        stderr = io.StringIO()
        method = "tools/list\r\ncoding-tools-mcp: forged operator note\x07"
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="off"), contextlib.redirect_stderr(stderr):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                _modern_request(runtime, method)
                runtime.close()

        output = stderr.getvalue()
        lines = [line for line in output.splitlines() if line.startswith("coding-tools-mcp:")]
        self.assertEqual(
            lines,
            ["coding-tools-mcp: modern client request (tools/listcoding-tools-mcpforgedoperatornote)"],
            output,
        )
        self.assertNotIn("forged operator note", output)
        self.assertNotIn("\x07", output)


class FailureStreakKeyingTests(unittest.TestCase):
    """D2: streaks belong to a (tool, error_code) pair, not to one global slot."""

    def session(self) -> SessionTelemetry:
        return SessionTelemetry(permission_mode="safe")

    def fail(self, session: SessionTelemetry, tool: str, code: str) -> None:
        session.record_tool_call(tool, ok=False, error_code=code, duration_ms=1, truncated=False)

    def succeed(self, session: SessionTelemetry, tool: str, outcome: str | None = None) -> None:
        session.record_tool_call(
            tool, ok=True, error_code=None, duration_ms=1, truncated=False, outcome=outcome
        )

    def test_another_tools_success_does_not_clear_a_streak(self) -> None:
        session = self.session()
        self.fail(session, "apply_patch", "PATCH_CONTEXT_NOT_FOUND")
        self.fail(session, "apply_patch", "PATCH_CONTEXT_NOT_FOUND")
        self.succeed(session, "read_file")
        self.fail(session, "apply_patch", "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(session.consecutive_failures("apply_patch", "PATCH_CONTEXT_NOT_FOUND"), 3)

    def test_two_error_codes_on_one_tool_count_separately(self) -> None:
        session = self.session()
        self.fail(session, "apply_patch", "PATCH_CONTEXT_NOT_FOUND")
        self.fail(session, "apply_patch", "PATCH_CONFLICT")
        self.assertEqual(session.consecutive_failures("apply_patch", "PATCH_CONTEXT_NOT_FOUND"), 1)
        self.assertEqual(session.consecutive_failures("apply_patch", "PATCH_CONFLICT"), 1)

    def test_a_success_of_the_same_tool_clears_its_streaks(self) -> None:
        session = self.session()
        self.fail(session, "apply_patch", "PATCH_CONTEXT_NOT_FOUND")
        self.succeed(session, "apply_patch")
        self.assertEqual(session.consecutive_failures("apply_patch", "PATCH_CONTEXT_NOT_FOUND"), 0)

    def test_a_failed_operation_is_not_a_success_and_clears_nothing(self) -> None:
        session = self.session()
        self.fail(session, "exec_command", "INVALID_ARGUMENT")
        self.succeed(session, "exec_command", outcome="exited_nonzero")
        self.assertEqual(session.consecutive_failures("exec_command", "INVALID_ARGUMENT"), 1)
        self.succeed(session, "exec_command", outcome="exited_0")
        self.assertEqual(session.consecutive_failures("exec_command", "INVALID_ARGUMENT"), 0)


class OperationOutcomeTests(unittest.TestCase):
    """D1: a command that exits non-zero is not a successful operation."""

    def test_tool_summary_separates_call_success_from_operation_success(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="on"), patch.object(
            telemetry, "_get_sender", return_value=sender
        ):
            session = SessionTelemetry(permission_mode="safe")
            session.record_request(LEGACY_PROTOCOL_VERSION, "tools/call")
            for outcome in ("exited_0", "exited_nonzero", "timeout", "signal"):
                session.record_tool_call(
                    "exec_command",
                    ok=True,
                    error_code=None,
                    duration_ms=1,
                    truncated=False,
                    outcome=outcome,
                )
            session.finish()
        summary = next(
            _properties(event)
            for event in sender.events
            if event["event"] == "tool_summary" and _properties(event)["tool"] == "exec_command"
        )
        self.assertEqual(summary["calls"], 4)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["operation_failures"], 3)
        self.assertEqual(summary["outcome_exited_nonzero"], 1)
        self.assertEqual(summary["outcome_timeout"], 1)
        self.assertEqual(summary["outcome_signal"], 1)

    def test_spawn_error_is_not_counted_as_two_failures(self) -> None:
        sender = _CapturingSender()
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="on"), patch.object(
            telemetry, "_get_sender", return_value=sender
        ):
            session = SessionTelemetry(permission_mode="safe")
            session.record_request(LEGACY_PROTOCOL_VERSION, "tools/call")
            session.record_tool_call(
                "exec_command",
                ok=False,
                error_code="COMMAND_SPAWN_FAILED",
                duration_ms=1,
                truncated=False,
                outcome="spawn_error",
            )
            session.finish()
        summary = next(
            _properties(event)
            for event in sender.events
            if event["event"] == "tool_summary" and _properties(event)["tool"] == "exec_command"
        )
        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["ok"], 0)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["operation_failures"], 0)
        self.assertEqual(summary["outcome_spawn_error"], 1)

    def test_a_nonzero_exit_is_reported_as_the_operation_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                payload = runtime.exec_command({"cmd": "exit 3", "timeout_ms": 5000})
            finally:
                runtime.close()
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["exit_code"], 3)
        self.assertEqual(payload["operation_outcome"], "exited_nonzero")

    def test_a_poll_observed_failure_is_attributed_to_exec_command(self) -> None:
        # Every poll of a finished command reports the same terminal outcome.
        # Counting each one turned a single failing build into as many failed
        # operations as the model happened to poll.
        sender = _CapturingSender()
        with scrubbed_env(CODING_TOOLS_MCP_TELEMETRY="on"), patch.object(
            telemetry, "_get_sender", return_value=sender
        ), tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            try:
                runtime.telemetry.record_request(LEGACY_PROTOCOL_VERSION, "tools/call")
                command = (
                    f'"{sys.executable}" -c "import sys,time; '
                    'time.sleep(0.2); sys.exit(7)"'
                )
                started = runtime.call_tool(
                    "exec_command",
                    {"cmd": command, "yield_time_ms": 1, "timeout_ms": 5000},
                )
                command_id = started["structuredContent"]["command_id"]
                self.assertEqual(started["structuredContent"]["operation_outcome"], "running")
                poll_calls = 0
                for _ in range(20):
                    polled = runtime.call_tool(
                        "write_stdin",
                        {"command_id": command_id, "chars": "", "yield_time_ms": 250},
                    )
                    poll_calls += 1
                    if polled["structuredContent"]["operation_outcome"] != "running":
                        break
                # The poll still tells the truth about the command…
                self.assertEqual(polled["structuredContent"]["operation_outcome"], "exited_nonzero")
                repeated = runtime.call_tool("write_stdin", {"command_id": command_id, "chars": ""})
                poll_calls += 1
                self.assertEqual(repeated["structuredContent"]["operation_outcome"], "exited_nonzero")
            finally:
                runtime.close()
        summaries = {
            _properties(event)["tool"]: _properties(event)
            for event in sender.events
            if event["event"] == "tool_summary"
        }
        # … but the failed operation is counted once, by the call that ran it.
        self.assertEqual(summaries["exec_command"]["outcome_exited_nonzero"], 1)
        self.assertEqual(summaries["exec_command"]["operation_failures"], 1)
        self.assertNotIn("outcome_exited_nonzero", summaries["write_stdin"])
        self.assertEqual(summaries["write_stdin"]["operation_failures"], 0)
        self.assertEqual(summaries["write_stdin"]["calls"], poll_calls)
        self.assertEqual(summaries["write_stdin"]["ok"], poll_calls)

    def test_every_terminal_observer_attributes_the_outcome_to_exec_command(self) -> None:
        for observer in ("write_stdin", "read_output", "kill_command"):
            with self.subTest(observer=observer), scrubbed_env(
                CODING_TOOLS_MCP_TELEMETRY="on"
            ), patch.object(telemetry, "_get_sender", return_value=(sender := _CapturingSender())):
                with tempfile.TemporaryDirectory() as tmp:
                    runtime = Runtime(Path(tmp), permission_mode="safe")
                    runtime.telemetry.record_request(LEGACY_PROTOCOL_VERSION, "tools/call")
                    started_at = time.time()
                    runtime.emit_tool_trace(
                        "exec_command",
                        {},
                        {"ok": True, "command_id": "background", "operation_outcome": "running"},
                        started_at,
                    )
                    runtime.emit_tool_trace(
                        observer,
                        {},
                        {
                            "ok": True,
                            "command_id": "background",
                            "operation_outcome": "exited_nonzero",
                        },
                        started_at,
                    )
                    runtime.close()

                summaries = {
                    _properties(event)["tool"]: _properties(event)
                    for event in sender.events
                    if event["event"] == "tool_summary"
                }
                self.assertEqual(summaries["exec_command"]["calls"], 1)
                self.assertEqual(summaries["exec_command"]["ok"], 0)
                self.assertEqual(summaries["exec_command"]["operation_failures"], 1)
                self.assertEqual(summaries["exec_command"]["outcome_exited_nonzero"], 1)
                self.assertEqual(summaries[observer]["calls"], 1)
                self.assertEqual(summaries[observer]["ok"], 1)
                self.assertEqual(summaries[observer]["operation_failures"], 0)
                self.assertNotIn("outcome_exited_nonzero", summaries[observer])


class BreakerBlockTests(unittest.TestCase):
    """Executed failures and legacy breaker refusals retain distinct accounting."""

    def test_repeated_actual_failures_are_not_hidden_as_blocks(self) -> None:
        """Every executed failure counts, even when accompanied by repetition advice."""
        sender, responses = RejectedCallTests().run_session(*(
            {"name": "read_file", "arguments": {"path": "missing.txt"}} for _ in range(10)
        ))
        for response in responses:
            assert response is not None
            result = response["result"]
            self.assertEqual(result["structuredContent"]["error"]["code"], "NOT_FOUND")
        events = _events_by_name(sender)
        summary = _properties(events["tool_summary"][0])
        self.assertEqual(summary["calls"], 10)
        self.assertEqual(summary["errors"], 10)
        self.assertEqual(summary["err_NOT_FOUND"], 10)
        self.assertEqual(summary["breaker_blocks"], 0)
        end = _properties(events["session_end"][0])
        self.assertEqual(end["tool_calls"], 10)
        self.assertEqual(end["breaker_blocks"], 0)

    def test_a_refusal_neither_extends_nor_clears_a_failure_streak(self) -> None:
        """Verify breaker refusals neither increment nor reset the underlying failure streak."""
        session = SessionTelemetry(permission_mode="safe")
        for _ in range(2):
            session.record_tool_call("read_output", ok=False, error_code="INVALID_ARGUMENT", duration_ms=1, truncated=False)
        session.record_tool_call(
            "read_output", ok=False, error_code="REPEATED_CALL_BLOCKED", duration_ms=0, truncated=False
        )
        self.assertEqual(session.consecutive_failures("read_output", "INVALID_ARGUMENT"), 2)
        self.assertEqual(session.consecutive_failures("read_output", "REPEATED_CALL_BLOCKED"), 0)


class DocumentationDriftTests(unittest.TestCase):
    def test_documented_schema_matches_emitted_events(self) -> None:
        doc = (Path(__file__).resolve().parents[1] / "docs" / "telemetry.md").read_text(encoding="utf-8")
        emitted = {str(event["event"]) for event in _run_probe_session().events}
        self.assertEqual(emitted, {"session_start", "handshake", "tool_error", "tool_summary", "session_end"})
        for name in emitted:
            self.assertIn(f"`{name}`", doc)
        self.assertIn(f"max {ERROR_EVENTS_PER_SESSION} per session", doc)

    def test_documented_properties_match_emitted_properties(self) -> None:
        """Verify emitted fingerprint and outcome properties are covered by telemetry documentation."""
        doc = (Path(__file__).resolve().parents[1] / "docs" / "telemetry.md").read_text(encoding="utf-8")
        by_name = _events_by_name(_run_probe_session())
        start = _properties(by_name["session_start"][0])
        for name in ("install", "build"):
            self.assertIn(name, start)
            self.assertIn(f"`{name}`", doc)
        self.assertIn("already_applied", _properties(by_name["tool_summary"][0]))
        self.assertIn("`already_applied`", doc)
        self.assertIn("unknown_tool_calls", _properties(by_name["session_end"][0]))
        self.assertIn("`unknown_tool_calls`", doc)
        self.assertIn("`err_INVALID_PARAMS`", doc)
        for kind in ("index", "editable", "local", "vcs", "source", "unknown"):
            self.assertIn(f"`{kind}`", doc)


class _FakeDistribution:
    def __init__(self, direct_url: object, *, package_dir: Path | None = None, broken: bool = False) -> None:
        """Configure synthetic install metadata, package location, and optional read failure."""
        self._direct_url = direct_url
        self._package_dir = package_dir if package_dir is not None else telemetry._PACKAGE_DIR
        self._broken = broken

    def read_text(self, name: str) -> str | None:
        """Return synthetic direct_url metadata or simulate absent or corrupt metadata."""
        if self._broken:
            raise RuntimeError("corrupt metadata")
        assert name == "direct_url.json"
        if self._direct_url is None:
            return None
        return json.dumps(self._direct_url)

    def locate_file(self, relative: str) -> Path:
        """Resolve a distribution-relative path beside the synthetic package directory."""
        return self._package_dir.parent / relative


class BuildFingerprintTests(unittest.TestCase):
    def install_kind_for(self, distribution: object) -> str:
        """Classify an installation using the supplied synthetic distribution metadata."""
        from importlib import metadata

        def fake(name: str) -> object:
            """Return the expected distribution or simulate a package absent from installed metadata."""
            self.assertEqual(name, "coding-tools-mcp")
            if distribution is None:
                raise metadata.PackageNotFoundError(name)
            return distribution

        with patch("importlib.metadata.distribution", fake):
            return telemetry.install_kind()

    def test_install_kind_for_each_way_of_installing(self) -> None:
        """Verify source, index, editable, local-directory, and VCS installations are classified."""
        cases = {
            "source": None,
            "index": _FakeDistribution(None),
            "editable": _FakeDistribution({"url": "file:///home/u/src/ctm", "dir_info": {"editable": True}}),
            "local": _FakeDistribution({"url": "file:///home/u/src/ctm", "dir_info": {}}),
            "vcs": _FakeDistribution(
                {"url": "https://github.com/example/ctm.git", "vcs_info": {"vcs": "git", "commit_id": "abc"}}
            ),
        }
        for expected, distribution in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(self.install_kind_for(distribution), expected)

    def test_a_local_archive_install_is_local(self) -> None:
        """Verify a local wheel archive is classified as a local installation."""
        archive = _FakeDistribution({"url": "file:///tmp/ctm-0.5.0-py3-none-any.whl", "archive_info": {}})
        self.assertEqual(self.install_kind_for(archive), "local")

    def test_an_installed_copy_shadowed_by_a_checkout_is_source(self) -> None:
        """Verify a checkout shadowing installed metadata is classified as source."""
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = _FakeDistribution(None, package_dir=Path(tmp) / "coding_tools_mcp")
            self.assertEqual(self.install_kind_for(elsewhere), "source")

    def test_unreadable_metadata_is_unknown_and_never_raises(self) -> None:
        """Verify corrupt distribution metadata yields unknown without raising."""
        self.assertEqual(self.install_kind_for(_FakeDistribution(None, broken=True)), "unknown")

    def test_build_id_is_a_stable_hash_of_the_package_sources(self) -> None:
        """Verify build IDs deterministically hash sorted source paths and bytes."""
        saved = telemetry._build_id
        try:
            telemetry._build_id = None
            first = telemetry.build_id()
            telemetry._build_id = None
            second = telemetry.build_id()
        finally:
            telemetry._build_id = saved
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{12}$")

        import hashlib

        package = telemetry._PACKAGE_DIR
        digest = hashlib.sha256()
        for path in sorted(package.rglob("*.py"), key=lambda item: item.relative_to(package).as_posix()):
            digest.update(path.relative_to(package).as_posix().encode("utf-8") + b"\0" + path.read_bytes())
        self.assertEqual(first, digest.hexdigest()[:12])

    def test_build_id_changes_with_the_sources_and_degrades_to_unknown(self) -> None:
        """Verify build IDs track source changes, cache within a process, and tolerate missing files."""
        saved = telemetry._build_id
        try:
            with tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp) / "coding_tools_mcp"
                package.mkdir()
                (package / "__init__.py").write_text("x = 1\n", encoding="utf-8")
                with patch.object(telemetry, "_PACKAGE_DIR", package):
                    telemetry._build_id = None
                    original = telemetry.build_id()
                    (package / "__init__.py").write_text("x = 2\n", encoding="utf-8")
                    telemetry._build_id = None
                    modified = telemetry.build_id()
                    telemetry._build_id = None
                    # Cached: a later edit does not change a running process's id.
                    self.assertEqual(telemetry.build_id(), modified)
                    (package / "__init__.py").write_text("x = 3\n", encoding="utf-8")
                    self.assertEqual(telemetry.build_id(), modified)
                with patch.object(telemetry, "_PACKAGE_DIR", Path(tmp) / "missing"):
                    telemetry._build_id = None
                    self.assertEqual(telemetry.build_id(), "unknown")
        finally:
            telemetry._build_id = saved
        self.assertNotEqual(original, modified)

    def test_every_event_carries_only_the_two_fingerprint_labels(self) -> None:
        """Verify events expose bounded install/build labels without package paths or URLs."""
        sender = _run_probe_session()
        for event in sender.events:
            with self.subTest(event=event["event"]):
                properties = _properties(event)
                self.assertIn(properties["install"], {"index", "editable", "local", "vcs", "source", "unknown"})
                self.assertRegex(str(properties["build"]), r"^([0-9a-f]{12}|unknown)$")
        serialized = json.dumps(sender.events)
        self.assertNotIn(str(telemetry._PACKAGE_DIR), serialized)
        self.assertNotIn("file://", serialized)


class RejectedCallTests(unittest.TestCase):
    """Schema violations are answered with -32602 but still counted."""

    def run_session(self, *calls: dict[str, object]) -> tuple[_CapturingSender, list[dict[str, object] | None]]:
        """Send modern tool calls to an isolated runtime and capture responses and telemetry."""
        sender = _CapturingSender()
        responses: list[dict[str, object] | None] = []
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                for params in calls:
                    responses.append(_modern_request(runtime, "tools/call", params))
                runtime.close()
        return sender, responses

    def test_a_schema_violation_is_recorded_as_invalid_params(self) -> None:
        """Verify schema and argument-shape rejections count as INVALID_PARAMS tool errors."""
        sender, responses = self.run_session(
            {"name": "read_file", "arguments": {"path": 5}},
            {"name": "read_file", "arguments": ["not", "an", "object"]},
        )
        for response in responses:
            assert response is not None
            self.assertEqual(response["error"]["code"], -32602)
        by_name = _events_by_name(sender)
        errors = [_properties(event) for event in by_name["tool_error"]]
        self.assertEqual([error["error_code"] for error in errors], ["INVALID_PARAMS", "INVALID_PARAMS"])
        self.assertEqual([error["tool"] for error in errors], ["read_file", "read_file"])
        summary = _properties(by_name["tool_summary"][0])
        self.assertEqual(summary["tool"], "read_file")
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["errors"], 2)
        self.assertEqual(summary["ok"], 0)
        self.assertEqual(summary["err_INVALID_PARAMS"], 2)
        self.assertEqual(_properties(by_name["session_end"][0])["unknown_tool_calls"], 0)

    def test_unknown_tool_names_are_counted_without_per_tool_stats(self) -> None:
        """Verify unknown tools increment session totals without emitting arbitrary tool labels."""
        sender, responses = self.run_session(
            {"name": "no_such_tool_a", "arguments": {}},
            {"name": "no_such_tool_b", "arguments": "nope"},
        )
        for response in responses:
            assert response is not None
            self.assertEqual(response["error"]["code"], -32602)
        by_name = _events_by_name(sender)
        self.assertNotIn("tool_summary", by_name)
        self.assertNotIn("tool_error", by_name)
        end = _properties(by_name["session_end"][0])
        self.assertEqual(end["unknown_tool_calls"], 2)
        self.assertEqual(end["tool_calls"], 0)
        self.assertNotIn("no_such_tool", json.dumps(sender.events))

    def test_falsy_non_object_arguments_are_rejected_and_counted(self) -> None:
        """Verify falsy non-object arguments are rejected and counted instead of defaulting to {}."""
        invalid = ([], "", False, 0)
        sender, responses = self.run_session(*(
            {"name": "server_info", "arguments": value} for value in invalid
        ))
        for value, response in zip(invalid, responses):
            with self.subTest(arguments=value):
                assert response is not None
                self.assertEqual(response.get("error", {}).get("code"), -32602)
        summary = _properties(_events_by_name(sender)["tool_summary"][0])
        self.assertEqual(summary["err_INVALID_PARAMS"], len(invalid))

    def test_missing_and_null_arguments_preserve_empty_object_compatibility(self) -> None:
        sender, responses = self.run_session(
            {"name": "server_info"},
            {"name": "server_info", "arguments": None},
            {"name": "server_info", "arguments": {}},
        )
        for response in responses:
            assert response is not None
            self.assertNotIn("error", response)
            self.assertIn("result", response)
        by_name = _events_by_name(sender)
        self.assertNotIn("tool_error", by_name)
        summary = _properties(by_name["tool_summary"][0])
        self.assertEqual(summary["calls"], 3)
        self.assertEqual(summary["ok"], 3)
        self.assertEqual(summary["errors"], 0)


class AlreadyAppliedCounterTests(unittest.TestCase):
    def test_session_counts_only_successful_already_applied_results(self) -> None:
        """Verify already_applied counts only successful calls carrying that flag."""
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            session = SessionTelemetry(permission_mode="safe")
            session.record_request("legacy", "tools/call")
            for already_applied, ok in ((True, True), (False, True), (True, False)):
                session.record_tool_call(
                    "apply_patch",
                    ok=ok,
                    error_code=None if ok else "PATCH_FAILED",
                    duration_ms=1,
                    truncated=False,
                    already_applied=already_applied,
                )
            session.finish()
        summary = _properties(_events_by_name(sender)["tool_summary"][0])
        self.assertEqual(summary["already_applied"], 1)
        self.assertEqual(summary["calls"], 3)
        self.assertEqual(summary["ok"], 2)

    def test_runtime_reads_already_applied_from_the_payload(self) -> None:
        """Verify runtime telemetry reads already_applied from the handler's result payload."""
        sender = _CapturingSender()
        with scrubbed_env(), patch.object(telemetry, "_get_sender", lambda: sender):
            with tempfile.TemporaryDirectory() as tmp:
                runtime = Runtime(Path(tmp))
                _initialize(runtime)
                runtime._tool_handlers["apply_patch"] = lambda _args: {"already_applied": True, "affected_files": []}
                runtime.call_tool("apply_patch", {"patch": "*** Begin Patch\n*** End Patch\n"})
                runtime._tool_handlers["apply_patch"] = lambda _args: {"affected_files": []}
                runtime.call_tool("apply_patch", {"patch": "*** Begin Patch\n*** End Patch\n"})
                runtime.close()
        summaries = {str(_properties(event)["tool"]): _properties(event) for event in _events_by_name(sender)["tool_summary"]}
        self.assertEqual(summaries["apply_patch"]["already_applied"], 1)
        self.assertEqual(summaries["apply_patch"]["calls"], 2)


class LocalHarnessEnvTests(unittest.TestCase):
    """Benchmark, dogfood, and agent-eval servers must default telemetry off."""

    def test_local_server_env_defaults_off_and_respects_an_override(self) -> None:
        """Verify local environments default telemetry off, preserve overrides, and do not mutate os.environ."""
        from benchmarks.mcp_http import local_server_env

        self.assertEqual(local_server_env({"PATH": "/bin"}), {"PATH": "/bin", "CODING_TOOLS_MCP_TELEMETRY": "off"})
        self.assertEqual(
            local_server_env({"CODING_TOOLS_MCP_TELEMETRY": "debug"})["CODING_TOOLS_MCP_TELEMETRY"], "debug"
        )
        with scrubbed_env():
            env = local_server_env()
            self.assertEqual(env["CODING_TOOLS_MCP_TELEMETRY"], "off")
            self.assertNotIn("CODING_TOOLS_MCP_TELEMETRY", os.environ)

    def test_harness_server_launchers_start_servers_with_telemetry_off(self) -> None:
        """Verify latency and dogfood launchers pass telemetry-off environments to child servers."""
        from benchmarks import runtime_latency
        from benchmarks.dogfood import mcp_deterministic_runner

        launches: list[dict[str, str]] = []

        def fake_popen(*_args: object, **kwargs: object) -> Mock:
            """Capture the child environment and return a process mock without launching a server."""
            env = kwargs.get("env")
            assert isinstance(env, dict)
            launches.append(env)
            return Mock()

        with scrubbed_env(), tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.Popen", fake_popen):
                runtime_latency.start_server("{python} -c pass", Path(tmp), 1)
                mcp_deterministic_runner.start_server("true", Path(tmp), "http://127.0.0.1:1/mcp")
        self.assertEqual(len(launches), 2)
        for env in launches:
            self.assertEqual(env["CODING_TOOLS_MCP_TELEMETRY"], "off")

    def test_the_makefile_exports_telemetry_off(self) -> None:
        """Verify Makefile commands export the overridable telemetry-off default."""
        makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text(encoding="utf-8")
        self.assertIn("export CODING_TOOLS_MCP_TELEMETRY ?= off", makefile)


class InstallIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = telemetry._install_id
        telemetry._install_id = None

    def tearDown(self) -> None:
        telemetry._install_id = self._saved

    def test_install_id_is_random_stable_and_resettable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}):
                first = telemetry.install_id()
                self.assertEqual(telemetry.install_id(), first)
                path = Path(tmp) / ".coding-tools-mcp" / "id"
                self.assertEqual(path.read_text(encoding="utf-8").strip(), first)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

                telemetry._install_id = None
                path.unlink()
                second = telemetry.install_id()
                self.assertNotEqual(second, first)
                self.assertEqual(len(second), 32)


if __name__ == "__main__":
    unittest.main()
