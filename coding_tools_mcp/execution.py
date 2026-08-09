"""Local OS/process execution boundary used by Runtime composition.

The first implementation is intentionally local-only. Network/Runner routing
belongs to WorkspaceHost rather than growing a RemoteExecutionBackend here.
"""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


SpawnProcess = Callable[..., tuple[subprocess.Popen[bytes], int | None]]
TerminateProcess = Callable[..., None]


@runtime_checkable
class ExecutionBackend(Protocol):
    @property
    def workspace_root(self) -> Path: ...

    def spawn(
        self,
        command: Any,
        *,
        cwd: str,
        shell: bool,
        env: dict[str, str],
        tty: bool,
        popen_kwargs: dict[str, Any],
    ) -> tuple[subprocess.Popen[bytes], int | None]: ...

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        signum: signal.Signals,
        *,
        force: bool = False,
    ) -> None: ...


class LocalExecutionBackend:
    """Workspace-confined process launcher with injected local primitives."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        spawn_process: SpawnProcess,
        terminate_process: TerminateProcess,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("execution workspace root must be a directory")
        self._workspace_root = root
        self._spawn_process = spawn_process
        self._terminate_process = terminate_process

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def spawn(
        self,
        command: Any,
        *,
        cwd: str,
        shell: bool,
        env: dict[str, str],
        tty: bool,
        popen_kwargs: dict[str, Any],
    ) -> tuple[subprocess.Popen[bytes], int | None]:
        resolved_cwd = Path(cwd).expanduser().resolve(strict=True)
        try:
            resolved_cwd.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ValueError("execution cwd must remain inside the workspace") from exc
        if not resolved_cwd.is_dir():
            raise ValueError("execution cwd must be a directory")
        return self._spawn_process(
            command,
            cwd=str(resolved_cwd),
            shell=shell,
            env=env,
            tty=tty,
            popen_kwargs=popen_kwargs,
        )

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        signum: signal.Signals,
        *,
        force: bool = False,
    ) -> None:
        self._terminate_process(process, signum, force=force)


__all__ = ["ExecutionBackend", "LocalExecutionBackend"]
