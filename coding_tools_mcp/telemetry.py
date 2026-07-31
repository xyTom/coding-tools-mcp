"""Anonymous product telemetry: counters and enums sent to PostHog over HTTPS.

The wire payload is a closed schema built exclusively from tool names, error
codes, durations, counts, and version/platform strings. It is structurally
incapable of carrying file paths, tool arguments, command lines, or file
contents. The exact event list is documented in docs/telemetry.md.

Controls, evaluated on every send decision:

- ``CODING_TOOLS_MCP_TELEMETRY=off`` (or ``0``/``false``/``no``) disables sending.
- ``DO_NOT_TRACK=1`` disables sending.
- ``CI`` set truthy disables sending so CI runs never pollute usage data.
- ``CODING_TOOLS_MCP_TELEMETRY=debug`` prints events to stderr instead of sending.

Sending never blocks a tool call: ``record_tool_call`` only increments in-memory
counters under its lock, events queue on a bounded queue serviced by a daemon
thread, failures are swallowed, and overflow is dropped. Nothing
telemetry-related is written to disk except one random install id under
``~/.coding-tools-mcp/id``, used only to de-duplicate active-user counts;
deleting that file resets the identity.

Events are emitted only for sessions that completed a real MCP ``initialize``
handshake, so importing this module or exercising a Runtime directly (as unit
tests do) produces no traffic.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from . import __version__
from .envutils import ENV_PREFIX, truthy_env, utc_now

POSTHOG_ENDPOINT = "https://us.i.posthog.com/batch/"
# Public write-only ingest key: it can create events but never read them back.
POSTHOG_PROJECT_KEY = "phc_ySPcp83qCtwWyTECxpHpZsouvucHUQ5bhQnLUoYMPqdG"

ERROR_EVENTS_PER_SESSION = 20
FLUSH_INTERVAL_SECONDS = 30.0
FLUSH_BATCH_SIZE = 50
QUEUE_LIMIT = 500
SEND_TIMEOUT_SECONDS = 3.0

_LABEL_LIMIT = 64
_OFF_VALUES = {"0", "off", "false", "no", "disable", "disabled"}
_DURATION_BUCKETS = ((100, "dur_lt_100ms"), (1_000, "dur_lt_1s"), (10_000, "dur_lt_10s"))
_DURATION_OVERFLOW = "dur_gte_10s"


def telemetry_mode() -> str:
    """Return ``"on"``, ``"off"``, or ``"debug"`` from the environment."""

    raw = (os.environ.get(f"{ENV_PREFIX}_TELEMETRY") or "").strip().lower()
    if raw == "debug":
        return "debug"
    if raw in _OFF_VALUES:
        return "off"
    if truthy_env(os.environ.get("DO_NOT_TRACK")) or truthy_env(os.environ.get("CI")):
        return "off"
    return "on"


def _label(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:_LABEL_LIMIT] if text else None


def _looks_like_install_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


_install_id_lock = threading.Lock()
_install_id: str | None = None


def install_id() -> str:
    """A random per-install id, never derived from hardware, hostname, or paths.

    Persisted so active-user counts de-duplicate across sessions; falls back to
    a per-process id when the home directory is unwritable.
    """

    global _install_id
    cached = _install_id
    if cached:
        return cached
    with _install_id_lock:
        if _install_id:
            return _install_id
        path = Path.home() / ".coding-tools-mcp" / "id"
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if not _looks_like_install_id(value):
            value = uuid.uuid4().hex
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text(value + "\n", encoding="utf-8")
                path.chmod(0o600)
            except OSError:
                pass
        _install_id = value
        return value


def _post(events: list[dict[str, Any]]) -> None:
    """Deliver one batch; prints in debug mode, never raises, never uses stdout."""

    if not events:
        return
    mode = telemetry_mode()
    if mode == "off":
        return
    if mode == "debug":
        for event in events:
            print(
                f"telemetry (not sent): {json.dumps(event, sort_keys=True, separators=(',', ':'))}",
                file=sys.stderr,
                flush=True,
            )
        return
    try:
        request = Request(
            POSTHOG_ENDPOINT,
            data=json.dumps({"api_key": POSTHOG_PROJECT_KEY, "batch": events}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "coding-tools-mcp-telemetry"},
        )
        with urlopen(request, timeout=SEND_TIMEOUT_SECONDS):
            pass
    except Exception:  # noqa: BLE001 - telemetry must never surface failures
        pass


class _Sender:
    """Bounded background queue that posts event batches off the caller's thread."""

    def __init__(self) -> None:
        self._wake = threading.Condition(threading.Lock())
        self._queue: list[dict[str, Any]] = []
        threading.Thread(target=self._run, name="coding-tools-mcp-telemetry", daemon=True).start()

    def enqueue(self, events: list[dict[str, Any]], *, wake: bool = False) -> None:
        if not events:
            return
        with self._wake:
            room = QUEUE_LIMIT - len(self._queue)
            if room > 0:
                self._queue.extend(events[:room])
            if wake or len(self._queue) >= FLUSH_BATCH_SIZE:
                self._wake.notify_all()

    def flush(self) -> None:
        with self._wake:
            batch, self._queue = self._queue, []
        _post(batch)

    def _run(self) -> None:
        while True:
            with self._wake:
                self._wake.wait(timeout=FLUSH_INTERVAL_SECONDS)
                batch, self._queue = self._queue, []
            _post(batch)


_sender_lock = threading.Lock()
_sender: _Sender | None = None


def _get_sender() -> _Sender:
    global _sender
    with _sender_lock:
        if _sender is None:
            _sender = _Sender()
            atexit.register(_sender.flush)
        return _sender


class SessionTelemetry:
    """Per-session in-memory counters emitted as closed-schema events.

    ``record_tool_call`` only increments dictionary counters under its lock;
    event dictionaries are built after the lock is released. Events exist only
    for sessions activated by a real MCP ``initialize``
    (``record_session_start``).
    """

    def __init__(self, *, permission_mode: str, transport: str = "stdio") -> None:
        self._session_id = uuid.uuid4().hex
        self._started_monotonic = time.monotonic()
        self._base_properties: dict[str, Any] = {
            "version": __version__,
            "os": sys.platform,
            "arch": platform.machine(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "transport": _label(transport),
            "permission_mode": _label(permission_mode),
            "session_id": self._session_id,
            "$process_person_profile": False,
        }
        self._client_name: str | None = None
        self._client_version: str | None = None
        self._protocol_version: str | None = None
        self._tools: dict[str, dict[str, Any]] = {}
        self._error_events_sent = 0
        self._errors_dropped = 0
        self._failure_streak: tuple[str, int] | None = None
        self._active = False
        self._finished = False
        self._lock = threading.Lock()

    def record_session_start(self, client_info: dict[str, Any] | None, protocol_version: str) -> None:
        with self._lock:
            if self._active or self._finished:
                return
            self._active = True
            if client_info:
                self._client_name = _label(client_info.get("name"))
                self._client_version = _label(client_info.get("version"))
            self._protocol_version = _label(protocol_version)
        if telemetry_mode() != "off":
            self._emit([self._event("session_start", {})], wake=True)

    def record_tool_call(
        self, tool: str, *, ok: bool, error_code: str | None, duration_ms: int, truncated: bool
    ) -> None:
        emit_error: tuple[str, int] | None = None
        with self._lock:
            stats = self._tools.get(tool)
            if stats is None:
                stats = self._tools[tool] = {"calls": 0, "errors": {}, "buckets": {}, "truncated": 0}
            stats["calls"] += 1
            bucket = _DURATION_OVERFLOW
            for limit, name in _DURATION_BUCKETS:
                if duration_ms < limit:
                    bucket = name
                    break
            stats["buckets"][bucket] = stats["buckets"].get(bucket, 0) + 1
            if truncated:
                stats["truncated"] += 1
            if ok:
                self._failure_streak = None
            else:
                code = _label(error_code) or "UNKNOWN"
                stats["errors"][code] = stats["errors"].get(code, 0) + 1
                streak = 1
                if self._failure_streak and self._failure_streak[0] == tool:
                    streak = self._failure_streak[1] + 1
                self._failure_streak = (tool, streak)
                if self._active:
                    if self._error_events_sent < ERROR_EVENTS_PER_SESSION:
                        self._error_events_sent += 1
                        emit_error = (code, streak)
                    else:
                        self._errors_dropped += 1
        if emit_error is not None and telemetry_mode() != "off":
            self._emit(
                [
                    self._event(
                        "tool_error",
                        {
                            "tool": _label(tool),
                            "error_code": emit_error[0],
                            "duration_ms": duration_ms,
                            "consecutive_failures": emit_error[1],
                        },
                    )
                ]
            )

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if not self._active or telemetry_mode() == "off":
                return
            duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)
            events = []
            for tool, stats in sorted(self._tools.items()):
                failures = sum(stats["errors"].values())
                properties: dict[str, Any] = {
                    "tool": _label(tool),
                    "calls": stats["calls"],
                    "ok": stats["calls"] - failures,
                    "errors": failures,
                    "truncated": stats["truncated"],
                }
                for code, count in sorted(stats["errors"].items()):
                    properties[f"err_{code}"] = count
                properties.update(stats["buckets"])
                events.append(self._event("tool_summary", properties))
            events.append(
                self._event(
                    "session_end",
                    {
                        "duration_ms": duration_ms,
                        "tool_calls": sum(stats["calls"] for stats in self._tools.values()),
                        "distinct_tools": len(self._tools),
                        "errors_dropped": self._errors_dropped,
                    },
                )
            )
        self._emit(events, wake=True)

    def _event(self, name: str, properties: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {
            **self._base_properties,
            "client_name": self._client_name,
            "client_version": self._client_version,
            "protocol_version": self._protocol_version,
        }
        merged.update(properties)
        return {
            "event": name,
            "distinct_id": install_id(),
            "timestamp": utc_now(),
            "properties": merged,
        }

    def _emit(self, events: list[dict[str, Any]], *, wake: bool = False) -> None:
        if telemetry_mode() == "off":
            return
        _get_sender().enqueue(events, wake=wake)
