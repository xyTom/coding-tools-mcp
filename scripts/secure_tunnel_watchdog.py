#!/usr/bin/env python3
"""Repair a Windows Secure MCP Tunnel task when it exits or becomes unhealthy.

The primary scheduled task continues to own ``tunnel-client.exe``.  This
watchdog is intentionally short lived so Task Scheduler can run it once per
minute without creating a second long-lived process tree.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNNING = "Running"
READY = "Ready"
DISABLED = "Disabled"
HEALTHY = "healthy"
LOCAL_UNHEALTHY = "local_unhealthy"
CONTROL_PLANE_DEGRADED = "control_plane_degraded"
DEADLINE_DEGRADED = "deadline_degraded"
DEADLINE_MESSAGE = "command response deadline reached; dropping without posting a response"
DEFAULT_RECOVERY_BACKOFF_SECONDS = (60.0, 120.0, 300.0, 600.0, 1800.0)


@dataclass(frozen=True)
class Decision:
    action: str
    consecutive_failures: int


@dataclass(frozen=True)
class Config:
    main_task_name: str
    tunnel_client: Path
    health_url_file: Path
    pid_file: Path
    state_file: Path
    event_log: Path
    powershell: Path
    failure_threshold: int
    startup_grace_seconds: float
    health_timeout_seconds: float
    main_task_path: str = "\\"
    tunnel_log: Path | None = None
    recovery_backoff_seconds: tuple[float, ...] = DEFAULT_RECOVERY_BACKOFF_SECONDS
    stable_health_seconds: float = 300.0
    event_log_max_bytes: int = 5 * 1024 * 1024
    event_log_backups: int = 3


@dataclass(frozen=True)
class TaskStatus:
    state: str
    last_task_result: int | None


@dataclass(frozen=True)
class HealthProbe:
    kind: str
    ok: bool
    local_live: bool
    ready: bool
    control_plane_poll: bool


def decide_action(
    task_state: str,
    health_kind: str | None,
    consecutive_failures: int,
    failure_threshold: int,
) -> Decision:
    """Return the next repair action without performing external effects."""
    if failure_threshold < 1:
        raise ValueError("failure_threshold must be at least 1")
    if task_state == DISABLED:
        return Decision("disabled", 0)
    if task_state == READY:
        return Decision("start", 0)
    if task_state != RUNNING:
        return Decision("wait", max(0, consecutive_failures))
    if health_kind == HEALTHY:
        return Decision("healthy", 0)
    if health_kind == CONTROL_PLANE_DEGRADED:
        # Restarting a live local process cannot repair an upstream outage.
        return Decision("control_plane_degraded", 0)
    failures = max(0, consecutive_failures) + 1
    if failures >= failure_threshold:
        return Decision("restart", failures)
    return Decision("degraded", failures)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {
            "consecutive_failures": 0,
            "recovery_attempts": 0,
            "next_recovery_at": 0.0,
            "last_recovery_at": 0.0,
            "healthy_since_at": 0.0,
            "last_deadline_time": "",
        }
    if not isinstance(payload, dict):
        return {
            "consecutive_failures": 0,
            "recovery_attempts": 0,
            "next_recovery_at": 0.0,
            "last_recovery_at": 0.0,
            "healthy_since_at": 0.0,
            "last_deadline_time": "",
        }
    try:
        failures = max(0, int(payload.get("consecutive_failures", 0)))
    except (TypeError, ValueError):
        failures = 0
    try:
        attempts = max(0, int(payload.get("recovery_attempts", 0)))
    except (TypeError, ValueError):
        attempts = 0
    try:
        next_recovery_at = max(0.0, float(payload.get("next_recovery_at", 0.0)))
    except (TypeError, ValueError):
        next_recovery_at = 0.0
    try:
        last_recovery_at = max(0.0, float(payload.get("last_recovery_at", 0.0)))
    except (TypeError, ValueError):
        last_recovery_at = 0.0
    try:
        healthy_since_at = max(0.0, float(payload.get("healthy_since_at", 0.0)))
    except (TypeError, ValueError):
        healthy_since_at = 0.0
    return {
        **payload,
        "consecutive_failures": failures,
        "recovery_attempts": attempts,
        "next_recovery_at": next_recovery_at,
        "last_recovery_at": last_recovery_at,
        "healthy_since_at": healthy_since_at,
        "last_deadline_time": str(payload.get("last_deadline_time", "") or ""),
    }


def save_state(
    path: Path,
    *,
    consecutive_failures: int,
    last_action: str,
    recovery_attempts: int = 0,
    next_recovery_at: float = 0.0,
    last_recovery_at: float = 0.0,
    healthy_since_at: float = 0.0,
    last_deadline_time: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "consecutive_failures": max(0, consecutive_failures),
        "recovery_attempts": max(0, recovery_attempts),
        "next_recovery_at": max(0.0, next_recovery_at),
        "last_recovery_at": max(0.0, last_recovery_at),
        "healthy_since_at": max(0.0, healthy_since_at),
        "last_deadline_time": last_deadline_time,
        "last_action": last_action,
        "updated_at": utc_now(),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def rotate_event_log(path: Path, *, max_bytes: int, backups: int) -> None:
    if max_bytes <= 0 or backups <= 0:
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return
    for index in range(backups, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        destination = path.with_name(f"{path.name}.{index}")
        if not source.exists():
            continue
        if destination.exists():
            destination.unlink()
        os.replace(source, destination)


def append_event(
    path: Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 3,
    **fields: Any,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_event_log(path, max_bytes=max_bytes, backups=backups)
    event = {"time": utc_now(), **fields}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run_powershell(config: Config, script: str, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["OPENAI_TUNNEL_TASK_NAME"] = config.main_task_name
    environment["OPENAI_TUNNEL_TASK_PATH"] = config.main_task_path
    prefix = (
        "$ErrorActionPreference='Stop';"
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
    )
    return subprocess.run(
        [
            str(config.powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            prefix + script,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=environment,
        creationflags=creation_flags(),
    )


def task_status(config: Config) -> TaskStatus:
    result = run_powershell(
        config,
        "$task=Get-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "$info=Get-ScheduledTaskInfo -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "[pscustomobject]@{State=[string]$task.State;LastTaskResult=[int64]$info.LastTaskResult}"
        "|ConvertTo-Json -Compress",
    )
    if result.returncode != 0:
        raise RuntimeError("could not query the primary tunnel task")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("primary tunnel task query returned no data")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("primary tunnel task query returned invalid JSON") from exc
    last_result = payload.get("LastTaskResult")
    return TaskStatus(
        state=str(payload.get("State", "Unknown")),
        last_task_result=int(last_result) if last_result is not None else None,
    )


def start_task(config: Config) -> None:
    result = run_powershell(
        config,
        "Start-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME",
    )
    if result.returncode != 0:
        raise RuntimeError("could not start the primary tunnel task")


def restart_task(config: Config) -> None:
    result = run_powershell(
        config,
        "$task=Get-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "if([string]$task.State -eq 'Disabled'){exit 23};"
        "if([string]$task.State -eq 'Running'){Stop-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME};"
        "$deadline=(Get-Date).AddSeconds(10);"
        "do{$task=Get-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME;"
        "if([string]$task.State -ne 'Running'){break};Start-Sleep -Milliseconds 250}while((Get-Date)-lt $deadline);"
        "Start-ScheduledTask -TaskPath $env:OPENAI_TUNNEL_TASK_PATH -TaskName $env:OPENAI_TUNNEL_TASK_NAME",
        timeout=25.0,
    )
    if result.returncode == 23:
        raise RuntimeError("primary tunnel task was disabled during restart")
    if result.returncode != 0:
        raise RuntimeError("could not restart the primary tunnel task")


def current_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def scan_deadline_drops(path: Path | None, after_time: str) -> tuple[str, int]:
    """Track response-deadline drops without rescanning an unbounded log."""
    if path is None or not path.is_file():
        return after_time, 0
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            start = max(0, size - 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return after_time, 0

    newest = after_time
    baseline_only = not after_time
    count = 0
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("msg") != DEADLINE_MESSAGE:
            continue
        event_time = str(event.get("time", ""))
        if not event_time:
            continue
        if event_time > newest:
            newest = event_time
        if not baseline_only and event_time > after_time:
            count += 1
    return newest, count


def probe_health(config: Config) -> HealthProbe:
    if not config.health_url_file.is_file() or not config.pid_file.is_file():
        return HealthProbe(LOCAL_UNHEALTHY, False, False, False, False)
    try:
        result = subprocess.run(
            [
                str(config.tunnel_client),
                "health",
                "--url-file",
                str(config.health_url_file),
                "--pid-file",
                str(config.pid_file),
                "--require-control-plane-poll",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.health_timeout_seconds,
            creationflags=creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return HealthProbe(LOCAL_UNHEALTHY, False, False, False, False)
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return HealthProbe(LOCAL_UNHEALTHY, False, False, False, False)
    process = payload.get("process") if isinstance(payload, dict) else None
    healthz = payload.get("healthz") if isinstance(payload, dict) else None
    readyz = payload.get("readyz") if isinstance(payload, dict) else None
    poll = payload.get("control_plane_poll") if isinstance(payload, dict) else None
    local_live = bool(
        isinstance(process, dict)
        and process.get("running") is True
        and isinstance(healthz, dict)
        and healthz.get("ok") is True
    )
    ready = bool(isinstance(readyz, dict) and readyz.get("ok") is True)
    control_plane_poll = bool(isinstance(poll, dict) and poll.get("ok") is True)
    if result.returncode == 0 and local_live and ready and control_plane_poll:
        return HealthProbe(HEALTHY, True, True, True, True)
    if local_live and not control_plane_poll:
        return HealthProbe(CONTROL_PLANE_DEGRADED, False, True, ready, False)
    return HealthProbe(LOCAL_UNHEALTHY, False, local_live, ready, control_plane_poll)


def wait_for_health(config: Config) -> HealthProbe:
    deadline = time.monotonic() + max(0.0, config.startup_grace_seconds)
    while True:
        probe = probe_health(config)
        if probe.ok:
            return probe
        if time.monotonic() >= deadline:
            return probe
        time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))


def record(
    config: Config,
    *,
    action: str,
    status: TaskStatus,
    failures: int,
    recovery_attempts: int,
    next_recovery_at: float,
    last_recovery_at: float,
    healthy_since_at: float,
    probe: HealthProbe | None,
    last_deadline_time: str,
    new_deadline_drops: int = 0,
) -> None:
    append_event(
        config.event_log,
        max_bytes=config.event_log_max_bytes,
        backups=config.event_log_backups,
        action=action,
        task_state=status.state,
        last_task_result=status.last_task_result,
        consecutive_failures=failures,
        healthy=probe.ok if probe is not None else None,
        health_kind=probe.kind if probe is not None else None,
        pid=current_pid(config.pid_file) if status.state == RUNNING else None,
        recovery_attempts=recovery_attempts,
        next_recovery_at=next_recovery_at or None,
        healthy_since_at=healthy_since_at or None,
        new_deadline_drops=new_deadline_drops,
    )
    save_state(
        config.state_file,
        consecutive_failures=failures,
        last_action=action,
        recovery_attempts=recovery_attempts,
        next_recovery_at=next_recovery_at,
        last_recovery_at=last_recovery_at,
        healthy_since_at=healthy_since_at,
        last_deadline_time=last_deadline_time,
    )


def recovery_delay(config: Config, attempt: int) -> float:
    index = min(max(1, attempt), len(config.recovery_backoff_seconds)) - 1
    return config.recovery_backoff_seconds[index]


def run_once(config: Config) -> int:
    if os.name != "nt":
        raise RuntimeError("the Secure MCP Tunnel watchdog requires Windows")
    for required in (config.tunnel_client, config.powershell):
        if not required.is_file():
            raise RuntimeError(f"required executable is missing: {required}")
    state = load_state(config.state_file)
    failures = int(state.get("consecutive_failures", 0))
    recovery_attempts = int(state.get("recovery_attempts", 0))
    next_recovery_at = float(state.get("next_recovery_at", 0.0))
    last_recovery_at = float(state.get("last_recovery_at", 0.0))
    healthy_since_at = float(state.get("healthy_since_at", 0.0))
    last_deadline_time = str(state.get("last_deadline_time", "") or "")
    status = task_status(config)
    probe = probe_health(config) if status.state == RUNNING else None
    new_deadline_drops = 0
    if status.state == RUNNING:
        last_deadline_time, new_deadline_drops = scan_deadline_drops(
            config.tunnel_log, last_deadline_time
        )
        if probe is not None and probe.kind == HEALTHY and new_deadline_drops:
            probe = HealthProbe(
                DEADLINE_DEGRADED,
                False,
                probe.local_live,
                probe.ready,
                probe.control_plane_poll,
            )
    decision = decide_action(
        status.state,
        probe.kind if probe is not None else None,
        failures,
        config.failure_threshold,
    )

    if decision.action in {"disabled", "wait", "healthy", "degraded", "control_plane_degraded"}:
        action = decision.action
        if decision.action == "disabled":
            recovery_attempts = 0
            next_recovery_at = 0.0
            last_recovery_at = 0.0
            healthy_since_at = 0.0
        elif decision.action == "healthy":
            now = time.time()
            if recovery_attempts == 0:
                next_recovery_at = 0.0
                last_recovery_at = 0.0
                healthy_since_at = 0.0
            else:
                if healthy_since_at <= 0.0:
                    healthy_since_at = now
                if now - healthy_since_at >= config.stable_health_seconds:
                    action = "healthy_stable"
                    recovery_attempts = 0
                    next_recovery_at = 0.0
                    last_recovery_at = 0.0
                    healthy_since_at = 0.0
        else:
            healthy_since_at = 0.0
        record(
            config,
            action=action,
            status=status,
            failures=decision.consecutive_failures,
            recovery_attempts=recovery_attempts,
            next_recovery_at=next_recovery_at,
            last_recovery_at=last_recovery_at,
            healthy_since_at=healthy_since_at,
            probe=probe,
            last_deadline_time=last_deadline_time,
            new_deadline_drops=new_deadline_drops,
        )
        return 0

    if decision.action in {"start", "restart"}:
        now = time.time()
        if decision.action == "restart" and now < next_recovery_at:
            record(
                config,
                action=f"{decision.action}_cooldown",
                status=status,
                failures=decision.consecutive_failures,
                recovery_attempts=recovery_attempts,
                next_recovery_at=next_recovery_at,
                last_recovery_at=last_recovery_at,
                healthy_since_at=0.0,
                probe=probe,
                last_deadline_time=last_deadline_time,
                new_deadline_drops=new_deadline_drops,
            )
            return 0

        if decision.action == "start":
            # A Ready task is not a live restart loop: tunnel-client has already
            # exited and the one-minute scheduler cadence is the throttle.  Do
            # not let backoff from an earlier unhealthy-running restart turn a
            # clean exit into a multi-minute outage.
            recovery_attempts = 0
            next_recovery_at = 0.0
            last_recovery_at = 0.0

        # Persist the recovery lease before touching Task Scheduler.  If the
        # process is killed mid-attempt, the next run still observes backoff.
        recovery_attempts += 1
        last_recovery_at = now
        next_recovery_at = now + recovery_delay(config, recovery_attempts)
        healthy_since_at = 0.0
        record(
            config,
            action=f"{decision.action}_attempt",
            status=status,
            failures=0,
            recovery_attempts=recovery_attempts,
            next_recovery_at=next_recovery_at,
            last_recovery_at=last_recovery_at,
            healthy_since_at=healthy_since_at,
            probe=probe,
            last_deadline_time=last_deadline_time,
            new_deadline_drops=new_deadline_drops,
        )

        if decision.action == "start":
            start_task(config)
        else:
            restart_task(config)
        recovered_probe = wait_for_health(config)
        refreshed = task_status(config)
        if recovered_probe.ok:
            # A process that becomes ready and immediately crashes is still a
            # failure loop.  Keep its backoff lease until it stays healthy for
            # the configured stability window.
            healthy_since_at = time.time()
        record(
            config,
            action=f"{decision.action}ed_healthy" if recovered_probe.ok else f"{decision.action}ed_unready",
            status=refreshed,
            failures=0,
            recovery_attempts=recovery_attempts,
            next_recovery_at=next_recovery_at,
            last_recovery_at=last_recovery_at,
            healthy_since_at=healthy_since_at,
            probe=recovered_probe,
            last_deadline_time=last_deadline_time,
            new_deadline_drops=new_deadline_drops,
        )
        return 0

    raise RuntimeError(f"unsupported watchdog action: {decision.action}")


def default_powershell() -> Path:
    resolved = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    return Path(resolved) if resolved else Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def parse_backoff(value: str) -> tuple[float, ...]:
    try:
        parsed = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("backoff values must be numbers") from exc
    if not parsed or any(item <= 0 for item in parsed):
        raise argparse.ArgumentTypeError("backoff values must be positive")
    return parsed


def normalize_task_path(value: str) -> str:
    normalized = value.strip().replace("/", "\\")
    if not normalized.startswith("\\"):
        normalized = "\\" + normalized
    if not normalized.endswith("\\"):
        normalized += "\\"
    if any(character in normalized for character in "*?[]"):
        raise argparse.ArgumentTypeError("task path must not contain wildcards")
    return normalized


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-task-name", required=True)
    parser.add_argument("--main-task-path", type=normalize_task_path, default="\\")
    parser.add_argument("--tunnel-client", type=Path, required=True)
    parser.add_argument("--health-url-file", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--tunnel-log", type=Path)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--powershell", type=Path, default=default_powershell())
    parser.add_argument("--failure-threshold", type=int, default=2)
    parser.add_argument("--startup-grace-seconds", type=float, default=45.0)
    parser.add_argument("--health-timeout-seconds", type=float, default=10.0)
    parser.add_argument(
        "--recovery-backoff-seconds",
        type=parse_backoff,
        default=DEFAULT_RECOVERY_BACKOFF_SECONDS,
        help="comma-separated delays; the final value is the retry cap",
    )
    parser.add_argument("--event-log-max-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--event-log-backups", type=int, default=3)
    parser.add_argument("--stable-health-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    if args.failure_threshold < 1:
        parser.error("--failure-threshold must be at least 1")
    return Config(
        main_task_name=args.main_task_name,
        tunnel_client=args.tunnel_client.resolve(),
        health_url_file=args.health_url_file.resolve(),
        pid_file=args.pid_file.resolve(),
        tunnel_log=args.tunnel_log.resolve() if args.tunnel_log is not None else None,
        state_file=args.state_file.resolve(),
        event_log=args.event_log.resolve(),
        powershell=args.powershell.resolve(),
        failure_threshold=args.failure_threshold,
        startup_grace_seconds=max(0.0, args.startup_grace_seconds),
        health_timeout_seconds=max(0.1, args.health_timeout_seconds),
        main_task_path=args.main_task_path,
        recovery_backoff_seconds=args.recovery_backoff_seconds,
        stable_health_seconds=max(0.0, args.stable_health_seconds),
        event_log_max_bytes=max(1024, args.event_log_max_bytes),
        event_log_backups=max(1, args.event_log_backups),
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        return run_once(config)
    except Exception as exc:  # noqa: BLE001 - scheduled task must leave durable evidence
        append_event(
            config.event_log,
            max_bytes=config.event_log_max_bytes,
            backups=config.event_log_backups,
            action="watchdog_error",
            error_type=type(exc).__name__,
        )
        if sys.stderr is not None:
            print(f"Secure MCP Tunnel watchdog failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
