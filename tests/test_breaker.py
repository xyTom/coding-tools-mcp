from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import server as server_module
from coding_tools_mcp.breaker import (
    BREAKER_TTL_SECONDS,
    REPEAT_FAILURE_LIMIT,
    RepeatFailureBreaker,
    argument_fingerprint,
)
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime, WorkspaceMutationPolicy


class ArgumentFingerprintTests(unittest.TestCase):
    def test_key_order_does_not_change_the_fingerprint(self) -> None:
        self.assertEqual(
            argument_fingerprint({"path": "a.txt", "start_line": 1}),
            argument_fingerprint({"start_line": 1, "path": "a.txt"}),
        )

    def test_a_different_value_is_a_different_call(self) -> None:
        self.assertNotEqual(
            argument_fingerprint({"path": "a.txt"}),
            argument_fingerprint({"path": "b.txt"}),
        )

    def test_varying_only_the_idempotency_key_does_not_dodge_the_breaker(self) -> None:
        self.assertEqual(
            argument_fingerprint({"patch": "p", "idempotency_key": "one"}),
            argument_fingerprint({"patch": "p", "idempotency_key": "two"}),
        )

    def test_unserializable_arguments_still_produce_a_fingerprint(self) -> None:
        self.assertIsInstance(argument_fingerprint({"value": object()}), str)


class RepeatFailureBreakerTests(unittest.TestCase):
    def test_the_third_identical_deterministic_failure_is_refused(self) -> None:
        breaker = RepeatFailureBreaker()
        self.assertIsNone(breaker.blocked_error_code("read_file", "fp"))
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        self.assertIsNone(breaker.blocked_error_code("read_file", "fp"))
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        self.assertEqual(breaker.blocked_error_code("read_file", "fp"), "NOT_FOUND")

    def test_a_retryable_failure_never_counts(self) -> None:
        breaker = RepeatFailureBreaker()
        for _ in range(5):
            breaker.record_failure("apply_patch", "fp", error_code="PATCH_CONFLICT", retryable=True)
        self.assertIsNone(breaker.blocked_error_code("apply_patch", "fp"))

    def test_a_success_with_the_same_arguments_clears_the_count(self) -> None:
        breaker = RepeatFailureBreaker()
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        breaker.record_success("read_file", "fp")
        self.assertIsNone(breaker.blocked_error_code("read_file", "fp"))

    def test_different_arguments_have_their_own_budget(self) -> None:
        breaker = RepeatFailureBreaker()
        for _ in range(3):
            breaker.record_failure("read_file", "one", error_code="NOT_FOUND", retryable=False)
        self.assertIsNone(breaker.blocked_error_code("read_file", "two"))

    def test_the_entry_map_stays_bounded(self) -> None:
        breaker = RepeatFailureBreaker(capacity=4)
        for index in range(20):
            breaker.record_failure("read_file", f"fp{index}", error_code="NOT_FOUND", retryable=False)
        self.assertLessEqual(len(breaker._entries), 4)


class BreakerInRuntimeTests(unittest.TestCase):
    def call(self, runtime: Runtime, args: dict[str, object]) -> dict[str, object]:
        return runtime.call_tool("read_file", args)

    def test_a_verbatim_retry_preserves_the_real_error_and_adds_advice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                args = {"path": "missing.txt"}
                first = self.call(runtime, args)
                second = self.call(runtime, args)
                third = self.call(runtime, args)
            finally:
                runtime.close()
        self.assertEqual(first["structuredContent"]["error"]["code"], "NOT_FOUND")
        # The second failure advises without vetoing the next attempt.
        self.assertEqual(second["structuredContent"]["error"]["details"]["recent_identical_failures"], 2)
        self.assertEqual(third["structuredContent"]["error"]["code"], "NOT_FOUND")
        self.assertIs(third["structuredContent"]["error"]["retryable"], False)
        self.assertIn("Repeated failures do not block execution.", third["content"][0]["text"])

    def test_a_returned_ok_false_payload_counts_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            args = {
                "tool_name": "exec_command",
                "permission": "network",
                "reason": "breaker regression",
                "arguments": {"cmd": "curl https://example.com"},
            }
            try:
                first = runtime.call_tool("request_permissions", args)
                second = runtime.call_tool("request_permissions", args)
                third = runtime.call_tool("request_permissions", args)
            finally:
                runtime.close()
        self.assertEqual(first["structuredContent"]["error"]["code"], "ELICITATION_UNSUPPORTED")
        self.assertEqual(
            second["structuredContent"]["error"]["details"]["recent_identical_failures"],
            2,
        )
        self.assertEqual(third["structuredContent"]["error"]["code"], "ELICITATION_UNSUPPORTED")

    def test_revision_required_needs_a_changed_call_to_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "a.txt").write_text("old\n", encoding="utf-8")
            runtime = Runtime(workspace, permission_mode="safe")
            args = {
                "changes": [{"action": "write", "path": "a.txt", "content": "new\n"}]
            }
            try:
                first = runtime.call_tool("apply_changes", args)
                second = runtime.call_tool("apply_changes", args)
                third = runtime.call_tool("apply_changes", args)
            finally:
                runtime.close()
        self.assertEqual(first["structuredContent"]["error"]["code"], "REVISION_REQUIRED")
        self.assertEqual(
            second["structuredContent"]["error"]["details"]["recent_identical_failures"],
            2,
        )
        self.assertEqual(third["structuredContent"]["error"]["code"], "REVISION_REQUIRED")

    def test_changing_the_arguments_is_never_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                for _ in range(3):
                    self.call(runtime, {"path": "missing.txt"})
                other = self.call(runtime, {"path": "also-missing.txt"})
            finally:
                runtime.close()
        self.assertEqual(other["structuredContent"]["error"]["code"], "NOT_FOUND")

    def test_a_write_that_lands_makes_every_earlier_verdict_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                self.call(runtime, {"path": "late.txt"})
                self.call(runtime, {"path": "late.txt"})
                self.assertEqual(
                    self.call(runtime, {"path": "late.txt"})["structuredContent"]["error"]["code"],
                    "NOT_FOUND",
                )
                created = runtime.call_tool(
                    "apply_patch",
                    {"patch": "*** Begin Patch\n*** Add File: late.txt\n+here\n*** End Patch\n"},
                )
                self.assertFalse(created["isError"])
                self.assertFalse(self.call(runtime, {"path": "late.txt"})["isError"])
            finally:
                runtime.close()

    def test_a_write_tool_with_no_net_mutation_keeps_earlier_verdicts(self) -> None:
        for tool_name in ("apply_patch", "apply_changes"):
            with self.subTest(tool=tool_name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                path = workspace / "app.py"
                path.write_text("one\ntwo\n", encoding="utf-8")
                runtime = Runtime(workspace, permission_mode="safe")
                missing_args = {"path": "missing.txt"}
                if tool_name == "apply_patch":
                    write_args: dict[str, object] = {
                        "patch": (
                            "*** Begin Patch\n"
                            "*** Update File: app.py\n"
                            "*** End Patch\n"
                        )
                    }
                else:
                    write_args = {
                        "changes": [
                            {
                                "action": "write",
                                "path": "app.py",
                                "revision": server_module.content_revision("one\ntwo\n"),
                                "content": "one\ntwo\n",
                            }
                        ]
                    }
                try:
                    self.call(runtime, missing_args)
                    self.call(runtime, missing_args)
                    before = path.stat().st_mtime_ns
                    no_op = runtime.call_tool(tool_name, write_args)
                    third = self.call(runtime, missing_args)
                finally:
                    runtime.close()
                self.assertFalse(no_op["isError"], no_op)
                self.assertEqual(path.read_text(encoding="utf-8"), "one\ntwo\n")
                self.assertEqual(path.stat().st_mtime_ns, before)
                self.assertTrue(
                    all(
                        entry["operation"] == "unchanged"
                        for entry in no_op["structuredContent"]["affected_files"]
                    )
                )
                self.assertEqual(
                    third["structuredContent"]["error"]["code"],
                    "NOT_FOUND",
                )

    def test_failures_started_before_a_reset_do_not_strike_the_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            original = runtime._tool_handlers["read_file"]
            all_entered = threading.Event()
            release = threading.Event()
            count_lock = threading.Lock()
            entered = 0

            def delayed(arguments: dict[str, object]) -> dict[str, object]:
                nonlocal entered
                with count_lock:
                    entered += 1
                    if entered == 2:
                        all_entered.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("timed out waiting for breaker reset")
                return original(arguments)

            runtime._tool_handlers["read_file"] = delayed
            old_results: list[dict[str, object] | None] = [None, None]

            def read_missing(index: int) -> None:
                old_results[index] = self.call(runtime, {"path": "missing.txt"})

            threads = [
                threading.Thread(target=read_missing, args=(0,)),
                threading.Thread(target=read_missing, args=(1,)),
            ]
            try:
                for thread in threads:
                    thread.start()
                self.assertTrue(all_entered.wait(timeout=1))
                write = runtime.call_tool(
                    "apply_patch",
                    {"patch": "*** Begin Patch\n*** Add File: reset.txt\n+new\n*** End Patch\n"},
                )
                self.assertFalse(write["isError"], write)
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=2)

            runtime._tool_handlers["read_file"] = original
            try:
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertTrue(
                    all(
                        result is not None
                        and result["structuredContent"]["error"]["code"] == "NOT_FOUND"
                        for result in old_results
                    )
                )
                fresh = self.call(runtime, {"path": "missing.txt"})
                self.assertEqual(fresh["structuredContent"]["error"]["code"], "NOT_FOUND")
            finally:
                runtime.close()

    def test_an_already_applied_update_that_moves_the_file_is_a_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "source.txt"
            source.write_text("anchor\nnew\n", encoding="utf-8")
            runtime = Runtime(workspace, permission_mode="safe")
            destination_args = {"path": "destination.txt"}
            try:
                self.call(runtime, destination_args)
                self.call(runtime, destination_args)
                moved = runtime.call_tool(
                    "apply_patch",
                    {
                        "patch": (
                            "*** Begin Patch\n"
                            "*** Update File: source.txt\n"
                            "*** Move to: destination.txt\n"
                            "@@\n"
                            " anchor\n"
                            "-old\n"
                            "+new\n"
                            "*** End Patch\n"
                        )
                    },
                )
                read = self.call(runtime, destination_args)
                source_exists_after_move = source.exists()
            finally:
                runtime.close()
        self.assertFalse(moved["isError"], moved)
        self.assertIs(moved["structuredContent"]["already_applied"], False)
        self.assertEqual(moved["structuredContent"]["affected_files"][0]["operation"], "move")
        self.assertFalse(source_exists_after_move)
        self.assertFalse(read["isError"], read)
        self.assertEqual(read["structuredContent"]["content"], "anchor\nnew\n")

    def test_move_source_deletion_resets_failures_even_when_destination_returns_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "a.txt").write_text("A\n", encoding="utf-8")
            (workspace / "b.txt").write_text("B\n", encoding="utf-8")
            runtime = Runtime(workspace, permission_mode="safe")
            create_args = {
                "changes": [{"action": "create", "path": "a.txt", "content": "NEW\n"}]
            }
            try:
                first = runtime.call_tool("apply_changes", create_args)
                second = runtime.call_tool("apply_changes", create_args)
                moved = runtime.call_tool(
                    "apply_patch",
                    {
                        "patch": (
                            "*** Begin Patch\n"
                            "*** Update File: a.txt\n"
                            "*** Move to: b.txt\n"
                            "@@\n"
                            " A\n"
                            "*** Update File: b.txt\n"
                            "@@\n"
                            "-A\n"
                            "+B\n"
                            "*** End Patch\n"
                        )
                    },
                )
                retried = runtime.call_tool("apply_changes", create_args)
                recreated = (workspace / "a.txt").read_text(encoding="utf-8")
            finally:
                runtime.close()

        self.assertEqual(first["structuredContent"]["error"]["code"], "PATCH_FAILED")
        self.assertEqual(second["structuredContent"]["error"]["code"], "PATCH_FAILED")
        self.assertFalse(moved["isError"], moved)
        self.assertNotIn("_workspace_mutated", moved["structuredContent"])
        self.assertIn(
            {"path": "a.txt", "operation": "delete", "total_lines": 0},
            moved["structuredContent"]["affected_files"],
        )
        self.assertFalse(retried["isError"], retried)
        self.assertEqual(recreated, "NEW\n")

    def test_a_completed_exec_makes_workspace_read_verdicts_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            try:
                self.call(runtime, {"path": "late.txt"})
                self.call(runtime, {"path": "late.txt"})
                command = runtime.call_tool(
                    "exec_command",
                    {
                        "cmd": "printf 'created\\n' > late.txt",
                        "yield_time_ms": 5000,
                        "timeout_ms": 5000,
                    },
                )
                read = self.call(runtime, {"path": "late.txt"})
            finally:
                runtime.close()
        self.assertFalse(command["isError"], command)
        self.assertEqual(command["structuredContent"]["operation_outcome"], "exited_0")
        self.assertFalse(read["isError"], read)

    def test_a_terminal_write_stdin_poll_makes_workspace_read_verdicts_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")
            command_text = (
                f'"{sys.executable}" -c "import time; time.sleep(0.2); '
                "open('late.txt', 'w').write('created\\\\n')\""
            )
            try:
                self.call(runtime, {"path": "late.txt"})
                self.call(runtime, {"path": "late.txt"})
                command = runtime.call_tool(
                    "exec_command",
                    {"cmd": command_text, "yield_time_ms": 1, "timeout_ms": 5000},
                )
                self.assertEqual(command["structuredContent"]["operation_outcome"], "running")
                poll: dict[str, object] = {}
                for _ in range(20):
                    poll = runtime.call_tool(
                        "write_stdin",
                        {
                            "command_id": command["structuredContent"]["command_id"],
                            "chars": "",
                            "yield_time_ms": 250,
                        },
                    )
                    if poll["structuredContent"]["operation_outcome"] != "running":
                        break
                read = self.call(runtime, {"path": "late.txt"})
            finally:
                runtime.close()
        self.assertEqual(poll["structuredContent"]["operation_outcome"], "exited_0")
        self.assertFalse(read["isError"], read)

    def test_every_terminal_command_observer_can_clear_stale_verdicts(self) -> None:
        arguments = {
            "write_stdin": {"command_id": "observed", "chars": ""},
            "read_output": {"output_ref": "command:observed:stdout"},
            "kill_command": {"command_id": "observed"},
        }
        for tool_name, tool_args in arguments.items():
            with self.subTest(tool=tool_name), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                runtime = Runtime(workspace, permission_mode="trusted")
                try:
                    self.call(runtime, {"path": "late.txt"})
                    self.call(runtime, {"path": "late.txt"})
                    (workspace / "late.txt").write_text("created\n", encoding="utf-8")
                    runtime._tool_handlers[tool_name] = lambda _args: {
                        "command_id": "observed",
                        "operation_outcome": "exited_0",
                    }
                    observed = runtime.call_tool(tool_name, tool_args)
                    read = self.call(runtime, {"path": "late.txt"})
                finally:
                    runtime.close()
                self.assertFalse(observed["isError"], observed)
                self.assertFalse(read["isError"], read)

    def test_a_terminal_command_resets_the_breaker_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "a.txt").write_text("present\n", encoding="utf-8")
            runtime = Runtime(workspace, permission_mode="trusted")
            patch_args = {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: a.txt\n"
                    "@@\n"
                    "-missing\n"
                    "+replacement\n"
                    "*** End Patch\n"
                )
            }
            try:
                command = runtime.call_tool(
                    "exec_command",
                    {
                        "cmd": f'"{sys.executable}" -c "print(\'done\')"',
                        "yield_time_ms": 5000,
                        "timeout_ms": 5000,
                        "verbosity": "summary",
                    },
                )
                self.assertEqual(command["structuredContent"]["operation_outcome"], "exited_0")
                output_ref = command["structuredContent"]["output_refs"]["stdout"]

                first = runtime.call_tool("apply_patch", patch_args)
                runtime.call_tool("read_output", {"output_ref": output_ref})
                second = runtime.call_tool("apply_patch", patch_args)
                runtime.call_tool("read_output", {"output_ref": output_ref})
                third = runtime.call_tool("apply_patch", patch_args)
            finally:
                runtime.close()
        self.assertEqual(first["structuredContent"]["error"]["code"], "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(second["structuredContent"]["error"]["code"], "PATCH_CONTEXT_NOT_FOUND")
        self.assertEqual(third["structuredContent"]["error"]["code"], "PATCH_CONTEXT_NOT_FOUND")

    def test_a_structured_only_command_with_a_write_path_clears_stale_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(
                workspace,
                permission_mode="trusted",
                workspace_mutation=WorkspaceMutationPolicy(
                    mode="structured-only", write_paths=("generated",)
                ),
            )
            try:
                args = {"path": "generated/late.txt"}
                self.call(runtime, args)
                self.call(runtime, args)
                command = runtime.call_tool(
                    "exec_command",
                    {
                        "cmd": "printf 'created\\n' > generated/late.txt",
                        "yield_time_ms": 5000,
                        "timeout_ms": 5000,
                    },
                )
                read = self.call(runtime, args)
            finally:
                runtime.close()
        self.assertEqual(command["structuredContent"]["operation_outcome"], "exited_0")
        self.assertFalse(read["isError"], read)

    def test_a_structured_only_command_without_a_write_path_keeps_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                permission_mode="trusted",
                workspace_mutation=WorkspaceMutationPolicy(mode="structured-only"),
            )
            try:
                mutation = runtime.workspace_mutation_payload()
                args = {"path": "late.txt"}
                self.call(runtime, args)
                self.call(runtime, args)
                command = runtime.call_tool(
                    "exec_command",
                    {
                        "cmd": f'"{sys.executable}" -c "pass"',
                        "yield_time_ms": 5000,
                        "timeout_ms": 5000,
                    },
                )
                read = self.call(runtime, args)
            finally:
                runtime.close()
        self.assertEqual(command["structuredContent"]["operation_outcome"], "exited_0")
        self.assertEqual(read["structuredContent"]["error"]["code"], "NOT_FOUND")
        details = read["structuredContent"]["error"]["details"]
        self.assertEqual(details.get("recent_identical_failures"), 3 if mutation["enforced"] else None)

    def test_unenforced_structured_only_mode_clears_stale_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(
                workspace,
                permission_mode="dangerous",
                workspace_mutation=WorkspaceMutationPolicy(mode="structured-only"),
            )
            try:
                mutation = runtime.workspace_mutation_payload()
                self.assertEqual(mutation["mode"], "structured-only")
                self.assertIs(mutation["enforced"], False)
                args = {"path": "late.txt"}
                self.call(runtime, args)
                self.call(runtime, args)
                command = runtime.call_tool(
                    "exec_command",
                    {
                        "cmd": "printf 'created\\n' > late.txt",
                        "yield_time_ms": 5000,
                        "timeout_ms": 5000,
                    },
                )
                read = self.call(runtime, args)
            finally:
                runtime.close()
        self.assertEqual(command["structuredContent"]["operation_outcome"], "exited_0")
        self.assertFalse(read["isError"], read)

    def test_landlock_install_failure_clears_stale_verdicts_for_that_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(
                workspace,
                permission_mode="trusted",
                workspace_mutation=WorkspaceMutationPolicy(mode="structured-only"),
            )

            def unavailable(*_args: object, **_kwargs: object) -> int:
                raise ToolFailure(
                    "SANDBOX_UNAVAILABLE",
                    "simulated Landlock install failure",
                    category="security",
                )

            args = {"path": "late.txt"}
            try:
                self.call(runtime, args)
                self.call(runtime, args)
                with patch.object(
                    server_module,
                    "landlock_status_payload",
                    return_value={"available": True, "abi_version": 6},
                ), patch.object(server_module, "open_landlock_ruleset", side_effect=unavailable):
                    self.assertIs(runtime.workspace_mutation_payload()["enforced"], True)
                    command = runtime.call_tool(
                        "exec_command",
                        {
                            "cmd": "printf 'created\\n' > late.txt",
                            "yield_time_ms": 5000,
                            "timeout_ms": 5000,
                        },
                    )
                read = self.call(runtime, args)
            finally:
                runtime.close()
        self.assertEqual(command["structuredContent"]["operation_outcome"], "exited_0")
        self.assertTrue(
            any("Landlock" in item for item in command["structuredContent"].get("warnings", []))
        )
        self.assertFalse(read["isError"], read)

    def test_a_dry_run_changes_nothing_and_does_not_clear_the_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                self.call(runtime, {"path": "late.txt"})
                self.call(runtime, {"path": "late.txt"})
                runtime.call_tool(
                    "apply_patch",
                    {
                        "patch": "*** Begin Patch\n*** Add File: other.txt\n+x\n*** End Patch\n",
                        "dry_run": True,
                    },
                )
                self.assertEqual(
                    self.call(runtime, {"path": "late.txt"})["structuredContent"]["error"]["code"],
                    "NOT_FOUND",
                )
            finally:
                runtime.close()

    def test_a_verbatim_patch_context_miss_counts_even_though_it_is_retryable(self) -> None:
        breaker = RepeatFailureBreaker()
        for _ in range(2):
            breaker.record_failure(
                "apply_patch", "fp", error_code="PATCH_CONTEXT_NOT_FOUND", retryable=True
            )
        self.assertEqual(breaker.blocked_error_code("apply_patch", "fp"), "PATCH_CONTEXT_NOT_FOUND")


class _Clock:
    def __init__(self) -> None:
        """Initialize a deterministic clock that tests can advance directly."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Return the current simulated monotonic time."""
        return self.now


class BreakerTtlTests(unittest.TestCase):
    def test_a_verdict_older_than_the_ttl_no_longer_blocks_and_is_dropped(self) -> None:
        """Verify verdicts remain valid at the TTL boundary and are removed after it."""
        clock = _Clock()
        breaker = RepeatFailureBreaker(clock=clock)
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        clock.now += BREAKER_TTL_SECONDS
        self.assertEqual(breaker.blocked_error_code("read_file", "fp"), "NOT_FOUND")
        clock.now += 1
        self.assertIsNone(breaker.blocked_error_code("read_file", "fp"))
        self.assertNotIn(("read_file", "fp"), breaker._entries)
        self.assertNotIn(("read_file", "fp"), breaker._last_failure)

    def test_a_failure_after_expiry_starts_a_fresh_budget(self) -> None:
        """Verify the first failure after expiry starts a new counter at one."""
        clock = _Clock()
        breaker = RepeatFailureBreaker(clock=clock)
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        clock.now += BREAKER_TTL_SECONDS + 1
        self.assertEqual(
            breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False), 1
        )
        self.assertIsNone(breaker.blocked_error_code("read_file", "fp"))

    def test_each_failure_refreshes_the_ttl(self) -> None:
        """Verify each counted failure renews the verdict's expiration time."""
        clock = _Clock()
        breaker = RepeatFailureBreaker(clock=clock)
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        clock.now += BREAKER_TTL_SECONDS - 1
        breaker.record_failure("read_file", "fp", error_code="NOT_FOUND", retryable=False)
        clock.now += BREAKER_TTL_SECONDS - 1
        self.assertEqual(breaker.blocked_error_code("read_file", "fp"), "NOT_FOUND")

    def test_the_default_limit_and_ttl(self) -> None:
        """Pin the default breaker budget to two failures and its TTL to sixty seconds."""
        self.assertEqual(REPEAT_FAILURE_LIMIT, 2)
        self.assertEqual(BREAKER_TTL_SECONDS, 60)

    def test_an_externally_created_file_is_readable_before_and_after_expiry(self) -> None:
        """Verify recent NOT_FOUND history never hides an externally created file."""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace, permission_mode="safe")
            runtime.breaker = RepeatFailureBreaker(clock=clock)
            try:
                runtime.call_tool("read_file", {"path": "late.txt"})
                runtime.call_tool("read_file", {"path": "late.txt"})
                (workspace / "late.txt").write_text("created\n", encoding="utf-8")
                immediate = runtime.call_tool("read_file", {"path": "late.txt"})
                clock.now += BREAKER_TTL_SECONDS + 1
                read = runtime.call_tool("read_file", {"path": "late.txt"})
            finally:
                runtime.close()
        self.assertFalse(immediate["isError"], immediate)
        self.assertFalse(read["isError"], read)


class InternalErrorIsNotCountedTests(unittest.TestCase):
    def test_the_breaker_ignores_internal_error(self) -> None:
        """Verify internal errors never consume the repeated-failure budget."""
        breaker = RepeatFailureBreaker()
        for _ in range(5):
            self.assertEqual(
                breaker.record_failure("apply_patch", "fp", error_code="INTERNAL_ERROR", retryable=False), 0
            )
        self.assertIsNone(breaker.blocked_error_code("apply_patch", "fp"))

    def test_repeated_os_errors_in_a_handler_are_never_blocked(self) -> None:
        """Verify repeated handler OSErrors remain callable and surface as internal errors."""
        calls: list[int] = []

        def failing(_args: object) -> dict[str, object]:
            """Count handler invocations and simulate a full filesystem."""
            calls.append(1)
            raise OSError(28, "No space left on device")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            runtime._tool_handlers["read_file"] = failing
            try:
                results = [runtime.call_tool("read_file", {"path": "a.txt"}) for _ in range(4)]
            finally:
                runtime.close()
        self.assertEqual(len(calls), 4)
        for result in results:
            self.assertEqual(result["structuredContent"]["error"]["code"], "INTERNAL_ERROR")
            self.assertNotIn("recent_identical_failures", result["structuredContent"]["error"]["details"])


class CommandLifecycleResetTests(unittest.TestCase):
    """Starting or killing a command that may write invalidates verdicts."""

    def blocked_then(self, tool: str, args: dict[str, object], payload: dict[str, object], **runtime_kwargs: object) -> dict[str, object]:
        """Seed a stale read verdict, invoke a mocked command tool, and return the next read."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runtime = Runtime(workspace, permission_mode="trusted", **runtime_kwargs)  # type: ignore[arg-type]
            try:
                runtime.call_tool("read_file", {"path": "late.txt"})
                runtime.call_tool("read_file", {"path": "late.txt"})
                (workspace / "late.txt").write_text("created\n", encoding="utf-8")
                runtime._tool_handlers[tool] = lambda _args: dict(payload)
                observed = runtime.call_tool(tool, args)
                self.assertFalse(observed["isError"], observed)
                return runtime.call_tool("read_file", {"path": "late.txt"})
            finally:
                runtime.close()

    def test_starting_a_background_command_clears_verdicts(self) -> None:
        """Verify starting a potentially writable command invalidates stale read verdicts."""
        read = self.blocked_then(
            "exec_command", {"cmd": "true"}, {"command_id": "bg", "operation_outcome": "running"}
        )
        self.assertFalse(read["isError"], read)

    def test_killing_a_command_clears_verdicts(self) -> None:
        """Verify killing a potentially writable command invalidates stale read verdicts."""
        read = self.blocked_then(
            "kill_command", {"command_id": "bg"}, {"command_id": "bg", "operation_outcome": "running"}
        )
        self.assertFalse(read["isError"], read)

    def test_polling_a_running_command_keeps_verdicts(self) -> None:
        """Verify polling a running command preserves existing failure verdicts."""
        for tool, args in (
            ("write_stdin", {"command_id": "bg", "chars": ""}),
            ("read_output", {"output_ref": "command:bg:stdout"}),
        ):
            with self.subTest(tool=tool):
                read = self.blocked_then(tool, args, {"command_id": "bg", "operation_outcome": "running"})
                self.assertFalse(read["isError"], read)

    def test_a_failed_exec_command_keeps_verdicts(self) -> None:
        """Verify rejected command starts retain their real error on repeated attempts."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="trusted")

            def refuse(_args: object) -> dict[str, object]:
                """Simulate command validation failure before a process can start."""
                raise ToolFailure("INVALID_ARGUMENT", "bad workdir", category="validation")

            runtime._tool_handlers["exec_command"] = refuse
            try:
                results = [runtime.call_tool("exec_command", {"cmd": "true"}) for _ in range(3)]
            finally:
                runtime.close()
        self.assertEqual(results[2]["structuredContent"]["error"]["code"], "INVALID_ARGUMENT")

    def test_a_command_that_cannot_write_keeps_verdicts(self) -> None:
        """Verify enforced structured-only execution preserves existing failure verdicts."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(
                Path(tmp),
                permission_mode="trusted",
                workspace_mutation=WorkspaceMutationPolicy(mode="structured-only"),
            )
            try:
                enforced = runtime.workspace_mutation_payload()["enforced"] is True
                runtime.call_tool("read_file", {"path": "late.txt"})
                runtime.call_tool("read_file", {"path": "late.txt"})
                runtime._tool_handlers["exec_command"] = lambda _args: {
                    "command_id": "bg",
                    "operation_outcome": "running",
                }
                runtime.call_tool("exec_command", {"cmd": "true"})
                read = runtime.call_tool("read_file", {"path": "late.txt"})
            finally:
                runtime.close()
        self.assertEqual(read["structuredContent"]["error"]["code"], "NOT_FOUND")
        details = read["structuredContent"]["error"]["details"]
        self.assertEqual(details.get("recent_identical_failures"), 3 if enforced else None)


class IdempotencyDefaultNormalizationTests(unittest.TestCase):
    PATCH = "*** Begin Patch\n*** Add File: new.txt\n+hello\n*** End Patch\n"

    def test_an_explicit_default_dry_run_replays_instead_of_colliding(self) -> None:
        """Verify explicit default arguments replay a saved result while changed values conflict."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(Path(tmp), permission_mode="safe")
            try:
                first = runtime.call_tool("apply_patch", {"patch": self.PATCH, "idempotency_key": "k1"})
                retry = runtime.call_tool(
                    "apply_patch", {"patch": self.PATCH, "idempotency_key": "k1", "dry_run": False}
                )
                dry = runtime.call_tool(
                    "apply_patch", {"patch": self.PATCH, "idempotency_key": "k1", "dry_run": True}
                )
            finally:
                runtime.close()
        self.assertFalse(first["isError"], first)
        self.assertFalse(retry["isError"], retry)
        self.assertIs(retry["structuredContent"].get("idempotent_replay"), True)
        self.assertEqual(dry["structuredContent"]["error"]["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_only_values_equal_to_the_default_and_of_its_type_are_dropped(self) -> None:
        """Verify default normalization compares both the value and its type."""
        drop = server_module._drop_schema_defaults
        self.assertEqual(drop("apply_patch", {"patch": "p", "dry_run": False}), {"patch": "p"})
        self.assertEqual(drop("apply_patch", {"patch": "p", "dry_run": True}), {"patch": "p", "dry_run": True})
        self.assertEqual(drop("apply_patch", {"patch": "p", "dry_run": 0}), {"patch": "p", "dry_run": 0})

    def test_the_breaker_fingerprint_is_unchanged(self) -> None:
        """Verify breaker fingerprints still distinguish omitted and explicit default values."""
        self.assertNotEqual(
            argument_fingerprint({"patch": "p"}),
            argument_fingerprint({"patch": "p", "dry_run": False}),
        )


if __name__ == "__main__":
    unittest.main()
