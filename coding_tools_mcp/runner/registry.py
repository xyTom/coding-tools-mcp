"""Runner connection registry for the control-plane side."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock

from .protocol import RunnerHello, RunnerHeartbeat


class RunnerConnectionError(ValueError):
    pass


class DuplicateRunnerInstanceError(RunnerConnectionError):
    pass


@dataclass(frozen=True)
class RunnerSnapshot:
    runner_id: str
    instance_id: str
    capabilities: tuple[str, ...]
    workspace_ids: tuple[str, ...]
    credential_fingerprint: str
    connected: bool
    last_seen: str
    heartbeat_sequence: int


class RunnerRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._runners: dict[str, RunnerSnapshot] = {}

    def enroll(self, hello: RunnerHello, credential_fingerprint: str) -> RunnerSnapshot:
        with self._lock:
            current = self._runners.get(hello.runner_id)
            if current and current.connected and current.instance_id != hello.instance_id:
                raise DuplicateRunnerInstanceError("runner already has another active instance")
            snapshot = RunnerSnapshot(
                runner_id=hello.runner_id,
                instance_id=hello.instance_id,
                capabilities=hello.capabilities,
                workspace_ids=tuple(item.workspace_id for item in hello.workspaces),
                credential_fingerprint=credential_fingerprint,
                connected=True,
                last_seen=_now(),
                heartbeat_sequence=0,
            )
            self._runners[hello.runner_id] = snapshot
            return snapshot

    def heartbeat(self, heartbeat: RunnerHeartbeat) -> RunnerSnapshot:
        with self._lock:
            current = self._runners.get(heartbeat.runner_id)
            if current is None or current.instance_id != heartbeat.instance_id:
                raise RunnerConnectionError("runner instance is not enrolled")
            if heartbeat.sequence <= current.heartbeat_sequence:
                raise RunnerConnectionError("heartbeat sequence must advance monotonically")
            snapshot = replace(
                current,
                connected=True,
                last_seen=_now(),
                heartbeat_sequence=heartbeat.sequence,
                workspace_ids=(
                    tuple(item.workspace_id for item in heartbeat.workspaces)
                    if heartbeat.workspaces is not None
                    else current.workspace_ids
                ),
            )
            self._runners[heartbeat.runner_id] = snapshot
            return snapshot

    def disconnect(self, runner_id: str, instance_id: str) -> RunnerSnapshot:
        with self._lock:
            current = self._runners.get(runner_id)
            if current is None or current.instance_id != instance_id:
                raise RunnerConnectionError("runner instance is not enrolled")
            snapshot = replace(current, connected=False, last_seen=_now())
            self._runners[runner_id] = snapshot
            return snapshot

    def get(self, runner_id: str) -> RunnerSnapshot | None:
        with self._lock:
            return self._runners.get(runner_id)

    def list(self) -> tuple[RunnerSnapshot, ...]:
        with self._lock:
            return tuple(self._runners.values())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
