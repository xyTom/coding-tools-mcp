"""Run one task in one arm and record what happened.

The harness, not the agent, drives the loop: it prepares a fresh checkout,
invokes the agent, runs the verification, and repeats until the task is green
or the round budget runs out. That keeps `rounds_to_green` and
`first_attempt_success` measurable for any agent, including one that reports
nothing about itself, and keeps both arms measured identically.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..mcp_http import local_server_env
from .tasks import Task

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_START_TIMEOUT_S = 30
NATIVE_ARM = "native"
MCP_ARM = "mcp"


@dataclass(frozen=True)
class ArmConfig:
    """One side of the comparison.

    ``agent_command`` is a shell command run in the task workspace. It receives
    the prompt on stdin and in ``CODING_TOOLS_EVAL_PROMPT``; in the MCP arm it
    also receives ``CODING_TOOLS_MCP_URL`` for a server already bound to that
    workspace.
    """

    name: str
    agent_command: str
    serve_mcp: bool = False
    env: dict[str, str] = field(default_factory=dict)
    server_args: tuple[str, ...] = ()


@dataclass
class RoundResult:
    index: int
    agent_exit_code: int
    agent_timed_out: bool
    verify_passed: bool
    regression_passed: bool
    duration_s: float


@dataclass
class TaskRun:
    task_id: str
    arm: str
    category: str
    setup_ok: bool
    started_green: bool
    """Whether `verify` already passed before the agent ran, which invalidates the task."""
    solved: bool
    rounds: list[RoundResult]
    wall_time_s: float
    error: str = ""

    @property
    def rounds_to_green(self) -> int | None:
        for entry in self.rounds:
            if entry.verify_passed and entry.regression_passed:
                return entry.index
        return None

    @property
    def first_attempt_success(self) -> bool:
        return self.rounds_to_green == 1

    @property
    def regression_introduced(self) -> bool:
        return bool(self.rounds) and not self.rounds[-1].regression_passed

    def to_dict(self) -> dict[str, Any]:
        return {
            **{key: value for key, value in asdict(self).items() if key != "rounds"},
            "rounds": [asdict(entry) for entry in self.rounds],
            "rounds_to_green": self.rounds_to_green,
            "first_attempt_success": self.first_attempt_success,
            "regression_introduced": self.regression_introduced,
        }


def run_task(task: Task, arm: ArmConfig, workdir: Path, *, python: str = sys.executable) -> TaskRun:
    started = time.time()
    workspace = workdir / f"{task.id}--{arm.name}"
    rounds: list[RoundResult] = []
    try:
        _prepare_workspace(task, workspace)
    except RuntimeError as exc:
        return TaskRun(task.id, arm.name, task.category, False, False, False, rounds, time.time() - started, str(exc))

    # A task whose verification already passes measures nothing, so it is
    # reported as invalid rather than as a free success for both arms.
    if _run_check(task.verify, workspace, task.timeout_s):
        return TaskRun(task.id, arm.name, task.category, True, True, False, rounds, time.time() - started,
                       "verify already passed before the agent ran")

    with _maybe_server(arm, workspace, python) as server_url:
        for index in range(1, task.max_rounds + 1):
            round_started = time.time()
            exit_code, timed_out = _run_agent(task, arm, workspace, server_url)
            verified = _run_check(task.verify, workspace, task.timeout_s)
            regression_ok = (
                _run_check(task.regression_verify, workspace, task.timeout_s)
                if task.regression_verify
                else True
            )
            rounds.append(
                RoundResult(index, exit_code, timed_out, verified, regression_ok, time.time() - round_started)
            )
            if verified and regression_ok:
                break
    solved = bool(rounds) and rounds[-1].verify_passed and rounds[-1].regression_passed
    return TaskRun(task.id, arm.name, task.category, True, False, solved, rounds, time.time() - started)


def _prepare_workspace(task: Task, workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    if task.repo:
        _run_or_raise(["git", "clone", "--quiet", task.repo, str(workspace)], cwd=workspace.parent)
        if task.commit:
            _run_or_raise(["git", "checkout", "--quiet", task.commit], cwd=workspace)
    if task.fixture:
        shutil.copytree(task.fixture, workspace, dirs_exist_ok=True)
    if task.setup and not _run_check(task.setup, workspace, task.timeout_s):
        raise RuntimeError("setup command failed")
    if not (workspace / ".git").exists():
        # Every task gets a git baseline so a diff of the agent's work exists
        # even for a fixture built by `setup`.
        _run_or_raise(["git", "init", "--quiet", "."], cwd=workspace)
        _run_or_raise(["git", "add", "-A"], cwd=workspace)
        _run_or_raise(
            [
                "git",
                "-c",
                "user.email=eval@example.com",
                "-c",
                "user.name=eval",
                "commit",
                "--quiet",
                # A fixture may legitimately start empty; the baseline commit
                # exists to make the agent's work diffable, not to assert
                # content.
                "--allow-empty",
                "-m",
                "baseline",
            ],
            cwd=workspace,
        )


def _run_or_raise(argv: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {completed.stderr.strip()[:400]}")


def _run_check(command: str, workspace: Path, timeout_s: int) -> bool:
    if not command:
        return True
    try:
        completed = subprocess.run(
            command, shell=True, cwd=str(workspace), capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def _run_agent(task: Task, arm: ArmConfig, workspace: Path, server_url: str) -> tuple[int, bool]:
    """Run the agent with the task prompt and return its exit code and timeout flag."""
    env = {**local_server_env(), **arm.env, "CODING_TOOLS_EVAL_PROMPT": task.prompt, "CODING_TOOLS_EVAL_TASK": task.id}
    if server_url:
        env["CODING_TOOLS_MCP_URL"] = server_url
    try:
        completed = subprocess.run(
            arm.agent_command,
            shell=True,
            cwd=str(workspace),
            input=task.prompt,
            capture_output=True,
            text=True,
            timeout=task.timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, True
    return completed.returncode, False


@contextlib.contextmanager
def _maybe_server(arm: ArmConfig, workspace: Path, python: str) -> Iterator[str]:
    """Yield a temporary MCP endpoint, or an empty string when this arm needs no server."""
    if not arm.serve_mcp:
        yield ""
        return
    port = _free_port()
    process = subprocess.Popen(
        [
            python,
            "-m",
            "coding_tools_mcp",
            "--workspace",
            str(workspace),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            *arm.server_args,
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**local_server_env(), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        if not _wait_for_port(port, process):
            raise RuntimeError(f"MCP server did not start{_exit_detail(process)}")
        yield url
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.poll() is None:
            process.kill()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def _exit_detail(process: subprocess.Popen[bytes]) -> str:
    """Report the server's own complaint, but only once it has exited.

    Reading the pipe of a live-but-unresponsive server would block until it is
    killed, so a hung server is reported without its output.
    """
    if process.poll() is None or process.stderr is None:
        return ""
    with contextlib.suppress(OSError, ValueError):
        return ": " + process.stderr.read().decode("utf-8", "replace").strip()[:400]
    return ""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[bytes]) -> bool:
    deadline = time.time() + SERVER_START_TIMEOUT_S
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def write_runs(runs: list[TaskRun], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([run.to_dict() for run in runs], indent=2) + "\n", encoding="utf-8")
