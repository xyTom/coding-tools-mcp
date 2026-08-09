"""Bounded job reconciliation primitives for reconnecting runners."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any

from .protocol import RunnerProtocolError, validate_identifier


class JobAccessError(ValueError):
    pass


MAX_RUNNER_JOBS = 1024
MAX_JOB_TIMESTAMP_LENGTH = 128


class JobState(str, Enum):
    RUNNING = "running"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    runner_id: str
    workspace_id: str
    owner_principal_id: str
    state: JobState = JobState.RUNNING
    process_fingerprint: str | None = None
    runner_instance_id: str | None = None
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    started_at: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.job_id, "job_id")
        validate_identifier(self.runner_id, "runner_id")
        validate_identifier(self.workspace_id, "workspace_id")
        if not isinstance(self.owner_principal_id, str) or not self.owner_principal_id:
            raise JobAccessError("job owner principal id is required")
        if len(self.owner_principal_id) > 256:
            raise JobAccessError("job owner principal id is too long")
        if self.process_fingerprint is not None:
            _validate_process_fingerprint(self.process_fingerprint)
        if self.runner_instance_id is not None:
            validate_identifier(self.runner_instance_id, "runner_instance_id")
        _validate_cursors(self.stdout_cursor, self.stderr_cursor)
        _validate_started_at(self.started_at)


@dataclass(frozen=True)
class JobInventoryItem:
    job_id: str
    workspace_id: str
    process_fingerprint: str
    state: JobState
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    started_at: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.job_id, "job_id")
        validate_identifier(self.workspace_id, "workspace_id")
        _validate_process_fingerprint(self.process_fingerprint)
        _validate_cursors(self.stdout_cursor, self.stderr_cursor)
        _validate_started_at(self.started_at)

    @classmethod
    def from_payload(cls, payload: Any) -> "JobInventoryItem":
        if not isinstance(payload, dict):
            raise RunnerProtocolError("job inventory item must be an object")
        try:
            state = JobState(payload.get("state"))
        except ValueError as exc:
            raise RunnerProtocolError("job inventory state is unsupported") from exc
        return cls(
            job_id=payload.get("job_id"),
            workspace_id=payload.get("workspace_id"),
            process_fingerprint=payload.get("process_fingerprint"),
            state=state,
            stdout_cursor=payload.get("stdout_cursor", 0),
            stderr_cursor=payload.get("stderr_cursor", 0),
            started_at=payload.get("started_at"),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "process_fingerprint": self.process_fingerprint,
            "state": self.state.value,
            "stdout_cursor": self.stdout_cursor,
            "stderr_cursor": self.stderr_cursor,
            "started_at": self.started_at,
        }


class RunnerJobInventoryRegistry:
    """Bounded Runner-local source of reconnect job inventory facts."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._items: dict[str, JobInventoryItem] = {}

    def upsert(self, item: JobInventoryItem) -> None:
        if not isinstance(item, JobInventoryItem):
            raise JobAccessError("runner job inventory item is invalid")
        with self._lock:
            if item.job_id not in self._items and len(self._items) >= MAX_RUNNER_JOBS:
                raise JobAccessError("runner job inventory registry is full")
            self._items[item.job_id] = item

    def remove(self, job_id: str) -> bool:
        validate_identifier(job_id, "job_id")
        with self._lock:
            return self._items.pop(job_id, None) is not None

    def snapshot(self) -> tuple[JobInventoryItem, ...]:
        with self._lock:
            return tuple(self._items[job_id] for job_id in sorted(self._items))


@dataclass(frozen=True)
class JobReconcileResult:
    recovered: tuple[str, ...]
    completed: tuple[str, ...]
    lost: tuple[str, ...]
    failed: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()


class RunnerJobReconciler:
    """Reconciles runner inventory without pretending processes migrate."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, JobRecord] = {}

    def register(self, job: JobRecord) -> None:
        with self._lock:
            if len(self._jobs) >= MAX_RUNNER_JOBS:
                raise JobAccessError("runner job registry is full")
            if job.job_id in self._jobs:
                raise JobAccessError("job is already registered")
            self._jobs[job.job_id] = job

    def mark_runner_disconnected(self, runner_id: str, runner_instance_id: str | None = None) -> None:
        with self._lock:
            for job_id, job in self._jobs.items():
                if job.runner_id != runner_id or job.state != JobState.RUNNING:
                    continue
                if (
                    runner_instance_id is not None
                    and job.runner_instance_id is not None
                    and job.runner_instance_id != runner_instance_id
                ):
                    continue
                self._jobs[job_id] = self._replace(job, state=JobState.RECOVERING)

    def reconcile(
        self,
        runner_id: str,
        inventory: list[JobInventoryItem],
        *,
        runner_instance_id: str | None = None,
    ) -> JobReconcileResult:
        if len(inventory) > MAX_RUNNER_JOBS:
            raise JobAccessError("runner job inventory exceeds the bounded limit")
        if runner_instance_id is not None:
            validate_identifier(runner_instance_id, "runner_instance_id")
        with self._lock:
            inventory_ids = [item.job_id for item in inventory]
            if len(set(inventory_ids)) != len(inventory_ids):
                raise JobAccessError("runner job inventory contains duplicate job ids")
            known = {job_id: job for job_id, job in self._jobs.items() if job.runner_id == runner_id}
            remote = {item.job_id: item for item in inventory}
            recovered: list[str] = []
            completed: list[str] = []
            lost: list[str] = []
            failed: list[str] = []
            cancelled: list[str] = []
            for job_id, job in known.items():
                if job.state in {
                    JobState.COMPLETED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.LOST,
                }:
                    continue
                item = remote.get(job_id)
                if item is None:
                    self._mark_lost(job)
                    lost.append(job_id)
                    continue
                if item.workspace_id != job.workspace_id:
                    self._mark_lost(job)
                    lost.append(job_id)
                    continue
                if not self._same_process(job, item, runner_instance_id):
                    self._mark_lost(job)
                    lost.append(job_id)
                    continue
                if item.stdout_cursor < job.stdout_cursor or item.stderr_cursor < job.stderr_cursor:
                    self._mark_lost(job)
                    lost.append(job_id)
                    continue
                if item.state == JobState.COMPLETED:
                    self._jobs[job_id] = self._replace(
                        job,
                        state=JobState.COMPLETED,
                        item=item,
                        runner_instance_id=runner_instance_id,
                    )
                    completed.append(job_id)
                elif item.state == JobState.FAILED:
                    self._jobs[job_id] = self._replace(
                        job,
                        state=JobState.FAILED,
                        item=item,
                        runner_instance_id=runner_instance_id,
                    )
                    failed.append(job_id)
                elif item.state == JobState.CANCELLED:
                    self._jobs[job_id] = self._replace(
                        job,
                        state=JobState.CANCELLED,
                        item=item,
                        runner_instance_id=runner_instance_id,
                    )
                    cancelled.append(job_id)
                elif item.state == JobState.LOST:
                    self._mark_lost(job)
                    lost.append(job_id)
                elif item.state == JobState.RECOVERING:
                    self._mark_lost(job)
                    lost.append(job_id)
                else:
                    was_recovering = job.state == JobState.RECOVERING
                    self._jobs[job_id] = self._replace(
                        job,
                        state=JobState.RUNNING,
                        item=item,
                        runner_instance_id=runner_instance_id,
                    )
                    if was_recovering:
                        recovered.append(job_id)
            return JobReconcileResult(
                tuple(recovered),
                tuple(completed),
                tuple(lost),
                tuple(failed),
                tuple(cancelled),
            )

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            if job_id not in self._jobs:
                raise JobAccessError("unknown job")
            return self._jobs[job_id]

    def get_for_owner(self, job_id: str, owner_principal_id: str, workspace_id: str) -> JobRecord:
        job = self.get(job_id)
        if job.owner_principal_id != owner_principal_id or job.workspace_id != workspace_id:
            raise JobAccessError("job is not available to this principal or workspace")
        return job

    def list_for_owner(self, owner_principal_id: str, workspace_id: str) -> tuple[JobRecord, ...]:
        with self._lock:
            return tuple(
                job
                for job in self._jobs.values()
                if job.owner_principal_id == owner_principal_id and job.workspace_id == workspace_id
            )

    def observe(
        self,
        job_id: str,
        item: JobInventoryItem,
        *,
        runner_instance_id: str | None = None,
    ) -> JobRecord:
        """Update one already-authorized job from a live Runner observation."""

        if runner_instance_id is not None:
            validate_identifier(runner_instance_id, "runner_instance_id")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobAccessError("unknown job")
            if item.job_id != job.job_id or item.workspace_id != job.workspace_id:
                raise JobAccessError("job observation identity does not match")
            if not self._same_process(job, item, runner_instance_id):
                raise JobAccessError("job observation process identity does not match")
            if item.stdout_cursor < job.stdout_cursor or item.stderr_cursor < job.stderr_cursor:
                raise JobAccessError("job observation cursor regressed")
            if job.state in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.LOST,
            }:
                return job
            self._jobs[job_id] = self._replace(
                job,
                state=item.state,
                item=item,
                runner_instance_id=runner_instance_id,
            )
            return self._jobs[job_id]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            counts = {state.value: 0 for state in JobState}
            for job in self._jobs.values():
                counts[job.state.value] += 1
            return {"job_count": len(self._jobs), **counts}

    def _mark_lost(self, job: JobRecord) -> None:
        self._jobs[job.job_id] = self._replace(job, state=JobState.LOST)

    @staticmethod
    def _same_process(
        job: JobRecord,
        item: JobInventoryItem,
        runner_instance_id: str | None,
    ) -> bool:
        if not job.process_fingerprint or job.process_fingerprint != item.process_fingerprint:
            return False
        if (
            job.runner_instance_id is not None
            and runner_instance_id is not None
            and job.runner_instance_id != runner_instance_id
        ):
            return False
        if job.started_at is not None and item.started_at is not None and job.started_at != item.started_at:
            return False
        return True

    @staticmethod
    def _replace(
        job: JobRecord,
        *,
        state: JobState,
        item: JobInventoryItem | None = None,
        runner_instance_id: str | None = None,
    ) -> JobRecord:
        return JobRecord(
            job_id=job.job_id,
            runner_id=job.runner_id,
            workspace_id=job.workspace_id,
            owner_principal_id=job.owner_principal_id,
            state=state,
            process_fingerprint=(item.process_fingerprint if item is not None else job.process_fingerprint),
            runner_instance_id=(runner_instance_id or job.runner_instance_id),
            stdout_cursor=(item.stdout_cursor if item is not None else job.stdout_cursor),
            stderr_cursor=(item.stderr_cursor if item is not None else job.stderr_cursor),
            started_at=(item.started_at if item is not None and item.started_at is not None else job.started_at),
        )


class WorkspaceJobManager:
    """Workspace-scoped owner boundary over global Runner reconciliation state."""

    def __init__(
        self,
        reconciler: RunnerJobReconciler,
        *,
        runner_id: str,
        workspace_id: str,
    ) -> None:
        validate_identifier(runner_id, "runner_id")
        validate_identifier(workspace_id, "workspace_id")
        self.reconciler = reconciler
        self.runner_id = runner_id
        self.workspace_id = workspace_id

    def register_or_observe(
        self,
        owner_principal_id: str,
        item: JobInventoryItem,
        *,
        runner_instance_id: str | None = None,
    ) -> JobRecord:
        if item.workspace_id != self.workspace_id:
            raise JobAccessError("job inventory belongs to a different workspace")
        try:
            existing = self.reconciler.get(item.job_id)
        except JobAccessError:
            record = JobRecord(
                item.job_id,
                self.runner_id,
                self.workspace_id,
                owner_principal_id,
                state=item.state,
                process_fingerprint=item.process_fingerprint,
                runner_instance_id=runner_instance_id,
                stdout_cursor=item.stdout_cursor,
                stderr_cursor=item.stderr_cursor,
                started_at=item.started_at,
            )
            self.reconciler.register(record)
            return record
        if (
            existing.runner_id != self.runner_id
            or existing.workspace_id != self.workspace_id
            or existing.owner_principal_id != owner_principal_id
        ):
            raise JobAccessError("job is not available to this principal or workspace")
        return self.reconciler.observe(
            item.job_id,
            item,
            runner_instance_id=runner_instance_id,
        )

    def get_for_owner(self, job_id: str, owner_principal_id: str) -> JobRecord:
        job = self.reconciler.get_for_owner(job_id, owner_principal_id, self.workspace_id)
        if job.runner_id != self.runner_id:
            raise JobAccessError("job is not available on this runner")
        return job

    def list_for_owner(self, owner_principal_id: str) -> tuple[JobRecord, ...]:
        return tuple(
            job
            for job in self.reconciler.list_for_owner(owner_principal_id, self.workspace_id)
            if job.runner_id == self.runner_id
        )


def _validate_process_fingerprint(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise RunnerProtocolError("job process_fingerprint is required")
    if len(value) > 256:
        raise RunnerProtocolError("job process_fingerprint is too long")


def _validate_cursors(stdout_cursor: int, stderr_cursor: int) -> None:
    for cursor in (stdout_cursor, stderr_cursor):
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise RunnerProtocolError("job output cursors must be non-negative integers")


def _validate_started_at(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or len(value) > MAX_JOB_TIMESTAMP_LENGTH:
        raise RunnerProtocolError("job started_at must be a bounded non-empty string")
