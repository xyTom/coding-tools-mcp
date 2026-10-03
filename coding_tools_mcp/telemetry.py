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

A session is activated by the first request or notification that passes
envelope validation, whichever era it belongs to, and ``ping`` never activates
one: one runtime serves every client of a workspace, and a client that never
handshakes still uses the server. Importing this module, or exercising a
Runtime without dispatching anything through it, produces no traffic.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import platform
import string
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from . import __version__
from .breaker import REPEATED_CALL_BLOCKED
from .envutils import ENV_PREFIX, truthy_env, utc_now
from .protocol import DISCOVER_METHOD, MODERN_ERA

POSTHOG_ENDPOINT = "https://us.i.posthog.com/batch/"
# Public write-only ingest key: it can create events but never read them back.
POSTHOG_PROJECT_KEY = "phc_ySPcp83qCtwWyTECxpHpZsouvucHUQ5bhQnLUoYMPqdG"

ERROR_EVENTS_PER_SESSION = 20
FLUSH_INTERVAL_SECONDS = 30.0
FLUSH_BATCH_SIZE = 50
QUEUE_LIMIT = 500
SEND_TIMEOUT_SECONDS = 3.0

_LABEL_LIMIT = 64
_CLIENT_LABEL_LIMIT = 40
# clientInfo is whatever the client says it is, so it is narrowed to a
# printable ASCII subset before it can become an event property: anything else
# is either an injection into the log line or unbounded cardinality.
_CLIENT_LABEL_CHARS = frozenset(string.ascii_letters + string.digits + " ._-")
# A method name is ours to expect rather than the client's to invent, but it
# still arrives from the wire, so the log line is built from the characters a
# method name is made of and nothing else.
_METHOD_LABEL_CHARS = frozenset(string.ascii_letters + string.digits + "/._-")
_UNKNOWN_LABEL = "unknown"
_OFF_VALUES = {"0", "off", "false", "no", "disable", "disabled"}
_DURATION_BUCKETS = ((100, "dur_lt_100ms"), (1_000, "dur_lt_1s"), (10_000, "dur_lt_10s"))
_DURATION_OVERFLOW = "dur_gte_10s"
_RETENTION_COUNTERS = (
    "evict_events",
    "evicted_bytes_total",
    "read_output_omitted_hits",
    "poll_omitted_hits",
)
# Operation-level outcomes that mean the work failed even though the tool call
# itself completed. Counting these as successes is what let a dashboard report
# a failing build as a win.
FAILED_OPERATION_OUTCOMES = frozenset({"exited_nonzero", "timeout", "signal", "spawn_error"})
# A tool may pin one failure streak per error code; the cap stops a client
# cycling error codes from growing the map without bound.
MAX_TRACKED_FAILURE_STREAKS = 128
_LOG_PREFIX = "coding-tools-mcp"
_DISTRIBUTION_NAME = "coding-tools-mcp"
_PACKAGE_DIR = Path(__file__).resolve().parent


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


def _client_label(value: Any) -> str | None:
    """Normalize one self-reported ``clientInfo`` field into an event property.

    Unlike :func:`_label`, which shortens values this server produced itself,
    this drops every character outside a printable ASCII subset — control
    characters, newlines, and anything that would turn a name into free-form
    text — before truncating.
    """

    if value is None:
        return None
    text = "".join(character for character in str(value) if character in _CLIENT_LABEL_CHARS).strip()
    return text[:_CLIENT_LABEL_LIMIT] if text else None


def _method_label(value: Any) -> str:
    """Normalize a wire method name into something safe to log on one line."""

    text = "".join(character for character in str(value) if character in _METHOD_LABEL_CHARS).strip()
    return text[:_LABEL_LIMIT] if text else _UNKNOWN_LABEL


def _client_identity(client_info: Any) -> tuple[str | None, str | None]:
    """Read the sanitized ``name`` and ``version`` a client reported, if any.

    Only those two keys are read; a client may put anything else in the object
    and none of it reaches an event.
    """

    if not isinstance(client_info, Mapping):
        return None, None
    return _client_label(client_info.get("name")), _client_label(client_info.get("version"))


def _request_identity(context: Any) -> tuple[str | None, str | None]:
    """Name the client of one request, when the request itself named it.

    Only a modern request carries an identity, in the ``_meta`` the runtime
    handed on as an opaque context. A legacy request is left anonymous: it
    named itself in a handshake this runtime keeps no record of, and borrowing
    a name from some other client's handshake would attribute a failure to a
    client that never made the call.
    """

    if getattr(context, "era", None) != MODERN_ERA:
        return None, None
    return _client_identity(getattr(context, "client_info", None))


_first_seen_lock = threading.Lock()
_first_seen: set[str] = set()


def note_first_appearance(key: str, message: str) -> None:
    """Log one protocol choice the first time this process serves it.

    Written to stderr unconditionally — over stdio, stdout is the MCP wire —
    and never repeated, so an operator can tell from the log which era their
    clients actually speak without turning any tracing on.
    """

    with _first_seen_lock:
        if key in _first_seen:
            return
        _first_seen.add(key)
    print(f"{_LOG_PREFIX}: {message}", file=sys.stderr, flush=True)


def install_kind() -> str:
    """How this package is installed, as one of a closed set of labels.

    ``source`` (no installed distribution, or one whose files are not the code
    running), ``editable``, ``local`` (a non-editable install from a local
    path), ``vcs``, or ``index``; ``unknown`` if the metadata cannot be read.
    Only the label leaves this function — never the URL or path it came from.
    """

    try:
        from importlib import metadata

        try:
            distribution = metadata.distribution(_DISTRIBUTION_NAME)
        except metadata.PackageNotFoundError:
            return "source"
        raw = distribution.read_text("direct_url.json")
        info = json.loads(raw) if raw else None
        if isinstance(info, dict):
            dir_info = info.get("dir_info")
            if isinstance(dir_info, dict) and dir_info.get("editable") is True:
                return "editable"
        # A non-editable install copies the package; when the copy is not the
        # code actually running (a checkout on sys.path shadows it), report
        # the checkout rather than the install.
        installed = Path(str(distribution.locate_file("coding_tools_mcp/__init__.py"))).resolve()
        if installed.parent != _PACKAGE_DIR:
            return "source"
        if isinstance(info, dict):
            if "vcs_info" in info:
                return "vcs"
            url = info.get("url")
            if isinstance(url, str) and url.startswith("file:"):
                return "local"
        return "index"
    except Exception:  # noqa: BLE001 - a fingerprint must never break a session
        return "unknown"


_build_lock = threading.Lock()
_build_id: str | None = None


def build_id() -> str:
    """First 12 hex chars of a hash over this package's own ``.py`` sources.

    Each file contributes its path relative to the package directory, a NUL,
    and its bytes, in sorted path order. The official wheel of a release has
    one value; any modified copy has another. Computed once per process;
    ``unknown`` if the sources cannot be read.
    """

    global _build_id
    cached = _build_id
    if cached:
        return cached
    with _build_lock:
        if _build_id:
            return _build_id
        try:
            digest = hashlib.sha256()
            files = sorted((path.relative_to(_PACKAGE_DIR).as_posix(), path) for path in _PACKAGE_DIR.rglob("*.py"))
            if not files:
                raise FileNotFoundError("no package sources")
            for relative, path in files:
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
            value = digest.hexdigest()[:12]
        except Exception:  # noqa: BLE001 - a fingerprint must never break a session
            value = "unknown"
        _build_id = value
        return value


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
    """Per-runtime in-memory counters emitted as closed-schema events.

    ``record_tool_call`` only increments dictionary counters under its lock;
    event dictionaries are built after the lock is released. One runtime is
    shared by every client of its workspace, so a "session" is the runtime's
    lifetime rather than one client's: it is activated by the first request
    that reaches :meth:`record_request` (or by a handshake) and closed once,
    when the runtime is closed.
    """

    def __init__(self, *, permission_mode: str, transport: str = "stdio") -> None:
        """Initialize inactive session counters and fixed environment and build labels."""
        self._session_id = uuid.uuid4().hex
        self._started_monotonic = time.monotonic()
        self._base_properties: dict[str, Any] = {
            "version": __version__,
            "os": sys.platform,
            "arch": platform.machine(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "transport": _label(transport),
            "permission_mode": _label(permission_mode),
            "install": install_kind(),
            "build": build_id(),
            "session_id": self._session_id,
            "$process_person_profile": False,
        }
        self._tools: dict[str, dict[str, Any]] = {}
        self._legacy_requests = 0
        self._modern_requests = 0
        self._discover_probes = 0
        self._unknown_tool_calls = 0
        self._error_events_sent = 0
        self._errors_dropped = 0
        self._breaker_blocks = 0
        # Keyed by (tool, error_code) rather than held in one global slot: a
        # single slot reset by any success of any tool cannot tell a model
        # stuck in a loop from a model making progress alongside one failure.
        self._failure_streaks: dict[tuple[str, str], int] = {}
        self._active = False
        self._finished = False
        self._lock = threading.Lock()

    def record_request(self, era: str, method: str) -> None:
        """Count one envelope-valid request and activate on the first of them.

        Called before the method runs, so a first request that fails still
        reports its ``tool_error``. ``ping`` is counted but never activates: an
        HTTP health probe must not conjure a session out of an idle server.
        """

        with self._lock:
            if era == MODERN_ERA:
                self._modern_requests += 1
            else:
                self._legacy_requests += 1
            if method == DISCOVER_METHOD:
                self._discover_probes += 1
            activated = self._activate_locked() if method != "ping" else False
        if era == MODERN_ERA:
            note_first_appearance("modern-request", f"modern client request ({_method_label(method)})")
        if method == DISCOVER_METHOD:
            note_first_appearance("discover-probe", f"{DISCOVER_METHOD} probe")
        # ``_event`` reads (and, once per install, writes) the install id, so
        # it must not be built at all while telemetry is off.
        if activated and telemetry_mode() != "off":
            self._emit([self._event("session_start", {})], wake=True)

    def record_session_start(self, client_info: dict[str, Any] | None, protocol_version: str) -> None:
        """Record one legacy handshake, activating the session if it is the first.

        Every ``initialize`` emits its own ``handshake`` event — a connector
        that probes, falls back, and handshakes again produces several — while
        ``session_start`` is emitted at most once. The client identity belongs
        to the handshake rather than to the session: the next request may come
        from an entirely different client.
        """

        with self._lock:
            activated = self._activate_locked()
        note_first_appearance("legacy-handshake", f"legacy client handshake ({protocol_version})")
        if telemetry_mode() == "off":
            return
        events = [self._event("session_start", {})] if activated else []
        client_name, client_version = _client_identity(client_info)
        events.append(
            self._event(
                "handshake",
                {
                    "protocol_version": _label(protocol_version),
                    "client_name": client_name,
                    "client_version": client_version,
                },
            )
        )
        self._emit(events, wake=True)

    def _activate_locked(self) -> bool:
        if self._active or self._finished:
            return False
        self._active = True
        return True

    def record_tool_call(
        self,
        tool: str,
        *,
        ok: bool,
        error_code: str | None,
        duration_ms: int,
        truncated: bool,
        context: Any = None,
        outcome: str | None = None,
        already_applied: bool = False,
    ) -> None:
        """Count one tool call.

        ``ok`` is transport-level: the call was dispatched and returned.
        ``outcome`` is operation-level, set by tools that run something whose
        result is not the call's result — a command that exits non-zero is a
        successful call and a failed operation, and only the operation is what
        a failure rate should be measured against. ``already_applied`` marks a
        successful write call that found its change already in place and wrote
        nothing.

        A ``REPEATED_CALL_BLOCKED`` refusal is neither: the breaker answered
        before any handler ran. It is counted only as a breaker block, so a
        client that ignores the refusal and resends the same call thousands of
        times measures how persistent that client is, not how often a tool
        fails.
        """

        emit_error: tuple[str, int] | None = None
        operation_failed = outcome in FAILED_OPERATION_OUTCOMES
        with self._lock:
            stats = self._tools.get(tool)
            if stats is None:
                stats = self._tools[tool] = {
                    "calls": 0,
                    "errors": {},
                    "buckets": {},
                    "truncated": 0,
                    "outcomes": {},
                    "operation_failures": 0,
                    "breaker_blocks": 0,
                    "already_applied": 0,
                }
            if not ok and error_code == REPEATED_CALL_BLOCKED:
                # Not a call the tool handled, so it touches no call, error,
                # duration, or streak counter and spends no tool_error budget.
                stats["breaker_blocks"] += 1
                self._breaker_blocks += 1
                return
            stats["calls"] += 1
            bucket = _DURATION_OVERFLOW
            for limit, name in _DURATION_BUCKETS:
                if duration_ms < limit:
                    bucket = name
                    break
            stats["buckets"][bucket] = stats["buckets"].get(bucket, 0) + 1
            if truncated:
                stats["truncated"] += 1
            if ok and already_applied:
                stats["already_applied"] += 1
            if outcome:
                label = _label(outcome) or "unknown"
                stats["outcomes"][label] = stats["outcomes"].get(label, 0) + 1
            if ok and operation_failed:
                # Operation failures are successful tool dispatches whose
                # underlying command failed. A spawn_error is itself a tool
                # error, so counting its outcome again would make one call
                # contribute two failures and could drive `ok` negative.
                stats["operation_failures"] += 1
            if ok and not operation_failed:
                self._clear_streaks_locked(tool)
            elif ok:
                # A failed operation neither clears a streak nor is a protocol
                # error, so it is counted as an outcome and nothing else.
                pass
            else:
                code = _label(error_code) or "UNKNOWN"
                stats["errors"][code] = stats["errors"].get(code, 0) + 1
                streak = self._failure_streaks.get((tool, code), 0) + 1
                self._record_streak_locked(tool, code, streak)
                if self._active:
                    if self._error_events_sent < ERROR_EVENTS_PER_SESSION:
                        self._error_events_sent += 1
                        emit_error = (code, streak)
                    else:
                        self._errors_dropped += 1
        if emit_error is not None and telemetry_mode() != "off":
            client_name, client_version = _request_identity(context)
            self._emit(
                [
                    self._event(
                        "tool_error",
                        {
                            "tool": _label(tool),
                            "error_code": emit_error[0],
                            "duration_ms": duration_ms,
                            "consecutive_failures": emit_error[1],
                            "client_name": client_name,
                            "client_version": client_version,
                        },
                    )
                ]
            )

    def record_unknown_tool_call(self) -> None:
        """Count one tools/call naming a tool this server does not have.

        The name is not kept: it is whatever the client sent, and per-tool
        statistics keyed by it would let any client mint new properties.
        """

        with self._lock:
            self._unknown_tool_calls += 1

    def record_deferred_operation_outcome(self, tool: str, outcome: str) -> None:
        """Attach a later-observed terminal outcome to its original tool call.

        Background commands can still be running when ``exec_command`` returns,
        so a polling tool may be the first call to observe the terminal result.
        The result belongs to the already-recorded ``exec_command`` call; this
        updates that tool's operation counters without adding another call.
        """

        operation_failed = outcome in FAILED_OPERATION_OUTCOMES
        with self._lock:
            stats = self._tools.get(tool)
            if stats is None:
                # The owning call belongs to another runtime/session. Avoid a
                # summary with a failure but no corresponding call.
                return
            label = _label(outcome) or "unknown"
            stats["outcomes"][label] = stats["outcomes"].get(label, 0) + 1
            if operation_failed:
                stats["operation_failures"] += 1
            else:
                self._clear_streaks_locked(tool)

    def _clear_streaks_locked(self, tool: str) -> None:
        for key in [key for key in self._failure_streaks if key[0] == tool]:
            del self._failure_streaks[key]

    def _record_streak_locked(self, tool: str, code: str, streak: int) -> None:
        self._failure_streaks[(tool, code)] = streak
        while len(self._failure_streaks) > MAX_TRACKED_FAILURE_STREAKS:
            self._failure_streaks.pop(next(iter(self._failure_streaks)))

    def consecutive_failures(self, tool: str, error_code: str) -> int:
        """Current streak for one (tool, error_code) pair; 0 when unbroken."""

        with self._lock:
            return self._failure_streaks.get((tool, _label(error_code) or "UNKNOWN"), 0)

    def finish(self, *, output_retention: Mapping[str, int] | None = None) -> None:
        """Emit tool summaries and session totals once, if telemetry is active and enabled."""
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
                outcomes: dict[str, int] = stats.get("outcomes", {})
                operation_failures = int(stats.get("operation_failures", 0))
                properties: dict[str, Any] = {
                    "tool": _label(tool),
                    "calls": stats["calls"],
                    # Operation-level: a call that ran a command which then
                    # failed is not counted here.
                    "ok": stats["calls"] - failures - operation_failures,
                    "errors": failures,
                    "operation_failures": operation_failures,
                    "breaker_blocks": int(stats.get("breaker_blocks", 0)),
                    "truncated": stats["truncated"],
                    "already_applied": int(stats.get("already_applied", 0)),
                }
                for code, count in sorted(stats["errors"].items()):
                    properties[f"err_{code}"] = count
                for name, count in sorted(outcomes.items()):
                    properties[f"outcome_{name}"] = count
                properties.update(stats["buckets"])
                events.append(self._event("tool_summary", properties))
            end_properties: dict[str, Any] = {
                "duration_ms": duration_ms,
                "tool_calls": sum(stats["calls"] for stats in self._tools.values()),
                "distinct_tools": len(self._tools),
                "errors_dropped": self._errors_dropped,
                "breaker_blocks": self._breaker_blocks,
                "legacy_requests": self._legacy_requests,
                "modern_requests": self._modern_requests,
                "discover_probes": self._discover_probes,
                "unknown_tool_calls": self._unknown_tool_calls,
            }
            for counter in _RETENTION_COUNTERS:
                value = output_retention.get(counter, 0) if output_retention else 0
                end_properties[counter] = int(value)
            events.append(self._event("session_end", end_properties))
        self._emit(events, wake=True)

    def _event(self, name: str, properties: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = dict(self._base_properties)
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
