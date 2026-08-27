#!/usr/bin/env python3
"""Capture short-lived Windows UDP ephemeral-port exhaustion bursts.

The process is intentionally cheap while healthy: it samples UDP endpoints,
keeps a short in-memory ring, and overwrites one status file.  It writes a
durable capture only when usage crosses a threshold or a new Tcpip 4266 event
appears.  This preserves the evidence that normally disappears before an
operator can inspect the machine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


DEFAULT_DYNAMIC_START = 49152
DEFAULT_DYNAMIC_COUNT = 16384


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Thresholds:
    warning: int = 4000
    critical: int = 10000


def classify(total_dynamic_udp: int, thresholds: Thresholds) -> str:
    if total_dynamic_udp >= thresholds.critical:
        return "critical"
    if total_dynamic_udp >= thresholds.warning:
        return "warning"
    return "healthy"


def process_name(pid: int | None) -> str:
    if not pid:
        return "unknown"
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):
        return "unknown"


def sample_udp(dynamic_start: int, dynamic_count: int) -> dict[str, Any]:
    dynamic_end = dynamic_start + dynamic_count - 1
    by_pid: Counter[int] = Counter()
    total = 0
    all_udp = 0
    try:
        connections = psutil.net_connections(kind="udp")
    except (psutil.Error, OSError):
        connections = []
    for connection in connections:
        all_udp += 1
        laddr = connection.laddr
        port = getattr(laddr, "port", None)
        if port is None and isinstance(laddr, tuple) and len(laddr) >= 2:
            port = laddr[1]
        if not isinstance(port, int) or not (dynamic_start <= port <= dynamic_end):
            continue
        total += 1
        if connection.pid:
            by_pid[int(connection.pid)] += 1
    top = [
        {"pid": pid, "name": process_name(pid), "count": count}
        for pid, count in by_pid.most_common(15)
    ]
    return {
        "time": utc_now(),
        "dynamic_udp": total,
        "all_udp": all_udp,
        "top_processes": top,
    }


def latest_4266_timestamp() -> float:
    """Return the newest Tcpip 4266 event timestamp, or zero when unavailable."""
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$e=Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Tcpip';Id=4266} "
            "-MaxEvents 1 -ErrorAction SilentlyContinue;"
            "if($e){[DateTimeOffset]$e.TimeCreated|ForEach-Object{$_.ToUnixTimeSeconds()}}"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            check=False,
        )
        return float(completed.stdout.strip() or 0.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def write_capture(directory: Path, reason: str, history: list[dict[str, Any]], event_ts: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"udp-port-capture-{stamp}.json"
    payload = {
        "captured_at": utc_now(),
        "reason": reason,
        "event_4266_timestamp": event_ts or None,
        "history": history,
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    thresholds = Thresholds(args.warning_threshold, args.critical_threshold)
    samples = max(2, int(args.history_seconds / args.interval_seconds))
    history: deque[dict[str, Any]] = deque(maxlen=samples)
    last_event = latest_4266_timestamp()
    last_capture_at = 0.0
    event_check_every = max(1, int(30 / args.interval_seconds))
    iteration = 0

    while True:
        sample = sample_udp(args.dynamic_start, args.dynamic_count)
        state = classify(int(sample["dynamic_udp"]), thresholds)
        sample["state"] = state
        history.append(sample)
        iteration += 1

        event_ts = last_event
        event_trigger = False
        if iteration % event_check_every == 0:
            event_ts = latest_4266_timestamp()
            event_trigger = event_ts > last_event
            last_event = max(last_event, event_ts)

        now = time.time()
        threshold_trigger = state in {"warning", "critical"}
        capture_allowed = now - last_capture_at >= args.capture_cooldown_seconds
        if capture_allowed and (event_trigger or threshold_trigger):
            reason = "tcpip_4266" if event_trigger else f"udp_{state}"
            capture = write_capture(args.capture_dir, reason, list(history), event_ts)
            last_capture_at = now
            sample["last_capture"] = str(capture)

        atomic_json(
            args.status_file,
            {
                "updated_at": utc_now(),
                "state": state,
                "dynamic_start": args.dynamic_start,
                "dynamic_count": args.dynamic_count,
                "warning_threshold": thresholds.warning,
                "critical_threshold": thresholds.critical,
                "latest": sample,
                "last_4266_timestamp": last_event or None,
            },
        )
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--history-seconds", type=float, default=120.0)
    parser.add_argument("--capture-cooldown-seconds", type=float, default=60.0)
    parser.add_argument("--warning-threshold", type=int, default=4000)
    parser.add_argument("--critical-threshold", type=int, default=10000)
    parser.add_argument("--dynamic-start", type=int, default=DEFAULT_DYNAMIC_START)
    parser.add_argument("--dynamic-count", type=int, default=DEFAULT_DYNAMIC_COUNT)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval_seconds <= 0 or args.history_seconds <= 0:
        parser.error("sampling intervals must be positive")
    if not 0 < args.warning_threshold < args.critical_threshold:
        parser.error("thresholds must satisfy 0 < warning < critical")
    return args


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
