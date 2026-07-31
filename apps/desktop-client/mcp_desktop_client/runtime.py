from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from .i18n import tr
from .models import MCP_ENDPOINT_PATH, RuntimeStatus, WorkspaceProfile
from .storage import log_dir_for_profile, runtime_state_file_for_profile, write_private_json

TRYCLOUDFLARE_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)
HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)
HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)


@dataclass
class ManagedSession:
    runtime_process: subprocess.Popen[str] | None = None
    tunnel_process: subprocess.Popen[str] | None = None
    runtime_pid: int | None = None
    runtime_create_time: float | None = None
    tunnel_create_time: float | None = None


class RuntimeManager:
    RUNTIME_START_TIMEOUT_SECONDS = 20.0
    PORT_RELEASE_TIMEOUT_SECONDS = 8.0

    def __init__(self) -> None:
        self._sessions: dict[str, ManagedSession] = {}

    def start(self, profile: WorkspaceProfile) -> RuntimeStatus:
        try:
            self._validate_tunnel_requirements(profile)
        except Exception as exc:
            self._append_tunnel_error_log(profile, str(exc))
            raise
        self._cleanup_orphan_tunnel(profile)
        existing_pid = self._find_runtime_pid(profile)
        if existing_pid is not None:
            if profile.tunnel.type == "cloudflare" and self._find_tunnel_pid(profile) is None:
                try:
                    recovered_tunnel_process, public_url = self._start_cloudflare_tunnel(profile)
                except Exception as exc:
                    self._append_tunnel_error_log(profile, str(exc))
                    raise
                self._sessions[profile.id] = ManagedSession(
                    runtime_process=None,
                    tunnel_process=recovered_tunnel_process,
                    runtime_pid=existing_pid,
                    runtime_create_time=self._process_create_time(existing_pid),
                    tunnel_create_time=self._process_create_time(recovered_tunnel_process.pid),
                )
                self._write_runtime_state(
                    profile,
                    runtime_pid=existing_pid,
                    tunnel_pid=recovered_tunnel_process.pid,
                    public_url=public_url,
                )
            elif profile.id not in self._sessions:
                self._sessions[profile.id] = ManagedSession(
                    runtime_pid=existing_pid,
                    runtime_create_time=self._process_create_time(existing_pid),
                )
            return self.status(profile)

        state_runtime = self._find_state_runtime(profile.id)
        if state_runtime is not None:
            _state_pid, state_port = state_runtime
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "An MCP runtime using the previous configuration is still running on port {port}. "
                    "Stop it before starting the new configuration.",
                ).format(port=state_port)
            )

        conflicting_pid = self._find_pid_by_port(profile.runtime.local_port)
        if conflicting_pid is not None:
            conflict_command = self._command_line_for_pid(conflicting_pid)
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "The local port is already in use.\nPort: {port}\nProcess PID: {pid}\nCommand: {command}",
                ).format(
                    port=profile.runtime.local_port,
                    pid=conflicting_pid,
                    command=conflict_command or tr("RuntimeManager", "Unknown"),
                )
            )

        runtime_process, runtime_pid = self._start_runtime_process(profile)
        tunnel_process: subprocess.Popen[str] | None = None
        public_url = self._server_url_for_profile(profile) if profile.tunnel.type == "frp" else ""

        try:
            if profile.tunnel.type == "cloudflare":
                tunnel_process, public_url = self._start_cloudflare_tunnel(profile)
            elif profile.tunnel.type != "frp":
                raise RuntimeError(tr("RuntimeManager", "Only FRP and Cloudflare are currently supported."))
        except Exception as exc:
            self._append_tunnel_error_log(profile, str(exc))
            self._terminate_live_process_tree(runtime_pid)
            if runtime_process.pid != runtime_pid:
                self._terminate_live_process_tree(runtime_process.pid)
            self._clear_runtime_state(profile.id)
            raise

        self._sessions[profile.id] = ManagedSession(
            runtime_process=runtime_process,
            tunnel_process=tunnel_process,
            runtime_pid=runtime_pid,
            runtime_create_time=self._process_create_time(runtime_pid),
            tunnel_create_time=self._process_create_time(tunnel_process.pid) if tunnel_process else None,
        )
        self._write_runtime_state(
            profile,
            runtime_pid=runtime_pid,
            tunnel_pid=tunnel_process.pid if tunnel_process else None,
            public_url=public_url,
        )
        return self.status(profile)

    def stop(self, profile: WorkspaceProfile) -> RuntimeStatus:
        session = self._sessions.pop(profile.id, None)
        release_port = profile.runtime.local_port

        tunnel_pid: int | None = None
        tunnel_create_time: float | None = None
        runtime_pid: int | None = None
        runtime_create_time: float | None = None
        if session is not None:
            if (
                session.tunnel_process
                and session.tunnel_process.poll() is None
                and self._process_has_create_time(session.tunnel_process.pid, session.tunnel_create_time)
            ):
                tunnel_pid = session.tunnel_process.pid
                tunnel_create_time = session.tunnel_create_time
            if session.runtime_pid is not None and self._process_has_create_time(
                session.runtime_pid,
                session.runtime_create_time,
            ):
                runtime_pid = session.runtime_pid
                runtime_create_time = session.runtime_create_time
            elif session.runtime_process and session.runtime_process.poll() is None:
                runtime_pid = session.runtime_process.pid
                runtime_create_time = self._process_create_time(runtime_pid)
        if tunnel_pid is None:
            tunnel_pid = self._find_tunnel_pid(profile)
            if tunnel_pid is None:
                tunnel_pid = self._find_state_tunnel_pid(profile.id)
            tunnel_create_time = self._process_create_time(tunnel_pid)
        if runtime_pid is None:
            runtime_pid = self._find_runtime_pid(profile)
            if runtime_pid is None:
                state_runtime = self._find_state_runtime(profile.id)
                if state_runtime is not None:
                    runtime_pid, release_port = state_runtime
            runtime_create_time = self._process_create_time(runtime_pid)

        if tunnel_pid is not None:
            self._terminate_process_tree(tunnel_pid, expected_create_time=tunnel_create_time)
        if runtime_pid is not None:
            self._terminate_process_tree(runtime_pid, expected_create_time=runtime_create_time)
        if session is not None and session.runtime_process and session.runtime_process.poll() is None:
            if runtime_pid != session.runtime_process.pid:
                wrapper_create_time = self._process_create_time(session.runtime_process.pid)
                self._terminate_process_tree(
                    session.runtime_process.pid,
                    expected_create_time=wrapper_create_time,
                )
        self._wait_for_port_state(release_port, listening=False, timeout=self.PORT_RELEASE_TIMEOUT_SECONDS)

        self._clear_runtime_state(profile.id)
        return RuntimeStatus(
            state="stopped",
            local_message=tr("RuntimeManager", "Stopped"),
            public_message=tr("RuntimeManager", "Stopped"),
        )

    def status(self, profile: WorkspaceProfile) -> RuntimeStatus:
        state = self._read_runtime_state(profile.id)
        runtime_pid = self._find_runtime_pid(profile, state=state)
        if runtime_pid is None:
            state_runtime = self._find_state_runtime(profile.id, state=state)
            if state_runtime is not None:
                state_pid, state_port = state_runtime
                return RuntimeStatus(
                    state="error",
                    pid=state_pid,
                    local_message=tr(
                        "RuntimeManager",
                        "An MCP runtime using the previous configuration is listening on 127.0.0.1:{port}",
                    ).format(port=state_port),
                    public_message=tr(
                        "RuntimeManager",
                        "Stop the previous runtime before saving or starting the new configuration",
                    ),
                )
        tunnel_pid = self._find_tunnel_pid(profile, state=state)
        public_url = self.resolved_public_url(profile, state=state)

        if runtime_pid is None:
            if tunnel_pid is not None:
                self._terminate_live_process_tree(tunnel_pid)
            self._clear_runtime_state(profile.id)
            return RuntimeStatus(
                state="stopped",
                local_message=tr("RuntimeManager", "Not running"),
                public_message=tr("RuntimeManager", "Unknown"),
            )

        if profile.tunnel.type == "cloudflare" and tunnel_pid is None:
            return RuntimeStatus(
                state="error",
                pid=runtime_pid,
                local_message=tr(
                    "RuntimeManager",
                    "The local MCP runtime is listening on 127.0.0.1:{port}",
                ).format(port=profile.runtime.local_port),
                public_message=tr("RuntimeManager", "The Cloudflare tunnel is not connected"),
            )

        public_message = public_url or profile.endpoint
        if profile.tunnel.type == "frp":
            public_message = tr(
                "RuntimeManager",
                "{url} (an external FRP client must remain running)",
            ).format(url=public_message)
        if profile.tunnel.type == "cloudflare" and not public_url:
            public_message = tr("RuntimeManager", "Waiting for Cloudflare to assign a public URL")
        return RuntimeStatus(
            state="running",
            pid=runtime_pid,
            local_message=tr(
                "RuntimeManager",
                "Listening on 127.0.0.1:{port}",
            ).format(port=profile.runtime.local_port),
            public_message=public_message,
        )

    def summary_state(self, profile: WorkspaceProfile) -> str:
        return self.status(profile).state

    def resolved_public_url(self, profile: WorkspaceProfile, *, state: dict[str, object] | None = None) -> str:
        if profile.tunnel.type == "frp":
            return profile.effective_public_url
        if profile.tunnel.type == "cloudflare" and profile.tunnel.cloudflare_mode == "named":
            return profile.tunnel.public_url.rstrip("/")
        if state is None:
            state = self._read_runtime_state(profile.id)
        value = state.get("public_url")
        return str(value).rstrip("/") if isinstance(value, str) and value.strip() else ""

    def resolved_endpoint(self, profile: WorkspaceProfile) -> str:
        public_url = self.resolved_public_url(profile)
        if not public_url:
            return ""
        return f"{public_url.rstrip('/')}{MCP_ENDPOINT_PATH}"

    def _start_runtime_process(self, profile: WorkspaceProfile) -> tuple[subprocess.Popen[str], int]:
        command = self._resolve_command(profile)
        env = os.environ.copy()
        server_url = self._server_url_for_profile(profile)
        if server_url:
            env["CODING_TOOLS_MCP_SERVER_URL"] = server_url
        else:
            env.pop("CODING_TOOLS_MCP_SERVER_URL", None)
        self._configure_pythonpath_for_local_repo(command, env)
        args = command + self._runtime_args(profile, env)

        log_dir = log_dir_for_profile(profile.id)
        stdout_path = log_dir / "stdout.log"
        stderr_path = log_dir / "stderr.log"
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        popen_kwargs: dict[str, Any] = {
            "args": args,
            "cwd": profile.path,
            "env": env,
            "stdout": stdout_handle,
            "stderr": stderr_handle,
            "text": True,
            **self._detached_popen_kwargs(),
        }
        try:
            process = subprocess.Popen(**popen_kwargs)
        finally:
            stdout_handle.close()
            stderr_handle.close()
        if not self._wait_for_port_state(
            profile.runtime.local_port,
            listening=True,
            timeout=self.RUNTIME_START_TIMEOUT_SECONDS,
        ):
            self._terminate_live_process_tree(process.pid)
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "The MCP runtime did not start listening on port {port} before the timeout.",
                ).format(port=profile.runtime.local_port)
            )
        runtime_pid = self._find_pid_by_port(profile.runtime.local_port)
        if runtime_pid is None:
            self._terminate_live_process_tree(process.pid)
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "The MCP runtime started, but the process listening on port {port} could not be identified.",
                ).format(port=profile.runtime.local_port)
            )
        return process, runtime_pid

    def _start_cloudflare_tunnel(self, profile: WorkspaceProfile) -> tuple[subprocess.Popen[str], str]:
        cloudflared = self._find_cloudflared_command()
        if not cloudflared:
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "cloudflared was not found. Install the Cloudflare Tunnel CLI before using Cloudflare mode.",
                )
            )

        log_dir = log_dir_for_profile(profile.id)
        tunnel_log = log_dir / "cloudflared.log"
        if profile.tunnel.cloudflare_mode == "named":
            if not profile.tunnel.cloudflare_token.strip():
                raise RuntimeError(tr("RuntimeManager", "Cloudflare named-tunnel mode requires a Tunnel Token."))
            if not profile.tunnel.public_url.strip():
                raise RuntimeError(
                    tr("RuntimeManager", "Cloudflare named-tunnel mode requires a fixed public URL.")
                )
            args = [cloudflared, "tunnel", "run", "--token", profile.tunnel.cloudflare_token.strip()]
        else:
            args = [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{profile.runtime.local_port}"]
        popen_kwargs: dict[str, Any] = {
            "args": args,
            "cwd": profile.path,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            **self._detached_popen_kwargs(),
        }
        process = subprocess.Popen(**popen_kwargs)
        public_url_holder: dict[str, str] = {"value": ""}
        ready = threading.Event()
        thread = threading.Thread(
            target=self._stream_cloudflare_output,
            args=(profile, process, tunnel_log, public_url_holder, ready),
            daemon=True,
        )
        thread.start()
        signalled = ready.wait(timeout=12)
        if profile.tunnel.cloudflare_mode == "named":
            if process.poll() is not None:
                raise RuntimeError(
                    tr(
                        "RuntimeManager",
                        "cloudflared exited before establishing the named tunnel. Check cloudflared.log.",
                    )
                )
            if not signalled:
                self._terminate_live_process_tree(process.pid)
                thread.join(timeout=2)
                raise RuntimeError(
                    tr(
                        "RuntimeManager",
                        "cloudflared started but did not establish the named tunnel before the timeout.",
                    )
                )
            return process, profile.tunnel.public_url.rstrip("/")
        if not public_url_holder["value"]:
            if process.poll() is None:
                self._terminate_live_process_tree(process.pid)
            thread.join(timeout=2)
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "cloudflared started but did not return a trycloudflare.com public URL before the timeout.",
                )
            )
        return process, public_url_holder["value"]

    def _stream_cloudflare_output(
        self,
        profile: WorkspaceProfile,
        process: subprocess.Popen[str],
        log_path: Path,
        public_url_holder: dict[str, str],
        ready: threading.Event,
    ) -> None:
        stream = process.stdout
        if stream is None:
            ready.set()
            return

        with log_path.open("a", encoding="utf-8") as handle:
            for raw_line in stream:
                line = raw_line.rstrip("\n")
                handle.write(raw_line)
                handle.flush()
                if profile.tunnel.cloudflare_mode == "named":
                    lowered = line.lower()
                    if "registered tunnel connection" in lowered:
                        ready.set()
                    continue
                if not public_url_holder["value"]:
                    matched = TRYCLOUDFLARE_URL_RE.search(line)
                    if matched:
                        public_url_holder["value"] = matched.group(0).rstrip("/")
                        self._update_runtime_state(profile.id, public_url=public_url_holder["value"])
                        ready.set()
            ready.set()

    def _server_url_for_profile(self, profile: WorkspaceProfile) -> str:
        if profile.tunnel.type == "frp":
            return profile.effective_public_url
        if profile.tunnel.type == "cloudflare" and profile.tunnel.cloudflare_mode == "named":
            return profile.tunnel.public_url.rstrip("/")
        return ""

    def _validate_tunnel_requirements(self, profile: WorkspaceProfile) -> None:
        workspace = Path(profile.path)
        if not workspace.is_dir():
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "Workspace directory does not exist: {path}",
                ).format(path=profile.path)
            )
        if profile.auth.type == "oauth":
            if not profile.auth.oauth_password.strip():
                raise RuntimeError(tr("RuntimeManager", "OAuth mode requires an authorization password."))
        elif profile.auth.type == "bearer" and not profile.auth.bearer_token.strip():
            raise RuntimeError(tr("RuntimeManager", "Bearer Token mode requires a token."))

        if profile.tunnel.type == "frp":
            if not profile.tunnel.frp_server.strip() or not profile.tunnel.frp_subdomain.strip():
                raise RuntimeError(
                    tr(
                        "RuntimeManager",
                        "FRP mode requires a server domain and subdomain, and an external FRP client must be running.",
                    )
                )
            if HOSTNAME_RE.fullmatch(profile.tunnel.frp_server.strip()) is None:
                raise RuntimeError(
                    tr(
                        "RuntimeManager",
                        "The FRP server domain is invalid. Enter a domain without a scheme or path.",
                    )
                )
            if HOST_LABEL_RE.fullmatch(profile.tunnel.frp_subdomain.strip()) is None:
                raise RuntimeError(tr("RuntimeManager", "The FRP subdomain is invalid."))
            return
        if profile.tunnel.type != "cloudflare":
            raise RuntimeError(tr("RuntimeManager", "Only FRP and Cloudflare are currently supported."))
        if not self._find_cloudflared_command():
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "cloudflared was not found. Install the Cloudflare Tunnel CLI first.\n"
                    "On Windows, run: winget install Cloudflare.cloudflared",
                )
            )
        if profile.tunnel.cloudflare_mode == "named":
            if not profile.tunnel.cloudflare_token.strip():
                raise RuntimeError(tr("RuntimeManager", "Cloudflare fixed-domain mode requires a Tunnel Token."))
            if not profile.tunnel.public_url.strip():
                raise RuntimeError(tr("RuntimeManager", "Cloudflare fixed-domain mode requires a public URL."))
            parsed_url = urlparse(profile.tunnel.public_url.strip())
            try:
                has_custom_port = parsed_url.port is not None
            except ValueError:
                has_custom_port = True
            if (
                parsed_url.scheme != "https"
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
                or has_custom_port
                or parsed_url.path not in {"", "/"}
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise RuntimeError(
                    tr(
                        "RuntimeManager",
                        "The Cloudflare public URL must be an HTTPS URL containing only a domain.",
                    )
                )

    def _append_tunnel_error_log(self, profile: WorkspaceProfile, message: str) -> None:
        path = log_dir_for_profile(profile.id) / "cloudflared.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")

    def _resolve_command(self, profile: WorkspaceProfile) -> list[str]:
        if profile.runtime.runtime_command.strip():
            try:
                parts = shlex.split(profile.runtime.runtime_command.strip(), posix=os.name != "nt")
            except ValueError as exc:
                raise RuntimeError(
                    tr(
                        "RuntimeManager",
                        "The custom command is invalid: {error}",
                    ).format(error=exc)
                ) from exc
            if os.name == "nt":
                parts = [self._strip_matching_quotes(part) for part in parts]
            if not parts:
                raise RuntimeError(tr("RuntimeManager", "The custom command cannot be empty."))
            return parts
        if shutil.which("coding-tools-mcp"):
            return ["coding-tools-mcp"]
        if shutil.which("uvx"):
            return ["uvx", "coding-tools-mcp"]

        repo_root = Path(__file__).resolve().parents[3]
        if (repo_root / "coding_tools_mcp").exists():
            return [sys.executable, "-m", "coding_tools_mcp"]
        raise RuntimeError(
            tr(
                "RuntimeManager",
                "Could not find uvx, coding-tools-mcp, or the local Python module entry point.",
            )
        )

    def _runtime_args(self, profile: WorkspaceProfile, env: dict[str, str]) -> list[str]:
        for name in (
            "CODING_TOOLS_MCP_AUTH_MODE",
            "CODING_TOOLS_MCP_AUTH_TOKEN",
            "CODING_TOOLS_MCP_OAUTH_MODE",
            "CODING_TOOLS_MCP_OAUTH_PASSWORD",
            "CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET",
        ):
            env.pop(name, None)
        args = [
            "--workspace",
            profile.path,
            "--host",
            "127.0.0.1",
            "--port",
            str(profile.runtime.local_port),
            "--permission-mode",
            profile.runtime.permission_mode,
        ]
        if profile.auth.type == "oauth":
            env["CODING_TOOLS_MCP_OAUTH_PASSWORD"] = profile.auth.oauth_password
            env["CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET"] = profile.auth.oauth_token_secret
            args.append("--oauth-mode")
        elif profile.auth.type == "bearer":
            env["CODING_TOOLS_MCP_AUTH_MODE"] = "bearer"
            env["CODING_TOOLS_MCP_AUTH_TOKEN"] = profile.auth.bearer_token
        elif profile.auth.type == "noauth":
            env["CODING_TOOLS_MCP_AUTH_MODE"] = "noauth"
        return args

    def _configure_pythonpath_for_local_repo(self, command: list[str], env: dict[str, str]) -> None:
        if command[:2] != [sys.executable, "-m"]:
            return
        repo_root = str(Path(__file__).resolve().parents[3])
        current = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = repo_root if not current else os.pathsep.join([repo_root, current])

    def _strip_matching_quotes(self, value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    def _state_file(self, profile_id: str) -> Path:
        return runtime_state_file_for_profile(profile_id)

    def _write_runtime_state(
        self,
        profile: WorkspaceProfile,
        *,
        runtime_pid: int | None,
        tunnel_pid: int | None = None,
        public_url: str = "",
    ) -> None:
        payload = {
            "runtime_pid": runtime_pid,
            "runtime_create_time": self._process_create_time(runtime_pid),
            "tunnel_pid": tunnel_pid,
            "tunnel_create_time": self._process_create_time(tunnel_pid),
            "port": profile.runtime.local_port,
            "workspace": profile.path,
            "tunnel_type": profile.tunnel.type,
            "tunnel_mode": profile.tunnel.cloudflare_mode,
            "public_url": public_url.rstrip("/"),
        }
        write_private_json(self._state_file(profile.id), payload)

    def _update_runtime_state(self, profile_id: str, **updates: object) -> None:
        state = self._read_runtime_state(profile_id)
        if not state:
            return
        state.update(updates)
        write_private_json(self._state_file(profile_id), state)

    def _clear_runtime_state(self, profile_id: str) -> None:
        path = self._state_file(profile_id)
        if path.exists():
            path.unlink()

    def _read_runtime_state(self, profile_id: str) -> dict[str, object]:
        path = self._state_file(profile_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError):
            return {}

    def _detached_popen_kwargs(self) -> dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        return {"start_new_session": True}

    def _find_runtime_pid(
        self, profile: WorkspaceProfile, *, state: dict[str, object] | None = None
    ) -> int | None:
        session = self._sessions.get(profile.id)
        if (
            session
            and session.runtime_pid is not None
            and self._process_has_create_time(session.runtime_pid, session.runtime_create_time)
            and self._process_matches_profile(session.runtime_pid, profile)
        ):
            return session.runtime_pid
        if session and session.runtime_process and session.runtime_process.poll() is None:
            process_pid = session.runtime_process.pid
            if self._process_matches_profile(process_pid, profile):
                return process_pid
        port_pid = self._find_pid_by_port(profile.runtime.local_port)
        if port_pid is not None and self._process_matches_profile(port_pid, profile):
            return port_pid
        if state is None:
            state = self._read_runtime_state(profile.id)
        runtime_pid = state.get("runtime_pid", state.get("pid"))
        runtime_create_time = state.get("runtime_create_time")
        if (
            isinstance(runtime_pid, int)
            and isinstance(runtime_create_time, (int, float))
            and self._process_has_create_time(runtime_pid, float(runtime_create_time))
            and self._process_matches_profile(runtime_pid, profile)
        ):
            return runtime_pid
        return None

    def _find_tunnel_pid(
        self, profile: WorkspaceProfile, *, state: dict[str, object] | None = None
    ) -> int | None:
        session = self._sessions.get(profile.id)
        if (
            session
            and session.tunnel_process
            and session.tunnel_process.poll() is None
            and self._process_has_create_time(session.tunnel_process.pid, session.tunnel_create_time)
        ):
            return session.tunnel_process.pid
        if state is None:
            state = self._read_runtime_state(profile.id)
        tunnel_pid = state.get("tunnel_pid")
        tunnel_create_time = state.get("tunnel_create_time")
        if (
            isinstance(tunnel_pid, int)
            and isinstance(tunnel_create_time, (int, float))
            and self._process_has_create_time(tunnel_pid, float(tunnel_create_time))
            and self._process_matches_tunnel(tunnel_pid, profile)
        ):
            return tunnel_pid
        if profile.tunnel.type == "cloudflare":
            return self._find_cloudflare_tunnel_pid(profile)
        return None

    def _process_matches_profile(self, pid: int, profile: WorkspaceProfile) -> bool:
        return self._process_matches_runtime(
            pid,
            workspace=profile.path,
            port=profile.runtime.local_port,
        )

    def _process_matches_runtime(self, pid: int, *, workspace: str, port: int) -> bool:
        command = self._command_for_pid(pid)
        if not command:
            return False
        port_value = self._option_value(command, "--port")
        workspace_value = self._option_value(command, "--workspace")
        if port_value != str(port) or workspace_value is None:
            return False
        return self._normalized_path(workspace_value) == self._normalized_path(workspace)

    def _find_state_runtime(
        self, profile_id: str, *, state: dict[str, object] | None = None
    ) -> tuple[int, int] | None:
        if state is None:
            state = self._read_runtime_state(profile_id)
        pid = state.get("runtime_pid", state.get("pid"))
        create_time = state.get("runtime_create_time")
        port = state.get("port")
        workspace = state.get("workspace")
        if not (
            isinstance(pid, int)
            and isinstance(create_time, (int, float))
            and isinstance(port, int)
            and isinstance(workspace, str)
        ):
            return None
        if not self._process_has_create_time(pid, float(create_time)):
            return None
        if not self._process_matches_runtime(pid, workspace=workspace, port=port):
            return None
        return pid, port

    def _find_state_tunnel_pid(
        self, profile_id: str, *, state: dict[str, object] | None = None
    ) -> int | None:
        if state is None:
            state = self._read_runtime_state(profile_id)
        pid = state.get("tunnel_pid")
        create_time = state.get("tunnel_create_time")
        if not (
            isinstance(pid, int)
            and isinstance(create_time, (int, float))
            and self._process_has_create_time(pid, float(create_time))
        ):
            return None
        mode = state.get("tunnel_mode")
        port = state.get("port")
        if mode == "quick" and not isinstance(port, int):
            return None
        command = self._command_for_pid(pid)
        if not self._command_is_cloudflared_tunnel(
            command,
            mode=str(mode),
            port=port if isinstance(port, int) else None,
        ):
            return None
        return pid

    def _process_matches_tunnel(self, pid: int, profile: WorkspaceProfile) -> bool:
        return self._command_matches_profile_tunnel(self._command_for_pid(pid), profile)

    def _command_matches_profile_tunnel(self, command: list[str], profile: WorkspaceProfile) -> bool:
        if profile.tunnel.cloudflare_mode == "quick":
            return self._command_is_cloudflared_tunnel(
                command, mode="quick", port=profile.runtime.local_port
            )
        token = profile.tunnel.cloudflare_token.strip()
        if not token:
            return False
        return self._command_is_cloudflared_tunnel(command, mode="named", token=token)

    def _command_is_cloudflared_tunnel(
        self,
        command: list[str],
        *,
        mode: str,
        port: int | None = None,
        token: str | None = None,
    ) -> bool:
        """Single cloudflared-command fingerprint. token=None skips the token
        check (runtime-state files do not record it); an unrecognized mode only
        asserts the cloudflared+tunnel shape."""
        if not command or not any("cloudflared" in Path(item).name.lower() for item in command):
            return False
        lowered = [item.lower() for item in command]
        if "tunnel" not in lowered:
            return False
        if mode == "quick":
            if port is None:
                return False
            target = f"http://127.0.0.1:{port}".lower()
            return self._option_value(command, "--url", case_sensitive=False) == target
        if mode == "named":
            if "run" not in lowered:
                return False
            return token is None or self._option_value(command, "--token") == token
        return token is None

    def _option_value(
        self,
        command: list[str],
        option: str,
        *,
        case_sensitive: bool = True,
    ) -> str | None:
        expected = option if case_sensitive else option.lower()
        matched_value: str | None = None
        for index, item in enumerate(command):
            candidate = item if case_sensitive else item.lower()
            if candidate == expected and index + 1 < len(command):
                value = command[index + 1]
                matched_value = value if case_sensitive else value.lower()
                continue
            prefix = f"{expected}="
            if candidate.startswith(prefix):
                value = item[len(option) + 1 :]
                matched_value = value if case_sensitive else value.lower()
        return matched_value

    def _normalized_path(self, value: str) -> str:
        return os.path.normcase(os.path.normpath(value))

    def _process_create_time(self, pid: int | None) -> float | None:
        if pid is None:
            return None
        process = self._safe_process(pid)
        if process is None:
            return None
        try:
            return process.create_time()
        except (psutil.AccessDenied, psutil.Error):
            return None

    def _process_has_create_time(self, pid: int, expected_create_time: float | None) -> bool:
        if expected_create_time is None:
            return False
        actual_create_time = self._process_create_time(pid)
        return actual_create_time is not None and abs(actual_create_time - expected_create_time) < 0.001

    def _find_pid_by_port(self, port: int) -> int | None:
        try:
            for connection in psutil.net_connections(kind="tcp"):
                if connection.status != psutil.CONN_LISTEN:
                    continue
                if not connection.laddr or connection.laddr.port != port:
                    continue
                if connection.pid:
                    return connection.pid
        except (psutil.AccessDenied, psutil.Error):
            return None
        return None

    def _wait_for_port_state(self, port: int, listening: bool, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._port_is_listening(port) == listening:
                return True
            time.sleep(0.2)
        return False

    def _port_is_listening(self, port: int) -> bool:
        # The runtime always binds 127.0.0.1, so a loopback connect probe is
        # equivalent to scanning the system connection table and far cheaper.
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            return False

    def _command_line_for_pid(self, pid: int) -> str:
        return " ".join(self._command_for_pid(pid)).strip()

    def _command_for_pid(self, pid: int) -> list[str]:
        process = self._safe_process(pid)
        if process is None:
            return []
        try:
            return process.cmdline()
        except (psutil.AccessDenied, psutil.Error):
            return []

    def _terminate_live_process_tree(self, pid: int) -> None:
        self._terminate_process_tree(pid, expected_create_time=self._process_create_time(pid))

    def _terminate_process_tree(self, pid: int, *, expected_create_time: float | None = None) -> None:
        if expected_create_time is None:
            return
        try:
            process = psutil.Process(pid)
        except psutil.Error:
            return

        try:
            if abs(process.create_time() - expected_create_time) >= 0.001:
                return
        except (psutil.AccessDenied, psutil.Error):
            return

        try:
            children = process.children(recursive=True)
        except (psutil.AccessDenied, psutil.Error):
            children = []

        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                continue
        try:
            process.terminate()
        except psutil.Error:
            pass

        _gone, alive = psutil.wait_procs(children + [process], timeout=3)
        if alive:
            for item in alive:
                try:
                    item.kill()
                except psutil.Error:
                    continue
            psutil.wait_procs(alive, timeout=2)

    def _safe_process(self, pid: int) -> psutil.Process | None:
        try:
            return psutil.Process(pid)
        except psutil.Error:
            return None

    def _cleanup_orphan_tunnel(self, profile: WorkspaceProfile) -> None:
        if self._find_runtime_pid(profile) is not None or self._find_state_runtime(profile.id) is not None:
            return
        tunnel_pid = self._find_tunnel_pid(profile)
        if tunnel_pid is None:
            tunnel_pid = self._find_state_tunnel_pid(profile.id)
        if tunnel_pid is None:
            return
        self._terminate_live_process_tree(tunnel_pid)
        self._clear_runtime_state(profile.id)

    def _find_cloudflare_tunnel_pid(self, profile: WorkspaceProfile) -> int | None:
        for process in psutil.process_iter(["pid", "cmdline"]):
            try:
                pid = int(process.info["pid"])
                command = list(process.info.get("cmdline") or [])
            except (psutil.AccessDenied, psutil.Error):
                continue
            if self._command_matches_profile_tunnel(command, profile):
                return pid
        return None

    def _find_cloudflared_command(self) -> str | None:
        direct = shutil.which("cloudflared")
        if direct:
            return direct
        candidates = [
            Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
            Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
            Path.home() / ".cloudflared" / "cloudflared.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None
