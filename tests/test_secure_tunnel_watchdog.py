from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "secure_tunnel_watchdog.py"
SPEC = importlib.util.spec_from_file_location("secure_tunnel_watchdog", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watchdog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watchdog
SPEC.loader.exec_module(watchdog)


class SecureTunnelWatchdogTests(unittest.TestCase):
    def test_ready_task_is_started_even_after_zero_exit(self) -> None:
        decision = watchdog.decide_action("Ready", None, 0, 2)
        self.assertEqual(decision.action, "start")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_running_task_requires_two_consecutive_health_failures(self) -> None:
        first = watchdog.decide_action("Running", watchdog.LOCAL_UNHEALTHY, 0, 2)
        second = watchdog.decide_action(
            "Running", watchdog.LOCAL_UNHEALTHY, first.consecutive_failures, 2
        )
        self.assertEqual(first.action, "degraded")
        self.assertEqual(first.consecutive_failures, 1)
        self.assertEqual(second.action, "restart")
        self.assertEqual(second.consecutive_failures, 2)

    def test_repeated_deadline_degradation_uses_the_same_restart_budget(self) -> None:
        first = watchdog.decide_action("Running", watchdog.DEADLINE_DEGRADED, 0, 2)
        second = watchdog.decide_action(
            "Running", watchdog.DEADLINE_DEGRADED, first.consecutive_failures, 2
        )
        self.assertEqual(first.action, "degraded")
        self.assertEqual(second.action, "restart")

    def test_deadline_log_scan_baselines_then_reports_only_new_events(self) -> None:
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "tunnel.jsonl"
            old = {
                "time": "2026-08-27T00:01:00+08:00",
                "msg": watchdog.DEADLINE_MESSAGE,
            }
            log.write_text(json.dumps(old) + "\n", encoding="utf-8")
            cursor, count = watchdog.scan_deadline_drops(log, "")
            self.assertEqual(cursor, old["time"])
            self.assertEqual(count, 0)
            newer = {
                "time": "2026-08-27T00:02:00+08:00",
                "msg": watchdog.DEADLINE_MESSAGE,
            }
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(newer) + "\n")
            next_cursor, count = watchdog.scan_deadline_drops(log, cursor)
            self.assertEqual(next_cursor, newer["time"])
            self.assertEqual(count, 1)

    def test_health_success_resets_failure_counter(self) -> None:
        decision = watchdog.decide_action("Running", watchdog.HEALTHY, 9, 2)
        self.assertEqual(decision.action, "healthy")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_disabled_task_is_never_started(self) -> None:
        decision = watchdog.decide_action("Disabled", None, 1, 2)
        self.assertEqual(decision.action, "disabled")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_unknown_transient_state_is_left_alone(self) -> None:
        decision = watchdog.decide_action("Queued", None, 1, 2)
        self.assertEqual(decision.action, "wait")
        self.assertEqual(decision.consecutive_failures, 1)

    def test_control_plane_failure_does_not_restart_live_local_process(self) -> None:
        decision = watchdog.decide_action("Running", watchdog.CONTROL_PLANE_DEGRADED, 1, 2)
        self.assertEqual(decision.action, "control_plane_degraded")
        self.assertEqual(decision.consecutive_failures, 0)

    def test_state_write_is_atomic_and_round_trips(self) -> None:
        with TemporaryDirectory() as temporary:
            state_file = Path(temporary) / "state.json"
            watchdog.save_state(state_file, consecutive_failures=3, last_action="degraded")
            payload = watchdog.load_state(state_file)
            self.assertEqual(payload["consecutive_failures"], 3)
            self.assertEqual(payload["last_action"], "degraded")
            self.assertFalse(state_file.with_name("state.json.tmp").exists())
            json.loads(state_file.read_text(encoding="utf-8"))

    def test_event_log_rotates_at_a_fixed_size(self) -> None:
        with TemporaryDirectory() as temporary:
            event_log = Path(temporary) / "events.jsonl"
            event_log.write_text("x" * 128, encoding="utf-8")
            watchdog.append_event(event_log, max_bytes=64, backups=2, action="healthy")
            self.assertTrue(event_log.with_name("events.jsonl.1").is_file())
            event = json.loads(event_log.read_text(encoding="utf-8"))
            self.assertEqual(event["action"], "healthy")

    def test_recovery_backoff_caps_at_last_delay(self) -> None:
        config = self._config(Path.cwd())
        self.assertEqual(watchdog.recovery_delay(config, 1), 60.0)
        self.assertEqual(watchdog.recovery_delay(config, 99), 1800.0)

    def test_task_queries_are_scoped_by_name_and_path(self) -> None:
        config = self._config(Path.cwd())
        completed = watchdog.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"State":"Ready","LastTaskResult":0}',
            stderr="",
        )
        with patch.object(watchdog, "run_powershell", return_value=completed) as runner:
            status = watchdog.task_status(config)
        self.assertEqual(status.state, "Ready")
        script = runner.call_args.args[1]
        self.assertIn("-TaskPath $env:OPENAI_TUNNEL_TASK_PATH", script)

    def test_ready_task_bypasses_persisted_recovery_cooldown(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=0,
                last_action="started_unready",
                recovery_attempts=2,
                next_recovery_at=2000.0,
                last_recovery_at=1000.0,
            )
            unhealthy = watchdog.HealthProbe(
                watchdog.LOCAL_UNHEALTHY, False, False, False, False
            )
            statuses = [
                watchdog.TaskStatus("Ready", 0),
                watchdog.TaskStatus("Running", 267009),
            ]
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "start_task") as starter,
                patch.object(watchdog, "wait_for_health", return_value=unhealthy),
                patch.object(watchdog.time, "time", return_value=1500.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            starter.assert_called_once_with(config)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "started_unready")
            self.assertEqual(state["recovery_attempts"], 1)
            self.assertEqual(state["next_recovery_at"], 1560.0)

    def test_running_restart_respects_persisted_recovery_cooldown(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=1,
                last_action="restarted_unready",
                recovery_attempts=2,
                next_recovery_at=2000.0,
                last_recovery_at=1000.0,
            )
            unhealthy = watchdog.HealthProbe(
                watchdog.LOCAL_UNHEALTHY, False, False, False, False
            )
            with (
                patch.object(
                    watchdog, "task_status", return_value=watchdog.TaskStatus("Running", 267009)
                ),
                patch.object(watchdog, "probe_health", return_value=unhealthy),
                patch.object(watchdog, "restart_task") as restart,
                patch.object(watchdog.time, "time", return_value=1500.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            restart.assert_not_called()
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "restart_cooldown")
            self.assertEqual(state["recovery_attempts"], 2)

    def test_brief_healthy_period_does_not_reset_recovery_backoff(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=0,
                last_action="started_healthy",
                recovery_attempts=2,
                next_recovery_at=2000.0,
                last_recovery_at=1300.0,
                healthy_since_at=1400.0,
            )
            healthy = watchdog.HealthProbe(watchdog.HEALTHY, True, True, True, True)
            with (
                patch.object(
                    watchdog, "task_status", return_value=watchdog.TaskStatus("Running", 267009)
                ),
                patch.object(watchdog, "probe_health", return_value=healthy),
                patch.object(watchdog.time, "time", return_value=1500.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "healthy")
            self.assertEqual(state["recovery_attempts"], 2)
            self.assertEqual(state["healthy_since_at"], 1400.0)

    def test_stable_healthy_period_resets_recovery_backoff(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=0,
                last_action="started_healthy",
                recovery_attempts=3,
                next_recovery_at=2000.0,
                last_recovery_at=900.0,
                healthy_since_at=1000.0,
            )
            healthy = watchdog.HealthProbe(watchdog.HEALTHY, True, True, True, True)
            with (
                patch.object(
                    watchdog, "task_status", return_value=watchdog.TaskStatus("Running", 267009)
                ),
                patch.object(watchdog, "probe_health", return_value=healthy),
                patch.object(watchdog.time, "time", return_value=1400.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "healthy_stable")
            self.assertEqual(state["recovery_attempts"], 0)
            self.assertEqual(state["next_recovery_at"], 0.0)

    def test_successful_start_keeps_backoff_until_stability_window(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            healthy = watchdog.HealthProbe(watchdog.HEALTHY, True, True, True, True)
            statuses = [
                watchdog.TaskStatus("Ready", 0),
                watchdog.TaskStatus("Running", 267009),
            ]
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "start_task"),
                patch.object(watchdog, "wait_for_health", return_value=healthy),
                patch.object(watchdog.time, "time", side_effect=[1000.0, 1001.0]),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "started_healthy")
            self.assertEqual(state["recovery_attempts"], 1)
            self.assertEqual(state["next_recovery_at"], 1060.0)
            self.assertEqual(state["healthy_since_at"], 1001.0)

    def test_unready_restart_keeps_backoff_lease(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            watchdog.save_state(
                config.state_file,
                consecutive_failures=1,
                last_action="degraded",
            )
            unhealthy = watchdog.HealthProbe(
                watchdog.LOCAL_UNHEALTHY, False, False, False, False
            )
            statuses = [
                watchdog.TaskStatus("Running", 267009),
                watchdog.TaskStatus("Running", 267009),
            ]
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "probe_health", return_value=unhealthy),
                patch.object(watchdog, "restart_task") as restart,
                patch.object(watchdog, "wait_for_health", return_value=unhealthy),
                patch.object(watchdog.time, "time", return_value=1000.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            restart.assert_called_once_with(config)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["last_action"], "restarted_unready")
            self.assertEqual(state["recovery_attempts"], 1)
            self.assertEqual(state["next_recovery_at"], 1060.0)

    def test_slow_initial_poll_does_not_consume_failure_budget(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            statuses = [
                watchdog.TaskStatus("Ready", 0),
                watchdog.TaskStatus("Running", 267009),
            ]
            unready = watchdog.HealthProbe(
                watchdog.CONTROL_PLANE_DEGRADED, False, True, False, False
            )
            with (
                patch.object(watchdog, "task_status", side_effect=statuses),
                patch.object(watchdog, "start_task"),
                patch.object(watchdog, "wait_for_health", return_value=unready),
                patch.object(watchdog.time, "time", return_value=1000.0),
            ):
                self.assertEqual(watchdog.run_once(config), 0)
            state = watchdog.load_state(config.state_file)
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertEqual(state["last_action"], "started_unready")
            self.assertEqual(state["recovery_attempts"], 1)

    def test_main_records_error_when_no_console_stream_exists(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            with (
                patch.object(watchdog, "parse_args", return_value=config),
                patch.object(watchdog, "run_once", side_effect=RuntimeError("boom")),
                patch.object(watchdog.sys, "stderr", None),
            ):
                self.assertEqual(watchdog.main([]), 1)
            event = json.loads(config.event_log.read_text(encoding="utf-8"))
            self.assertEqual(event["action"], "watchdog_error")
            self.assertEqual(event["error_type"], "RuntimeError")

    @staticmethod
    def _config(root: Path) -> object:
        return watchdog.Config(
            main_task_name="test tunnel",
            tunnel_client=Path(sys.executable),
            health_url_file=root / "health.url",
            pid_file=root / "tunnel.pid",
            state_file=root / "watchdog-state.json",
            event_log=root / "watchdog-events.jsonl",
            powershell=Path(sys.executable),
            failure_threshold=2,
            startup_grace_seconds=0,
            health_timeout_seconds=1,
        )


if __name__ == "__main__":
    unittest.main()
