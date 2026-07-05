from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import html
import difflib
import fnmatch
import functools
import http.server
import json
import mimetypes
import os
import platform
import posixpath
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast

import jwt

from . import __version__
from .admin import ADMIN_TOOL_NAMES, McpAdminManager, McpManagementError
from .codex_sessions import import_codex_session_candidates, scan_codex_session_candidates
from .transcript import TranscriptStore
from .upstream import UpstreamManager
from .webui import admin_asset_response, admin_console_html


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "coding-tools-mcp"
LOGGING_LEVELS = (
    "debug",
    "info",
    "notice",
    "warning",
    "error",
    "critical",
    "alert",
    "emergency",
)
DEFAULT_EXCLUDED_NAMES = {
    ".git",
    ".reference",
    "node_modules",
    "target",
    "dist",
    "build",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
DEFAULT_MAX_LINES = 2000
GREP_MAX_LINE_CHARS = 500
IMAGE_RESIZE_MAX_DIMENSION = 2000
SENSITIVE_ENV_RE = re.compile(r"(token|secret|credential|api[_-]?key|password|passwd|private)", re.I)
SENSITIVE_VALUE_RE = re.compile(
    r"(COMPLIANCE_SHOULD_NOT_LEAK|-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})"
)
RISKY_ENV_NAMES = {
    "BASH_ENV",
    "ENV",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "NODE_OPTIONS",
    "PERL5LIB",
    "PERL5OPT",
    "RUBYOPT",
    "RUBYLIB",
}
SHELL_ENV_INHERIT_CHOICES = ("core", "all", "none")


@dataclass(frozen=True)
class ModeCapabilities:
    """What a permission mode allows. Gates consult this instead of comparing mode strings."""

    network: bool
    shell_expansion: bool
    inline_script: bool
    landlock: bool
    secret_env_filter: bool
    global_tmp_write: str  # "blocked" | "tmp-prefix" | "allowed"
    skip_all_permissions: bool


PERMISSION_MODE_CAPABILITIES: dict[str, ModeCapabilities] = {
    "safe": ModeCapabilities(
        network=False,
        shell_expansion=False,
        inline_script=False,
        landlock=True,
        secret_env_filter=True,
        global_tmp_write="blocked",
        skip_all_permissions=False,
    ),
    "trusted": ModeCapabilities(
        network=True,
        shell_expansion=True,
        inline_script=True,
        landlock=True,
        secret_env_filter=True,
        global_tmp_write="tmp-prefix",
        skip_all_permissions=False,
    ),
    "dangerous": ModeCapabilities(
        network=True,
        shell_expansion=True,
        inline_script=True,
        landlock=False,
        secret_env_filter=False,
        global_tmp_write="allowed",
        skip_all_permissions=True,
    ),
}
PERMISSION_MODE_CHOICES = tuple(PERMISSION_MODE_CAPABILITIES)
# Documented kill_session status enum (docs/profile-v0.1.md); guarded by test_schema_drift.
KILL_SESSION_STATUSES = ("terminated", "killed", "exited", "terminating", "not_found")
POSIX_CORE_ENV_NAMES = {"PATH", "LANG", "LC_ALL", "TERM"}
WINDOWS_CORE_ENV_NAMES = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR"}
WINDOWS_CANONICAL_ENV_NAMES = {
    "PATH": "Path",
    "PATHEXT": "PATHEXT",
    "COMSPEC": "ComSpec",
    "SYSTEMROOT": "SystemRoot",
    "WINDIR": "windir",
}
FORCE_KILL_SIGNAL = cast(signal.Signals, getattr(signal, "SIGKILL", signal.SIGTERM))
NETWORK_RE = re.compile(
    r"(https?://|urllib\.request|urllib3|requests\.|http\.client|\bHTTPConnection\b|\bHTTPSConnection\b|socket\.|aiohttp|httpx|\bcurl\b|\bwget\b|\bnc\b|\bnetcat\b|\bssh\b|\bscp\b|\bftp\b)",
    re.I,
)
SHELL_EXPANSION_RE = re.compile(r"(`|\$\(|\$\{)")
DESTRUCTIVE_RE = re.compile(
    r"(^|\s)(sudo|su|chmod\s+-R|chown\s+-R|mkfs|mount|umount|find\b[^;&|]*\s-delete\b|git\b[^;&|]*\breset\s+--hard\b|git\b[^;&|]*\bclean\s+-[^\s]*[fx][^\s]*|rm\s+-[^\s]*r[^\s]*f|rm\s+-[^\s]*f[^\s]*r)\b",
    re.I,
)
MAX_HTTP_REQUEST_BYTES = 1_048_576
MAX_JSON_RPC_BATCH_ITEMS = 50
PATCH_CHECKPOINT_LIMIT = 50
HTTP_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.~:-]{1,128}$")
RECENT_MCP_REQUEST_LIMIT = 100
SESSION_BUFFER_BYTES = 1_048_576
SHELL_CONTROL_TOKENS = {"|", "||", "&", "&&", ";", "(", ")"}
REDIRECTION_TOKENS = {">", ">>", "<", "<>", ">&", "<&", "&>", "&>>"}
HEREDOC_TOKENS = {"<<", "<<<"}
PATH_ARGUMENT_COMMANDS = {
    "cat",
    "cd",
    "chdir",
    "chmod",
    "chown",
    "cp",
    "head",
    "less",
    "ln",
    "ls",
    "mkdir",
    "more",
    "mv",
    "rm",
    "rmdir",
    "stat",
    "tail",
    "touch",
    "wc",
}
PATTERN_THEN_PATH_COMMANDS = {"grep", "egrep", "fgrep", "rg", "sed", "awk"}
SCRIPT_COMMANDS = {"bash", "sh", "zsh", "python", "python3", "node", "ruby", "perl"}
ENV_OPTIONS_WITH_ARGUMENT = {
    "-u",
    "--unset",
    "-C",
    "--chdir",
    "-S",
    "--split-string",
    "-a",
    "--argv0",
}
ENV_LONG_OPTIONS_WITH_ARGUMENT = {
    "--unset",
    "--chdir",
    "--split-string",
    "--argv0",
}
ENV_LONG_OPTIONS_WITH_OPTIONAL_ARGUMENT = {
    "--ignore-signal",
    "--default-signal",
    "--block-signal",
}
ENV_SHORT_OPTIONS_WITH_ATTACHED_ARGUMENT = ("-u", "-C", "-S", "-a")
ENV_FLAG_OPTIONS = {
    "-i",
    "--ignore-environment",
    "-0",
    "--null",
    "-v",
    "--debug",
    "--ignore-signal",
    "--default-signal",
    "--block-signal",
    "--list-signal-handling",
}
NETWORK_LITERAL_COMMANDS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "wc"}
INLINE_SCRIPT_PERMISSION = "inline_script"
ENV_PREFIX = "CODING_TOOLS_MCP"
RUNTIME_ROOT_DIR_NAME = "coding-tools-mcp"
DEFAULT_CONFIG_DIR_NAME = ".coding-tools-mcp"
UPSTREAM_CONFIG_FILENAME = "mcp-servers.json"
SERVER_SETTINGS_FILENAME = "server-settings.json"
TRANSCRIPT_DB_FILENAME = "transcripts.sqlite3"
RECENT_TOOL_TRACE_LIMIT = 100
STARTUP_SETTING_KEYS = {
    "host",
    "port",
    "workspace",
    "auth_token",
    "admin_token",
    "oauth_password",
    "oauth_server_url",
    "oauth_token_secret",
    "permission_mode",
    "tool_profile",
    "shell_env_inherit",
    "allowed_origins",
}
SPECIAL_DEVICE_PATHS = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")
DNS_RESOLVER_READ_ROOTS = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/gai.conf",
    "/etc/protocols",
    "/etc/services",
    "/run/systemd/resolve",
    "/run/resolvconf",
)
TOOLCHAIN_READ_ROOTS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc/alternatives",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
    "/etc/localtime",
    "/etc/npmrc",
    "/usr/local/sdkman/candidates",
)
GIT_READ_ROOTS = (
    "/etc/gitconfig",
    "/etc/gitconfig.d",
)
SYSTEM_PATH_ROOT_PREFIXES = (
    "/bin",
    "/sbin",
    "/usr",
    "/lib",
    "/lib64",
    "/etc/alternatives",
    "/usr/local/sdkman/candidates",
)
ECOSYSTEM_CACHE_ENV_NAMES = {
    "MAVEN_USER_HOME",
    "GRADLE_USER_HOME",
    "NPM_CONFIG_CACHE",
    "npm_config_cache",
    "PIP_CACHE_DIR",
    "GOCACHE",
    "GOMODCACHE",
    "CARGO_HOME",
    "RUSTUP_HOME",
}

OAUTH_CODE_TTL_SECONDS = 300
OAUTH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30
OAUTH_MAX_BODY_BYTES = 8_192


@dataclass(frozen=True)
class ShellEnvPolicy:
    inherit: str = "core"
    include_only: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    set: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimePolicy:
    permission_mode: str
    shell_env_policy: ShellEnvPolicy
    allow_network: bool


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str | None
    client_secret: str | None
    password: str
    server_url: str | None
    token_secret: bytes
    token_ttl: int = OAUTH_TOKEN_TTL_SECONDS
    admin_scope: str = "admin"


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


def _create_oauth_token(cfg: OAuthConfig, server_url: str, *, scope: str = "mcp") -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": server_url, "aud": server_url, "iat": now, "exp": now + cfg.token_ttl, "scope": scope},
        cfg.token_secret,
        algorithm="HS256",
    )


def _decode_oauth_token(token: str, cfg: OAuthConfig, server_url: str) -> dict[str, Any] | None:
    try:
        decoded = jwt.decode(token, cfg.token_secret, algorithms=["HS256"], audience=server_url, issuer=server_url)
    except jwt.PyJWTError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _validate_oauth_token(token: str, cfg: OAuthConfig, server_url: str) -> bool:
    return _decode_oauth_token(token, cfg, server_url) is not None


def _oauth_scope_allowed(scope: str, cfg: OAuthConfig) -> bool:
    requested = {part for part in scope.split() if part}
    return requested.issubset({"mcp", cfg.admin_scope})


def _oauth_client_id_allowed(client_id: str, cfg: OAuthConfig) -> bool:
    if not client_id:
        return False
    if cfg.client_id is None:
        return True
    return secrets.compare_digest(client_id, cfg.client_id)


def _oauth_token_auth_methods(cfg: OAuthConfig) -> list[str]:
    if cfg.client_secret is None:
        return ["none"]
    return ["client_secret_post", "client_secret_basic"]


def _http_base_for_bind_host(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _first_header_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def _forwarded_header_param(value: str | None, name: str) -> str:
    first = _first_header_value(value)
    for part in first.split(";"):
        key, sep, raw = part.strip().partition("=")
        if sep and key.lower() == name:
            return raw.strip().strip('"')
    return ""


def _safe_external_host(host: str) -> str:
    host = host.strip()
    if not host or any(ch in host for ch in "\r\n/\\"):
        return ""
    return host


def env_pattern_matches(name: str, patterns: tuple[str, ...]) -> bool:
    upper_name = name.upper()
    return any(fnmatch.fnmatchcase(upper_name, pattern.upper()) for pattern in patterns)


def is_risky_env_name(name: str) -> bool:
    upper = name.upper()
    return upper in RISKY_ENV_NAMES or upper.startswith("DYLD_")


def is_filtered_env_var(name: str, value: str) -> bool:
    return bool(SENSITIVE_ENV_RE.search(name) or is_risky_env_name(name) or SENSITIVE_VALUE_RE.search(value))


def is_core_command_env_name(name: str) -> bool:
    upper = name.upper()
    if os.name == "nt":
        return upper in WINDOWS_CORE_ENV_NAMES
    return upper in POSIX_CORE_ENV_NAMES or upper.startswith("LC_")


def canonical_command_env_name(name: str) -> str:
    if os.name == "nt":
        return WINDOWS_CANONICAL_ENV_NAMES.get(name.upper(), name)
    return name


def split_env_patterns(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_shell_env_set(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ENV_PREFIX}_SHELL_ENV_SET must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{ENV_PREFIX}_SHELL_ENV_SET must be a JSON object")
    return {str(key): str(item) for key, item in parsed.items()}


def truthy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def falsy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "off"}


def env_int(name: str, fallback: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _settings_text(settings: dict[str, Any], key: str) -> str | None:
    value = settings.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def read_server_settings(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_server_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_oauth_token_secret(startup_settings: dict[str, Any], settings_path: Path | None) -> bytes:
    raw_secret = os.environ.get(f"{ENV_PREFIX}_OAUTH_TOKEN_SECRET") or _settings_text(
        startup_settings, "oauth_token_secret"
    ) or ""
    if raw_secret:
        try:
            return bytes.fromhex(raw_secret)
        except ValueError as exc:
            raise ValueError(
                f"{ENV_PREFIX}_OAUTH_TOKEN_SECRET or oauth_token_secret setting must be hex-encoded bytes."
            ) from exc
    token_secret = secrets.token_bytes(32)
    if settings_path is None:
        return token_secret
    updated_settings = dict(startup_settings)
    updated_settings["oauth_token_secret"] = token_secret.hex()
    try:
        write_server_settings(settings_path, updated_settings)
    except OSError as exc:
        print(
            f"WARNING: generated OAuth token secret could not be saved to {settings_path}: {exc}",
            file=sys.stderr,
        )
    else:
        startup_settings.clear()
        startup_settings.update(updated_settings)
        print(
            f"Generated and saved OAuth token secret to {settings_path}; OAuth tokens can survive restarts.",
            file=sys.stderr,
        )
    return token_secret


def effective_workspace_path(args: argparse.Namespace, settings: dict[str, Any] | None = None) -> Path:
    settings = settings or {}
    raw = (
        getattr(args, "workspace", None)
        or os.environ.get(f"{ENV_PREFIX}_WORKSPACE")
        or _settings_text(settings, "workspace")
        or os.getcwd()
    )
    return Path(str(raw)).expanduser()


def effective_host(args: argparse.Namespace, settings: dict[str, Any] | None = None) -> str:
    settings = settings or {}
    return str(
        getattr(args, "host", None)
        or os.environ.get(f"{ENV_PREFIX}_HOST")
        or _settings_text(settings, "host")
        or "127.0.0.1"
    )


def effective_port(args: argparse.Namespace, settings: dict[str, Any] | None = None) -> int:
    settings = settings or {}
    arg_port = _coerce_optional_int(getattr(args, "port", None))
    if arg_port is not None:
        return arg_port
    env_port = _coerce_optional_int(os.environ.get(f"{ENV_PREFIX}_PORT"))
    if env_port is not None:
        return env_port
    setting_port = _coerce_optional_int(settings.get("port"))
    return setting_port if setting_port is not None else 8000


def resolve_config_paths(args: argparse.Namespace, workspace: Path) -> tuple[Path, Path, Path]:
    upstream_config = getattr(args, "upstream_config", None) or os.environ.get(f"{ENV_PREFIX}_UPSTREAM_CONFIG") or None
    raw_config_dir = getattr(args, "config_dir", None) or os.environ.get(f"{ENV_PREFIX}_CONFIG_DIR") or None
    if raw_config_dir:
        config_dir = Path(str(raw_config_dir)).expanduser()
    elif upstream_config:
        config_dir = Path(str(upstream_config)).expanduser().parent
    else:
        config_dir = workspace / DEFAULT_CONFIG_DIR_NAME
    upstream_path = Path(str(upstream_config)).expanduser() if upstream_config else config_dir / UPSTREAM_CONFIG_FILENAME
    return config_dir, upstream_path, config_dir / SERVER_SETTINGS_FILENAME


def apply_startup_settings(args: argparse.Namespace, settings: dict[str, Any]) -> None:
    if getattr(args, "permission_mode", None) is None and _settings_text(settings, "permission_mode"):
        args.permission_mode = _settings_text(settings, "permission_mode")
    if getattr(args, "tool_profile", None) is None and _settings_text(settings, "tool_profile"):
        args.tool_profile = _settings_text(settings, "tool_profile")
    if getattr(args, "shell_env_inherit", None) is None and _settings_text(settings, "shell_env_inherit"):
        args.shell_env_inherit = _settings_text(settings, "shell_env_inherit")


def configured_runtime_root() -> Path | None:
    configured = os.environ.get(f"{ENV_PREFIX}_RUNTIME_ROOT") or ""
    if not configured.strip():
        return None
    return Path(configured).expanduser()


def runtime_parent_root() -> Path:
    return configured_runtime_root() or Path(tempfile.gettempdir()) / RUNTIME_ROOT_DIR_NAME


def runtime_parent_fallback_root() -> Path | None:
    if configured_runtime_root() is not None:
        return None
    if os.name == "nt":
        return None
    fallback = Path("/tmp") / RUNTIME_ROOT_DIR_NAME
    if fallback == runtime_parent_root():
        return None
    return fallback


def workspace_runtime_hash(workspace: Path) -> str:
    resolved = workspace.expanduser().resolve(strict=False)
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]


def runtime_dir_for_workspace(workspace: Path, instance_id: str) -> Path:
    root = runtime_parent_root()
    try:
        root_in_workspace = is_relative_to(root.resolve(strict=False), workspace.expanduser().resolve(strict=False))
    except OSError:
        root_in_workspace = False
    if root_in_workspace:
        if configured_runtime_root() is not None:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"{ENV_PREFIX}_RUNTIME_ROOT must be outside the configured workspace.",
                category="validation",
            )
        root = runtime_parent_fallback_root() or root
    return root / workspace_runtime_hash(workspace) / instance_id


def fallback_runtime_dir_for_workspace(workspace: Path, instance_id: str) -> Path | None:
    fallback = runtime_parent_fallback_root()
    if fallback is None:
        return None
    return fallback / workspace_runtime_hash(workspace) / instance_id


def shell_env_policy_from_args(args: argparse.Namespace) -> ShellEnvPolicy:
    raw_inherit = args.shell_env_inherit or os.environ.get(f"{ENV_PREFIX}_SHELL_ENV_INHERIT") or "core"
    inherit = raw_inherit.strip().lower()
    if inherit not in SHELL_ENV_INHERIT_CHOICES:
        supported = ", ".join(SHELL_ENV_INHERIT_CHOICES)
        raise ValueError(f"shell env inherit must be one of: {supported}")
    return ShellEnvPolicy(
        inherit=inherit,
        include_only=split_env_patterns(os.environ.get(f"{ENV_PREFIX}_SHELL_ENV_INCLUDE_ONLY")),
        exclude=split_env_patterns(os.environ.get(f"{ENV_PREFIX}_SHELL_ENV_EXCLUDE")),
        set=parse_shell_env_set(os.environ.get(f"{ENV_PREFIX}_SHELL_ENV_SET")),
    )


def permission_mode_from_args(args: argparse.Namespace) -> str:
    skip_all = bool(getattr(args, "dangerously_skip_all_permissions", False)) or truthy_env(
        os.environ.get(f"{ENV_PREFIX}_DANGEROUSLY_SKIP_ALL_PERMISSIONS")
    )
    raw_mode = (
        getattr(args, "permission_mode", None)
        or os.environ.get(f"{ENV_PREFIX}_PERMISSION_MODE")
        or ("dangerous" if skip_all else "safe")
    )
    mode = raw_mode.strip().lower()
    if mode not in PERMISSION_MODE_CHOICES:
        supported = ", ".join(PERMISSION_MODE_CHOICES)
        raise ValueError(f"permission mode must be one of: {supported}")
    return "dangerous" if skip_all else mode


def runtime_policy_from_args(args: argparse.Namespace) -> RuntimePolicy:
    permission_mode = permission_mode_from_args(args)
    allow_network = (
        PERMISSION_MODE_CAPABILITIES[permission_mode].network
        or bool(getattr(args, "allow_network", False))
        or truthy_env(os.environ.get(f"{ENV_PREFIX}_ALLOW_NETWORK"))
    )
    return RuntimePolicy(
        permission_mode=permission_mode,
        shell_env_policy=shell_env_policy_from_args(args),
        allow_network=allow_network,
    )


TOOL_PROFILE_CHOICES = ("full", "read-only", "compat-readonly-all")


@dataclass(frozen=True)
class ToolSpec:
    """Single source of truth for one tool: title, description, annotation hints, profile membership.

    Handler methods on Runtime are named exactly after the tool. Input schemas live in
    input_schemas(), keyed by the same names.
    """

    title: str
    description: str
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    in_read_only_profile: bool = False


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "server_info": ToolSpec(
        title="Server info",
        description="Return server, workspace, auth, profile, and exposed-tool metadata.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "workspace_identity": ToolSpec(
        title="Workspace identity",
        description="Return stable workspace identity, host, platform, and git state for remote editing confirmation.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "check_exec_environment": ToolSpec(
        title="Check exec environment",
        description="Return lightweight exec_command sandbox and environment status known to the server.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "get_default_cwd": ToolSpec(
        title="Get default cwd",
        description="Return the current default cwd inside the workspace.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "set_default_cwd": ToolSpec(
        title="Set default cwd",
        description="Set the default cwd for relative tool paths inside the workspace.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "read_file": ToolSpec(
        title="Read file",
        description="Read a UTF-8 text file slice inside the configured workspace.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "file_stat": ToolSpec(
        title="File stat",
        description="Return path identity, existence, size, mtime, and sha256 version metadata.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "list_dir": ToolSpec(
        title="List directory",
        description="List directory entries inside the configured workspace.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "list_files": ToolSpec(
        title="List files",
        description="List workspace files using glob filters.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "search_text": ToolSpec(
        title="Search text",
        description="Search UTF-8 workspace files for text or regex matches.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "apply_patch": ToolSpec(
        title="Apply patch",
        description="Apply a patch envelope transactionally inside the workspace.",
        destructive=True,
    ),
    "restore_patch_checkpoint": ToolSpec(
        title="Restore patch checkpoint",
        description="Restore the before-image captured by a previous apply_patch checkpoint.",
        destructive=True,
    ),
    "exec_command": ToolSpec(
        title="Execute command",
        description="Run a bounded command in the workspace under runtime policy.",
        destructive=True,
        open_world=True,
    ),
    "write_stdin": ToolSpec(
        title="Write stdin",
        description="Write characters to a server-managed running command session.",
    ),
    "command_status": ToolSpec(
        title="Command status",
        description="Return status and retained output for a server-managed command session without writing stdin.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "kill_session": ToolSpec(
        title="Kill session",
        description="Terminate a server-managed running command session.",
        destructive=True,
    ),
    "git_status": ToolSpec(
        title="Git status",
        description="Return git working tree status for the workspace.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "git_diff": ToolSpec(
        title="Git diff",
        description="Return unified git diff for workspace changes.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "git_log": ToolSpec(
        title="Git log",
        description="Return recent git commits with bounded structured metadata.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "git_show": ToolSpec(
        title="Git show",
        description="Return bounded git show output for a revision.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "git_blame": ToolSpec(
        title="Git blame",
        description="Return bounded git blame metadata for a workspace file.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "request_permissions": ToolSpec(
        title="Request permissions",
        description="Request a scoped permission grant for dangerous runtime operations.",
        read_only=True,
    ),
    "view_image": ToolSpec(
        title="View image",
        description="Return a workspace image as MCP image content.",
        read_only=True,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "record_chat_transcript": ToolSpec(
        title="Record chat transcript",
        description="Append user, assistant, system, or tool chat messages to a host-side persistent transcript for later Markdown export.",
        read_only=False,
        destructive=False,
        open_world=False,
    ),
    "record_chat_message": ToolSpec(
        title="Record chat message",
        description="Append one flat user, assistant, system, or tool chat message to a host-side persistent transcript.",
        read_only=False,
        destructive=False,
        open_world=False,
    ),
    "recall_chat_context": ToolSpec(
        title="Recall chat context",
        description="Return recently persisted messages and Markdown context for one chat conversation.",
        read_only=True,
        destructive=False,
        open_world=False,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "list_chat_projects": ToolSpec(
        title="List chat projects",
        description="List persisted chat projects for quick WebUI grouping and agent indexing.",
        read_only=True,
        destructive=False,
        open_world=False,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "list_chat_conversations": ToolSpec(
        title="List chat conversations",
        description="List persisted chat conversations so clients can select a conversation before recalling its context.",
        read_only=True,
        destructive=False,
        open_world=False,
        idempotent=True,
        in_read_only_profile=True,
    ),
    "recall_project_context": ToolSpec(
        title="Recall project context",
        description="Return persisted conversations and Markdown context for one chat project.",
        read_only=True,
        destructive=False,
        open_world=False,
        idempotent=True,
        in_read_only_profile=True,
    ),
}

FULL_TOOL_NAMES = tuple(TOOL_REGISTRY)
READ_ONLY_TOOL_NAMES = tuple(name for name, spec in TOOL_REGISTRY.items() if spec.in_read_only_profile)

ADMIN_TOOL_REGISTRY: dict[str, ToolSpec] = {
    "mcp_catalog_list": ToolSpec(
        title="MCP catalog list",
        description="List managed upstream MCP server configurations and runtime status.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_template_list": ToolSpec(
        title="MCP template list",
        description="List built-in upstream MCP server installation templates.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_template_render": ToolSpec(
        title="MCP template render",
        description="Render a built-in upstream MCP template and return its dry-run install plan.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_server_plan": ToolSpec(
        title="MCP server plan",
        description="Validate an upstream MCP server configuration and return a dry-run install or update plan.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_server_install": ToolSpec(
        title="MCP server install",
        description="Install an upstream MCP server from structured configuration; writes only when apply is true.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_server_update": ToolSpec(
        title="MCP server update",
        description="Update an installed upstream MCP server configuration; writes only when apply is true.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_server_enable": ToolSpec(
        title="MCP server enable",
        description="Enable an installed upstream MCP server; writes only when apply is true.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_server_disable": ToolSpec(
        title="MCP server disable",
        description="Disable an installed upstream MCP server; writes only when apply is true.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_server_remove": ToolSpec(
        title="MCP server remove",
        description="Remove an installed upstream MCP server configuration; writes only when apply is true.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_server_reload": ToolSpec(
        title="MCP server reload",
        description="Reload upstream MCP servers from the managed configuration.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_server_health": ToolSpec(
        title="MCP server health",
        description="Return runtime health for one or all managed upstream MCP servers.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_server_start": ToolSpec(
        title="MCP server start",
        description="Start or restart one configured upstream MCP server without editing config.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_server_stop": ToolSpec(
        title="MCP server stop",
        description="Stop one running upstream MCP server without editing config.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_server_logs": ToolSpec(
        title="MCP server logs",
        description="Return recent stderr logs captured from managed upstream MCP servers.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_secret_set": ToolSpec(
        title="MCP secret set",
        description="Store a secret in the encrypted local MCP secret vault.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_secret_list": ToolSpec(
        title="MCP secret list",
        description="List secret names in the local MCP secret vault without revealing values.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_secret_delete": ToolSpec(
        title="MCP secret delete",
        description="Delete a secret from the encrypted local MCP secret vault.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_transcript_sessions": ToolSpec(
        title="MCP transcript sessions",
        description="List persisted MCP HTTP sessions recorded on the host.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_transcript_export": ToolSpec(
        title="MCP transcript export",
        description="Export persisted MCP session activity to readable Markdown.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_codex_sessions_preview": ToolSpec(
        title="MCP Codex sessions preview",
        description="Scan opt-in Codex session roots and return importable conversation candidates without message content.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_codex_sessions_import": ToolSpec(
        title="MCP Codex sessions import",
        description="Import selected Codex session candidates into the persistent chat transcript store.",
        read_only=False,
        destructive=False,
        open_world=False,
    ),
    "mcp_codex_sessions_sync": ToolSpec(
        title="MCP Codex sessions sync",
        description="Re-read selected or all Codex session candidates and append any new messages to persisted chat transcripts.",
        read_only=False,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_projects": ToolSpec(
        title="MCP chat projects",
        description="List persisted chat projects submitted by MCP clients or agents.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_conversations": ToolSpec(
        title="MCP chat conversations",
        description="List persisted chat conversations submitted by MCP clients or agents.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_messages": ToolSpec(
        title="MCP chat messages",
        description="List persisted messages for one chat conversation.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_context": ToolSpec(
        title="MCP chat context",
        description="List persisted restore-context entries for one chat conversation.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_record_context": ToolSpec(
        title="MCP chat record context",
        description="Create one persisted restore-context entry for a chat conversation.",
        read_only=False,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_update_context": ToolSpec(
        title="MCP chat update context",
        description="Edit one persisted restore-context entry.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_chat_delete_context": ToolSpec(
        title="MCP chat delete context",
        description="Delete one persisted restore-context entry.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_chat_recall": ToolSpec(
        title="MCP chat recall",
        description="Build a recovery payload from persisted chat messages and restore-context entries.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_project_recall": ToolSpec(
        title="MCP chat project recall",
        description="Build a recovery payload from persisted project-scoped chat records.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_export": ToolSpec(
        title="MCP chat export",
        description="Export persisted chat conversation text to readable Markdown.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_context_export": ToolSpec(
        title="MCP chat context export",
        description="Export persisted restore-context entries to readable Markdown.",
        read_only=True,
        destructive=False,
        open_world=False,
    ),
    "mcp_chat_update_message": ToolSpec(
        title="MCP chat update message",
        description="Edit one persisted chat message.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_chat_delete_message": ToolSpec(
        title="MCP chat delete message",
        description="Delete one persisted chat message.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_chat_delete_conversation": ToolSpec(
        title="MCP chat delete conversation",
        description="Delete all messages in one persisted chat conversation.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_chat_clear": ToolSpec(
        title="MCP chat clear",
        description="Delete all persisted chat conversations and messages.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
    "mcp_chat_merge": ToolSpec(
        title="MCP chat merge",
        description="Merge multiple persisted chat conversations into one conversation id.",
        read_only=False,
        destructive=True,
        open_world=False,
    ),
}

LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14
LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15


class ToolFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "runtime",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.retryable = retryable
        self.details = details or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_response_payload(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def normalize_allowed_origin(origin: Any) -> str | None:
    text = str(origin or "").strip().rstrip("/")
    if not text or text == "*" or text == "null":
        return None
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return None
    if parsed.username or parsed.password or parsed.hostname is None:
        return None
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def parse_allowed_origins(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items: list[Any] = re.split(r"[\s,]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    origins: list[str] = []
    for item in raw_items:
        origin = normalize_allowed_origin(item)
        if origin and origin not in origins:
            origins.append(origin)
    return tuple(origins)


def combine_allowed_origins(*values: Any) -> tuple[str, ...]:
    origins: list[str] = []
    for value in values:
        for origin in parse_allowed_origins(value):
            if origin not in origins:
                origins.append(origin)
    return tuple(origins)


def is_allowed_origin(origin: str, *, auth_enabled: bool = False, allowed_origins: tuple[str, ...] = ()) -> bool:
    if normalize_allowed_origin(origin) in allowed_origins:
        return True
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if auth_enabled:
        return parsed.hostname is not None
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def is_loopback_bind_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", ""}


@dataclass(frozen=True)
class TextTruncation:
    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int

    def metadata(self, *, prefix: str = "") -> dict[str, Any]:
        key = f"{prefix}_" if prefix else ""
        return {
            f"{key}truncated_by": self.truncated_by,
            f"{key}total_lines": self.total_lines,
            f"{key}total_bytes": self.total_bytes,
            f"{key}output_lines": self.output_lines,
            f"{key}output_bytes": self.output_bytes,
            f"{key}last_line_partial": self.last_line_partial,
            f"{key}first_line_exceeds_limit": self.first_line_exceeds_limit,
        }


def truncate_bytes(data: bytes, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        limit = 1
    truncated = len(data) > limit
    if truncated:
        marker = b"\n... output truncated ...\n"
        if limit > len(marker) + 2:
            remaining = limit - len(marker)
            head = max(1, remaining // 2)
            tail = max(1, remaining - head)
            data = data[:head] + marker + data[-tail:]
        else:
            data = data[:limit]
    return data.decode("utf-8", errors="replace"), truncated


def truncate_text_head(text: str, *, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = 50 * 1024) -> TextTruncation:
    if max_lines <= 0:
        max_lines = 1
    if max_bytes <= 0:
        max_bytes = 1
    total_bytes = len(text.encode("utf-8"))
    lines = text.split("\n")
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TextTruncation(text, False, None, total_lines, total_bytes, total_lines, total_bytes, False, False, max_lines, max_bytes)

    first_line_bytes = len(lines[0].encode("utf-8")) if lines else 0
    if first_line_bytes > max_bytes:
        prefix = truncate_string_to_bytes_from_start(lines[0], max_bytes)
        return TextTruncation(
            prefix,
            True,
            "bytes",
            total_lines,
            total_bytes,
            1 if prefix else 0,
            len(prefix.encode("utf-8")),
            False,
            True,
            max_lines,
            max_bytes,
        )

    output: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    for index, line in enumerate(lines):
        if len(output) >= max_lines:
            truncated_by = "lines"
            break
        line_bytes = len(line.encode("utf-8")) + (1 if index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output.append(line)
        output_bytes += line_bytes
    content = "\n".join(output)
    return TextTruncation(
        content,
        True,
        truncated_by,
        total_lines,
        total_bytes,
        len(output),
        len(content.encode("utf-8")),
        False,
        False,
        max_lines,
        max_bytes,
    )


def truncate_text_tail(text: str, *, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = 50 * 1024) -> TextTruncation:
    if max_lines <= 0:
        max_lines = 1
    if max_bytes <= 0:
        max_bytes = 1
    total_bytes = len(text.encode("utf-8"))
    lines = text.split("\n")
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TextTruncation(text, False, None, total_lines, total_bytes, total_lines, total_bytes, False, False, max_lines, max_bytes)

    candidate_lines = lines[:-1] if lines and lines[-1] == "" else lines
    output: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    last_line_partial = False
    for reverse_index, line in enumerate(reversed(candidate_lines)):
        if len(output) >= max_lines:
            truncated_by = "lines"
            break
        line_bytes = len(line.encode("utf-8")) + (1 if reverse_index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output:
                partial = truncate_string_to_bytes_from_end(line, max_bytes)
                output.insert(0, partial)
                last_line_partial = True
            break
        output.insert(0, line)
        output_bytes += line_bytes
    content = "\n".join(output)
    return TextTruncation(
        content,
        True,
        truncated_by,
        total_lines,
        total_bytes,
        len(output),
        len(content.encode("utf-8")),
        last_line_partial,
        False,
        max_lines,
        max_bytes,
    )


def truncate_string_to_bytes_from_start(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    end = max(0, min(max_bytes, len(data)))
    while end > 0 and end < len(data) and (data[end] & 0xC0) == 0x80:
        end -= 1
    return data[:end].decode("utf-8", errors="replace")


def truncate_string_to_bytes_from_end(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    start = len(data) - max_bytes
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return data[start:].decode("utf-8", errors="replace")


def truncate_line_chars(line: str, max_chars: int = GREP_MAX_LINE_CHARS) -> tuple[str, bool]:
    if len(line) <= max_chars:
        return line, False
    suffix = " ... [truncated]"
    keep = max(0, max_chars - len(suffix))
    return line[:keep] + suffix, True


def truncate_output_bytes_tail(data: bytes, limit: int) -> TextTruncation:
    text = data.decode("utf-8", errors="replace")
    return truncate_text_tail(text, max_lines=DEFAULT_MAX_LINES, max_bytes=limit)


def strip_bom(text: str) -> tuple[str, str]:
    return ("\ufeff", text[1:]) if text.startswith("\ufeff") else ("", text)


def detect_line_ending(text: str) -> str:
    crlf = text.find("\r\n")
    lf = text.find("\n")
    if lf < 0:
        return "\n"
    if crlf < 0:
        return "\n"
    return "\r\n" if crlf <= lf else "\n"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def read_text_preserve_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def normalize_rel_display(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix()
    text = rel.as_posix()
    return "." if text == "" else text


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def utc_isoformat(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def signal_name(signum: int | signal.Signals) -> str:
    try:
        return signal.Signals(int(signum)).name
    except ValueError:
        return str(int(signum))


def file_version_payload(
    path: Path,
    root: Path,
    *,
    display: str | None = None,
    data: bytes | None = None,
    stat_result: os.stat_result | None = None,
) -> dict[str, Any]:
    display_path = display or normalize_rel_display(path, root)
    try:
        stat_payload = stat_result or path.stat()
    except FileNotFoundError:
        return {
            "path": display_path,
            "exists": False,
            "is_file": False,
            "is_dir": False,
            "size_bytes": None,
            "mtime_ns": None,
            "mtime": None,
            "sha256": None,
        }
    is_file = path.is_file()
    content = data if data is not None else (path.read_bytes() if is_file else None)
    return {
        "path": display_path,
        "exists": True,
        "is_file": is_file,
        "is_dir": path.is_dir(),
        "size_bytes": stat_payload.st_size,
        "mtime_ns": stat_payload.st_mtime_ns,
        "mtime": utc_isoformat(stat_payload.st_mtime),
        "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
    }


def git_workspace_summary(root: Path) -> dict[str, Any]:
    git = shutil.which("git")
    if not git:
        return {"available": False, "reason": "git_not_found"}

    def run_git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [git, "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )

    try:
        inside = run_git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return {"available": True, "inside_work_tree": False}
        top = run_git("rev-parse", "--show-toplevel")
        branch = run_git("rev-parse", "--abbrev-ref", "HEAD")
        commit = run_git("rev-parse", "HEAD")
        status = run_git("status", "--porcelain=v1")
    except (OSError, subprocess.SubprocessError):
        return {"available": True, "inside_work_tree": None, "error": "git_query_failed"}
    return {
        "available": True,
        "inside_work_tree": True,
        "root": top.stdout.strip() if top.returncode == 0 else None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def terminate_process_group(process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
    if not hasattr(os, "killpg"):
        if os.name == "nt" and signum != FORCE_KILL_SIGNAL:
            event = getattr(signal, "CTRL_BREAK_EVENT", None)
            if event is not None:
                try:
                    process.send_signal(event)
                    process.wait(timeout=1)
                    return
                except Exception:
                    pass
        try:
            if signum == FORCE_KILL_SIGNAL:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=1)
        except Exception:
            process.kill()
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, FORCE_KILL_SIGNAL)
        except Exception:
            process.kill()


def landlock_unavailable_warning(exc: ToolFailure) -> str:
    reason = ""
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details.get("reason"):
        reason = f" ({details['reason']})"
    return (
        "Linux Landlock filesystem confinement is unavailable on this host"
        f"{reason}; exec_command ran with policy checks only. "
        "Use an external sandbox before running untrusted commands."
    )


def landlock_status_payload() -> dict[str, Any]:
    try:
        version = landlock_abi_version()
    except ToolFailure as exc:
        return {
            "available": False,
            "abi_version": None,
            "reason": exc.message,
            "details": exc.details,
        }
    return {
        "available": True,
        "abi_version": version,
    }


def truncate_evidence(text: str, limit: int = 240) -> str:
    text = " ".join(text.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def diagnostic(
    code: str,
    *,
    evidence: str = "",
    severity: str = "error",
    suggested_fix: str | None = None,
    suggested_next_command: str | None = None,
    suggested_server_flag: str | None = None,
) -> dict[str, str]:
    item = {"code": code, "severity": severity}
    if evidence:
        item["evidence"] = truncate_evidence(evidence)
    if suggested_fix:
        item["suggested_fix"] = suggested_fix
    if suggested_next_command:
        item["suggested_next_command"] = suggested_next_command
    if suggested_server_flag:
        item["suggested_server_flag"] = suggested_server_flag
    return item


PERMISSION_FAILURE_DIAGNOSTICS: dict[str, dict[str, str]] = {
    "network": {
        "code": "NETWORK_PERMISSION_REQUIRED",
        "suggested_fix": "Restart the server with --permission-mode trusted or --allow-network.",
        "suggested_server_flag": "--permission-mode trusted",
    },
    "shell_expansion": {
        "code": "SHELL_EXPANSION_PERMISSION_REQUIRED",
        "suggested_fix": "Restart the server with --permission-mode trusted for local development shell expansion.",
        "suggested_server_flag": "--permission-mode trusted",
    },
    INLINE_SCRIPT_PERMISSION: {
        "code": "INLINE_SCRIPT_PERMISSION_REQUIRED",
        "suggested_fix": "Restart the server with --permission-mode trusted for local development inline scripts.",
        "suggested_server_flag": "--permission-mode trusted",
    },
    "sensitive_env": {
        "code": "SECRET_ENV_REJECTED",
        "suggested_fix": "Remove secret-looking or loader/startup environment variables from exec_command env.",
    },
}


def permission_failure_diagnostics(exc: ToolFailure) -> list[dict[str, str]]:
    spec = PERMISSION_FAILURE_DIAGNOSTICS.get(str(exc.details.get("permission") or ""))
    if spec is None:
        return []
    return [
        diagnostic(
            spec["code"],
            evidence=exc.message,
            suggested_fix=spec["suggested_fix"],
            suggested_server_flag=spec.get("suggested_server_flag"),
        )
    ]


def exec_output_diagnostics(payload: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    stdout = str(payload.get("stdout", ""))
    stderr = str(payload.get("stderr", ""))
    combined = "\n".join(part for part in (stderr, stdout) if part)
    lower = combined.lower()
    if payload.get("timed_out") or payload.get("status") == "timeout":
        diagnostics.append(
            diagnostic(
                "COMMAND_TIMED_OUT",
                evidence="command timed out",
                suggested_fix="Increase timeout_ms only for trusted workloads, or run a narrower command.",
            )
        )
    if payload.get("truncated") or payload.get("stdout_truncated") or payload.get("stderr_truncated"):
        diagnostics.append(
            diagnostic(
                "OUTPUT_TRUNCATED",
                evidence="stdout/stderr exceeded max_output_bytes or session buffer limits",
                severity="warning",
                suggested_fix="Increase max_output_bytes or poll the running session more frequently.",
            )
        )
    if "/dev/null" in lower and "permission denied" in lower:
        diagnostics.append(
            diagnostic(
                "DEV_NULL_DENIED",
                evidence=combined,
                suggested_fix="Landlock special device rules should include WRITE_FILE, TRUNCATE, and IOCTL_DEV for /dev/null.",
            )
        )
    if "could not resolve host" in lower or "temporary failure in name resolution" in lower or "name or service not known" in lower:
        diagnostics.append(
            diagnostic(
                "DNS_RESOLUTION_FAILED",
                evidence=combined,
                suggested_next_command="cat /etc/resolv.conf && getent hosts repo.maven.apache.org",
            )
        )
    if "java.security" in lower and ("permission denied" in lower or "could not" in lower or "error loading" in lower):
        diagnostics.append(
            diagnostic(
                "JDK_SECURITY_CONFIG_BLOCKED",
                evidence=combined,
                suggested_fix="Ensure the JDK security configuration path is included in Landlock read roots.",
            )
        )
    if "tmpdir" in lower and ("permission denied" in lower or "not writable" in lower or "cannot write" in lower):
        diagnostics.append(
            diagnostic(
                "TMPDIR_NOT_WRITABLE",
                evidence=combined,
                suggested_next_command="printf ok > \"$TMPDIR/coding-tools-write-test\"",
            )
        )
    home_error_terms = ("permission denied", "not writable", "cannot write", "eacces")
    home_path_error = any(
        re.search(r"(?:\.coding-tools/home|/home(?:/|[\"'\s]|$))", line)
        and any(term in line for term in home_error_terms)
        for line in lower.splitlines()
    )
    home_error = (
        "$home" in lower
        or "home=" in lower
        or re.search(r"\bhome directory\b", lower)
        or "cannot write to home" in lower
        or re.search(r"not writable:\s+\S*home", lower)
        or re.search(r"permission denied:\s+\S*home", lower)
        or home_path_error
    )
    if home_error and any(term in lower for term in home_error_terms):
        diagnostics.append(
            diagnostic(
                "HOME_NOT_WRITABLE",
                evidence=combined,
                suggested_next_command="printf ok > \"$HOME/coding-tools-write-test\"",
            )
        )
    if "permission denied" in lower and any(root in combined for root in ("/usr", "/bin", "/lib", "/etc", "/usr/local/sdkman")):
        diagnostics.append(
            diagnostic(
                "LANDLOCK_READ_ROOT_BLOCKED",
                evidence=combined,
                suggested_fix="Add the missing toolchain path to CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS or the default read roots.",
            )
        )
    if payload.get("exit_code") == 127 or "command not found" in lower or ("not found" in lower and "exec" in lower):
        diagnostics.append(
            diagnostic(
                "EXECUTABLE_NOT_FOUND",
                evidence=combined or "exit_code=127",
                suggested_next_command="command -v <executable>",
            )
        )
    return diagnostics


def process_group_popen_kwargs() -> dict[str, Any]:
    if hasattr(os, "setsid"):
        return {"start_new_session": True}
    if os.name == "nt":
        creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if creation_flag:
            return {"creationflags": creation_flag}
    return {}


@dataclass
class ResolvedPath:
    display: str
    path: Path
    existed: bool


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ToolFailure("INVALID_ARGUMENT", "Workspace root must be a directory.", category="validation")
        unsafe_roots = {"/"}
        try:
            unsafe_roots.add(str(Path.home().resolve()))
        except RuntimeError:
            pass
        if str(self.root) in unsafe_roots:
            raise ToolFailure("INVALID_ARGUMENT", "Unsafe workspace root rejected.", category="security")

    def _validate_path_text(self, raw_path: str) -> str:
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolFailure("INVALID_ARGUMENT", "Path must be a non-empty string.", category="validation")
        if "\x00" in raw_path:
            raise ToolFailure("INVALID_ARGUMENT", "Path contains a NUL byte.", category="validation")
        return raw_path

    def _relative_candidate(self, base: Path, raw_path: str) -> Path:
        pure = PurePosixPath(raw_path.replace("\\", "/"))
        if any(part == ".." for part in pure.parts):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
        return base.joinpath(*pure.parts)

    def _candidate_path(self, base: Path, raw_path: str) -> Path:
        raw_path = self._validate_path_text(raw_path)
        native = Path(raw_path).expanduser()
        if native.is_absolute():
            if any(part == ".." for part in native.parts) or not is_relative_to(native, self.root):
                raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
            return native
        if raw_path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw_path):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
        return self._relative_candidate(self._validate_base(base), raw_path)

    def resolve_existing(self, raw_path: str = ".") -> ResolvedPath:
        return self.resolve_existing_at(self.root, raw_path)

    def resolve_existing_at(self, base: Path, raw_path: str = ".") -> ResolvedPath:
        candidate = self._candidate_path(base, raw_path or ".")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("NOT_FOUND", f"Path not found: {raw_path}", category="not_found") from exc
        if not is_relative_to(resolved, self.root):
            code = "SYMLINK_ESCAPE" if candidate.is_symlink() else "PATH_OUTSIDE_WORKSPACE"
            raise ToolFailure(code, "Path escapes the configured workspace.", category="security")
        return ResolvedPath(normalize_rel_display(resolved, self.root), resolved, True)

    def resolve_for_write(self, raw_path: str) -> ResolvedPath:
        return self.resolve_for_write_at(self.root, raw_path)

    def resolve_for_write_at(self, base: Path, raw_path: str) -> ResolvedPath:
        candidate = self._candidate_path(base, raw_path)
        if candidate.name in {"", ".", ".."}:
            raise ToolFailure("INVALID_ARGUMENT", "Invalid write target.", category="validation")
        if candidate.exists() or candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            if not is_relative_to(resolved, self.root):
                raise ToolFailure("SYMLINK_ESCAPE", "Path escapes the configured workspace.", category="security")
            return ResolvedPath(normalize_rel_display(resolved, self.root), resolved, True)

        parent = candidate.parent
        missing: list[Path] = []
        while not parent.exists():
            missing.append(parent)
            if parent == self.root or parent.parent == parent:
                break
            parent = parent.parent
        try:
            resolved_parent = parent.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("NOT_FOUND", f"Parent directory not found: {raw_path}", category="not_found") from exc
        if not is_relative_to(resolved_parent, self.root):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
        target = resolved_parent.joinpath(*reversed([p.name for p in missing]), candidate.name)
        return ResolvedPath(normalize_rel_display(target, self.root), target, False)

    def _validate_base(self, base: Path) -> Path:
        try:
            resolved = base.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("NOT_FOUND", "Default cwd path no longer exists.", category="not_found") from exc
        if not resolved.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "Default cwd is not a directory.", category="validation")
        if not is_relative_to(resolved, self.root):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Default cwd escapes the configured workspace.", category="security")
        return resolved

    def reject_write_symlink(self, raw_path: str) -> None:
        candidate = self._candidate_path(self.root, raw_path)
        if candidate.is_symlink():
            raise ToolFailure("SYMLINK_ESCAPE", "Writing through symlinks is denied.", category="security")

    def is_ignored_path(self, path: Path, *, include_hidden: bool = False, include_ignored: bool = False) -> bool:
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return True
        parts = rel.parts
        if not include_hidden and any(part.startswith(".") for part in parts if part not in {".", ""}):
            return True
        if not include_ignored and any(part in DEFAULT_EXCLUDED_NAMES for part in parts):
            return True
        if include_ignored:
            return False
        if self._git_ignored(rel.as_posix()):
            return True
        return False

    def is_safe_existing_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            return False
        return is_relative_to(resolved, self.root)

    def _git_ignored(self, rel_path: str) -> bool:
        git = shutil.which("git")
        if not git:
            return False
        try:
            completed = subprocess.run(
                [git, "-C", str(self.root), "check-ignore", "-q", "--", rel_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            return False
        return completed.returncode == 0


def trim_buffer(
    buffer: bytearray,
    *,
    total_bytes: int,
    start_offset_attr: str,
    cursor_attr: str,
    session: Any,
) -> int:
    overflow = len(buffer) - session.buffer_limit
    if overflow <= 0:
        return 0
    del buffer[:overflow]
    setattr(session, start_offset_attr, total_bytes - len(buffer))
    cursor = getattr(session, cursor_attr)
    if cursor < getattr(session, start_offset_attr):
        setattr(session, cursor_attr, getattr(session, start_offset_attr))
    return overflow


@dataclass
class ExecSession:
    session_id: str
    process: subprocess.Popen[bytes]
    command: str = ""
    workdir: str = ""
    timeout_at: float | None = None
    warnings: list[str] = field(default_factory=list)
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_start_offset: int = 0
    stderr_start_offset: int = 0
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    buffer_limit: int = SESSION_BUFFER_BYTES
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader_threads: list[threading.Thread] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    closed: bool = False
    exit_code: int | None = None
    signal_name: str | None = None
    timed_out: bool = False
    terminating: bool = False

    def append_stdout(self, chunk: bytes) -> None:
        with self.lock:
            self.stdout.extend(chunk)
            self.stdout_total_bytes += len(chunk)
            self.stdout_dropped_bytes += trim_buffer(
                self.stdout,
                total_bytes=self.stdout_total_bytes,
                start_offset_attr="stdout_start_offset",
                cursor_attr="stdout_cursor",
                session=self,
            )

    def append_stderr(self, chunk: bytes) -> None:
        with self.lock:
            self.stderr.extend(chunk)
            self.stderr_total_bytes += len(chunk)
            self.stderr_dropped_bytes += trim_buffer(
                self.stderr,
                total_bytes=self.stderr_total_bytes,
                start_offset_attr="stderr_start_offset",
                cursor_attr="stderr_cursor",
                session=self,
            )

    def snapshot_output(self, max_output_bytes: int, *, consume: bool = True, from_start: bool = False) -> dict[str, Any]:
        self.last_heartbeat_at = time.time()
        self.refresh_status()
        with self.lock:
            stdout_cursor = self.stdout_start_offset if from_start else self.stdout_cursor
            stderr_cursor = self.stderr_start_offset if from_start else self.stderr_cursor
            stdout_omitted = max(0, self.stdout_start_offset - stdout_cursor)
            stderr_omitted = max(0, self.stderr_start_offset - stderr_cursor)
            stdout_start = max(0, stdout_cursor - self.stdout_start_offset)
            stderr_start = max(0, stderr_cursor - self.stderr_start_offset)
            stdout_bytes = bytes(self.stdout[stdout_start:])
            stderr_bytes = bytes(self.stderr[stderr_start:])
            if consume:
                self.stdout_cursor = self.stdout_total_bytes
                self.stderr_cursor = self.stderr_total_bytes
        stdout_truncation = truncate_output_bytes_tail(stdout_bytes, max_output_bytes)
        stderr_truncation = truncate_output_bytes_tail(stderr_bytes, max_output_bytes)
        stdout = stdout_truncation.content
        stderr = stderr_truncation.content
        stdout_truncated = stdout_truncation.truncated
        stderr_truncated = stderr_truncation.truncated
        if self.timed_out:
            status = "timeout"
        elif self.terminating and self.process.poll() is None:
            status = "terminating"
        else:
            status = "running" if self.process.poll() is None else "exited"
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "status": status,
            "exit_code": self.exit_code,
            "signal": self.signal_name,
            "timed_out": self.timed_out,
            "command": self.command,
            "workdir": self.workdir,
            "started_at": utc_isoformat(self.started_at),
            "heartbeat_at": utc_isoformat(self.last_heartbeat_at),
            "timeout_at": utc_isoformat(self.timeout_at) if self.timeout_at is not None else None,
            "consume": consume,
            "from_start": from_start,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdout_truncated_by": stdout_truncation.truncated_by,
            "stderr_truncated_by": stderr_truncation.truncated_by,
            "stdout_output_lines": stdout_truncation.output_lines,
            "stderr_output_lines": stderr_truncation.output_lines,
            "stdout_output_bytes": stdout_truncation.output_bytes,
            "stderr_output_bytes": stderr_truncation.output_bytes,
            "stdout_dropped_bytes": self.stdout_dropped_bytes,
            "stderr_dropped_bytes": self.stderr_dropped_bytes,
            "stdout_omitted_bytes": stdout_omitted,
            "stderr_omitted_bytes": stderr_omitted,
            "truncated": stdout_truncated or stderr_truncated or stdout_omitted > 0 or stderr_omitted > 0,
            "ok": True,
        }
        warnings: list[str] = list(self.warnings)
        if stdout_truncated:
            warnings.append(f"stdout truncated from tail by {stdout_truncation.truncated_by}")
        if stderr_truncated:
            warnings.append(f"stderr truncated from tail by {stderr_truncation.truncated_by}")
        if stdout_omitted > 0:
            warnings.append("stdout cursor skipped dropped bytes")
        if stderr_omitted > 0:
            warnings.append("stderr cursor skipped dropped bytes")
        if warnings:
            payload["warnings"] = warnings
        return payload

    def snapshot_since_cursor(self, max_output_bytes: int) -> dict[str, Any]:
        return self.snapshot_output(max_output_bytes, consume=True, from_start=False)

    def refresh_status(self) -> None:
        if (
            self.timeout_at is not None
            and not self.timed_out
            and self.process.poll() is None
            and time.time() >= self.timeout_at
        ):
            self.timed_out = True
            terminate_process_group(self.process, signal.SIGTERM)
            self.drain_readers()
        code = self.process.poll()
        if code is None:
            return
        self.drain_readers()
        self.exit_code = code
        self.terminating = False
        if code < 0:
            self.signal_name = signal.Signals(-code).name if -code in [s.value for s in signal.Signals] else str(-code)
        self.closed = True

    def drain_readers(self, timeout: float = 0.2) -> None:
        deadline = time.time() + timeout
        for thread in list(self.reader_threads):
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)


@dataclass(frozen=True)
class PatchCheckpoint:
    checkpoint_id: str
    operation_id: str
    created_at: float
    summary: str
    files: dict[str, bytes | None]


class Runtime:
    def __init__(
        self,
        workspace: Path,
        *,
        enable_view_image: bool = True,
        permission_mode: str = "safe",
        dangerously_skip_all_permissions: bool = False,
        shell_env_policy: ShellEnvPolicy | None = None,
        allow_network: bool = False,
        tool_profile: str = "full",
        auth_token: str | None = None,
        admin_token: str | None = None,
        oauth_config: OAuthConfig | None = None,
        upstream_manager: UpstreamManager | None = None,
        admin_manager: McpAdminManager | None = None,
        config_dir: Path | None = None,
        upstream_config_path: Path | None = None,
        settings_path: Path | None = None,
        startup_settings: dict[str, Any] | None = None,
        server_host: str | None = None,
        server_port: int | None = None,
        admin_ui_enabled: bool = False,
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self.workspace = Workspace(workspace)
        self.enable_view_image = enable_view_image
        if dangerously_skip_all_permissions:
            permission_mode = "dangerous"
        if permission_mode not in PERMISSION_MODE_CHOICES:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Unknown permission mode: {permission_mode}",
                category="validation",
                details={"supported": list(PERMISSION_MODE_CHOICES)},
            )
        self.permission_mode = permission_mode
        self.capabilities = PERMISSION_MODE_CAPABILITIES[permission_mode]
        self.dangerously_skip_all_permissions = self.capabilities.skip_all_permissions
        self.shell_env_policy = shell_env_policy or ShellEnvPolicy()
        if self.shell_env_policy.inherit not in SHELL_ENV_INHERIT_CHOICES:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Unknown shell env inherit policy: {self.shell_env_policy.inherit}",
                category="validation",
                details={"supported": list(SHELL_ENV_INHERIT_CHOICES)},
            )
        self.allow_network = allow_network or self.capabilities.network
        if tool_profile not in TOOL_PROFILE_CHOICES:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Unknown tool profile: {tool_profile}",
                category="validation",
                details={"supported": list(TOOL_PROFILE_CHOICES)},
            )
        self.tool_profile = tool_profile
        self.auth_token = auth_token or None
        self.admin_token = admin_token or None
        self.oauth_config = oauth_config
        self.upstream_manager = upstream_manager or UpstreamManager.empty(PROTOCOL_VERSION)
        self.admin_manager = admin_manager
        self.config_dir = config_dir
        self.upstream_config_path = upstream_config_path
        self.settings_path = settings_path
        self.startup_settings = dict(startup_settings or {})
        transcript_dir = self.config_dir or (self.workspace.root / DEFAULT_CONFIG_DIR_NAME)
        self.transcript_store = TranscriptStore(transcript_dir / TRANSCRIPT_DB_FILENAME)
        self.server_host = server_host
        self.server_port = server_port
        self.admin_ui_enabled = admin_ui_enabled
        self.allowed_origins = allowed_origins
        self.server_instance_id = secrets.token_urlsafe(12)
        self._set_runtime_dir(runtime_dir_for_workspace(self.workspace.root, self.server_instance_id))
        self.fallback_runtime_dir = fallback_runtime_dir_for_workspace(self.workspace.root, self.server_instance_id)
        self._pending_codes: dict[str, dict[str, Any]] = {}
        self._pending_codes_lock = threading.Lock()
        self.default_cwd = self.workspace.root
        self.session_default_cwds: dict[str, Path] = {}
        self.session_default_cwds_lock = threading.Lock()
        self._tool_context = threading.local()
        self.sessions: dict[str, ExecSession] = {}
        self.sessions_lock = threading.Lock()
        self.recent_tool_traces: list[dict[str, Any]] = []
        self.recent_tool_traces_lock = threading.Lock()
        self.http_session_id = secrets.token_urlsafe(24)
        self.http_session_ids = {self.http_session_id}
        self.http_session_ids_lock = threading.Lock()
        self.http_sessions: dict[str, dict[str, Any]] = {}
        self.http_sessions_lock = threading.Lock()
        self.recent_mcp_requests: list[dict[str, Any]] = []
        self.recent_mcp_requests_lock = threading.Lock()
        self.patch_baselines: dict[str, str | None] = {}
        self.patch_checkpoints: dict[str, PatchCheckpoint] = {}
        self.patch_checkpoints_lock = threading.Lock()
        self.initialized = False
        self.logging_level = "warning"
        self._tool_handlers = {name: getattr(self, name) for name in TOOL_REGISTRY}

    def _set_runtime_dir(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.home_dir = self.runtime_dir / "home"
        self.tmp_dir = self.runtime_dir / "tmp"
        self.cache_dir = self.runtime_dir / "cache"

    def _ensure_runtime_dirs(self) -> None:
        candidates = [self.runtime_dir]
        if self.fallback_runtime_dir is not None and self.fallback_runtime_dir not in candidates:
            candidates.append(self.fallback_runtime_dir)
        errors: list[str] = []
        for runtime_dir in candidates:
            self._set_runtime_dir(runtime_dir)
            try:
                for path in (
                    self.runtime_dir.parent,
                    self.runtime_dir,
                    self.home_dir,
                    self.tmp_dir,
                    self.cache_dir,
                ):
                    path.mkdir(parents=True, mode=0o700, exist_ok=True)
                    if os.name != "nt":
                        try:
                            path.chmod(0o700)
                        except OSError:
                            pass
                return
            except OSError as exc:
                errors.append(f"{runtime_dir}: {exc}")
        raise ToolFailure(
            "RUNTIME_DIR_UNWRITABLE",
            "Runtime directory could not be created outside the workspace.",
            category="runtime",
            details={"attempted": errors},
        )

    def command_home_dir(self) -> Path:
        return self.home_dir

    def command_tmp_dir(self) -> Path:
        return self.tmp_dir

    def global_tmp_write_policy(self) -> str:
        return self.capabilities.global_tmp_write

    def shell_expansion_policy(self) -> str:
        return "allowed" if self.capabilities.shell_expansion else "blocked"

    def inline_script_policy(self) -> str:
        return "allowed" if self.capabilities.inline_script else "blocked"

    def secret_env_filter_policy(self) -> str:
        return "enabled" if self.capabilities.secret_env_filter else "disabled"

    def landlock_enabled(self) -> bool:
        return self.capabilities.landlock

    def landlock_write_roots(self) -> list[Path]:
        return [self.runtime_dir]

    def is_allowed_command_tmp_path(self, candidate: str) -> bool:
        if self.capabilities.skip_all_permissions:
            return False
        try:
            resolved = Path(candidate).expanduser().resolve(strict=False)
        except OSError:
            return False
        return is_relative_to(resolved, self.runtime_dir)

    def initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}, "logging": {}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "Coding Tools MCP",
                "version": __version__,
            },
            "instructions": "Use these tools only for local coding operations inside the configured workspace.",
        }

    def list_tools(self, *, include_admin: bool = False) -> dict[str, Any]:
        local_tools = [tool_definition(name, tool_profile=self.tool_profile) for name in self.local_exposed_tool_names()]
        upstream_tools = self.upstream_manager.tool_definitions(tool_profile=self.tool_profile)
        admin_tools = [admin_tool_definition(name, tool_profile=self.tool_profile) for name in self.admin_tool_names()]
        return {"tools": [*local_tools, *upstream_tools, *(admin_tools if include_admin else [])]}

    def exposed_tool_names(self, *, include_admin: bool = False) -> list[str]:
        return [
            *self.local_exposed_tool_names(),
            *self.upstream_manager.tool_names(tool_profile=self.tool_profile),
            *(self.admin_tool_names() if include_admin else []),
        ]

    def local_exposed_tool_names(self) -> list[str]:
        names = READ_ONLY_TOOL_NAMES if self.tool_profile == "read-only" else FULL_TOOL_NAMES
        return [name for name in names if self.enable_view_image or name != "view_image"]

    def admin_tool_names(self) -> list[str]:
        if self.admin_manager is None:
            return []
        return [name for name in ADMIN_TOOL_NAMES if name in ADMIN_TOOL_REGISTRY]

    def auth_enabled(self) -> bool:
        return self.auth_token is not None or self.oauth_config is not None

    def admin_auth_enabled(self) -> bool:
        return self.admin_token is not None or self.oauth_config is not None

    def oauth_enabled(self) -> bool:
        return self.oauth_config is not None

    def create_http_session(self) -> str:
        with self.http_session_ids_lock:
            session_id = secrets.token_urlsafe(24)
            while session_id in self.http_session_ids:
                session_id = secrets.token_urlsafe(24)
            self.http_session_ids.add(session_id)
            return session_id

    def has_http_session(self, session_id: str) -> bool:
        with self.http_session_ids_lock:
            return session_id in self.http_session_ids

    def ensure_http_session(self, session_id: str | None) -> str:
        if session_id and self.has_http_session(session_id):
            return session_id
        return self.create_http_session()

    def http_session_default_cwd(self, session_id: str) -> tuple[Path, str]:
        with self.session_default_cwds_lock:
            path = self.session_default_cwds.get(session_id, self.default_cwd)
        return path, normalize_rel_display(path, self.workspace.root)

    def record_mcp_http_access(
        self,
        *,
        session_id: str,
        method: str,
        path: str,
        rpc_method: str | None,
        status: int,
        remote_addr: str | None,
        user_agent: str | None,
        protocol_version: str | None,
    ) -> None:
        if not HTTP_SESSION_ID_RE.fullmatch(session_id):
            session_id = self.http_session_id
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        cwd, cwd_display = self.http_session_default_cwd(session_id)
        with self.http_session_ids_lock:
            self.http_session_ids.add(session_id)
        request_event = {
            "event": "mcp_http_request",
            "timestamp": now,
            "session_id": session_id,
            "method": method,
            "path": path,
            "rpc_method": rpc_method,
            "status": status,
            "remote_addr": remote_addr,
            "user_agent": user_agent,
            "protocol_version": protocol_version,
            "workspace": str(self.workspace.root),
            "default_cwd": str(cwd),
            "default_cwd_display": cwd_display,
        }
        with self.http_sessions_lock:
            current = self.http_sessions.get(session_id)
            if current is None:
                current = {
                    "session_id": session_id,
                    "first_seen": now,
                    "request_count": 0,
                }
                self.http_sessions[session_id] = current
            current["last_seen"] = now
            current["request_count"] = int(current.get("request_count", 0)) + 1
            current["last_method"] = method
            current["last_path"] = path
            current["last_rpc_method"] = rpc_method
            current["last_status"] = status
            current["remote_addr"] = remote_addr
            current["user_agent"] = user_agent
            current["protocol_version"] = protocol_version
            current["default_cwd"] = str(cwd)
            current["default_cwd_display"] = cwd_display
            current["workspace"] = str(self.workspace.root)
        with self.recent_mcp_requests_lock:
            self.recent_mcp_requests.append(request_event)
            if len(self.recent_mcp_requests) > RECENT_MCP_REQUEST_LIMIT:
                del self.recent_mcp_requests[: len(self.recent_mcp_requests) - RECENT_MCP_REQUEST_LIMIT]
        try:
            self.transcript_store.record_http_request(request_event)
        except Exception as exc:  # noqa: BLE001 - transcript persistence must not break MCP traffic
            self.report_transcript_error(exc)

    def current_tool_session_id(self) -> str | None:
        session_id = getattr(self._tool_context, "session_id", None)
        return session_id if isinstance(session_id, str) and session_id else None

    def effective_default_cwd(self) -> Path:
        session_id = self.current_tool_session_id()
        if session_id:
            with self.session_default_cwds_lock:
                return self.session_default_cwds.get(session_id, self.default_cwd)
        return self.default_cwd

    def default_cwd_display(self) -> str:
        return normalize_rel_display(self.effective_default_cwd(), self.workspace.root)

    def resolve_existing(self, raw_path: str = ".") -> ResolvedPath:
        return self.workspace.resolve_existing_at(self.effective_default_cwd(), raw_path)

    def resolve_for_write(self, raw_path: str) -> ResolvedPath:
        return self.workspace.resolve_for_write_at(self.effective_default_cwd(), raw_path)

    def git_path_filter(self, raw_path: str) -> str:
        if raw_path == ".":
            return self.default_cwd_display()
        return self.resolve_for_write(raw_path).display

    def _exec_environment_summary(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace.root),
            "permission_mode": self.permission_mode,
            "network_allowed": self.allow_network,
            "runtime_dir": str(self.runtime_dir),
            "home": str(self.command_home_dir()),
            "tmpdir": str(self.command_tmp_dir()),
            "cache_dir": str(self.cache_dir),
        }

    def workspace_ref_payload(self) -> dict[str, Any]:
        return {
            "workspace_id": workspace_runtime_hash(self.workspace.root),
            "root": str(self.workspace.root),
            "server_instance_id": self.server_instance_id,
        }

    def workspace_identity_payload(self) -> dict[str, Any]:
        return {
            **self.workspace_ref_payload(),
            "host": socket.gethostname(),
            "platform": {
                "os_name": os.name,
                "sys_platform": sys.platform,
                "platform": platform.platform(),
            },
            "default_cwd": self.default_cwd_display(),
            "git": git_workspace_summary(self.workspace.root),
        }

    def _landlock_enforced(self, landlock: dict[str, Any]) -> bool:
        return bool(landlock.get("available")) and self.landlock_enabled()

    def workspace_identity(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.workspace_identity_payload()

    def server_info_payload(self) -> dict[str, Any]:
        tools = self.exposed_tool_names()
        landlock = landlock_status_payload()
        landlock["enabled"] = self._landlock_enforced(landlock)
        return {
            "server": SERVER_NAME,
            "title": "Coding Tools MCP",
            "version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            **self._exec_environment_summary(),
            "workspace_identity": self.workspace_identity_payload(),
            "default_cwd": self.default_cwd_display(),
            "package_root": str(Path(__file__).resolve().parents[1]),
            "config_dir": str(self.config_dir or (self.workspace.root / DEFAULT_CONFIG_DIR_NAME)),
            "transcript_db": str(self.transcript_store.db_path),
            "tool_profile": self.tool_profile,
            "auth_enabled": self.auth_enabled(),
            "dangerously_skip_all_permissions": self.dangerously_skip_all_permissions,
            "landlock": landlock,
            "exec_policy": {
                "shell_expansion": self.shell_expansion_policy(),
                "inline_script": self.inline_script_policy(),
                "global_tmp_write": self.global_tmp_write_policy(),
                "secret_env_filter": self.secret_env_filter_policy(),
            },
            "shell_env_inherit": self.shell_env_policy.inherit,
            "shell_env_include_only": list(self.shell_env_policy.include_only),
            "shell_env_exclude": list(self.shell_env_policy.exclude),
            "endpoint_path": "/mcp",
            "tools": tools,
            "tool_count": len(tools),
            "upstream": self.upstream_manager.status_payload(),
        }

    def config_paths_payload(self) -> dict[str, Any]:
        return {
            "config_dir": str(self.config_dir) if self.config_dir else None,
            "upstream_config": str(self.upstream_config_path) if self.upstream_config_path else None,
            "settings": str(self.settings_path) if self.settings_path else None,
        }

    def active_exec_sessions_payload(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        with self.sessions_lock:
            snapshot = list(self.sessions.values())
        for session in snapshot:
            session.refresh_status()
            with session.lock:
                status = "running" if session.process.poll() is None else "exited"
                if session.timed_out:
                    status = "timeout"
                elif session.terminating and session.process.poll() is None:
                    status = "terminating"
                sessions.append(
                    {
                        "session_id": session.session_id,
                        "status": status,
                        "command": session.command,
                        "workdir": session.workdir,
                        "started_at": datetime.fromtimestamp(session.started_at, timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z"),
                        "exit_code": session.exit_code,
                        "signal": session.signal_name,
                        "stdout_total_bytes": session.stdout_total_bytes,
                        "stderr_total_bytes": session.stderr_total_bytes,
                        "stdout_dropped_bytes": session.stdout_dropped_bytes,
                        "stderr_dropped_bytes": session.stderr_dropped_bytes,
                    }
                )
        return sessions

    def recent_tool_calls_payload(self) -> list[dict[str, Any]]:
        with self.recent_tool_traces_lock:
            return list(self.recent_tool_traces)

    def http_sessions_payload(self) -> list[dict[str, Any]]:
        with self.http_sessions_lock:
            sessions = [dict(session) for session in self.http_sessions.values()]
        sessions.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
        return sessions

    def recent_mcp_requests_payload(self) -> list[dict[str, Any]]:
        with self.recent_mcp_requests_lock:
            return list(self.recent_mcp_requests)

    def report_transcript_error(self, exc: Exception) -> None:
        if os.environ.get(f"{ENV_PREFIX}_TRACE") == "1":
            print(f"transcript persistence failed: {exc}", file=sys.stderr, flush=True)

    def restart_command(self) -> str:
        parts = [
            "uvx",
            SERVER_NAME,
            "--host",
            str(self.server_host or "127.0.0.1"),
            "--port",
            str(self.server_port or 8000),
            "--workspace",
            str(self.workspace.root),
        ]
        if self.config_dir is not None:
            parts.extend(["--config-dir", str(self.config_dir)])
        if self.oauth_config is not None:
            parts.append("--oauth-mode")
        for origin in self.allowed_origins:
            parts.extend(["--allowed-origin", origin])
        return " ".join(shlex.quote(part) for part in parts)

    def admin_status_payload(self, *, base_url: str | None = None) -> dict[str, Any]:
        upstream_status = self.upstream_manager.status_payload()
        admin_status = self.admin_manager.status_payload() if self.admin_manager is not None else {"enabled": False}
        try:
            catalog = self.admin_manager.catalog_list(upstream_status) if self.admin_manager is not None else {"servers": [], "server_count": 0}
        except Exception as exc:  # noqa: BLE001
            catalog = {"ok": False, "error": str(exc), "servers": [], "server_count": 0}
        try:
            templates = self.admin_manager.template_list() if self.admin_manager is not None else {"templates": [], "template_count": 0}
        except Exception as exc:  # noqa: BLE001
            templates = {"ok": False, "error": str(exc), "templates": [], "template_count": 0}
        try:
            transcripts = self.transcript_store.status()
            chat_projects = self.transcript_store.list_chat_projects(limit=100)
            chat_conversations = self.transcript_store.list_chat_conversations(limit=100)
        except Exception as exc:  # noqa: BLE001
            transcripts = {"ok": False, "error": str(exc)}
            chat_projects = {"ok": False, "error": str(exc), "projects": [], "project_count": 0}
            chat_conversations = {"ok": False, "error": str(exc), "conversations": [], "conversation_count": 0}
        http_sessions = self.http_sessions_payload()
        with self.session_default_cwds_lock:
            session_cwds = {session: str(path) for session, path in self.session_default_cwds.items()}
        startup_settings = dict(self.startup_settings)
        for secret_key in ("auth_token", "admin_token", "oauth_password", "oauth_token_secret"):
            if _settings_text(startup_settings, secret_key):
                startup_settings[f"{secret_key}_configured"] = True
                startup_settings.pop(secret_key, None)
        local_names = self.local_exposed_tool_names()
        upstream_names = self.upstream_manager.tool_names(tool_profile=self.tool_profile)
        admin_names = self.admin_tool_names()
        return {
            "ok": True,
            "server_info": self.server_info_payload(),
            "server": {
                "host": self.server_host,
                "port": self.server_port,
                "base_url": base_url,
                "mcp_endpoint": "/mcp",
                "admin_endpoint": "/admin",
                "oauth_authorize_endpoint": "/oauth/authorize",
            },
            "config_paths": self.config_paths_payload(),
            "startup_settings": startup_settings,
            "admin": admin_status,
            "upstream": upstream_status,
            "catalog": catalog,
            "templates": templates,
            "transcripts": transcripts,
            "chat_projects": chat_projects,
            "chat_conversations": chat_conversations,
            "tool_counts": {
                "local": len(local_names),
                "upstream": len(upstream_names),
                "admin": len(admin_names),
                "total": len(local_names) + len(upstream_names) + len(admin_names),
            },
            "auth": {
                "auth_enabled": self.auth_enabled(),
                "admin_auth_enabled": self.admin_auth_enabled(),
                "oauth_enabled": self.oauth_enabled(),
                "auth_token_configured": self.auth_token is not None,
                "admin_token_configured": self.admin_token is not None,
                "oauth_admin_scope": self.oauth_config.admin_scope if self.oauth_config else None,
                "allowed_origins": list(self.allowed_origins),
            },
            "runtime": {
                "workspace": str(self.workspace.root),
                "default_cwd": str(self.default_cwd),
                "default_cwd_display": self.default_cwd_display(),
                "runtime_dir": str(self.runtime_dir),
                "home": str(self.home_dir),
                "tmpdir": str(self.tmp_dir),
                "cache_dir": str(self.cache_dir),
                "http_session_count": len(http_sessions),
                "session_default_cwds": session_cwds,
                "permission_mode": self.permission_mode,
                "tool_profile": self.tool_profile,
                "shell_env_inherit": self.shell_env_policy.inherit,
            },
            "http_sessions": http_sessions,
            "exec_sessions": self.active_exec_sessions_payload(),
            "recent_mcp_requests": self.recent_mcp_requests_payload(),
            "recent_tool_calls": self.recent_tool_calls_payload(),
        }

    def save_startup_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        if self.settings_path is None:
            return {"ok": False, "error": "Settings path is not configured."}
        current = dict(self.startup_settings)
        for key, value in updates.items():
            if key not in STARTUP_SETTING_KEYS:
                continue
            if value is None or value == "":
                current.pop(key, None)
            else:
                current[key] = value
        write_server_settings(self.settings_path, current)
        self.startup_settings = current
        return {"ok": True, "requires_restart": True, "restart_command": self.restart_command(), "settings": current}

    def apply_runtime_update(self, request: dict[str, Any]) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if "auth_token" in request:
            self.auth_token = str(request.get("auth_token") or "") or None
            changes["auth_token"] = self.auth_token is not None
        if "admin_token" in request:
            self.admin_token = str(request.get("admin_token") or "") or None
            changes["admin_token"] = self.admin_token is not None
        if "oauth_password" in request and self.oauth_config is not None:
            password = str(request.get("oauth_password") or "") or secrets.token_urlsafe(32)
            self.oauth_config = replace(self.oauth_config, password=password)
            changes["oauth_password"] = True
        if "default_cwd" in request:
            resolved = self.workspace.resolve_existing(str(request.get("default_cwd") or "."))
            if not resolved.path.is_dir():
                raise ToolFailure("NOT_A_DIRECTORY", "Default cwd must be a directory.", category="validation")
            self.default_cwd = resolved.path
            changes["default_cwd"] = resolved.display
        if request.get("terminate_session"):
            changes["terminate_session"] = self.kill_session(
                {
                    "session_id": request.get("terminate_session"),
                    "signal": request.get("signal", "TERM"),
                    "wait_ms": request.get("wait_ms", 5000),
                    "max_output_bytes": request.get("max_output_bytes", 65536),
                }
            )
        if request.get("reload_upstream") and self.admin_manager is not None:
            self.upstream_manager.close()
            self.upstream_manager = self.admin_manager.reload_upstreams()
            changes["reload_upstream"] = self.upstream_manager.status_payload()
        return {"ok": True, "changes": changes}

    def set_logging_level(self, params: dict[str, Any]) -> dict[str, Any]:
        level = params.get("level")
        if not isinstance(level, str) or level not in LOGGING_LEVELS:
            raise JsonRpcError(
                -32602,
                "logging/setLevel requires a valid logging level",
                {"supported": list(LOGGING_LEVELS), "received": level},
            )
        self.logging_level = level
        return {}

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        admin: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        had_previous = hasattr(self._tool_context, "session_id")
        previous_session_id = getattr(self._tool_context, "session_id", None)
        if session_id is not None:
            self._tool_context.session_id = session_id
        try:
            return self._call_tool(name, arguments, admin=admin)
        finally:
            if had_previous:
                self._tool_context.session_id = previous_session_id
            elif hasattr(self._tool_context, "session_id"):
                delattr(self._tool_context, "session_id")

    def _call_tool(self, name: str, arguments: dict[str, Any] | None, *, admin: bool = False) -> dict[str, Any]:
        started_at = time.time()
        args = arguments or {}
        if name in ADMIN_TOOL_REGISTRY:
            if not admin or name not in self.admin_tool_names():
                raise JsonRpcError(-32602, f"Unknown tool: {name}", {"reason": "unknown_tool"})
            validate_admin_arguments(name, args)
            payload = self.call_admin_tool(name, args)
            payload.setdefault("ok", True)
            self.emit_tool_trace(name, args, payload, started_at)
            return tool_result(payload, is_error=payload.get("ok") is False)
        handler = self._tool_handlers.get(name) if name in self.local_exposed_tool_names() else None
        if handler is None:
            if self.upstream_manager.has_tool(name, tool_profile=self.tool_profile):
                result = self.upstream_manager.call_tool(name, args)
                self.emit_tool_trace(name, args, result.get("structuredContent", result), started_at)
                return result
            raise JsonRpcError(-32602, f"Unknown tool: {name}", {"reason": "unknown_tool"})
        validate_arguments(name, args)
        try:
            payload = handler(args)
            payload.setdefault("ok", True)
            self.emit_tool_trace(name, args, payload, started_at)
            content = None
            if name == "view_image" and args.get("output", "mcp_image") == "mcp_image":
                content = [
                    {
                        "type": "image",
                        "data": str(payload.get("base64", "")),
                        "mimeType": str(payload.get("mime_type", "application/octet-stream")),
                    }
                ]
            return tool_result(payload, is_error=payload.get("ok") is False, content=content)
        except ToolFailure as exc:
            payload = {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "category": exc.category,
                    "retryable": exc.retryable,
                    "details": exc.details,
                },
            }
            diagnostics = permission_failure_diagnostics(exc)
            if diagnostics:
                payload["diagnostics"] = diagnostics
            if exc.code == "PERMISSION_REQUIRED":
                permission = exc.details.get("permission")
                payload["permission_request"] = {
                    "tool_name": name,
                    "permission": permission or "unknown",
                    "status": "required",
                    "retryable": True,
                }
            if exc.code == "ELICITATION_UNSUPPORTED":
                payload["status"] = "unsupported"
            self.emit_tool_trace(name, args, payload, started_at)
            return tool_result(payload, is_error=True)
        except Exception as exc:  # noqa: BLE001 - tool failures must stay structured
            payload = {
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
                    "category": "internal",
                    "retryable": False,
                    "details": {},
                },
            }
            self.emit_tool_trace(name, args, payload, started_at)
            return tool_result(payload, is_error=True)

    def call_admin_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.admin_manager is None:
            return admin_error_payload("ADMIN_DISABLED", "Admin management is not configured.")
        try:
            if name == "mcp_catalog_list":
                return self.admin_manager.catalog_list(self.upstream_manager.status_payload())
            if name == "mcp_template_list":
                return self.admin_manager.template_list()
            if name == "mcp_template_render":
                variables = args.get("variables") if isinstance(args.get("variables"), dict) else None
                overrides = args.get("overrides") if isinstance(args.get("overrides"), dict) else None
                return self.admin_manager.render_template(str(args.get("template", "")), variables=variables, overrides=overrides)
            if name == "mcp_server_plan":
                return self.admin_manager.plan_server(admin_config_arg(args))
            if name == "mcp_server_install":
                return self.admin_manager.install_server(admin_config_arg(args), apply_changes=bool(args.get("apply", False)))
            if name == "mcp_server_update":
                return self.admin_manager.update_server(
                    str(args.get("alias", "")),
                    admin_config_arg(args),
                    apply_changes=bool(args.get("apply", False)),
                )
            if name == "mcp_server_enable":
                return self.admin_manager.set_server_enabled(str(args.get("alias", "")), True, apply_changes=bool(args.get("apply", False)))
            if name == "mcp_server_disable":
                return self.admin_manager.set_server_enabled(str(args.get("alias", "")), False, apply_changes=bool(args.get("apply", False)))
            if name == "mcp_server_remove":
                return self.admin_manager.remove_server(str(args.get("alias", "")), apply_changes=bool(args.get("apply", False)))
            if name == "mcp_server_reload":
                self.upstream_manager.close()
                self.upstream_manager = self.admin_manager.reload_upstreams()
                return {"ok": True, "upstream": self.upstream_manager.status_payload()}
            if name == "mcp_server_health":
                alias = args.get("alias")
                return self.upstream_manager.health_payload(str(alias) if isinstance(alias, str) and alias else None)
            if name == "mcp_server_start":
                return self.upstream_manager.start_server(str(args.get("alias", "")))
            if name == "mcp_server_stop":
                return self.upstream_manager.stop_server(str(args.get("alias", "")))
            if name == "mcp_server_logs":
                alias = args.get("alias")
                return self.upstream_manager.logs_payload(
                    str(alias) if isinstance(alias, str) and alias else None,
                    max_lines=int(args.get("max_lines", 200)),
                )
            if name == "mcp_secret_set":
                return self.admin_manager.secret_set(str(args.get("name", "")), str(args.get("value", "")))
            if name == "mcp_secret_list":
                return self.admin_manager.secret_list()
            if name == "mcp_secret_delete":
                return self.admin_manager.secret_delete(str(args.get("name", "")))
            if name == "mcp_transcript_sessions":
                return self.transcript_store.list_sessions(limit=int(args.get("limit", 100)))
            if name == "mcp_transcript_export":
                session_id = args.get("session_id")
                return self.transcript_store.export_markdown(
                    session_id=str(session_id) if isinstance(session_id, str) and session_id else None,
                    max_events=int(args.get("max_events", 1000)),
                    write_file=bool(args.get("write_file", True)),
                )
            if name == "mcp_codex_sessions_preview":
                return scan_codex_session_candidates(**codex_session_scan_kwargs(args))
            if name == "mcp_codex_sessions_import":
                return import_codex_session_candidates(
                    self.transcript_store,
                    **codex_session_import_kwargs(args),
                    mode="import",
                )
            if name == "mcp_codex_sessions_sync":
                return import_codex_session_candidates(
                    self.transcript_store,
                    **codex_session_import_kwargs(args),
                    mode="sync",
                )
            if name == "mcp_chat_projects":
                query = args.get("query")
                return self.transcript_store.list_chat_projects(
                    limit=int(args.get("limit", 100)),
                    query=str(query) if isinstance(query, str) and query else None,
                )
            if name == "mcp_chat_conversations":
                query = args.get("query")
                return self.transcript_store.list_chat_conversations(
                    limit=int(args.get("limit", 100)),
                    query=str(query) if isinstance(query, str) and query else None,
                    project_id=optional_text_arg(args, "project_id"),
                )
            if name == "mcp_chat_messages":
                return self.transcript_store.list_chat_messages(
                    conversation_id=str(args.get("conversation_id", "")),
                    limit=int(args.get("limit", 500)),
                )
            if name == "mcp_chat_context":
                return self.transcript_store.list_context_entries(
                    conversation_id=str(args.get("conversation_id", "")),
                    limit=int(args.get("limit", 200)),
                )
            if name == "mcp_chat_record_context":
                entry: dict[str, Any] = {
                    "entry_id": str(args.get("entry_id")) if isinstance(args.get("entry_id"), str) and args.get("entry_id") else None,
                    "kind": str(args.get("kind") or "checkpoint"),
                    "timestamp": str(args.get("timestamp")) if isinstance(args.get("timestamp"), str) and args.get("timestamp") else None,
                    "content": str(args.get("content") or ""),
                    "source": str(args.get("source")) if isinstance(args.get("source"), str) and args.get("source") else None,
                }
                metadata = args.get("metadata")
                if isinstance(metadata, dict):
                    entry["metadata"] = metadata
                return self.transcript_store.record_context_entries(
                    conversation_id=str(args.get("conversation_id") or "default"),
                    entries=[entry],
                    source=str(args.get("source")) if isinstance(args.get("source"), str) and args.get("source") else None,
                    conversation_title=str(args.get("conversation_title")) if isinstance(args.get("conversation_title"), str) else None,
                    conversation_uid=str(args.get("conversation_uid")) if isinstance(args.get("conversation_uid"), str) else None,
                    **project_kwargs_from_args(args),
                )
            if name == "mcp_chat_update_context":
                return self.transcript_store.update_context_entry(
                    row_id=int(args.get("id", 0)),
                    updates={key: value for key, value in args.items() if key in {"entry_id", "kind", "timestamp", "content", "source", "metadata"}},
                )
            if name == "mcp_chat_delete_context":
                return self.transcript_store.delete_context_entry(row_id=int(args.get("id", 0)))
            if name == "mcp_chat_recall":
                return self.recall_chat_context(
                    {
                        "conversation_id": args.get("conversation_id"),
                        "max_messages": args.get("max_messages", 200),
                        "max_context_entries": args.get("max_context_entries", 200),
                    }
                )
            if name == "mcp_chat_project_recall":
                return self.recall_project_context(
                    {
                        "project_id": args.get("project_id"),
                        "max_conversations": args.get("max_conversations", 50),
                        "max_messages": args.get("max_messages", 200),
                        "max_context_entries": args.get("max_context_entries", 200),
                    }
                )
            if name == "mcp_chat_export":
                conversation_id = args.get("conversation_id")
                return self.transcript_store.export_chat_markdown(
                    conversation_id=str(conversation_id) if isinstance(conversation_id, str) and conversation_id else None,
                    project_id=optional_text_arg(args, "project_id"),
                    max_messages=int(args.get("max_messages", 5000)),
                    write_file=bool(args.get("write_file", True)),
                )
            if name == "mcp_chat_context_export":
                conversation_id = args.get("conversation_id")
                return self.transcript_store.export_context_markdown(
                    conversation_id=str(conversation_id) if isinstance(conversation_id, str) and conversation_id else None,
                    project_id=optional_text_arg(args, "project_id"),
                    max_entries=int(args.get("max_entries", 5000)),
                    write_file=bool(args.get("write_file", True)),
                )
            if name == "mcp_chat_update_message":
                return self.transcript_store.update_chat_message(
                    row_id=int(args.get("id", 0)),
                    updates={key: value for key, value in args.items() if key in {"role", "timestamp", "content", "source", "metadata"}},
                )
            if name == "mcp_chat_delete_message":
                return self.transcript_store.delete_chat_message(row_id=int(args.get("id", 0)))
            if name == "mcp_chat_delete_conversation":
                return self.transcript_store.delete_chat_conversation(conversation_id=str(args.get("conversation_id", "")))
            if name == "mcp_chat_clear":
                return self.transcript_store.clear_chat_messages()
            if name == "mcp_chat_merge":
                source_ids = args.get("source_conversation_ids")
                return self.transcript_store.merge_chat_conversations(
                    target_conversation_id=str(args.get("target_conversation_id", "")),
                    source_conversation_ids=[str(item) for item in source_ids] if isinstance(source_ids, list) else [],
                )
        except (McpManagementError, ValueError) as exc:
            return admin_error_payload("ADMIN_OPERATION_FAILED", str(exc))
        return admin_error_payload("ADMIN_UNKNOWN_TOOL", f"Unknown admin tool: {name}")

    def server_info(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.server_info_payload()

    def check_exec_environment(self, args: dict[str, Any]) -> dict[str, Any]:
        landlock = landlock_status_payload()
        warnings: list[str] = []
        if not landlock.get("available"):
            warnings.append("Linux Landlock filesystem confinement is unavailable")
        if self.capabilities.skip_all_permissions:
            warnings.append("permission_mode=dangerous disables MCP safety gates")
        return {
            "ok": True,
            **self._exec_environment_summary(),
            "landlock_enabled": self._landlock_enforced(landlock),
            "landlock_abi": landlock.get("abi_version"),
            "global_tmp_write": self.global_tmp_write_policy(),
            "warnings": warnings,
        }

    def record_chat_transcript(self, args: dict[str, Any]) -> dict[str, Any]:
        messages = args.get("messages")
        if not isinstance(messages, list):
            raise ToolFailure("INVALID_ARGUMENT", "messages must be an array.", category="validation")
        source = args.get("source")
        return self.transcript_store.record_chat_messages(
            conversation_id=str(args.get("conversation_id") or "default"),
            messages=messages,
            source=str(source) if isinstance(source, str) and source else None,
            conversation_title=str(args.get("conversation_title")) if isinstance(args.get("conversation_title"), str) else None,
            conversation_uid=str(args.get("conversation_uid")) if isinstance(args.get("conversation_uid"), str) else None,
            **project_kwargs_from_args(args),
        )

    def record_chat_message(self, args: dict[str, Any]) -> dict[str, Any]:
        source = args.get("source")
        message: dict[str, Any] = {
            "role": str(args.get("role") or "message"),
            "content": str(args.get("content") or ""),
        }
        for message_field in ("message_id", "timestamp", "source"):
            value = args.get(message_field)
            if isinstance(value, str) and value:
                message[message_field] = value
        metadata_json = args.get("metadata_json")
        if isinstance(metadata_json, str) and metadata_json.strip():
            try:
                metadata = json.loads(metadata_json)
            except json.JSONDecodeError as exc:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "metadata_json must be valid JSON.",
                    category="validation",
                    details={"error": str(exc)},
                ) from exc
            if not isinstance(metadata, dict):
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "metadata_json must decode to an object.",
                    category="validation",
                )
            message["metadata"] = metadata
        return self.transcript_store.record_chat_messages(
            conversation_id=str(args.get("conversation_id") or "default"),
            messages=[message],
            source=str(source) if isinstance(source, str) and source else None,
            conversation_title=str(args.get("conversation_title")) if isinstance(args.get("conversation_title"), str) else None,
            conversation_uid=str(args.get("conversation_uid")) if isinstance(args.get("conversation_uid"), str) else None,
            **project_kwargs_from_args(args),
        )

    def list_chat_projects(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query")
        return self.transcript_store.list_chat_projects(
            limit=int(args.get("limit", 100)),
            query=str(query) if isinstance(query, str) and query else None,
        )

    def list_chat_conversations(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query")
        return self.transcript_store.list_chat_conversations(
            limit=int(args.get("limit", 100)),
            query=str(query) if isinstance(query, str) and query else None,
            project_id=optional_text_arg(args, "project_id"),
        )

    def recall_chat_context(self, args: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(args.get("conversation_id") or "")
        try:
            max_messages = int(args.get("max_messages", 200))
        except (TypeError, ValueError) as exc:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "max_messages must be an integer.",
                category="validation",
                details={"value": args.get("max_messages")},
            ) from exc
        max_messages = max(1, min(max_messages, 20000))
        try:
            max_context_entries = int(args.get("max_context_entries", max_messages))
        except (TypeError, ValueError) as exc:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "max_context_entries must be an integer.",
                category="validation",
                details={"value": args.get("max_context_entries")},
            ) from exc
        max_context_entries = max(1, min(max_context_entries, 5000))
        try:
            messages_payload = self.transcript_store.list_chat_messages(
                conversation_id=conversation_id,
                limit=max_messages,
            )
            context_payload = self.transcript_store.list_context_entries(
                conversation_id=conversation_id,
                limit=max_context_entries,
            )
            context_export = self.transcript_store.export_context_markdown(
                conversation_id=conversation_id,
                max_entries=max_context_entries,
                write_file=False,
            )
            if messages_payload.get("message_count", 0):
                chat_export = self.transcript_store.export_chat_markdown(
                    conversation_id=conversation_id,
                    max_messages=max_messages,
                    write_file=False,
                )
            else:
                chat_export = {"conversations": context_export.get("conversations", []), "markdown": ""}
        except ValueError as exc:
            raise ToolFailure(
                "NOT_FOUND",
                str(exc),
                category="not_found",
            ) from exc
        context_markdown = str(context_export.get("markdown") or "")
        chat_markdown = str(chat_export.get("markdown") or "")
        context_text = context_markdown if context_payload.get("entry_count", 0) else chat_markdown
        return {
            "ok": True,
            "conversation_id": messages_payload.get("conversation_id"),
            "message_count": messages_payload.get("message_count", 0),
            "context_entry_count": context_payload.get("entry_count", 0),
            "max_messages": max_messages,
            "max_context_entries": max_context_entries,
            "messages": messages_payload.get("messages", []),
            "context_entries": context_payload.get("entries", []),
            "conversations": context_export.get("conversations") or chat_export.get("conversations", []),
            "markdown": context_text,
            "context_text": context_text,
            "context_markdown": context_markdown,
            "chat_markdown": chat_markdown,
        }

    def recall_project_context(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "").strip()
        if not project_id:
            raise ToolFailure("INVALID_ARGUMENT", "project_id is required.", category="validation")
        try:
            max_conversations = int(args.get("max_conversations", 50))
            max_messages = int(args.get("max_messages", 200))
            max_context_entries = int(args.get("max_context_entries", 200))
        except (TypeError, ValueError) as exc:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "max_conversations, max_messages, and max_context_entries must be integers.",
                category="validation",
            ) from exc
        max_conversations = max(1, min(max_conversations, 500))
        max_messages = max(1, min(max_messages, 20000))
        max_context_entries = max(1, min(max_context_entries, 5000))
        projects_payload = self.transcript_store.list_chat_projects(limit=500, query=project_id)
        projects = projects_payload.get("projects", [])
        exact_project = next((project for project in projects if project.get("project_id") == project_id), None)
        conversations_payload = self.transcript_store.list_chat_conversations(
            limit=max_conversations,
            project_id=project_id,
        )
        context_export = self.transcript_store.export_context_markdown(
            project_id=project_id,
            max_entries=max_context_entries,
            write_file=False,
        )
        chat_export = self.transcript_store.export_chat_markdown(
            project_id=project_id,
            max_messages=max_messages,
            write_file=False,
        )
        context_markdown = str(context_export.get("markdown") or "")
        chat_markdown = str(chat_export.get("markdown") or "")
        context_text = context_markdown if context_export.get("entry_count", 0) else chat_markdown
        return {
            "ok": True,
            "project_id": project_id,
            "project": exact_project,
            "projects": projects,
            "conversation_count": conversations_payload.get("conversation_count", 0),
            "message_count": chat_export.get("message_count", 0),
            "context_entry_count": context_export.get("entry_count", 0),
            "max_conversations": max_conversations,
            "max_messages": max_messages,
            "max_context_entries": max_context_entries,
            "conversations": conversations_payload.get("conversations", []),
            "markdown": context_text,
            "context_text": context_text,
            "context_markdown": context_markdown,
            "chat_markdown": chat_markdown,
        }

    def get_default_cwd(self, args: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "workspace": str(self.workspace.root),
            "default_cwd": self.default_cwd_display(),
        }
        session_id = self.current_tool_session_id()
        if session_id:
            payload["session_id"] = session_id
        return payload

    def set_default_cwd(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.workspace.resolve_existing(str(args.get("path", ".")))
        if not resolved.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "Default cwd must be a directory.", category="validation")
        session_id = self.current_tool_session_id()
        if session_id:
            with self.session_default_cwds_lock:
                self.session_default_cwds[session_id] = resolved.path
        else:
            self.default_cwd = resolved.path
        payload: dict[str, Any] = {
            "workspace": str(self.workspace.root),
            "default_cwd": resolved.display,
        }
        if session_id:
            payload["session_id"] = session_id
        return payload

    def emit_tool_trace(self, name: str, args: dict[str, Any], payload: dict[str, Any], started_at: float) -> None:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        session_id = self.current_tool_session_id()
        if session_id is None and isinstance(payload.get("session_id"), str):
            session_id = str(payload.get("session_id"))
        cwd = self.effective_default_cwd()
        event = {
            "event": "tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "tool": name,
            "ok": bool(payload.get("ok", False)),
            "status": payload.get("status"),
            "error_code": error.get("code") if isinstance(error, dict) else None,
            "duration_ms": int((time.time() - started_at) * 1000),
            "session_id": session_id,
            "workspace": str(self.workspace.root),
            "default_cwd": str(cwd),
            "default_cwd_display": normalize_rel_display(cwd, self.workspace.root),
            "truncated": payload.get("truncated"),
            "args": redact_for_trace(args),
        }
        transcript_event = {**event, "result": redact_for_transcript(payload)}
        with self.recent_tool_traces_lock:
            self.recent_tool_traces.append(event)
            if len(self.recent_tool_traces) > RECENT_TOOL_TRACE_LIMIT:
                del self.recent_tool_traces[: len(self.recent_tool_traces) - RECENT_TOOL_TRACE_LIMIT]
        try:
            self.transcript_store.record_tool_call(transcript_event)
        except Exception as exc:  # noqa: BLE001 - transcript persistence must not break tool calls
            self.report_transcript_error(exc)
        if os.environ.get(f"{ENV_PREFIX}_TRACE") == "1":
            print(json.dumps(event, sort_keys=True, separators=(",", ":")), file=sys.stderr, flush=True)

    def _resolve_version_path(self, raw_path: str) -> ResolvedPath:
        try:
            return self.resolve_existing(raw_path)
        except ToolFailure as exc:
            if exc.code != "NOT_FOUND":
                raise
            return self.resolve_for_write(raw_path)

    def file_stat(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self._resolve_version_path(str(args.get("path", "")))
        return {
            **file_version_payload(resolved.path, self.workspace.root, display=resolved.display),
            "workspace": self.workspace_ref_payload(),
        }

    def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_existing(str(args.get("path", "")))
        if resolved.path.is_dir():
            raise ToolFailure("IS_DIRECTORY", "Path is a directory.", category="validation")
        max_bytes = int(args.get("max_bytes", 131072))
        start_line = int(args.get("start_line", 1))
        end_line = args.get("end_line")
        encoding = args.get("encoding", "utf-8")
        if encoding != "utf-8":
            raise ToolFailure("UNSUPPORTED_ENCODING", "Only utf-8 is supported.", category="validation")
        stat_result = resolved.path.stat()
        data = resolved.path.read_bytes()
        if b"\x00" in data[:4096]:
            raise ToolFailure("BINARY_FILE", "Binary file read blocked for text tool.", category="validation")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolFailure("UNSUPPORTED_ENCODING", "File is not valid utf-8.", category="validation") from exc
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        total_bytes = len(data)
        if start_line < 1:
            raise ToolFailure("INVALID_ARGUMENT", "start_line must be >= 1.", category="validation")
        end = int(end_line) if end_line is not None else total_lines
        if end < start_line:
            selected = ""
        else:
            selected = "".join(lines[start_line - 1 : end])
        truncation = truncate_text_head(selected, max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes)
        selected = truncation.content
        truncated = truncation.truncated
        actual_end = min(end, total_lines)
        if truncated and truncation.output_lines > 0:
            actual_end = min(total_lines, start_line + truncation.output_lines - 1)
        next_start_line = actual_end + 1 if truncated and actual_end < total_lines else None
        warnings = []
        if truncated:
            warnings.append("content truncated")
        if truncation.first_line_exceeds_limit:
            warnings.append("first selected line exceeds max_bytes")
        return {
            "path": resolved.display,
            "content": selected,
            "encoding": "utf-8",
            "start_line": start_line,
            "end_line": actual_end,
            "total_lines": total_lines,
            "total_bytes": total_bytes,
            "version": file_version_payload(
                resolved.path,
                self.workspace.root,
                display=resolved.display,
                data=data,
                stat_result=stat_result,
            ),
            "bytes_read": len(selected.encode("utf-8")),
            "truncated": truncated,
            "truncated_by": truncation.truncated_by,
            "output_lines": truncation.output_lines,
            "output_bytes": truncation.output_bytes,
            "next_start_line": next_start_line,
            "warnings": warnings,
        }

    def list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_existing(str(args.get("path", ".")))
        if not resolved.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "Path is not a directory.", category="validation")
        recursive = bool(args.get("recursive", False))
        max_depth = int(args.get("max_depth", 1))
        max_entries = int(args.get("max_entries", 1000))
        include_hidden = bool(args.get("include_hidden", False))
        include_ignored = bool(args.get("include_ignored", False))
        sort_key = args.get("sort", "name")
        entries: list[dict[str, Any]] = []
        truncated = False

        def visit(directory: Path, depth: int) -> None:
            nonlocal truncated
            if truncated:
                return
            try:
                children = list(directory.iterdir())
            except OSError:
                return
            for child in children:
                if self.workspace.is_ignored_path(child, include_hidden=include_hidden, include_ignored=include_ignored):
                    continue
                entries.append(entry_for_path(child, self.workspace.root))
                if len(entries) >= max_entries:
                    truncated = True
                    return
                if recursive and depth < max_depth and child.is_dir() and not child.is_symlink():
                    visit(child, depth + 1)

        visit(resolved.path, 1)
        entries.sort(key=lambda item: sort_value(item, sort_key))
        return {
            "path": resolved.display,
            "entries": entries,
            "truncated": truncated,
            "warnings": ["entry limit reached"] if truncated else [],
        }

    def list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_existing(str(args.get("path", ".")))
        if not resolved.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "Path is not a directory.", category="validation")
        patterns_arg = args.get("patterns")
        glob_arg = args.get("glob")
        if isinstance(patterns_arg, list) and patterns_arg:
            patterns = [str(item) for item in patterns_arg]
        elif isinstance(glob_arg, str) and glob_arg:
            patterns = [glob_arg]
        else:
            patterns = ["**/*"]
        exclude_patterns = [str(item) for item in args.get("exclude_patterns", [])]
        include_hidden = bool(args.get("include_hidden", False))
        include_ignored = bool(args.get("include_ignored", False))
        max_results = int(args.get("max_results", 5000))
        fast_result = self._list_files_with_fd(
            resolved,
            patterns,
            exclude_patterns,
            include_hidden=include_hidden,
            include_ignored=include_ignored,
            max_results=max_results,
            sort_key=str(args.get("sort", "path")),
        )
        if fast_result is not None:
            return fast_result
        files: list[dict[str, Any]] = []
        truncated = False
        for path in walk_files(resolved.path):
            if path.is_symlink() and not self.workspace.is_safe_existing_path(path):
                continue
            if self.workspace.is_ignored_path(path, include_hidden=include_hidden, include_ignored=include_ignored):
                continue
            rel = normalize_rel_display(path, self.workspace.root)
            if not any(fnmatch.fnmatch(rel, pattern) or PurePosixPath(rel).match(pattern) for pattern in patterns):
                continue
            if any(fnmatch.fnmatch(rel, pattern) or PurePosixPath(rel).match(pattern) for pattern in exclude_patterns):
                continue
            stat = path.lstat()
            files.append(
                {
                    "path": rel,
                    "type": "symlink" if path.is_symlink() else "file",
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            )
            if len(files) >= max_results:
                truncated = True
                break
        files.sort(key=lambda item: item["modified"] if args.get("sort") == "modified" else item["path"])
        return {
            "path": resolved.display,
            "files": files,
            "truncated": truncated,
            "warnings": ["result limit reached"] if truncated else [],
        }

    def _list_files_with_fd(
        self,
        resolved: ResolvedPath,
        patterns: list[str],
        exclude_patterns: list[str],
        *,
        include_hidden: bool,
        include_ignored: bool,
        max_results: int,
        sort_key: str,
    ) -> dict[str, Any] | None:
        fd = shutil.which("fd") or shutil.which("fdfind")
        if not fd or not resolved.path.is_dir():
            return None
        args_base = [
            fd,
            "--glob",
            "--color=never",
            "--type",
            "f",
            "--type",
            "l",
            "--max-results",
            str(max_results),
            "--no-require-git",
        ]
        if include_hidden:
            args_base.append("--hidden")
        if include_ignored:
            args_base.append("--no-ignore")
        else:
            for name in sorted(DEFAULT_EXCLUDED_NAMES):
                args_base.extend(["--exclude", name])
        for pattern in exclude_patterns:
            args_base.extend(["--exclude", pattern])

        paths: dict[str, Path] = {}
        for pattern in patterns:
            effective = pattern
            args = list(args_base)
            if "/" in pattern:
                args.append("--full-path")
                if not pattern.startswith("/") and not pattern.startswith("**/") and pattern != "**":
                    effective = f"**/{pattern}"
            args.extend(["--", effective, "."])
            try:
                completed = subprocess.run(
                    args,
                    cwd=str(resolved.path),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
            except Exception:
                return None
            if completed.returncode not in {0, 1}:
                return None
            for raw in completed.stdout.splitlines():
                rel_to_search = raw.strip().removeprefix("./")
                if not rel_to_search:
                    continue
                path = resolved.path / rel_to_search
                if path.is_symlink() and not self.workspace.is_safe_existing_path(path):
                    continue
                if self.workspace.is_ignored_path(path, include_hidden=include_hidden, include_ignored=include_ignored):
                    continue
                rel = normalize_rel_display(path, self.workspace.root)
                if any(fnmatch.fnmatch(rel, pat) or PurePosixPath(rel).match(pat) for pat in exclude_patterns):
                    continue
                paths[rel] = path
                if len(paths) >= max_results:
                    break
            if len(paths) >= max_results:
                break
        files: list[dict[str, Any]] = []
        for rel, path in paths.items():
            try:
                stat = path.lstat()
            except OSError:
                continue
            files.append(
                {
                    "path": rel,
                    "type": "symlink" if path.is_symlink() else "file",
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            )
        files.sort(key=lambda item: item["modified"] if sort_key == "modified" else item["path"])
        truncated = len(paths) >= max_results
        return {
            "path": resolved.display,
            "files": files,
            "truncated": truncated,
            "engine": "fd",
            "warnings": ["result limit reached"] if truncated else [],
        }

    def search_text(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", ""))
        if not query:
            raise ToolFailure("INVALID_ARGUMENT", "query is required.", category="validation")
        resolved = self.resolve_existing(str(args.get("path", ".")))
        regex = bool(args.get("regex", False))
        case_sensitive = bool(args.get("case_sensitive", False))
        include_globs = [str(item) for item in args.get("include_globs", [])]
        if isinstance(args.get("glob"), str):
            include_globs.append(str(args["glob"]))
        exclude_globs = [str(item) for item in args.get("exclude_globs", [])]
        context_lines = int(args.get("context_lines", 0))
        max_results = int(args.get("max_results", 1000))
        max_preview_bytes = int(args.get("max_preview_bytes", 512))
        fast_result = self._search_text_with_rg(
            resolved,
            query,
            regex=regex,
            case_sensitive=case_sensitive,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            context_lines=context_lines,
            max_results=max_results,
            max_preview_bytes=max_preview_bytes,
        )
        if fast_result is not None:
            return fast_result
        matches: list[dict[str, Any]] = []
        total = 0
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(query, flags) if regex else None
        except re.error as exc:
            raise ToolFailure("INVALID_ARGUMENT", f"Invalid regex: {exc}", category="validation") from exc

        roots = [resolved.path] if resolved.path.is_file() else walk_files(resolved.path)
        for path in roots:
            if path.is_dir() or self.workspace.is_ignored_path(path):
                continue
            if path.is_symlink() and not self.workspace.is_safe_existing_path(path):
                continue
            rel = normalize_rel_display(path, self.workspace.root)
            if include_globs and not any(fnmatch.fnmatch(rel, pat) or PurePosixPath(rel).match(pat) for pat in include_globs):
                continue
            if any(fnmatch.fnmatch(rel, pat) or PurePosixPath(rel).match(pat) for pat in exclude_globs):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            try:
                lines = data.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines):
                found = compiled.search(line) if compiled else find_literal(line, query, case_sensitive)
                if not found:
                    continue
                total += 1
                if len(matches) >= max_results:
                    continue
                column = found.start() + 1 if hasattr(found, "start") else 1
                preview, line_truncated = truncate_line_chars(line)
                preview_truncation = truncate_text_head(preview, max_lines=1, max_bytes=max_preview_bytes)
                preview = preview_truncation.content
                before = lines[max(0, index - context_lines) : index]
                after = lines[index + 1 : index + 1 + context_lines]
                item = {
                    "path": rel,
                    "line": index + 1,
                    "column": column,
                    "preview": preview,
                    "before": before,
                    "after": after,
                }
                if line_truncated or preview_truncation.truncated:
                    item["preview_truncated"] = True
                    item["preview_truncated_by"] = "chars" if line_truncated else preview_truncation.truncated_by
                matches.append(item)
        return {
            "query": query,
            "matches": matches,
            "total_matches": total,
            "truncated": total > len(matches),
            "warnings": ["result limit reached"] if total > len(matches) else [],
        }

    def _search_text_with_rg(
        self,
        resolved: ResolvedPath,
        query: str,
        *,
        regex: bool,
        case_sensitive: bool,
        include_globs: list[str],
        exclude_globs: list[str],
        context_lines: int,
        max_results: int,
        max_preview_bytes: int,
    ) -> dict[str, Any] | None:
        rg = shutil.which("rg")
        if not rg:
            return None
        args = [rg, "--json", "--line-number", "--color=never"]
        if not case_sensitive:
            args.append("--ignore-case")
        if not regex:
            args.append("--fixed-strings")
        for name in sorted(DEFAULT_EXCLUDED_NAMES):
            args.extend(["--glob", f"!{name}/**"])
        for pattern in include_globs:
            args.extend(["--glob", pattern])
        for pattern in exclude_globs:
            args.extend(["--glob", f"!{pattern}"])
        search_path = resolved.display if resolved.display != "." else "."
        args.extend(["--", query, search_path])
        try:
            completed = subprocess.run(
                args,
                cwd=str(self.workspace.root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except Exception:
            return None
        if completed.returncode not in {0, 1}:
            return None
        matches: list[dict[str, Any]] = []
        total = 0
        file_cache: dict[str, list[str]] = {}
        for raw in completed.stdout.splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            path_text = data.get("path", {}).get("text") if isinstance(data.get("path"), dict) else None
            line_number = data.get("line_number")
            line_text = data.get("lines", {}).get("text") if isinstance(data.get("lines"), dict) else ""
            if not isinstance(path_text, str) or not isinstance(line_number, int):
                continue
            total += 1
            if len(matches) >= max_results:
                continue
            rel = normalize_rel_display((self.workspace.root / path_text).resolve(), self.workspace.root)
            submatches = data.get("submatches") if isinstance(data.get("submatches"), list) else []
            first_submatch = submatches[0] if submatches and isinstance(submatches[0], dict) else {}
            column = int(first_submatch.get("start", 0)) + 1
            sanitized = str(line_text).replace("\r\n", "\n").replace("\r", "").rstrip("\n")
            preview, line_truncated = truncate_line_chars(sanitized)
            preview_truncation = truncate_text_head(preview, max_lines=1, max_bytes=max_preview_bytes)
            preview = preview_truncation.content
            lines = file_cache.get(rel)
            if lines is None:
                try:
                    lines = (self.workspace.root / rel).read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                file_cache[rel] = lines
            index = line_number - 1
            before = lines[max(0, index - context_lines) : index] if lines else []
            after = lines[index + 1 : index + 1 + context_lines] if lines else []
            item = {
                "path": rel,
                "line": line_number,
                "column": column,
                "preview": preview,
                "before": before,
                "after": after,
            }
            if line_truncated or preview_truncation.truncated:
                item["preview_truncated"] = True
                item["preview_truncated_by"] = "chars" if line_truncated else preview_truncation.truncated_by
            matches.append(item)
        return {
            "query": query,
            "matches": matches,
            "total_matches": total,
            "truncated": total > len(matches),
            "engine": "rg",
            "warnings": ["result limit reached"] if total > len(matches) else [],
        }

    def _normalize_expected_versions(self, raw: Any, *, name: str) -> dict[str, Any]:
        if raw in (None, ""):
            return {}
        if not isinstance(raw, dict):
            raise ToolFailure("INVALID_ARGUMENT", f"{name} must be an object keyed by workspace path.", category="validation")
        normalized: dict[str, Any] = {}
        for raw_path, expected in raw.items():
            resolved = self._resolve_version_path(str(raw_path))
            normalized[resolved.display] = expected
        return normalized

    def _collect_file_versions(self, paths: list[str] | set[str]) -> dict[str, dict[str, Any]]:
        versions: dict[str, dict[str, Any]] = {}
        for display in sorted(set(paths)):
            resolved = self._resolve_version_path(display)
            versions[resolved.display] = file_version_payload(resolved.path, self.workspace.root, display=resolved.display)
        return versions

    def _check_patch_preconditions(
        self,
        versions: dict[str, dict[str, Any]],
        expected_hashes: dict[str, Any],
        expected_mtimes: dict[str, Any],
    ) -> None:
        for display, expected in expected_hashes.items():
            actual = versions[display].get("sha256")
            expected_hash = None if expected is None else str(expected)
            if actual != expected_hash:
                raise ToolFailure(
                    "PATCH_CONFLICT",
                    "File sha256 no longer matches the expected version.",
                    category="conflict",
                    details={"path": display, "expected_sha256": expected_hash, "actual_sha256": actual, "version": versions[display]},
                )
        for display, expected in expected_mtimes.items():
            actual = versions[display].get("mtime_ns")
            expected_mtime = None if expected is None else int(expected)
            if actual != expected_mtime:
                raise ToolFailure(
                    "PATCH_CONFLICT",
                    "File mtime no longer matches the expected version.",
                    category="conflict",
                    details={"path": display, "expected_mtime_ns": expected_mtime, "actual_mtime_ns": actual, "version": versions[display]},
                )

    def _store_patch_checkpoint(
        self,
        *,
        checkpoint_id: str,
        operation_id: str,
        summary: str,
        files: dict[str, bytes | None],
    ) -> None:
        checkpoint = PatchCheckpoint(
            checkpoint_id=checkpoint_id,
            operation_id=operation_id,
            created_at=time.time(),
            summary=summary,
            files=dict(files),
        )
        with self.patch_checkpoints_lock:
            self.patch_checkpoints[checkpoint_id] = checkpoint
            if len(self.patch_checkpoints) > PATCH_CHECKPOINT_LIMIT:
                oldest = min(self.patch_checkpoints.values(), key=lambda item: item.created_at)
                self.patch_checkpoints.pop(oldest.checkpoint_id, None)

    def apply_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        patch = str(args.get("patch", ""))
        dry_run = bool(args.get("dry_run", False))
        operation_id = str(args.get("operation_id") or secrets.token_urlsafe(12))
        create_checkpoint = bool(args.get("create_checkpoint", True))
        expected_hashes = self._normalize_expected_versions(args.get("expected_hashes"), name="expected_hashes")
        expected_mtimes = self._normalize_expected_versions(args.get("expected_mtimes"), name="expected_mtimes")
        operations = parse_patch(patch)
        staged: dict[str, str | None] = {}
        summaries: list[str] = []
        affected: list[dict[str, str]] = []
        for op in operations:
            self._validate_patch_path(op.path, require_existing=op.kind in {"update", "delete"})
            if op.kind in {"add", "update", "delete"}:
                self.workspace.reject_write_symlink(op.path)
            if op.move_to:
                self._validate_patch_path(op.move_to, require_existing=False)
                self.workspace.reject_write_symlink(op.move_to)
            if op.kind == "add":
                target = self.workspace.resolve_for_write(op.path)
                if target.existed:
                    raise ToolFailure("PATCH_FAILED", "Cannot add file that already exists.", category="validation")
                staged[target.display] = op.add_content or ""
                affected.append({"path": target.display, "operation": "add"})
                summaries.append(f"A {target.display}")
            elif op.kind == "delete":
                target = self.workspace.resolve_existing(op.path)
                if target.path.is_dir():
                    raise ToolFailure("PATCH_FAILED", "Cannot delete a directory.", category="validation")
                staged[target.display] = None
                affected.append({"path": target.display, "operation": "delete"})
                summaries.append(f"D {target.display}")
            elif op.kind == "update":
                source = self.workspace.resolve_existing(op.path)
                if source.path.is_dir():
                    raise ToolFailure("PATCH_FAILED", "Cannot update a directory.", category="validation")
                current = staged.get(source.display)
                if current is None and source.display in staged:
                    raise ToolFailure("PATCH_FAILED", "Cannot update a deleted file.", category="validation")
                content = current if isinstance(current, str) else read_text_preserve_newlines(source.path)
                updated = apply_update_hunks(content, op.hunks, op.path)
                if op.move_to:
                    dest = self.workspace.resolve_for_write(op.move_to)
                    if dest.existed and dest.display != source.display:
                        raise ToolFailure("PATCH_FAILED", "Cannot move over an existing file.", category="validation")
                    staged[source.display] = None
                    staged[dest.display] = updated
                    affected.append({"path": dest.display, "old_path": source.display, "operation": "move"})
                    summaries.append(f"R {source.display} -> {dest.display}")
                else:
                    staged[source.display] = updated
                    affected.append({"path": source.display, "operation": "update"})
                    summaries.append(f"M {source.display}")
        if not affected:
            raise ToolFailure("PATCH_FAILED", "No files were modified.", category="validation")
        version_paths = set(staged) | set(expected_hashes) | set(expected_mtimes)
        pre_versions = self._collect_file_versions(version_paths)
        self._check_patch_preconditions(pre_versions, expected_hashes, expected_mtimes)
        checkpoint_id = secrets.token_urlsafe(12) if create_checkpoint and not dry_run else None
        summary = "\n".join(summaries)
        if not dry_run:
            backups = self._commit_staged_files(staged)
            if checkpoint_id is not None:
                self._store_patch_checkpoint(
                    checkpoint_id=checkpoint_id,
                    operation_id=operation_id,
                    summary=summary,
                    files=backups,
                )
        post_versions = self._collect_file_versions(set(staged)) if not dry_run else {}
        return {
            "dry_run": dry_run,
            "clean": True,
            "operation_id": operation_id,
            "checkpoint_id": checkpoint_id,
            "checkpoint_created": checkpoint_id is not None,
            "summary": summary,
            "affected_files": affected,
            "pre_versions": [pre_versions[path] for path in sorted(version_paths)],
            "post_versions": [post_versions[path] for path in sorted(post_versions)],
            "workspace": self.workspace_ref_payload(),
            "warnings": [],
        }

    def _validate_patch_path(self, raw_path: str, *, require_existing: bool) -> None:
        if require_existing:
            self.workspace.resolve_existing(raw_path)
        else:
            self.workspace.resolve_for_write(raw_path)

    def _commit_staged_files(self, staged: dict[str, str | None]) -> dict[str, bytes | None]:
        backups: dict[str, bytes | None] = {}
        try:
            for rel, content in staged.items():
                path = self.workspace.resolve_for_write(rel).path
                backups[rel] = path.read_bytes() if path.exists() and not path.is_dir() else None
                if rel not in self.patch_baselines:
                    baseline = backups[rel]
                    if baseline is None:
                        self.patch_baselines[rel] = None
                    else:
                        self.patch_baselines[rel] = baseline.decode("utf-8", errors="replace")
                if content is None:
                    if path.exists():
                        if path.is_dir():
                            raise ToolFailure("PATCH_FAILED", "Cannot delete a directory.", category="validation")
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("w", encoding="utf-8", newline="") as handle:
                        handle.write(content)
        except Exception:
            for rel, data in backups.items():
                try:
                    path = self.workspace.resolve_for_write(rel).path
                    if data is None:
                        if path.exists() and not path.is_dir():
                            path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                except OSError:
                    pass
            raise
        return backups

    def _restore_checkpoint_files(self, files: dict[str, bytes | None]) -> None:
        backups: dict[str, bytes | None] = {}
        try:
            for rel, data in files.items():
                path = self.workspace.resolve_for_write(rel).path
                backups[rel] = path.read_bytes() if path.exists() and not path.is_dir() else None
                if data is None:
                    if path.exists():
                        if path.is_dir():
                            raise ToolFailure("PATCH_FAILED", "Cannot restore over a directory.", category="validation")
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
        except Exception:
            for rel, data in backups.items():
                try:
                    path = self.workspace.resolve_for_write(rel).path
                    if data is None:
                        if path.exists() and not path.is_dir():
                            path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                except OSError:
                    pass
            raise

    def restore_patch_checkpoint(self, args: dict[str, Any]) -> dict[str, Any]:
        checkpoint_id = str(args.get("checkpoint_id", ""))
        with self.patch_checkpoints_lock:
            checkpoint = self.patch_checkpoints.get(checkpoint_id)
        if checkpoint is None:
            raise ToolFailure("CHECKPOINT_NOT_FOUND", "Patch checkpoint not found.", category="not_found")
        self._restore_checkpoint_files(checkpoint.files)
        versions = self._collect_file_versions(set(checkpoint.files))
        return {
            "checkpoint_id": checkpoint.checkpoint_id,
            "operation_id": checkpoint.operation_id,
            "created_at": utc_isoformat(checkpoint.created_at),
            "summary": checkpoint.summary,
            "restored_files": [versions[path] for path in sorted(versions)],
            "workspace": self.workspace_ref_payload(),
        }

    def exec_command(self, args: dict[str, Any]) -> dict[str, Any]:
        cmd = str(args.get("cmd", ""))
        if not cmd:
            raise ToolFailure("INVALID_ARGUMENT", "cmd is required.", category="validation")
        workdir = self.resolve_existing(str(args.get("workdir", ".")))
        if not workdir.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "workdir is not a directory.", category="validation")
        self._check_command_policy(cmd, args)
        timeout_ms = int(args.get("timeout_ms", 30000))
        yield_ms = int(args.get("yield_time_ms", 1000))
        max_output_bytes = int(args.get("max_output_bytes", 65536))
        tty = bool(args.get("tty", False))
        stdin_text = str(args.get("stdin", ""))
        env = self._command_env(args.get("env", {}))
        start = time.time()
        deadline = start + (timeout_ms / 1000.0)
        landlock_fd: int | None = None
        landlock_warning: str | None = None
        popen_cmd: Any = cmd
        popen_shell = True
        popen_extra = process_group_popen_kwargs()
        if self.landlock_enabled():
            try:
                landlock_fd = open_landlock_ruleset(
                    self.workspace.root,
                    guard_allow_roots(),
                    write_roots=self.landlock_write_roots(),
                )
                popen_cmd = landlock_exec_argv(landlock_fd, cmd)
                popen_shell = False
                popen_extra["pass_fds"] = (landlock_fd,)
            except ToolFailure as exc:
                if exc.code != "SANDBOX_UNAVAILABLE":
                    raise
                landlock_warning = landlock_unavailable_warning(exc)
        try:
            process = subprocess.Popen(
                popen_cmd,
                cwd=str(workdir.path),
                shell=popen_shell,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                **popen_extra,
            )
        finally:
            if landlock_fd is not None:
                try:
                    os.close(landlock_fd)
                except OSError:
                    pass
        session = self._make_session(
            process,
            command=cmd,
            workdir=str(workdir.path),
            timeout_at=deadline,
            warnings=[landlock_warning] if landlock_warning else None,
        )
        start_reader_threads(session)
        start_session_watchdog(session)
        if process.stdin is not None:
            try:
                if stdin_text:
                    process.stdin.write(stdin_text.encode("utf-8"))
                    process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                if not tty:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
        initial_wait = max(0, min(yield_ms, 30000)) / 1000.0

        def finish(status: str, **extra: Any) -> dict[str, Any]:
            payload = session.snapshot_since_cursor(max_output_bytes)
            payload["status"] = status
            payload["elapsed_ms"] = int((time.time() - start) * 1000)
            payload.update(extra)
            payload["workspace"] = self.workspace_ref_payload()
            self._add_exec_diagnostics(payload)
            return payload

        while True:
            if process.poll() is not None:
                session.refresh_status()
                session.drain_readers()
                return finish("timeout" if session.timed_out else "exited")
            now = time.time()
            if not tty and now >= deadline:
                session.timed_out = True
                self._terminate_process_group(process, signal.SIGTERM)
                session.refresh_status()
                session.drain_readers()
                return finish("timeout", timed_out=True)
            with session.lock:
                tty_has_initial_output = (
                    len(session.stdout) > session.stdout_cursor
                    or len(session.stderr) > session.stderr_cursor
                )
            if now - start >= initial_wait or (tty and tty_has_initial_output):
                with self.sessions_lock:
                    self.sessions[session.session_id] = session
                return finish("running")
            time.sleep(0.02)

    def _check_command_policy(self, cmd: str, args: dict[str, Any]) -> None:
        if self.dangerously_skip_all_permissions:
            return
        self._check_command_paths(cmd)
        env = args.get("env", {})
        if isinstance(env, dict) and any(
            is_filtered_env_var(str(key), str(value)) for key, value in env.items()
        ):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Sensitive or loader/startup environment variables require explicit permission.",
                category="permission",
                details={"permission": "sensitive_env", "env_keys": sorted(str(key) for key in env)},
            )
        if not self.capabilities.inline_script:
            inline_script = inline_script_command(cmd)
            if inline_script is not None:
                raise ToolFailure(
                    "PERMISSION_REQUIRED",
                    "Inline interpreter or shell code requires explicit permission because network and filesystem effects cannot be verified statically.",
                    category="permission",
                    details={"permission": INLINE_SCRIPT_PERMISSION, **inline_script},
                )
        compact = " ".join(cmd.split()).lower()
        if not self.capabilities.shell_expansion and SHELL_EXPANSION_RE.search(cmd):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Shell command substitution and parameter expansion require explicit permission.",
                category="permission",
                details={"permission": "shell_expansion", "command": compact},
            )
        if re.search(r"(^|[;&|]\s*)rm\s+(-[^\s]*r[^\s]*f|-?[^\s]*f[^\s]*r)\s+/", compact):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Destructive commands are blocked without explicit permission.",
                category="permission",
                details={"permission": "destructive_command", "command": compact},
            )
        if DESTRUCTIVE_RE.search(cmd):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Destructive commands are blocked without explicit permission.",
                category="permission",
                details={"permission": "destructive_command", "command": compact},
            )
        if not self.allow_network and NETWORK_RE.search(cmd) and not is_literal_network_reference_command(cmd):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Network access is denied by default.",
                category="permission",
                details={"permission": "network", "command": compact},
            )

    def _add_exec_diagnostics(self, payload: dict[str, Any]) -> None:
        status = str(payload.get("status") or "")
        exit_code = payload.get("exit_code")
        failure_kind = None
        if payload.get("timed_out") or status == "timeout":
            failure_kind = "timeout"
        elif isinstance(exit_code, int) and exit_code != 0 and status not in {"running", "terminating"}:
            failure_kind = "command_failed"
        payload["failure_kind"] = failure_kind
        diagnostics = exec_output_diagnostics(payload)
        if diagnostics:
            payload["diagnostics"] = diagnostics

    def _check_command_paths(self, cmd: str) -> None:
        try:
            tokens = shlex_split(cmd)
        except ValueError:
            tokens = cmd.split()
        for executable in command_executables(tokens):
            self._reject_setuid_executable(executable)
        for candidate in explicit_command_path_candidates(tokens):
            self._check_command_path_candidate(candidate)

    def _check_command_path_candidate(self, candidate: str) -> None:
        candidate = candidate.strip()
        if not candidate or candidate in {"-", "--"}:
            return
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", candidate):
            return
        normalized = candidate.replace("\\", "/")
        if normalized in SPECIAL_DEVICE_PATHS:
            return
        if self.is_allowed_command_tmp_path(normalized):
            return
        if (
            normalized.startswith("/")
            or normalized.startswith("~")
            or re.match(r"^[A-Za-z]:/", normalized)
            or any(part == ".." for part in PurePosixPath(normalized).parts)
        ):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Command path escapes the workspace and is blocked.",
                category="permission",
                details={"permission": "filesystem_escape", "path": candidate},
            )
        try:
            self.workspace.resolve_existing(normalized)
        except OSError as exc:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "Command path could not be inspected safely.",
                category="validation",
                details={"path": candidate[:200], "errno": exc.errno, "reason": exc.strerror},
            ) from exc
        except ToolFailure as exc:
            if exc.code == "NOT_FOUND":
                try:
                    self.workspace.resolve_for_write(normalized)
                except ToolFailure as write_exc:
                    if write_exc.code == "NOT_FOUND":
                        return
                    if write_exc.code in {"PATH_OUTSIDE_WORKSPACE", "ABSOLUTE_PATH_DENIED", "SYMLINK_ESCAPE"}:
                        raise ToolFailure(
                            "PERMISSION_REQUIRED",
                            "Command path escapes the workspace and is blocked.",
                            category="permission",
                            details={"permission": "filesystem_escape", "path": candidate},
                        ) from write_exc
                    raise
                return
            if exc.code in {"PATH_OUTSIDE_WORKSPACE", "ABSOLUTE_PATH_DENIED", "SYMLINK_ESCAPE"}:
                raise ToolFailure(
                    "PERMISSION_REQUIRED",
                    "Command path escapes the workspace and is blocked.",
                    category="permission",
                    details={"permission": "filesystem_escape", "path": candidate},
                ) from exc

    def _reject_setuid_executable(self, executable: str) -> None:
        if not executable:
            return
        executable_path = Path(executable) if "/" in executable else Path(shutil.which(executable) or "")
        if not str(executable_path):
            return
        try:
            stat = executable_path.stat()
        except OSError:
            return
        if stat.st_mode & 0o6000:
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Setuid/setgid executables are denied because they can bypass runtime process guards.",
                category="permission",
                details={"permission": "privileged_executable", "path": str(executable_path)},
            )

    def _command_env(self, extra: Any) -> dict[str, str]:
        env = self._base_command_env()
        if not self.dangerously_skip_all_permissions:
            env = {key: value for key, value in env.items() if not is_filtered_env_var(key, value)}
            env = {key: value for key, value in env.items() if key not in ECOSYSTEM_CACHE_ENV_NAMES}
        if self.shell_env_policy.exclude:
            env = {
                key: value
                for key, value in env.items()
                if not env_pattern_matches(key, self.shell_env_policy.exclude)
            }
        if self.shell_env_policy.include_only:
            env = {
                key: value
                for key, value in env.items()
                if env_pattern_matches(key, self.shell_env_policy.include_only)
            }
        env.update({canonical_command_env_name(str(key)): str(value) for key, value in self.shell_env_policy.set.items()})
        self._ensure_runtime_dirs()
        tmp_dir = self.command_tmp_dir()
        env["HOME"] = str(self.command_home_dir())
        env["TMPDIR"] = str(tmp_dir)
        if os.name == "nt":
            env["TEMP"] = str(tmp_dir)
            env["TMP"] = str(tmp_dir)
            system_root = env.get("SystemRoot") or env.get("WINDIR") or os.environ.get("SystemRoot") or os.environ.get("WINDIR")
            if not system_root and Path(r"C:\Windows\System32\cmd.exe").exists():
                system_root = r"C:\Windows"
            if system_root:
                env.setdefault("SystemRoot", system_root)
            comspec = env.get("ComSpec") or os.environ.get("ComSpec")
            if not comspec and system_root:
                candidate = Path(system_root) / "System32" / "cmd.exe"
                if candidate.exists():
                    comspec = str(candidate)
            if comspec:
                env.setdefault("ComSpec", comspec)
        if isinstance(extra, dict):
            for key, value in extra.items():
                key_text = str(key)
                value_text = str(value)
                if not self.dangerously_skip_all_permissions and is_filtered_env_var(key_text, value_text):
                    continue
                env[canonical_command_env_name(key_text)] = value_text
        return env

    def _base_command_env(self) -> dict[str, str]:
        if self.shell_env_policy.inherit == "none":
            return {}
        if self.shell_env_policy.inherit == "all":
            return {canonical_command_env_name(str(key)): str(value) for key, value in os.environ.items()}
        return {
            canonical_command_env_name(str(key)): str(value)
            for key, value in os.environ.items()
            if is_core_command_env_name(str(key))
        }

    def _make_session(
        self,
        process: subprocess.Popen[bytes],
        *,
        command: str = "",
        workdir: str = "",
        timeout_at: float | None = None,
        warnings: list[str] | None = None,
    ) -> ExecSession:
        return ExecSession(
            session_id=secrets.token_urlsafe(18),
            process=process,
            command=command,
            workdir=workdir,
            timeout_at=timeout_at,
            warnings=warnings or [],
        )

    def _decorate_session_payload(self, session: ExecSession, payload: dict[str, Any]) -> dict[str, Any]:
        payload["elapsed_ms"] = int((time.time() - session.started_at) * 1000)
        payload["workspace"] = self.workspace_ref_payload()
        self._add_exec_diagnostics(payload)
        return payload

    def command_status(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._get_session(session_id)
        wait_until = time.time() + (int(args.get("yield_time_ms", 0)) / 1000.0)
        while time.time() < wait_until and session.process.poll() is None:
            time.sleep(0.02)
        payload = session.snapshot_output(
            int(args.get("max_output_bytes", 65536)),
            consume=bool(args.get("consume", False)),
            from_start=bool(args.get("from_start", True)),
        )
        return self._decorate_session_payload(session, payload)

    def write_stdin(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._get_session(session_id)
        session.refresh_status()
        chars = str(args.get("chars", ""))
        if session.process.poll() is not None:
            if chars:
                raise ToolFailure("SESSION_CLOSED", "Session is closed; stdin write blocked.", category="runtime")
            payload = session.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))
            return self._decorate_session_payload(session, payload)
        if chars:
            if session.process.stdin is None or session.process.stdin.closed:
                raise ToolFailure("SESSION_CLOSED", "Session stdin is closed.", category="runtime")
            try:
                session.process.stdin.write(chars.encode("utf-8"))
                session.process.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise ToolFailure("SESSION_CLOSED", "Session stdin is closed.", category="runtime") from exc
        wait_until = time.time() + (int(args.get("yield_time_ms", 1000)) / 1000.0)
        first_output_at: float | None = None
        while time.time() < wait_until and session.process.poll() is None:
            time.sleep(0.02)
            with session.lock:
                has_new_output = len(session.stdout) > session.stdout_cursor or len(session.stderr) > session.stderr_cursor
                if has_new_output and not chars:
                    break
                if has_new_output and chars:
                    if first_output_at is None:
                        first_output_at = time.time()
                    if time.time() - first_output_at >= 0.05:
                        break
        payload = session.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))
        return self._decorate_session_payload(session, payload)

    def _wait_for_session_exit(self, session: ExecSession, wait_seconds: float) -> bool:
        wait_until = time.time() + max(0.0, wait_seconds)
        while time.time() < wait_until and session.process.poll() is None:
            time.sleep(0.02)
        session.refresh_status()
        session.drain_readers()
        return session.process.poll() is not None

    def kill_session(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._get_session(session_id)
        requested_signal = str(args.get("signal", "TERM"))
        signum = {"TERM": signal.SIGTERM, "KILL": FORCE_KILL_SIGNAL, "INT": signal.SIGINT}.get(
            requested_signal, signal.SIGTERM
        )
        evict = True
        signal_sent = signal_name(signum)
        if session.process.poll() is None:
            session.terminating = True
            self._terminate_process_group(session.process, signum)
            exited = self._wait_for_session_exit(session, int(args.get("wait_ms", 5000)) / 1000.0)
            if not exited and signum != FORCE_KILL_SIGNAL:
                signum = FORCE_KILL_SIGNAL
                signal_sent = signal_name(FORCE_KILL_SIGNAL)
                self._terminate_process_group(session.process, FORCE_KILL_SIGNAL)
                exited = self._wait_for_session_exit(session, int(args.get("kill_wait_ms", 2000)) / 1000.0)
            if exited:
                killed = True
                status = "killed" if signum == FORCE_KILL_SIGNAL else "terminated"
            else:
                killed = False
                evict = False
                status = "terminating"
        else:
            killed = False
            status = "exited"
        payload = session.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))
        payload.update({"killed": killed, "status": status, "evicted": evict, "signal_sent": signal_sent})
        if status == "terminating":
            warnings = list(payload.get("warnings", []))
            warnings.append("Process did not exit after TERM/SIGKILL; session retained for retry or watchdog cleanup.")
            payload["warnings"] = warnings
            payload["next_action"] = "retry kill_session or wait for watchdog cleanup"
        if evict:
            with self.sessions_lock:
                self.sessions.pop(session_id, None)
        return self._decorate_session_payload(session, payload)

    def cancel_session(self, session_id: str) -> None:
        with self.sessions_lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return
        session.refresh_status()
        if session.process.poll() is None:
            self._terminate_process_group(session.process, signal.SIGTERM)

    def _get_session(self, session_id: str) -> ExecSession:
        with self.sessions_lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise ToolFailure("SESSION_NOT_FOUND", "Session not found; stdin access denied.", category="not_found")
        return session

    def _terminate_process_group(self, process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
        terminate_process_group(process, signum)

    def git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_existing(str(args.get("path", ".")))
        max_entries = int(args.get("max_entries", 1000))
        include_untracked = bool(args.get("include_untracked", True))
        git = require_git()
        root_check = subprocess.run(
            [git, "-C", str(resolved.path), "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if root_check.returncode != 0:
            return {"is_repo": False, "clean": True, "entries": [], "truncated": False}
        status_cmd = [git, "-C", str(resolved.path), "status", "--porcelain=v1", "-b"]
        if not include_untracked:
            status_cmd.append("--untracked-files=no")
        completed = subprocess.run(status_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.strip() or "git status failed", category="runtime")
        lines = completed.stdout.splitlines()
        branch = ""
        upstream = ""
        ahead = 0
        behind = 0
        entries: list[dict[str, Any]] = []
        for line in lines:
            if line.startswith("## "):
                branch, upstream, ahead, behind = parse_branch_line(line[3:])
                continue
            if not line:
                continue
            path_text = line[3:]
            original = None
            if " -> " in path_text:
                original, path_text = path_text.split(" -> ", 1)
            entries.append(
                {
                    "path": path_text,
                    "original_path": original,
                    "index_status": line[0],
                    "worktree_status": line[1],
                }
            )
            if len(entries) >= max_entries:
                break
        return {
            "is_repo": True,
            "branch": branch,
            "head": git_rev_parse(resolved.path, "HEAD"),
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "clean": not entries,
            "entries": entries,
            "truncated": len(entries) >= max_entries and len(lines) > max_entries + 1,
        }

    def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        staged = bool(args.get("staged", False))
        unstaged = bool(args.get("unstaged", True))
        context = int(args.get("context_lines", 3))
        max_bytes = int(args.get("max_bytes", 262144))
        path_filters: list[str] = []
        if isinstance(args.get("path"), str):
            path_filters.append(str(args["path"]))
        if isinstance(args.get("paths"), list):
            path_filters.extend(str(item) for item in args["paths"])
        path_filters = [self.git_path_filter(path) for path in path_filters]
        if not is_git_repo(self.workspace.root):
            return self._fallback_diff(path_filters, max_bytes)
        chunks: list[bytes] = []
        if unstaged:
            chunks.append(self._run_git_diff(git, context, path_filters, cached=False))
        if staged:
            chunks.append(self._run_git_diff(git, context, path_filters, cached=True))
        combined = b""
        for chunk in chunks:
            if combined and chunk and not combined.endswith(b"\n"):
                combined += b"\n"
            combined += chunk
        diff_truncation = truncate_text_head(combined.decode("utf-8", errors="replace"), max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes)
        diff_text = diff_truncation.content
        truncated = diff_truncation.truncated
        return {
            "diff": diff_text,
            "files": parse_diff_files(diff_text),
            "truncated": truncated,
            "truncated_by": diff_truncation.truncated_by,
            "output_lines": diff_truncation.output_lines,
            "output_bytes": diff_truncation.output_bytes,
            "warnings": ["diff truncated"] if truncated else [],
        }

    def _run_git_diff(self, git: str, context: int, path_filters: list[str], *, cached: bool) -> bytes:
        cmd = [git, "-C", str(self.workspace.root), "diff", f"--unified={context}"]
        if cached:
            cmd.append("--cached")
        if path_filters:
            cmd.append("--")
            cmd.extend(path_filters)
        completed = subprocess.run(cmd, text=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if completed.returncode not in {0, 1}:
            raise ToolFailure("GIT_ERROR", completed.stderr.decode("utf-8", errors="replace"), category="runtime")
        return completed.stdout

    def _fallback_diff(self, path_filters: list[str], max_bytes: int) -> dict[str, Any]:
        selected = set(path_filters)
        chunks: list[str] = []
        files: list[dict[str, Any]] = []
        for rel, before in sorted(self.patch_baselines.items()):
            if selected and rel not in selected:
                continue
            current_path = self.workspace.resolve_for_write(rel).path
            after = read_text_preserve_newlines(current_path) if current_path.exists() and not current_path.is_dir() else None
            if before == after:
                continue
            before_lines = [] if before is None else before.splitlines(keepends=True)
            after_lines = [] if after is None else after.splitlines(keepends=True)
            chunks.extend(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                    lineterm="",
                )
            )
            status = "added" if before is None else "deleted" if after is None else "modified"
            files.append({"path": rel, "status": status, "binary": False})
        diff = "\n".join(chunks)
        if diff and not diff.endswith("\n"):
            diff += "\n"
        diff_truncation = truncate_text_head(diff, max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes)
        diff_text = diff_truncation.content
        truncated = diff_truncation.truncated
        return {
            "diff": diff_text,
            "files": files,
            "truncated": truncated,
            "truncated_by": diff_truncation.truncated_by,
            "output_lines": diff_truncation.output_lines,
            "output_bytes": diff_truncation.output_bytes,
            "warnings": ["non-git diff fallback"] + (["diff truncated"] if truncated else []),
        }

    def git_log(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        resolved = self.resolve_existing(str(args.get("path", ".")))
        if not is_git_repo(resolved.path):
            return {"is_repo": False, "commits": [], "truncated": False, "warnings": []}
        ref = validate_git_ref(str(args.get("ref", "HEAD")))
        max_count = int(args.get("max_count", 20))
        skip = int(args.get("skip", 0))
        path_filter = resolved.display
        cmd = [
            git,
            "-C",
            str(self.workspace.root),
            "log",
            f"--max-count={max_count + 1}",
            f"--skip={skip}",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ae%x1f%ad%x1f%s%x1e",
            ref,
        ]
        if path_filter != ".":
            cmd.extend(["--", path_filter])
        completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.strip() or "git log failed", category="runtime")
        commits: list[dict[str, Any]] = []
        for record in completed.stdout.split("\x1e"):
            fields = record.strip("\n").split("\x1f")
            if len(fields) < 6 or not fields[0]:
                continue
            commits.append(
                {
                    "hash": fields[0],
                    "short_hash": fields[1],
                    "author_name": fields[2],
                    "author_email": fields[3],
                    "author_date": fields[4],
                    "subject": fields[5],
                }
            )
        truncated = len(commits) > max_count
        return {
            "is_repo": True,
            "ref": ref,
            "path": path_filter,
            "commits": commits[:max_count],
            "truncated": truncated,
            "warnings": ["commit limit reached"] if truncated else [],
        }

    def git_show(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        if not is_git_repo(self.workspace.root):
            return {"is_repo": False, "content": "", "files": [], "truncated": False, "warnings": []}
        rev = validate_git_ref(str(args.get("rev", "HEAD")))
        context = int(args.get("context_lines", 3))
        max_bytes = int(args.get("max_bytes", 262144))
        include_diff = bool(args.get("include_diff", True))
        path_filters: list[str] = []
        if isinstance(args.get("path"), str):
            path_filters.append(str(args["path"]))
        if isinstance(args.get("paths"), list):
            path_filters.extend(str(item) for item in args["paths"])
        normalized_filters = [self.git_path_filter(path) for path in path_filters]
        cmd = [
            git,
            "-C",
            str(self.workspace.root),
            "show",
            "--no-ext-diff",
            "--format=fuller",
            f"--unified={context}",
        ]
        if not include_diff:
            cmd.append("--no-patch")
        cmd.append(rev)
        if normalized_filters:
            cmd.append("--")
            cmd.extend(normalized_filters)
        completed = subprocess.run(cmd, text=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.decode("utf-8", errors="replace").strip() or "git show failed", category="runtime")
        truncation = truncate_text_head(completed.stdout.decode("utf-8", errors="replace"), max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes)
        content = truncation.content
        return {
            "is_repo": True,
            "rev": rev,
            "content": content,
            "files": parse_diff_files(content),
            "truncated": truncation.truncated,
            "truncated_by": truncation.truncated_by,
            "output_lines": truncation.output_lines,
            "output_bytes": truncation.output_bytes,
            "warnings": ["output truncated"] if truncation.truncated else [],
        }

    def git_blame(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        resolved = self.resolve_existing(str(args.get("path", "")))
        if resolved.path.is_dir():
            raise ToolFailure("IS_DIRECTORY", "Path is a directory.", category="validation")
        if not is_git_repo(self.workspace.root):
            return {"is_repo": False, "path": resolved.display, "lines": [], "truncated": False, "warnings": []}
        ref_arg = args.get("rev")
        ref = validate_git_ref(str(ref_arg)) if isinstance(ref_arg, str) and ref_arg else None
        start_line = int(args.get("start_line", 1))
        end_line = args.get("end_line")
        max_lines = int(args.get("max_lines", 200))
        if end_line is None:
            final_line = start_line + max_lines - 1
        else:
            final_line = int(end_line)
        if final_line < start_line:
            raise ToolFailure("INVALID_ARGUMENT", "end_line must be >= start_line.", category="validation")
        requested_lines = final_line - start_line + 1
        truncated = requested_lines > max_lines
        final_line = min(final_line, start_line + max_lines - 1)
        cmd = [
            git,
            "-C",
            str(self.workspace.root),
            "blame",
            "--line-porcelain",
            "-L",
            f"{start_line},{final_line}",
        ]
        if ref:
            cmd.append(ref)
        cmd.extend(["--", resolved.display])
        completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.strip() or "git blame failed", category="runtime")
        lines = parse_git_blame_porcelain(completed.stdout)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated = True
        return {
            "is_repo": True,
            "path": resolved.display,
            "rev": ref,
            "start_line": start_line,
            "end_line": final_line,
            "lines": lines,
            "truncated": truncated,
            "warnings": ["line limit reached"] if truncated else [],
        }

    def request_permissions(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.dangerously_skip_all_permissions:
            return {
                "ok": True,
                "status": "granted",
                "grant_id": "dangerously-skip-all-permissions",
                "expires_at": None,
                "constraints": {
                    "mode": "dangerously_skip_all_permissions",
                    "workspace": str(self.workspace.root),
                    "requested": args,
                },
                "warnings": [
                    "dangerously-skip-all-permissions is enabled; permission-gated operations are auto-granted"
                ],
            }
        return {
            "ok": False,
            "status": "unsupported",
            "grant_id": None,
            "expires_at": None,
            "error": {
                "code": "ELICITATION_UNSUPPORTED",
                "message": "Permission elicitation is not available for this client.",
                "category": "permission",
                "retryable": False,
                "details": {"requested": args},
            },
        }

    def view_image(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_existing(str(args.get("path", "")))
        max_bytes = int(args.get("max_bytes", 5_242_880))
        max_width = int(args.get("max_width", IMAGE_RESIZE_MAX_DIMENSION))
        max_height = int(args.get("max_height", IMAGE_RESIZE_MAX_DIMENSION))
        auto_resize = bool(args.get("auto_resize", True))
        data = resolved.path.read_bytes()
        mime_type, width, height = identify_image(data, resolved.path)
        if mime_type is None:
            raise ToolFailure("BINARY_FILE", "File is not a supported image.", category="validation")
        original = {"bytes": len(data), "width": width, "height": height, "mime_type": mime_type}
        resized = False
        warnings: list[str] = []
        if auto_resize and should_resize_image(len(data), width, height, max_bytes, max_width, max_height):
            resized_data = resize_image_bytes(data, mime_type, max_width=max_width, max_height=max_height, max_bytes=max_bytes)
            if resized_data is not None:
                data, mime_type = resized_data
                mime_type, width, height = identify_image(data, resolved.path)
                resized = True
            else:
                warnings.append("auto_resize requested but Pillow is not installed or image resize failed")
        if len(data) > max_bytes:
            raise ToolFailure(
                "OUTPUT_TOO_LARGE",
                "Image exceeds max_bytes.",
                category="validation",
                details={"bytes": len(data), "max_bytes": max_bytes, "resize_attempted": auto_resize, "warnings": warnings},
            )
        encoded = base64.b64encode(data).decode("ascii")
        payload: dict[str, Any] = {
            "path": resolved.display,
            "mime_type": mime_type,
            "bytes": len(data),
            "width": width,
            "height": height,
            "resized": resized,
            "original": original,
            "base64": encoded,
            "data_url": f"data:{mime_type};base64,{encoded}",
            "warnings": warnings,
        }
        return payload


@dataclass
class PatchOperation:
    kind: str
    path: str
    add_content: str | None = None
    hunks: list[list[str]] = field(default_factory=list)
    move_to: str | None = None


def parse_patch(patch: str) -> list[PatchOperation]:
    lines = patch.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise ToolFailure("PATCH_FAILED", "Patch must use *** Begin Patch / *** End Patch envelope.", category="validation")
    operations: list[PatchOperation] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if not line:
            i += 1
            continue
        if line.startswith("*** Add File: "):
            path = line.removeprefix("*** Add File: ").strip()
            i += 1
            content_lines: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ToolFailure("PATCH_FAILED", "Add file lines must start with '+'.", category="validation")
                content_lines.append(lines[i][1:])
                i += 1
            operations.append(PatchOperation("add", path, add_content="\n".join(content_lines) + "\n"))
            continue
        if line.startswith("*** Delete File: "):
            path = line.removeprefix("*** Delete File: ").strip()
            operations.append(PatchOperation("delete", path))
            i += 1
            continue
        if line.startswith("*** Update File: "):
            path = line.removeprefix("*** Update File: ").strip()
            i += 1
            move_to: str | None = None
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                move_to = lines[i].removeprefix("*** Move to: ").strip()
                i += 1
            hunks: list[list[str]] = []
            current: list[str] = []
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if current:
                        hunks.append(current)
                    current = []
                else:
                    current.append(lines[i])
                i += 1
            if current:
                hunks.append(current)
            operations.append(PatchOperation("update", path, hunks=hunks, move_to=move_to))
            continue
        raise ToolFailure("PATCH_FAILED", f"Unrecognized patch line: {line}", category="validation")
    return operations


@dataclass(frozen=True)
class ParsedHunk:
    old: list[str]
    new: list[str]


@dataclass(frozen=True)
class MatchedHunk:
    hunk_index: int
    start: int
    end: int
    new: list[str]


def apply_update_hunks(content: str, hunks: list[list[str]], path: str = "<patch>") -> str:
    if not hunks:
        return content
    bom, text = strip_bom(content)
    line_ending = detect_line_ending(text)
    normalized = normalize_to_lf(text)
    had_trailing_newline = normalized.endswith("\n")
    lines = normalized.splitlines()
    parsed = [parse_update_hunk(hunk) for hunk in hunks]
    matched: list[MatchedHunk] = []
    for index, hunk in enumerate(parsed):
        if not hunk.old:
            match_start = 0
            match_count = 1
        else:
            matches = find_subsequence_all(lines, hunk.old)
            match_count = len(matches)
            match_start = matches[0] if matches else -1
        if match_start < 0:
            raise ToolFailure("PATCH_FAILED", f"Patch context did not match in {path}.", category="validation")
        if match_count > 1:
            raise ToolFailure(
                "PATCH_FAILED",
                f"Patch context matched {match_count} locations in {path}; add more context.",
                category="validation",
            )
        matched.append(MatchedHunk(index, match_start, match_start + len(hunk.old), hunk.new))

    matched.sort(key=lambda item: item.start)
    for previous, current in zip(matched, matched[1:]):
        if previous.end > current.start:
            raise ToolFailure(
                "PATCH_FAILED",
                f"Patch hunks {previous.hunk_index} and {current.hunk_index} overlap in {path}.",
                category="validation",
            )

    updated_lines = list(lines)
    for matched_hunk in sorted(matched, key=lambda item: item.start, reverse=True):
        updated_lines = updated_lines[: matched_hunk.start] + matched_hunk.new + updated_lines[matched_hunk.end :]
    updated = "\n".join(updated_lines)
    if had_trailing_newline and (updated_lines or updated == ""):
        updated += "\n"
    elif not text and updated_lines:
        updated += "\n"
    return bom + restore_line_endings(updated, line_ending)


def parse_update_hunk(hunk: list[str]) -> ParsedHunk:
    old: list[str] = []
    new: list[str] = []
    for raw in hunk:
        if raw == "*** End of File":
            continue
        if not raw:
            raise ToolFailure("PATCH_FAILED", "Invalid empty patch line.", category="validation")
        marker = raw[0]
        value = raw[1:] if marker in {" ", "-", "+"} else raw
        if marker == " ":
            old.append(value)
            new.append(value)
        elif marker == "-":
            old.append(value)
        elif marker == "+":
            new.append(value)
        else:
            raise ToolFailure("PATCH_FAILED", "Update lines must start with space, '-' or '+'.", category="validation")
    return ParsedHunk(old=old, new=new)


def find_subsequence(lines: list[str], needle: list[str]) -> int:
    matches = find_subsequence_all(lines, needle)
    return matches[0] if matches else -1


def find_subsequence_all(lines: list[str], needle: list[str]) -> list[int]:
    if not needle:
        return [0]
    limit = len(lines) - len(needle) + 1
    matches: list[int] = []
    for index in range(max(0, limit)):
        if lines[index : index + len(needle)] == needle:
            matches.append(index)
    return matches


def walk_files(root: Path) -> list[Path]:
    if root.is_file() or root.is_symlink():
        return [root]
    results: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in DEFAULT_EXCLUDED_NAMES]
        current_path = Path(current)
        for name in files:
            results.append(current_path / name)
    return results


def find_literal(line: str, query: str, case_sensitive: bool) -> Any:
    haystack = line if case_sensitive else line.lower()
    needle = query if case_sensitive else query.lower()
    index = haystack.find(needle)
    if index < 0:
        return None

    class Match:
        def start(self) -> int:
            return index

    return Match()


def shlex_split(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def command_executables(tokens: list[str]) -> list[str]:
    executables: list[str] = []
    expect_command = True
    for index, token in enumerate(tokens):
        if not token:
            continue
        if token in SHELL_CONTROL_TOKENS:
            expect_command = True
            continue
        if token in REDIRECTION_TOKENS or token in HEREDOC_TOKENS:
            expect_command = False
            continue
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in REDIRECTION_TOKENS:
            continue
        if expect_command:
            if is_env_assignment_token(token):
                continue
            executables.append(token)
            expect_command = False
    return executables


def explicit_command_path_candidates(tokens: list[str]) -> list[str]:
    candidates: list[str] = []
    index = 0
    current_command: str | None = None
    current_args: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token in SHELL_CONTROL_TOKENS:
            candidates.extend(command_argument_path_candidates(current_command, current_args))
            current_command = None
            current_args = []
            index += 1
            continue
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in REDIRECTION_TOKENS:
            index += 1
            continue
        if token in REDIRECTION_TOKENS:
            if index + 1 < len(tokens):
                candidates.append(tokens[index + 1])
            index += 2
            continue
        if token in HEREDOC_TOKENS:
            index += 2
            continue
        if current_command is None:
            if not is_env_assignment_token(token):
                current_command = token
        else:
            current_args.append(token)
        index += 1
    candidates.extend(command_argument_path_candidates(current_command, current_args))
    return list(dict.fromkeys(candidates))


def command_argument_path_candidates(command: str | None, args: list[str]) -> list[str]:
    if not command:
        return []
    name = PurePosixPath(command.replace("\\", "/")).name.lower()
    if name == "env":
        candidates, wrapped_command, wrapped_args = env_wrapped_command(args)
        if wrapped_command is not None:
            candidates.extend(command_argument_path_candidates(wrapped_command, wrapped_args))
        return candidates
    if name in PATH_ARGUMENT_COMMANDS:
        return [arg for arg in args if is_inspectable_path_argument(arg)]
    if name in PATTERN_THEN_PATH_COMMANDS:
        return pattern_command_path_candidates(args)
    if name == "find":
        return find_command_path_candidates(args)
    if name in SCRIPT_COMMANDS:
        return script_command_path_candidates(name, args)
    return []


def inline_script_command(command: str) -> dict[str, str] | None:
    try:
        tokens = shlex_split(command)
    except ValueError:
        tokens = command.split()
    index = 0
    current_command: str | None = None
    current_args: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token in SHELL_CONTROL_TOKENS:
            result = inline_script_segment(current_command, current_args)
            if result is not None:
                return result
            current_command = None
            current_args = []
            index += 1
            continue
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in REDIRECTION_TOKENS:
            index += 1
            continue
        if token in HEREDOC_TOKENS:
            result = stdin_script_segment(current_command, current_args, token)
            if result is not None:
                return result
            index += 2
            continue
        if token in REDIRECTION_TOKENS:
            index += 2
            continue
        if current_command is None:
            if not is_env_assignment_token(token):
                current_command = token
        else:
            current_args.append(token)
        index += 1
    return inline_script_segment(current_command, current_args)


def inline_script_segment(command: str | None, args: list[str]) -> dict[str, str] | None:
    if not command:
        return None
    name = PurePosixPath(command.replace("\\", "/")).name.lower()
    if name == "env":
        _candidates, wrapped_command, wrapped_args = env_wrapped_command(args)
        return inline_script_segment(wrapped_command, wrapped_args)
    if name in {"bash", "sh", "zsh"}:
        for arg in args:
            if arg.startswith("-") and "c" in arg.lstrip("-"):
                return {"command": name, "option": arg}
        return None
    if name in {"python", "python3"}:
        if "-c" in args:
            return {"command": name, "option": "-c"}
        if "-" in args:
            return {"command": name, "option": "-"}
        return None
    if name == "node":
        for option in ("-e", "--eval", "-p", "--print"):
            if option in args:
                return {"command": name, "option": option}
    if name in {"ruby", "perl"} and "-e" in args:
        return {"command": name, "option": "-e"}
    return None


def env_wrapped_command(args: list[str]) -> tuple[list[str], str | None, list[str]]:
    candidates: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in {"-S", "--split-string"}:
            if index + 1 >= len(args):
                return candidates, None, []
            return env_split_command(candidates, args[index + 1])
        if arg.startswith("--split-string="):
            return env_split_command(candidates, arg.split("=", 1)[1])
        if arg.startswith("-S") and arg != "-S":
            return env_split_command(candidates, arg[2:])
        if arg in {"-C", "--chdir"}:
            if index + 1 >= len(args):
                return candidates, None, []
            candidates.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("--chdir="):
            candidates.append(arg.split("=", 1)[1])
            index += 1
            continue
        if arg.startswith("-C") and arg != "-C":
            candidates.append(arg[2:])
            index += 1
            continue
        if arg in ENV_OPTIONS_WITH_ARGUMENT:
            index += 2
            continue
        if any(arg.startswith(f"{option}=") for option in ENV_LONG_OPTIONS_WITH_ARGUMENT):
            index += 1
            continue
        if any(arg.startswith(f"{option}=") for option in ENV_LONG_OPTIONS_WITH_OPTIONAL_ARGUMENT):
            index += 1
            continue
        if any(arg.startswith(prefix) and arg != prefix for prefix in ENV_SHORT_OPTIONS_WITH_ATTACHED_ARGUMENT):
            index += 1
            continue
        if arg in ENV_FLAG_OPTIONS:
            index += 1
            continue
        if arg.startswith("-") or is_env_assignment_token(arg):
            index += 1
            continue
        return candidates, arg, args[index + 1 :]
    if index < len(args):
        return candidates, args[index], args[index + 1 :]
    return candidates, None, []


def env_split_command(candidates: list[str], command: str) -> tuple[list[str], str | None, list[str]]:
    try:
        tokens = shlex_split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return candidates, None, []
    return candidates, tokens[0], tokens[1:]


def stdin_script_segment(command: str | None, args: list[str], redirection: str) -> dict[str, str] | None:
    if not command:
        return None
    name = PurePosixPath(command.replace("\\", "/")).name.lower()
    if name not in SCRIPT_COMMANDS:
        return None
    if name in {"python", "python3"} and "-m" in args:
        return None
    for arg in args:
        if not arg.startswith("-") or arg == "-":
            return None
    return {"command": name, "option": redirection}


def pattern_command_path_candidates(args: list[str]) -> list[str]:
    candidates: list[str] = []
    pattern_consumed = False
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-e", "-f", "--regexp", "--file", "-g", "--glob"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if not pattern_consumed:
            pattern_consumed = True
            continue
        if is_inspectable_path_argument(arg):
            candidates.append(arg)
    return candidates


def find_command_path_candidates(args: list[str]) -> list[str]:
    candidates: list[str] = []
    for arg in args:
        if arg in {"!", "(", ")"} or arg.startswith("-"):
            break
        if is_inspectable_path_argument(arg):
            candidates.append(arg)
    return candidates


def script_command_path_candidates(command_name: str, args: list[str]) -> list[str]:
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if command_name in {"bash", "sh", "zsh"} and arg.startswith("-") and "c" in arg.lstrip("-"):
            return []
        if command_name in {"python", "python3"} and arg == "-c":
            return []
        if command_name == "node" and arg in {"-e", "--eval", "-p", "--print"}:
            return []
        if command_name in {"ruby", "perl"} and arg == "-e":
            return []
        if arg in {"-m", "--require", "-r"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if command_name.startswith("python") and arg == "-":
            return []
        return [arg] if is_inspectable_path_argument(arg) else []
    return []


def is_env_assignment_token(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def is_inspectable_path_argument(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    normalized = token.replace("\\", "/")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", normalized):
        return False
    if normalized.startswith(("/", "~", "./", "../")) or re.match(r"^[A-Za-z]:/", normalized):
        return True
    if "/" in normalized:
        return True
    return "." in PurePosixPath(normalized).name


def is_literal_network_reference_command(command: str) -> bool:
    try:
        tokens = shlex_split(command)
    except ValueError:
        return False
    executables = command_executables(tokens)
    if not executables:
        return False
    return all(
        PurePosixPath(executable.replace("\\", "/")).name.lower() in NETWORK_LITERAL_COMMANDS
        for executable in executables
    )


def entry_for_path(path: Path, root: Path) -> dict[str, Any]:
    stat = path.lstat()
    if path.is_symlink():
        kind = "symlink"
    elif path.is_dir():
        kind = "directory"
    elif path.is_file():
        kind = "file"
    else:
        kind = "other"
    item: dict[str, Any] = {
        "name": path.name,
        "path": normalize_rel_display(path, root),
        "type": kind,
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "is_hidden": path.name.startswith("."),
        "is_ignored": False,
    }
    if path.is_symlink():
        try:
            item["symlink_target"] = os.readlink(path)
        except OSError:
            pass
    return item


def sort_value(item: dict[str, Any], sort_key: str) -> Any:
    if sort_key == "type":
        return (item.get("type", ""), item.get("name", ""))
    if sort_key == "modified":
        return (item.get("modified", ""), item.get("name", ""))
    return item.get("name", "")


def parse_branch_line(line: str) -> tuple[str, str, int, int]:
    branch = line
    upstream = ""
    ahead = 0
    behind = 0
    if "..." in line:
        branch, rest = line.split("...", 1)
        upstream = rest.split(" ", 1)[0]
    if "[" in line and "]" in line:
        meta = line.split("[", 1)[1].split("]", 1)[0]
        ahead_match = re.search(r"ahead (\d+)", meta)
        behind_match = re.search(r"behind (\d+)", meta)
        ahead = int(ahead_match.group(1)) if ahead_match else 0
        behind = int(behind_match.group(1)) if behind_match else 0
    return branch.strip(), upstream.strip(), ahead, behind


def git_rev_parse(path: Path, rev: str) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    completed = subprocess.run([git, "-C", str(path), "rev-parse", rev], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def is_git_repo(path: Path) -> bool:
    git = shutil.which("git")
    if not git:
        return False
    completed = subprocess.run(
        [git, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def require_git() -> str:
    git = shutil.which("git")
    if not git:
        raise ToolFailure("GIT_ERROR", "git executable not found.", category="runtime")
    return git


def validate_git_ref(ref: str) -> str:
    if not ref or ref.startswith("-") or "\x00" in ref or "\n" in ref or "\r" in ref:
        raise ToolFailure("INVALID_ARGUMENT", "Invalid git revision.", category="validation")
    return ref


def parse_git_blame_porcelain(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for raw in output.splitlines():
        parts = raw.split()
        if len(parts) >= 3 and re.fullmatch(r"[0-9a-fA-F^]{40}", parts[0]):
            current = {
                "commit": parts[0].lstrip("^"),
                "original_line": int(parts[1]) if parts[1].isdigit() else None,
                "line": int(parts[2]) if parts[2].isdigit() else None,
            }
            continue
        if raw.startswith("author "):
            current["author"] = raw.removeprefix("author ")
            continue
        if raw.startswith("author-mail "):
            current["author_mail"] = raw.removeprefix("author-mail ").strip("<>")
            continue
        if raw.startswith("author-time "):
            value = raw.removeprefix("author-time ")
            current["author_time"] = int(value) if value.isdigit() else value
            continue
        if raw.startswith("summary "):
            current["summary"] = raw.removeprefix("summary ")
            continue
        if raw.startswith("\t"):
            row = dict(current)
            row["content"] = raw[1:]
            rows.append(row)
    return rows


def redact_for_trace(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_ENV_RE.search(str(key)) else redact_for_trace(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_trace(item) for item in value[:50]]
    if isinstance(value, tuple):
        return [redact_for_trace(item) for item in value[:50]]
    if isinstance(value, str):
        if SENSITIVE_VALUE_RE.search(value):
            return "[REDACTED]"
        if len(value) > 240:
            return value[:240] + "...[truncated]"
        return value
    return value


def redact_for_transcript(value: Any, *, max_string: int = 4000) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                result["..."] = "[TRUNCATED]"
                break
            key_text = str(key)
            if SENSITIVE_ENV_RE.search(key_text):
                result[key_text] = "[REDACTED]"
            elif key_text in {"base64", "markdown"}:
                result[key_text] = "[OMITTED]"
            else:
                result[key_text] = redact_for_transcript(item, max_string=max_string)
        return result
    if isinstance(value, list):
        items = [redact_for_transcript(item, max_string=max_string) for item in value[:100]]
        if len(value) > 100:
            items.append("[TRUNCATED]")
        return items
    if isinstance(value, tuple):
        items = [redact_for_transcript(item, max_string=max_string) for item in value[:100]]
        if len(value) > 100:
            items.append("[TRUNCATED]")
        return items
    if isinstance(value, str):
        if SENSITIVE_VALUE_RE.search(value):
            return "[REDACTED]"
        if len(value) > max_string:
            return value[:max_string] + "...[truncated]"
        return value
    return value


class LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


_LIBC: Any | None = None


def landlock_libc() -> Any:
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(None, use_errno=True)
    return _LIBC


def libc_syscall(number: int, *args: Any) -> int:
    ctypes.set_errno(0)
    return int(landlock_libc().syscall(number, *args))


def landlock_abi_version() -> int:
    if sys.platform != "linux":
        raise ToolFailure(
            "SANDBOX_UNAVAILABLE",
            "Linux Landlock filesystem confinement is unavailable on this platform.",
            category="security",
        )
    version = libc_syscall(SYS_LANDLOCK_CREATE_RULESET, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    if version <= 0:
        err = ctypes.get_errno()
        raise ToolFailure(
            "SANDBOX_UNAVAILABLE",
            "Linux Landlock filesystem confinement is unavailable on this host.",
            category="security",
            details={"errno": err, "reason": os.strerror(err) if err else "unknown"},
        )
    return version


def landlock_handled_access(version: int) -> int:
    handled = (
        LANDLOCK_ACCESS_FS_EXECUTE
        | LANDLOCK_ACCESS_FS_WRITE_FILE
        | LANDLOCK_ACCESS_FS_READ_FILE
        | LANDLOCK_ACCESS_FS_READ_DIR
        | LANDLOCK_ACCESS_FS_REMOVE_DIR
        | LANDLOCK_ACCESS_FS_REMOVE_FILE
        | LANDLOCK_ACCESS_FS_MAKE_CHAR
        | LANDLOCK_ACCESS_FS_MAKE_DIR
        | LANDLOCK_ACCESS_FS_MAKE_REG
        | LANDLOCK_ACCESS_FS_MAKE_SOCK
        | LANDLOCK_ACCESS_FS_MAKE_FIFO
        | LANDLOCK_ACCESS_FS_MAKE_BLOCK
        | LANDLOCK_ACCESS_FS_MAKE_SYM
    )
    if version >= 2:
        handled |= LANDLOCK_ACCESS_FS_REFER
    if version >= 3:
        handled |= LANDLOCK_ACCESS_FS_TRUNCATE
    if version >= 5:
        handled |= LANDLOCK_ACCESS_FS_IOCTL_DEV
    return handled


def landlock_device_access(handled: int) -> int:
    readonly_file_access = handled & (LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE)
    return readonly_file_access | (
        handled
        & (
            LANDLOCK_ACCESS_FS_WRITE_FILE
            | LANDLOCK_ACCESS_FS_TRUNCATE
            | LANDLOCK_ACCESS_FS_IOCTL_DEV
        )
    )


def open_landlock_ruleset(workspace: Path, read_roots: list[str], *, write_roots: list[Path] | None = None) -> int:
    version = landlock_abi_version()
    handled = landlock_handled_access(version)
    ruleset_attr = LandlockRulesetAttr(handled)
    ruleset_fd = libc_syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        raise ToolFailure(
            "SANDBOX_UNAVAILABLE",
            "Failed to create Linux Landlock ruleset for exec_command.",
            category="security",
            details={"errno": err, "reason": os.strerror(err) if err else "unknown"},
        )
    try:
        workspace_access = handled
        readonly_access = handled & (
            LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR
        )
        device_access = landlock_device_access(handled)
        add_landlock_path(ruleset_fd, workspace, workspace_access)
        for root in write_roots or []:
            add_landlock_path(ruleset_fd, root, workspace_access, required=False)
        for root in read_roots:
            add_landlock_path(ruleset_fd, Path(root), readonly_access, required=False)
        for special in SPECIAL_DEVICE_PATHS:
            add_landlock_path(ruleset_fd, Path(special), device_access, required=False)
        for special_dir in ("/proc/self", "/proc/thread-self", "/dev/fd"):
            add_landlock_path(ruleset_fd, Path(special_dir), readonly_access, required=False)
    except Exception:
        os.close(ruleset_fd)
        raise
    return ruleset_fd


def add_landlock_path(ruleset_fd: int, path: Path, allowed_access: int, *, required: bool = True) -> None:
    try:
        fd = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if required:
            raise ToolFailure(
                "SANDBOX_UNAVAILABLE",
                "Failed to open path while preparing Landlock sandbox.",
                category="security",
                details={"path": str(path), "errno": exc.errno, "reason": exc.strerror},
            ) from exc
        return
    try:
        path_attr = LandlockPathBeneathAttr(allowed_access & landlock_path_allowed_access(path), fd)
        rc = libc_syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(path_attr), 0)
        if rc < 0 and required:
            err = ctypes.get_errno()
            raise ToolFailure(
                "SANDBOX_UNAVAILABLE",
                "Failed to add path to Landlock sandbox.",
                category="security",
                details={"path": str(path), "errno": err, "reason": os.strerror(err) if err else "unknown"},
            )
    finally:
        os.close(fd)


def landlock_path_allowed_access(path: Path) -> int:
    try:
        mode = path.stat().st_mode
    except OSError:
        return ~0
    if stat.S_ISDIR(mode):
        return ~0
    return (
        LANDLOCK_ACCESS_FS_EXECUTE
        | LANDLOCK_ACCESS_FS_WRITE_FILE
        | LANDLOCK_ACCESS_FS_READ_FILE
        | LANDLOCK_ACCESS_FS_TRUNCATE
        | LANDLOCK_ACCESS_FS_IOCTL_DEV
    )


def restrict_self_with_landlock(ruleset_fd: int) -> None:
    rc = int(landlock_libc().prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
    if rc != 0:
        os._exit(126)
    rc = libc_syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    if rc != 0:
        os._exit(126)
    try:
        os.close(ruleset_fd)
    except OSError:
        pass


def landlock_exec_argv(ruleset_fd: int, cmd: str) -> list[str]:
    helper = Path(__file__).with_name("landlock_exec.py")
    return [sys.executable, str(helper), str(ruleset_fd), cmd]


def is_default_system_path_root(resolved: Path) -> bool:
    for prefix_path in _resolved_system_path_root_prefixes():
        if resolved == prefix_path or is_relative_to(resolved, prefix_path):
            return True
    return False


@functools.lru_cache(maxsize=1)
def _resolved_system_path_root_prefixes() -> tuple[Path, ...]:
    prefixes: list[Path] = []
    for prefix in SYSTEM_PATH_ROOT_PREFIXES:
        try:
            prefixes.append(Path(prefix).resolve())
        except OSError:
            prefixes.append(Path(prefix))
    return tuple(prefixes)


def guard_allow_roots() -> list[str]:
    roots = set(TOOLCHAIN_READ_ROOTS)
    roots.update(GIT_READ_ROOTS)
    roots.update(DNS_RESOLVER_READ_ROOTS)
    roots.update(
        {
            str(Path(sys.executable).resolve().parent),
            str(Path(sys.prefix).resolve()),
            str(Path(sys.base_prefix).resolve()),
        }
    )
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        try:
            resolved_java_home = Path(java_home).expanduser().resolve()
        except OSError:
            pass
        else:
            roots.add(str(resolved_java_home))
    for item in os.environ.get("PATH", "").split(os.pathsep):
        if not item:
            continue
        try:
            resolved = Path(item).resolve()
        except OSError:
            continue
        if resolved.is_dir() and is_default_system_path_root(resolved):
            roots.add(str(resolved))
    for item in os.environ.get(f"{ENV_PREFIX}_EXEC_ALLOW_ROOTS", "").split(os.pathsep):
        if not item:
            continue
        try:
            resolved = Path(item).expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir():
            roots.add(str(resolved))
    return sorted(root for root in roots if root and Path(root).is_absolute())


def parse_diff_files(diff_text: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                current = {"path": path, "status": "modified", "binary": False}
                files.append(current)
        elif current is not None and line.startswith("new file mode"):
            current["status"] = "added"
        elif current is not None and line.startswith("deleted file mode"):
            current["status"] = "deleted"
        elif current is not None and line.startswith("Binary files"):
            current["binary"] = True
    return files


def start_reader_threads(session: ExecSession) -> None:
    def reader(stream: Any, append: Any) -> None:
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                append(chunk)
        except Exception:
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass

    if session.process.stdout is not None:
        thread = threading.Thread(target=reader, args=(session.process.stdout, session.append_stdout), daemon=True)
        session.reader_threads.append(thread)
        thread.start()
    if session.process.stderr is not None:
        thread = threading.Thread(target=reader, args=(session.process.stderr, session.append_stderr), daemon=True)
        session.reader_threads.append(thread)
        thread.start()


def start_session_watchdog(session: ExecSession) -> None:
    if session.timeout_at is None:
        return

    def watchdog() -> None:
        delay = session.timeout_at - time.time() if session.timeout_at is not None else 0
        if delay > 0:
            time.sleep(delay)
        if session.process.poll() is not None or session.timed_out:
            return
        session.timed_out = True
        terminate_process_group(session.process, signal.SIGTERM)
        session.refresh_status()

    threading.Thread(target=watchdog, daemon=True).start()


def identify_image(data: bytes, path: Path) -> tuple[str | None, int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return "image/png", width, height
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return "image/gif", width, height
    if data.startswith(b"\xff\xd8"):
        width, height = identify_jpeg_size(data)
        return "image/jpeg", width, height
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        width, height = identify_webp_size(data)
        return "image/webp", width, height
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed, None, None
    return None, None, None


def identify_jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    while index + 9 < len(data):
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA or index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        } and segment_length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        index += segment_length
    return None, None


def identify_webp_size(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 30:
        return None, None
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None, None


def should_resize_image(
    size_bytes: int,
    width: int | None,
    height: int | None,
    max_bytes: int,
    max_width: int,
    max_height: int,
) -> bool:
    if size_bytes > max_bytes:
        return True
    if width is not None and width > max_width:
        return True
    if height is not None and height > max_height:
        return True
    return False


def resize_image_bytes(
    data: bytes,
    mime_type: str,
    *,
    max_width: int,
    max_height: int,
    max_bytes: int,
) -> tuple[bytes, str] | None:
    try:
        from io import BytesIO
        from PIL import Image  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        image = Image.open(BytesIO(data))
        image.thumbnail((max_width, max_height))
        output = BytesIO()
        output_format = "JPEG" if mime_type == "image/jpeg" else "PNG" if mime_type == "image/png" else "WEBP"
        save_kwargs: dict[str, Any] = {}
        if output_format in {"JPEG", "WEBP"}:
            save_kwargs["quality"] = 85
            save_kwargs["optimize"] = True
        if output_format == "JPEG" and image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        image.save(output, format=output_format, **save_kwargs)
        resized = output.getvalue()
        if len(resized) > max_bytes and output_format in {"JPEG", "WEBP"}:
            for quality in (75, 65, 55):
                output = BytesIO()
                image.save(output, format=output_format, quality=quality, optimize=True)
                resized = output.getvalue()
                if len(resized) <= max_bytes:
                    break
        return resized, mime_type
    except Exception:
        return None


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def invalid_request_response() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}


def validate_rpc_envelope(request: dict[str, Any]) -> None:
    if request.get("jsonrpc") != "2.0":
        raise JsonRpcError(-32600, "Invalid Request: jsonrpc must be 2.0", {"reason": "jsonrpc_version"})
    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(-32600, "Invalid Request: method must be a string", {"reason": "method"})
    if "id" in request and not (
        request["id"] is None
        or isinstance(request["id"], str)
        or (isinstance(request["id"], int) and not isinstance(request["id"], bool))
    ):
        raise JsonRpcError(-32600, "Invalid Request: id must be string, integer, or null", {"reason": "id"})


def rpc_params(request: dict[str, Any]) -> dict[str, Any]:
    params = request.get("params", {})
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise JsonRpcError(-32602, "MCP method params must be an object")
    return params


def validate_initialize_params(params: dict[str, Any]) -> None:
    requested = params.get("protocolVersion")
    if requested is None:
        return
    if not protocol_version_is_supported(requested):
        raise JsonRpcError(
            -32602,
            "Unsupported MCP protocol version",
            {"supported": [PROTOCOL_VERSION], "received": requested},
        )


def protocol_version_is_supported(version: Any) -> bool:
    return isinstance(version, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", version) is not None and version >= PROTOCOL_VERSION


def tool_result(payload: dict[str, Any], *, is_error: bool, content: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    text = json.dumps(payload, sort_keys=True)
    result_content = content or []
    result_content.append({"type": "text", "text": text})
    return {"content": result_content, "structuredContent": payload, "isError": is_error}


def object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def tool_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "error": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "category": {"type": "string"},
                    "retryable": {"type": "boolean"},
                    "details": {"type": "object", "additionalProperties": True},
                },
                "required": ["code", "message", "category", "retryable", "details"],
                "additionalProperties": True,
            },
        },
        "required": ["ok"],
        "additionalProperties": True,
    }


def validate_arguments(tool_name: str, args: dict[str, Any]) -> None:
    schema = input_schemas()[tool_name]
    try:
        validate_schema_value(args, schema, path="arguments")
    except ToolFailure as exc:
        raise JsonRpcError(-32602, exc.message, {"reason": "invalid_arguments", "code": exc.code}) from exc


def validate_admin_arguments(tool_name: str, args: dict[str, Any]) -> None:
    schema = admin_input_schemas()[tool_name]
    try:
        validate_schema_value(args, schema, path="arguments")
    except ToolFailure as exc:
        raise JsonRpcError(-32602, exc.message, {"reason": "invalid_arguments", "code": exc.code}) from exc


def validate_schema_value(value: Any, schema: dict[str, Any], *, path: str) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not schema_type_matches(value, expected_type):
        raise ToolFailure("INVALID_ARGUMENT", f"{path} must be {schema_type_name(expected_type)}.", category="validation")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} is shorter than {min_length}.", category="validation")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be one of {schema['enum']!r}.", category="validation")

    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be >= {minimum}.", category="validation")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be <= {maximum}.", category="validation")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        for index, item in enumerate(value):
            validate_schema_value(item, item_schema, path=f"{path}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ToolFailure("INVALID_ARGUMENT", f"{path}.{key} is required.", category="validation")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                validate_schema_value(item, properties[key], path=child_path)
            elif additional is False:
                raise ToolFailure("INVALID_ARGUMENT", f"{child_path} is not a recognized argument.", category="validation")
            elif isinstance(additional, dict):
                validate_schema_value(item, additional, path=child_path)


def schema_type_matches(value: Any, expected_type: str | list[str]) -> bool:
    if isinstance(expected_type, list):
        return any(schema_type_matches(value, item) for item in expected_type)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "string":
        return isinstance(value, str)
    return False


def schema_type_name(expected_type: str | list[str]) -> str:
    if isinstance(expected_type, list):
        return " or ".join(expected_type)
    return expected_type


def tool_definition(name: str, *, tool_profile: str = "full") -> dict[str, Any]:
    schemas = input_schemas()
    annotations = tool_annotations(name)
    if tool_profile == "compat-readonly-all":
        annotations = {
            **annotations,
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        }
    return {
        "name": name,
        "title": annotations["title"],
        "description": TOOL_REGISTRY[name].description,
        "inputSchema": schemas[name],
        "outputSchema": tool_output_schema(),
        "annotations": annotations,
    }


def tool_annotations(name: str) -> dict[str, Any]:
    spec = TOOL_REGISTRY[name]
    return {
        "title": spec.title,
        "readOnlyHint": spec.read_only,
        "destructiveHint": spec.destructive,
        "idempotentHint": spec.idempotent,
        "openWorldHint": spec.open_world,
    }


def admin_tool_definition(name: str, *, tool_profile: str = "full") -> dict[str, Any]:
    schemas = admin_input_schemas()
    annotations = admin_tool_annotations(name)
    if tool_profile == "compat-readonly-all":
        annotations = {**annotations, "readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
    return {
        "name": name,
        "title": annotations["title"],
        "description": ADMIN_TOOL_REGISTRY[name].description,
        "inputSchema": schemas[name],
        "outputSchema": tool_output_schema(),
        "annotations": annotations,
    }


def admin_tool_annotations(name: str) -> dict[str, Any]:
    spec = ADMIN_TOOL_REGISTRY[name]
    return {
        "title": spec.title,
        "readOnlyHint": spec.read_only,
        "destructiveHint": spec.destructive,
        "idempotentHint": spec.idempotent,
        "openWorldHint": spec.open_world,
    }


def admin_input_schemas() -> dict[str, dict[str, Any]]:
    string = {"type": "string"}
    boolean = {"type": "boolean"}
    integer = {"type": "integer"}
    config = {"type": "object", "additionalProperties": True}
    string_array = {"type": "array", "items": {"type": "string"}}
    project_fields = {
        "project_id": string,
        "project_name": string,
        "project_path": string,
        "project_workspace": string,
        "project_metadata": config,
    }
    return {
        "mcp_catalog_list": object_schema(),
        "mcp_template_list": object_schema(),
        "mcp_template_render": object_schema(
            {"template": {**string, "minLength": 1}, "variables": config, "overrides": config},
            required=["template"],
        ),
        "mcp_server_plan": object_schema({"config": config}, required=["config"]),
        "mcp_server_install": object_schema({"config": config, "apply": {**boolean, "default": False}}, required=["config"]),
        "mcp_server_update": object_schema(
            {"alias": {**string, "minLength": 1}, "config": config, "apply": {**boolean, "default": False}},
            required=["alias", "config"],
        ),
        "mcp_server_enable": object_schema({"alias": {**string, "minLength": 1}, "apply": {**boolean, "default": False}}, required=["alias"]),
        "mcp_server_disable": object_schema({"alias": {**string, "minLength": 1}, "apply": {**boolean, "default": False}}, required=["alias"]),
        "mcp_server_remove": object_schema({"alias": {**string, "minLength": 1}, "apply": {**boolean, "default": False}}, required=["alias"]),
        "mcp_server_reload": object_schema(),
        "mcp_server_health": object_schema({"alias": string}),
        "mcp_server_start": object_schema({"alias": {**string, "minLength": 1}}, required=["alias"]),
        "mcp_server_stop": object_schema({"alias": {**string, "minLength": 1}}, required=["alias"]),
        "mcp_server_logs": object_schema({"alias": string, "max_lines": {**integer, "minimum": 1, "maximum": 500, "default": 200}}),
        "mcp_secret_set": object_schema({"name": {**string, "minLength": 1}, "value": string}, required=["name", "value"]),
        "mcp_secret_list": object_schema(),
        "mcp_secret_delete": object_schema({"name": {**string, "minLength": 1}}, required=["name"]),
        "mcp_transcript_sessions": object_schema({"limit": {**integer, "minimum": 1, "maximum": 500, "default": 100}}),
        "mcp_transcript_export": object_schema(
            {
                "session_id": string,
                "max_events": {**integer, "minimum": 1, "maximum": 5000, "default": 1000},
                "write_file": {**boolean, "default": True},
            }
        ),
        "mcp_codex_sessions_preview": object_schema(
            {
                "roots": string_array,
                "limit": {**integer, "minimum": 1, "maximum": 500, "default": 200},
                "max_depth": {**integer, "minimum": 0, "maximum": 16, "default": 8},
                "max_file_bytes": {**integer, "minimum": 1024, "maximum": 104857600, "default": 26214400},
                "max_messages": {**integer, "minimum": 1, "maximum": 100000, "default": 20000},
            }
        ),
        "mcp_codex_sessions_import": object_schema(
            {
                "roots": string_array,
                "candidate_ids": string_array,
                "import_all": {**boolean, "default": False},
                "limit": {**integer, "minimum": 1, "maximum": 500, "default": 200},
                "max_depth": {**integer, "minimum": 0, "maximum": 16, "default": 8},
                "max_file_bytes": {**integer, "minimum": 1024, "maximum": 104857600, "default": 26214400},
                "max_messages": {**integer, "minimum": 1, "maximum": 100000, "default": 20000},
            }
        ),
        "mcp_codex_sessions_sync": object_schema(
            {
                "roots": string_array,
                "candidate_ids": string_array,
                "import_all": {**boolean, "default": False},
                "limit": {**integer, "minimum": 1, "maximum": 500, "default": 200},
                "max_depth": {**integer, "minimum": 0, "maximum": 16, "default": 8},
                "max_file_bytes": {**integer, "minimum": 1024, "maximum": 104857600, "default": 26214400},
                "max_messages": {**integer, "minimum": 1, "maximum": 100000, "default": 20000},
            }
        ),
        "mcp_chat_projects": object_schema(
            {"limit": {**integer, "minimum": 1, "maximum": 500, "default": 100}, "query": string}
        ),
        "mcp_chat_conversations": object_schema(
            {"limit": {**integer, "minimum": 1, "maximum": 500, "default": 100}, "query": string, "project_id": string}
        ),
        "mcp_chat_messages": object_schema(
            {"conversation_id": {**string, "minLength": 1}, "limit": {**integer, "minimum": 1, "maximum": 5000, "default": 500}},
            required=["conversation_id"],
        ),
        "mcp_chat_context": object_schema(
            {"conversation_id": {**string, "minLength": 1}, "limit": {**integer, "minimum": 1, "maximum": 5000, "default": 200}},
            required=["conversation_id"],
        ),
        "mcp_chat_record_context": object_schema(
            {
                "conversation_id": {**string, "minLength": 1},
                "conversation_title": string,
                "conversation_uid": string,
                "entry_id": string,
                "kind": string,
                "timestamp": string,
                "content": string,
                "source": string,
                "metadata": config,
                **project_fields,
            },
            required=["conversation_id", "content"],
        ),
        "mcp_chat_update_context": object_schema(
            {
                "id": {**integer, "minimum": 1},
                "entry_id": string,
                "kind": string,
                "timestamp": string,
                "content": string,
                "source": string,
                "metadata": config,
            },
            required=["id"],
        ),
        "mcp_chat_delete_context": object_schema({"id": {**integer, "minimum": 1}}, required=["id"]),
        "mcp_chat_recall": object_schema(
            {
                "conversation_id": {**string, "minLength": 1},
                "max_messages": {**integer, "minimum": 1, "maximum": 20000, "default": 200},
                "max_context_entries": {**integer, "minimum": 1, "maximum": 5000, "default": 200},
            },
            required=["conversation_id"],
        ),
        "mcp_chat_project_recall": object_schema(
            {
                "project_id": {**string, "minLength": 1},
                "max_conversations": {**integer, "minimum": 1, "maximum": 500, "default": 50},
                "max_messages": {**integer, "minimum": 1, "maximum": 20000, "default": 200},
                "max_context_entries": {**integer, "minimum": 1, "maximum": 5000, "default": 200},
            },
            required=["project_id"],
        ),
        "mcp_chat_export": object_schema(
            {
                "conversation_id": string,
                "project_id": string,
                "max_messages": {**integer, "minimum": 1, "maximum": 20000, "default": 5000},
                "write_file": {**boolean, "default": True},
            }
        ),
        "mcp_chat_context_export": object_schema(
            {
                "conversation_id": string,
                "project_id": string,
                "max_entries": {**integer, "minimum": 1, "maximum": 20000, "default": 5000},
                "write_file": {**boolean, "default": True},
            }
        ),
        "mcp_chat_update_message": object_schema(
            {
                "id": {**integer, "minimum": 1},
                "role": string,
                "timestamp": string,
                "content": string,
                "source": string,
                "metadata": config,
            },
            required=["id"],
        ),
        "mcp_chat_delete_message": object_schema({"id": {**integer, "minimum": 1}}, required=["id"]),
        "mcp_chat_delete_conversation": object_schema({"conversation_id": {**string, "minLength": 1}}, required=["conversation_id"]),
        "mcp_chat_clear": object_schema(),
        "mcp_chat_merge": object_schema(
            {
                "target_conversation_id": {**string, "minLength": 1},
                "source_conversation_ids": string_array,
            },
            required=["target_conversation_id", "source_conversation_ids"],
        ),
    }


def admin_config_arg(args: dict[str, Any]) -> dict[str, Any]:
    config = args.get("config")
    if not isinstance(config, dict):
        raise ValueError("config must be an object.")
    return config


def admin_error_payload(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "category": "admin", "retryable": False, "details": {}},
    }


def optional_text_arg(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    return str(value) if isinstance(value, str) and value else None


def project_kwargs_from_args(args: dict[str, Any]) -> dict[str, Any]:
    metadata = args.get("project_metadata")
    return {
        "project_id": optional_text_arg(args, "project_id"),
        "project_name": optional_text_arg(args, "project_name"),
        "project_path": optional_text_arg(args, "project_path"),
        "project_workspace": optional_text_arg(args, "project_workspace"),
        "project_metadata": metadata if isinstance(metadata, dict) else None,
    }


def codex_session_scan_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    roots = args.get("roots")
    return {
        "roots": [str(item) for item in roots] if isinstance(roots, list) else None,
        "limit": int(args.get("limit", 200)),
        "max_depth": int(args.get("max_depth", 8)),
        "max_file_bytes": int(args.get("max_file_bytes", 25 * 1024 * 1024)),
        "max_messages": int(args.get("max_messages", 20_000)),
    }


def codex_session_import_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    candidate_ids = args.get("candidate_ids")
    return {
        **codex_session_scan_kwargs(args),
        "candidate_ids": [str(item) for item in candidate_ids] if isinstance(candidate_ids, list) else None,
        "import_all": bool(args.get("import_all", False)),
    }


def input_schemas() -> dict[str, dict[str, Any]]:
    string = {"type": "string"}
    integer = {"type": "integer"}
    boolean = {"type": "boolean"}
    string_array = {"type": "array", "items": {"type": "string"}}
    metadata = {"type": "object", "additionalProperties": True}
    version_expectations = {"type": "object", "additionalProperties": True}
    project_fields = {
        "project_id": string,
        "project_name": string,
        "project_path": string,
        "project_workspace": string,
        "project_metadata": metadata,
    }
    chat_message = {
        "type": "object",
        "properties": {
            "id": string,
            "message_id": string,
            "role": {**string, "minLength": 1},
            "timestamp": string,
            "content": string,
            "source": string,
            "metadata": metadata,
        },
        "required": ["role", "content"],
        "additionalProperties": True,
    }
    return {
        "server_info": object_schema(),
        "workspace_identity": object_schema(),
        "check_exec_environment": object_schema(),
        "record_chat_transcript": object_schema(
            {
                "conversation_id": {**string, "minLength": 1},
                "conversation_title": string,
                "conversation_uid": string,
                "messages": {"type": "array", "items": chat_message},
                "source": string,
                **project_fields,
            },
            ["conversation_id", "messages"],
        ),
        "record_chat_message": object_schema(
            {
                "conversation_id": {**string, "minLength": 1},
                "conversation_title": string,
                "conversation_uid": string,
                "message_id": string,
                "role": {**string, "minLength": 1},
                "timestamp": string,
                "content": string,
                "source": string,
                "metadata_json": string,
                **project_fields,
            },
            ["conversation_id", "role", "content"],
        ),
        "recall_chat_context": object_schema(
            {
                "conversation_id": {**string, "minLength": 1},
                "max_messages": {**integer, "minimum": 1, "maximum": 20000, "default": 200},
                "max_context_entries": {**integer, "minimum": 1, "maximum": 5000, "default": 200},
            },
            ["conversation_id"],
        ),
        "list_chat_projects": object_schema(
            {"limit": {**integer, "minimum": 1, "maximum": 500, "default": 100}, "query": string}
        ),
        "list_chat_conversations": object_schema(
            {"limit": {**integer, "minimum": 1, "maximum": 500, "default": 100}, "query": string, "project_id": string}
        ),
        "recall_project_context": object_schema(
            {
                "project_id": {**string, "minLength": 1},
                "max_conversations": {**integer, "minimum": 1, "maximum": 500, "default": 50},
                "max_messages": {**integer, "minimum": 1, "maximum": 20000, "default": 200},
                "max_context_entries": {**integer, "minimum": 1, "maximum": 5000, "default": 200},
            },
            ["project_id"],
        ),
        "get_default_cwd": object_schema(),
        "set_default_cwd": object_schema(
            {
                "path": {**string, "default": "."},
            }
        ),
        "read_file": object_schema(
            {
                "path": {**string, "minLength": 1},
                "start_line": {**integer, "minimum": 1, "default": 1},
                "end_line": {**integer, "minimum": 1},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 131072},
                "encoding": {**string, "enum": ["utf-8"], "default": "utf-8"},
            },
            ["path"],
        ),
        "file_stat": object_schema(
            {
                "path": {**string, "minLength": 1},
            },
            ["path"],
        ),
        "list_dir": object_schema(
            {
                "path": {**string, "default": "."},
                "recursive": {**boolean, "default": False},
                "max_depth": {**integer, "minimum": 1, "maximum": 20, "default": 1},
                "max_entries": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
                "include_hidden": {**boolean, "default": False},
                "include_ignored": {**boolean, "default": False},
                "sort": {**string, "enum": ["name", "type", "modified"], "default": "name"},
            }
        ),
        "list_files": object_schema(
            {
                "path": {**string, "default": "."},
                "patterns": string_array,
                "glob": string,
                "exclude_patterns": string_array,
                "include_hidden": {**boolean, "default": False},
                "include_ignored": {**boolean, "default": False},
                "max_results": {**integer, "minimum": 1, "maximum": 50000, "default": 5000},
                "sort": {**string, "enum": ["path", "modified"], "default": "path"},
            }
        ),
        "search_text": object_schema(
            {
                "query": {**string, "minLength": 1},
                "path": {**string, "default": "."},
                "regex": {**boolean, "default": False},
                "case_sensitive": {**boolean, "default": False},
                "include_globs": string_array,
                "glob": string,
                "exclude_globs": string_array,
                "context_lines": {**integer, "minimum": 0, "maximum": 5, "default": 0},
                "max_results": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
                "max_preview_bytes": {**integer, "minimum": 80, "maximum": 4096, "default": 512},
            },
            ["query"],
        ),
        "apply_patch": object_schema(
            {
                "patch": {**string, "minLength": 1},
                "dry_run": {**boolean, "default": False},
                "operation_id": string,
                "create_checkpoint": {**boolean, "default": True},
                "expected_hashes": version_expectations,
                "expected_mtimes": version_expectations,
            },
            ["patch"],
        ),
        "restore_patch_checkpoint": object_schema(
            {
                "checkpoint_id": {**string, "minLength": 1},
            },
            ["checkpoint_id"],
        ),
        "exec_command": object_schema(
            {
                "cmd": {**string, "minLength": 1},
                "workdir": {**string, "default": "."},
                "timeout_ms": {**integer, "minimum": 1, "maximum": 600000, "default": 30000},
                "yield_time_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 1000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "stdin": {**string, "default": ""},
                "tty": {**boolean, "default": False},
                "env": {"type": "object", "additionalProperties": {"type": "string"}, "default": {}},
            },
            ["cmd"],
        ),
        "write_stdin": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "chars": {**string, "default": ""},
                "yield_time_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 1000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
            },
            ["session_id"],
        ),
        "command_status": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "yield_time_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 0},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "consume": {**boolean, "default": False},
                "from_start": {**boolean, "default": True},
            },
            ["session_id"],
        ),
        "kill_session": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "signal": {**string, "enum": ["TERM", "KILL", "INT"], "default": "TERM"},
                "wait_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 5000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
            },
            ["session_id"],
        ),
        "git_status": object_schema(
            {
                "path": {**string, "default": "."},
                "include_untracked": {**boolean, "default": True},
                "max_entries": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
            }
        ),
        "git_diff": object_schema(
            {
                "path": string,
                "paths": string_array,
                "staged": {**boolean, "default": False},
                "unstaged": {**boolean, "default": True},
                "context_lines": {**integer, "minimum": 0, "maximum": 20, "default": 3},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 262144},
            }
        ),
        "git_log": object_schema(
            {
                "path": {**string, "default": "."},
                "ref": {**string, "default": "HEAD"},
                "max_count": {**integer, "minimum": 1, "maximum": 100, "default": 20},
                "skip": {**integer, "minimum": 0, "maximum": 10000, "default": 0},
            }
        ),
        "git_show": object_schema(
            {
                "rev": {**string, "default": "HEAD"},
                "path": string,
                "paths": string_array,
                "include_diff": {**boolean, "default": True},
                "context_lines": {**integer, "minimum": 0, "maximum": 20, "default": 3},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 262144},
            }
        ),
        "git_blame": object_schema(
            {
                "path": {**string, "minLength": 1},
                "rev": string,
                "start_line": {**integer, "minimum": 1, "default": 1},
                "end_line": {**integer, "minimum": 1},
                "max_lines": {**integer, "minimum": 1, "maximum": 1000, "default": 200},
            },
            ["path"],
        ),
        "request_permissions": object_schema(
            {
                "tool_name": {**string, "enum": ["exec_command", "apply_patch"]},
                "permission": {
                    **string,
                    "enum": [
                        "network",
                        "destructive_command",
                        "long_timeout",
                        "sensitive_env",
                        "shell_expansion",
                        INLINE_SCRIPT_PERMISSION,
                        "privileged_executable",
                        "write_generated_or_ignored",
                    ],
                },
                "reason": {**string, "minLength": 1},
                "arguments": {"type": "object", "additionalProperties": True},
                "scope": {**string, "enum": ["once", "session"], "default": "once"},
                "ttl_seconds": {**integer, "minimum": 1, "maximum": 3600, "default": 300},
            },
            ["tool_name", "permission", "reason", "arguments"],
        ),
        "view_image": object_schema(
            {
                "path": {**string, "minLength": 1},
                "max_bytes": {**integer, "minimum": 1024, "maximum": 10485760, "default": 5242880},
                "max_width": {**integer, "minimum": 1, "maximum": 10000, "default": IMAGE_RESIZE_MAX_DIMENSION},
                "max_height": {**integer, "minimum": 1, "maximum": 10000, "default": IMAGE_RESIZE_MAX_DIMENSION},
                "auto_resize": {**boolean, "default": True},
                "output": {**string, "enum": ["mcp_image", "data_url"], "default": "mcp_image"},
            },
            ["path"],
        ),
    }


def _server_card_auth(runtime: Runtime, *, oauth_base_url: str | None = None) -> dict[str, Any]:
    if runtime.oauth_enabled():
        cfg = runtime.oauth_config
        assert cfg is not None
        base = (oauth_base_url or cfg.server_url or "").rstrip("/")
        return {
            "type": "oauth2",
            "scheme": "Bearer",
            "header": "Authorization",
            "authorizationUrl": f"{base}/oauth/authorize",
            "tokenUrl": f"{base}/oauth/token",
        }
    if runtime.auth_token is not None:
        return {"type": "bearer", "scheme": "Bearer", "header": "Authorization"}
    return {"type": "none", "scheme": None, "header": None}


def server_card_payload(runtime: Runtime, *, oauth_base_url: str | None = None) -> dict[str, Any]:
    tool_definitions = runtime.list_tools()["tools"]
    names = [str(tool.get("name")) for tool in tool_definitions if isinstance(tool, dict) and tool.get("name")]
    annotations = {
        str(tool.get("name")): tool.get("annotations", {})
        for tool in tool_definitions
        if isinstance(tool, dict) and tool.get("name")
    }
    read_only = [name for name in names if annotations[name].get("readOnlyHint") is True]
    mutating = [name for name in names if annotations[name].get("readOnlyHint") is not True]
    payload = {
        "protocolVersion": PROTOCOL_VERSION,
        "server": {
            "name": SERVER_NAME,
            "title": "Coding Tools MCP",
            "version": __version__,
        },
        "transport": {
            "type": "streamable_http",
            "endpoint": "/mcp",
            "methods": ["GET", "HEAD", "POST", "OPTIONS"],
        },
        "auth": _server_card_auth(runtime, oauth_base_url=oauth_base_url),
        "toolProfile": runtime.tool_profile,
        "tools": {
            "count": len(names),
            "names": names,
            "readOnlyHintTrue": read_only,
            "readOnlyHintFalse": mutating,
        },
        "capabilities": {
            "tools": {"listChanged": False},
            "logging": {},
        },
    }
    if runtime.tool_profile == "compat-readonly-all":
        payload["warnings"] = [
            "compat-readonly-all advertises every tool as read-only, but mutation-capable tools still mutate local state."
        ]
    return payload


class MCPHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CodingToolsMCP/0.1"

    @property
    def runtime(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args, file=sys.stderr)

    def incoming_http_session_id(self) -> str | None:
        session_id = self.headers.get("Mcp-Session-Id")
        if not isinstance(session_id, str):
            return None
        session_id = session_id.strip()
        return session_id if HTTP_SESSION_ID_RE.fullmatch(session_id) else None

    def response_http_session_id(self) -> str:
        session_id = getattr(self, "_response_http_session_id", None)
        return session_id if isinstance(session_id, str) and session_id else self.runtime.http_session_id

    def request_tool_session_id(self) -> str | None:
        override = self.headers.get("X-Coding-Tools-Session")
        if isinstance(override, str):
            override = override.strip()
            if HTTP_SESSION_ID_RE.fullmatch(override):
                return override
        return self.incoming_http_session_id()

    def client_remote_addr(self) -> str | None:
        if isinstance(self.client_address, tuple) and self.client_address:
            return str(self.client_address[0])
        return None

    def rpc_method_label(self, request: Any) -> str | None:
        if isinstance(request, dict):
            method = request.get("method")
            return str(method) if isinstance(method, str) else None
        if isinstance(request, list):
            methods = [str(item.get("method")) for item in request if isinstance(item, dict) and isinstance(item.get("method"), str)]
            if not methods:
                return "batch"
            label = ",".join(methods[:3])
            if len(methods) > 3:
                label += ",..."
            return f"batch:{label}"
        return None

    def record_current_mcp_access(self, *, rpc_method: str | None, status: int) -> None:
        session_id = self.request_tool_session_id() or self.response_http_session_id()
        if self.headers.get("X-Coding-Tools-Session") is None and not self.runtime.has_http_session(session_id):
            session_id = self.response_http_session_id()
        self.runtime.record_mcp_http_access(
            session_id=session_id,
            method=self.command,
            path=posixpath.normpath(self.path.split("?", 1)[0]),
            rpc_method=rpc_method,
            status=status,
            remote_addr=self.client_remote_addr(),
            user_agent=self.headers.get("User-Agent"),
            protocol_version=self.headers.get("MCP-Protocol-Version"),
        )

    def do_GET(self) -> None:
        request_path = self.path.split("?", 1)[0]
        normalized = posixpath.normpath(request_path)
        if normalized == "/admin":
            if not self.runtime.admin_ui_enabled:
                self.send_json({"error": "Admin console disabled"}, status=404)
                return
            self._send_html(admin_console_html())
            return
        if normalized.startswith("/admin/assets/"):
            if not self.runtime.admin_ui_enabled:
                self.send_json({"error": "Admin console disabled"}, status=404)
                return
            self._send_admin_asset(normalized.removeprefix("/admin/assets/"))
            return
        if normalized == "/admin/health":
            self.send_json(
                {
                    "ok": True,
                    "admin_ui_enabled": self.runtime.admin_ui_enabled,
                    "auth_required": self.runtime.auth_enabled(),
                    "admin_auth_required": self.runtime.admin_auth_enabled(),
                    "oauth_enabled": self.runtime.oauth_enabled(),
                }
            )
            return
        if normalized == "/api/admin/status":
            if not self.is_admin_request():
                self.send_admin_unauthorized()
                return
            self.send_json(self.runtime.admin_status_payload(base_url=self.oauth_base_url()))
            return
        self.handle_metadata_request(head_only=False)

    def do_HEAD(self) -> None:
        self.handle_metadata_request(head_only=True)

    def do_OPTIONS(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if posixpath.normpath(request_path) not in {
            "/mcp",
            "/admin",
            "/admin/health",
            "/api/admin/status",
            "/api/admin/tool",
            "/api/admin/settings",
            "/api/admin/runtime",
            "/api/tool",
            "/.well-known/mcp.json",
            "/.well-known/mcp/server-card.json",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/oauth/authorize",
            "/oauth/token",
        }:
            self.send_json({"error": "Unknown endpoint"}, status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not is_allowed_origin(
            origin,
            auth_enabled=self.runtime.auth_enabled(),
            allowed_origins=self.runtime.allowed_origins,
        ):
            self.send_json({"error": "Origin denied"}, status=403)
            return
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_cors_headers()
        self.end_headers()

    def handle_metadata_request(self, *, head_only: bool) -> None:
        request_path = self.path.split("?", 1)[0]
        normalized = posixpath.normpath(request_path)
        if normalized == "/.well-known/oauth-authorization-server":
            self.handle_oauth_as_metadata(head_only=head_only)
            return
        if normalized == "/.well-known/oauth-protected-resource":
            self.handle_oauth_resource_metadata(head_only=head_only)
            return
        if normalized == "/oauth/authorize" and not head_only:
            self.handle_oauth_authorize_get()
            return
        if normalized == "/mcp":
            origin = self.headers.get("Origin")
            if origin and not is_allowed_origin(
                origin,
                auth_enabled=self.runtime.auth_enabled(),
                allowed_origins=self.runtime.allowed_origins,
            ):
                self.send_json({"error": "Origin denied"}, status=403, head_only=head_only)
                return
            if not self.is_authorized():
                self.send_unauthorized(head_only=head_only)
                return
            incoming_session_id = self.incoming_http_session_id()
            if incoming_session_id and self.runtime.has_http_session(incoming_session_id):
                self._response_http_session_id = incoming_session_id
            self.record_current_mcp_access(rpc_method="server-card", status=200)
            self.send_json(server_card_payload(self.runtime, oauth_base_url=self.oauth_base_url()), head_only=head_only)
            return
        if normalized in {"/.well-known/mcp.json", "/.well-known/mcp/server-card.json"}:
            self.send_json(server_card_payload(self.runtime, oauth_base_url=self.oauth_base_url()), head_only=head_only)
            return
        self.send_json({"error": "Unknown endpoint"}, status=404, head_only=head_only)

    def do_POST(self) -> None:
        request_path = self.path.split("?", 1)[0]
        normalized = posixpath.normpath(request_path)
        if normalized in {"/api/admin/tool", "/api/tool"}:
            self.handle_admin_tool()
            return
        if normalized == "/api/admin/settings":
            self.handle_admin_settings()
            return
        if normalized == "/api/admin/runtime":
            self.handle_admin_runtime()
            return
        if normalized == "/oauth/authorize":
            self.handle_oauth_authorize_post()
            return
        if normalized == "/oauth/token":
            self.handle_oauth_token()
            return
        if normalized != "/mcp":
            self.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "Unknown endpoint"}}, status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not is_allowed_origin(
            origin,
            auth_enabled=self.runtime.auth_enabled(),
            allowed_origins=self.runtime.allowed_origins,
        ):
            self.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Origin denied"}}, status=403)
            return
        if not self.is_authorized():
            self.send_unauthorized()
            return
        if self.headers.get_content_type().lower() != "application/json":
            self.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Content-Type must be application/json"},
                },
                status=415,
            )
            return
        protocol_version = self.headers.get("MCP-Protocol-Version")
        if protocol_version and not protocol_version_is_supported(protocol_version):
            self.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Unsupported MCP protocol version",
                        "data": {"supported": [PROTOCOL_VERSION], "received": protocol_version},
                    },
                },
                status=400,
            )
            return
        session_id = self.incoming_http_session_id()
        self._response_http_session_id = session_id or self.runtime.http_session_id
        if session_id and not self.runtime.has_http_session(session_id):
            self.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32001,
                        "message": "Unknown MCP session",
                    },
                },
                status=404,
            )
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Content-Length is required"},
                },
                status=411,
            )
            return
        try:
            length = int(raw_length)
        except ValueError:
            self.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Content-Length must be a non-negative integer"},
                },
                status=400,
            )
            return
        if length < 0:
            self.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Content-Length must be a non-negative integer"},
                },
                status=400,
            )
            return
        if length > MAX_HTTP_REQUEST_BYTES:
            self.close_connection = True
            self.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Request body exceeds maximum size",
                        "data": {"max_bytes": MAX_HTTP_REQUEST_BYTES},
                    },
                },
                status=413,
            )
            return
        body = self.rfile.read(length)
        try:
            request = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status=400)
            return
        rpc_method = self.rpc_method_label(request)
        if isinstance(request, list):
            if not request:
                self.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, status=400)
                return
            if len(request) > MAX_JSON_RPC_BATCH_ITEMS:
                self.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32600,
                            "message": "Batch request exceeds maximum item count",
                            "data": {"max_items": MAX_JSON_RPC_BATCH_ITEMS},
                        },
                    },
                    status=400,
                )
                return
            responses: list[dict[str, Any]] = []
            for item in request:
                if not isinstance(item, dict):
                    responses.append({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
                    continue
                response = self.handle_rpc(item)
                if response is not None:
                    responses.append(response)
            if not responses:
                self.record_current_mcp_access(rpc_method=rpc_method, status=202)
                self.send_response(202)
                self.send_header("Mcp-Session-Id", self.response_http_session_id())
                self.send_cors_headers()
                self.end_headers()
                return
            self.record_current_mcp_access(rpc_method=rpc_method, status=200)
            self.send_json(responses)
            return
        if not isinstance(request, dict):
            self.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, status=400)
            return
        response = self.handle_rpc(request)
        if response is None:
            self.record_current_mcp_access(rpc_method=rpc_method, status=202)
            self.send_response(202)
            self.send_header("Mcp-Session-Id", self.response_http_session_id())
            self.send_cors_headers()
            self.end_headers()
            return
        self.record_current_mcp_access(rpc_method=rpc_method, status=200)
        self.send_json(response)

    def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        try:
            validate_rpc_envelope(request)
            method = request["method"]
            params = rpc_params(request)
            if not self.runtime.initialized and method not in {"initialize", "ping"}:
                raise JsonRpcError(-32002, "Server not initialized")
            if method == "initialize":
                validate_initialize_params(params)
                result = self.runtime.initialize()
                self.runtime.initialized = True
                self._response_http_session_id = self.runtime.ensure_http_session(self.incoming_http_session_id())
            elif method == "notifications/initialized":
                return None
            elif method == "notifications/cancelled":
                session_id = params.get("session_id")
                if isinstance(session_id, str):
                    self.runtime.cancel_session(session_id)
                return None
            elif method == "ping":
                result = {}
            elif method == "logging/setLevel":
                result = self.runtime.set_logging_level(params)
            elif method == "tools/list":
                result = self.runtime.list_tools(include_admin=self.is_admin_request())
            elif method == "tools/call":
                if not isinstance(params.get("name"), str):
                    raise JsonRpcError(-32602, "tools/call requires a tool name")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise JsonRpcError(-32602, "tools/call arguments must be an object")
                result = self.runtime.call_tool(
                    params["name"],
                    arguments,
                    admin=self.is_admin_request(),
                    session_id=self.request_tool_session_id(),
                )
            else:
                raise JsonRpcError(-32601, f"Unknown method: {method}")
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JsonRpcError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            response: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
            if request_id is not None:
                response["id"] = request_id
            return response
        except Exception as exc:  # noqa: BLE001
            response = {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(exc)}}
            if request_id is not None:
                response["id"] = request_id
            return response

    def is_authorized(self) -> bool:
        if not self.runtime.auth_enabled():
            return True
        header = self.headers.get("Authorization", "").strip()
        if self.runtime.admin_token is not None:
            if secrets.compare_digest(header, f"Bearer {self.runtime.admin_token}"):
                return True
        if self.runtime.auth_token is not None:
            if secrets.compare_digest(header, f"Bearer {self.runtime.auth_token}"):
                return True
        if self.runtime.oauth_config is not None and header.startswith("Bearer "):
            token = header[len("Bearer "):]
            if _validate_oauth_token(token, self.runtime.oauth_config, self.oauth_base_url()):
                return True
        return False

    def is_admin_request(self) -> bool:
        if not self.runtime.admin_auth_enabled() and not self.runtime.auth_enabled():
            return True
        header = self.headers.get("Authorization", "").strip()
        if self.runtime.admin_token is not None:
            if secrets.compare_digest(header, f"Bearer {self.runtime.admin_token}"):
                return True
        if self.runtime.oauth_config is not None and header.startswith("Bearer "):
            token = header[len("Bearer "):]
            claims = _decode_oauth_token(token, self.runtime.oauth_config, self.oauth_base_url())
            scope = claims.get("scope", "") if claims else ""
            if isinstance(scope, str) and self.runtime.oauth_config.admin_scope in scope.split():
                return True
        return False

    def send_admin_unauthorized(self) -> None:
        if self.runtime.oauth_config is not None:
            base = self.oauth_base_url()
            www_auth = f'Bearer realm="coding-tools-mcp-admin", resource_metadata="{base}/.well-known/oauth-protected-resource"'
        else:
            www_auth = 'Bearer realm="coding-tools-mcp-admin"'
        self.send_json({"ok": False, "error": "Admin authorization required"}, status=401, extra_headers={"WWW-Authenticate": www_auth})

    def _read_admin_json_body(self) -> dict[str, Any] | None:
        if self.headers.get_content_type().lower() != "application/json":
            self.send_json({"ok": False, "error": "Content-Type must be application/json"}, status=415)
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_json({"ok": False, "error": "Content-Length is required"}, status=411)
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self.send_json({"ok": False, "error": "Content-Length must be a non-negative integer"}, status=400)
            return None
        if not (0 <= length <= MAX_HTTP_REQUEST_BYTES):
            self.close_connection = True
            self.send_json({"ok": False, "error": "Request body exceeds maximum size"}, status=413)
            return None
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "Invalid JSON"}, status=400)
            return None
        if not isinstance(request, dict):
            self.send_json({"ok": False, "error": "Request body must be a JSON object"}, status=400)
            return None
        return request

    def handle_admin_tool(self) -> None:
        if not self.is_admin_request():
            self.send_admin_unauthorized()
            return
        request = self._read_admin_json_body()
        if request is None:
            return
        if not isinstance(request.get("name"), str):
            self.send_json({"ok": False, "error": "name is required"}, status=400)
            return
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            self.send_json({"ok": False, "error": "arguments must be an object"}, status=400)
            return
        try:
            result = self.runtime.call_tool(request["name"], arguments, admin=True, session_id=self.request_tool_session_id())
        except JsonRpcError as exc:
            self.send_json({"ok": False, "error": exc.message, "data": exc.data}, status=400)
            return
        self.send_json(result)

    def handle_admin_settings(self) -> None:
        if not self.is_admin_request():
            self.send_admin_unauthorized()
            return
        request = self._read_admin_json_body()
        if request is None:
            return
        updates = request.get("settings") if isinstance(request.get("settings"), dict) else request
        self.send_json(self.runtime.save_startup_settings(cast(dict[str, Any], updates)))

    def handle_admin_runtime(self) -> None:
        if not self.is_admin_request():
            self.send_admin_unauthorized()
            return
        request = self._read_admin_json_body()
        if request is None:
            return
        try:
            self.send_json(self.runtime.apply_runtime_update(request))
        except ToolFailure as exc:
            self.send_json({"ok": False, "error": exc.message, "code": exc.code, "details": exc.details}, status=400)

    def oauth_base_url(self) -> str:
        cfg = self.runtime.oauth_config
        if cfg is not None and cfg.server_url:
            return cfg.server_url.rstrip("/")
        proto = _first_header_value(self.headers.get("X-Forwarded-Proto"))
        if not proto:
            proto = _forwarded_header_param(self.headers.get("Forwarded"), "proto")
        host = _safe_external_host(_first_header_value(self.headers.get("X-Forwarded-Host")))
        if not host:
            host = _safe_external_host(_forwarded_header_param(self.headers.get("Forwarded"), "host"))
        if not host:
            host = _safe_external_host(self.headers.get("Host", ""))
        if not host:
            server_address = cast(tuple[Any, ...], self.server.server_address)  # type: ignore[attr-defined]
            bind_host = server_address[0]
            bind_port = server_address[1]
            host = _http_base_for_bind_host(str(bind_host), int(bind_port)).removeprefix("http://")
        if proto not in {"http", "https"}:
            host_without_port = host.rsplit(":", 1)[0].strip("[]")
            proto = "http" if is_loopback_bind_host(host_without_port) else "https"
        return f"{proto}://{host}".rstrip("/")

    def send_unauthorized(self, *, head_only: bool = False) -> None:
        if self.runtime.oauth_config is not None:
            base = self.oauth_base_url()
            www_auth = f'Bearer realm="coding-tools-mcp", resource_metadata="{base}/.well-known/oauth-protected-resource"'
        else:
            www_auth = 'Bearer realm="coding-tools-mcp"'
        self.send_json(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Unauthorized"}},
            status=401,
            extra_headers={"WWW-Authenticate": www_auth},
            head_only=head_only,
        )

    def handle_oauth_as_metadata(self, *, head_only: bool = False) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404, head_only=head_only)
            return
        base = self.oauth_base_url()
        self.send_json(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "scopes_supported": ["mcp", cfg.admin_scope],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": _oauth_token_auth_methods(cfg),
            },
            head_only=head_only,
        )

    def handle_oauth_resource_metadata(self, *, head_only: bool = False) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404, head_only=head_only)
            return
        base = self.oauth_base_url()
        self.send_json(
            {"resource": base, "authorization_servers": [base], "bearer_methods_supported": ["header"], "scopes_supported": ["mcp", cfg.admin_scope]},
            head_only=head_only,
        )

    def _send_html(self, body: str, *, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_admin_asset(self, asset_name: str) -> None:
        asset = admin_asset_response(asset_name)
        if asset is None:
            self.send_json({"error": "Unknown admin asset"}, status=404)
            return
        data, content_type = asset
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _oauth_login_page(self, *, client_id: str, redirect_uri: str, code_challenge: str,
                          code_challenge_method: str, state: str, scope: str, error: str = "") -> str:
        def esc(v: str) -> str:
            return html.escape(v, quote=True)
        error_block = f'<p style="color:red">{html.escape(error)}</p>' if error else ""
        return (
            "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<title>Authorize MCP Server</title>"
            "<style>body{font-family:sans-serif;max-width:380px;margin:4rem auto;padding:1rem}"
            "input{width:100%;padding:.5rem;margin:.4rem 0;box-sizing:border-box}"
            "button{width:100%;padding:.7rem;background:#0066cc;color:#fff;border:none;cursor:pointer}</style>"
            "</head><body>"
            f"<h2>Authorize Coding Tools MCP</h2>"
            f"<p>Client: <strong>{esc(client_id)}</strong></p>"
            f"<p>Redirect URI: <code>{esc(redirect_uri)}</code></p>"
            f"{error_block}"
            "<form method='POST' action='/oauth/authorize'>"
            f"<input type='hidden' name='client_id' value='{esc(client_id)}'>"
            f"<input type='hidden' name='redirect_uri' value='{esc(redirect_uri)}'>"
            f"<input type='hidden' name='code_challenge' value='{esc(code_challenge)}'>"
            f"<input type='hidden' name='code_challenge_method' value='{esc(code_challenge_method)}'>"
            f"<input type='hidden' name='state' value='{esc(state)}'>"
            f"<input type='hidden' name='scope' value='{esc(scope)}'>"
            "<label>Password<input type='password' name='password' autocomplete='current-password' required></label>"
            "<button type='submit'>Authorize</button>"
            "</form></body></html>"
        )

    def _read_oauth_body(self) -> bytes | None:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self.send_json({"error": "Content-Length required"}, status=411)
            return None
        try:
            length = int(raw_len)
        except ValueError:
            self.send_json({"error": "Invalid Content-Length"}, status=400)
            return None
        if not (0 <= length <= OAUTH_MAX_BODY_BYTES):
            self.send_json({"error": "Request body too large"}, status=413)
            return None
        return self.rfile.read(length)

    def handle_oauth_authorize_get(self) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404)
            return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True)

        def _p(k: str) -> str:
            v = params.get(k)
            return v[0] if v else ""

        client_id = _p("client_id")
        redirect_uri = _p("redirect_uri")
        code_challenge = _p("code_challenge")
        code_challenge_method = _p("code_challenge_method")
        state = _p("state")
        scope = _p("scope") or "mcp"

        if _p("response_type") != "code":
            self._send_html("<h2>Error</h2><p>response_type must be 'code'</p>", status=400)
            return
        if not _oauth_client_id_allowed(client_id, cfg):
            self._send_html("<h2>Error</h2><p>Unknown client_id</p>", status=400)
            return
        if code_challenge_method != "S256" or not code_challenge:
            self._send_html("<h2>Error</h2><p>code_challenge_method must be S256 and code_challenge is required</p>", status=400)
            return
        if not _oauth_scope_allowed(scope, cfg):
            self._send_html("<h2>Error</h2><p>Unsupported OAuth scope</p>", status=400)
            return

        self._send_html(self._oauth_login_page(
            client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
            code_challenge_method=code_challenge_method, state=state, scope=scope,
        ))

    def handle_oauth_authorize_post(self) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404)
            return
        body = self._read_oauth_body()
        if body is None:
            return
        params = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)

        def _p(k: str) -> str:
            v = params.get(k)
            return v[0] if v else ""

        client_id = _p("client_id")
        redirect_uri = _p("redirect_uri")
        code_challenge = _p("code_challenge")
        code_challenge_method = _p("code_challenge_method")
        state = _p("state")
        password = _p("password")
        scope = _p("scope") or "mcp"

        if not _oauth_client_id_allowed(client_id, cfg):
            self._send_html(self._oauth_login_page(
                client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
                code_challenge_method=code_challenge_method, state=state, scope=scope, error="Invalid client",
            ), status=400)
            return
        if code_challenge_method != "S256" or not code_challenge:
            self._send_html(self._oauth_login_page(
                client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
                code_challenge_method=code_challenge_method, state=state, scope=scope, error="Invalid PKCE parameters",
            ), status=400)
            return
        if not _oauth_scope_allowed(scope, cfg):
            self._send_html(self._oauth_login_page(
                client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
                code_challenge_method=code_challenge_method, state=state, scope=scope, error="Unsupported OAuth scope",
            ), status=400)
            return
        if not secrets.compare_digest(password, cfg.password):
            self._send_html(self._oauth_login_page(
                client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
                code_challenge_method=code_challenge_method, state=state, scope=scope, error="Invalid password",
            ), status=401)
            return

        code = secrets.token_urlsafe(32)
        now = time.time()
        with self.runtime._pending_codes_lock:
            expired = [k for k, v in self.runtime._pending_codes.items() if v["expires_at"] < now]
            for k in expired:
                del self.runtime._pending_codes[k]
            self.runtime._pending_codes[code] = {
                "code_challenge": code_challenge,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": scope,
                "expires_at": now + OAUTH_CODE_TTL_SECONDS,
                "server_url": self.oauth_base_url(),
            }

        qs = urllib.parse.urlencode({"code": code, **({"state": state} if state else {})})
        sep = "&" if "?" in redirect_uri else "?"
        location = redirect_uri + sep + qs
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_oauth_token(self) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "unsupported_grant_type"}, status=400)
            return

        def _err(error: str, description: str) -> None:
            self.log_message("OAuth token error: %s - %s", error, description)
            self.send_json({"error": error, "error_description": description}, status=400)

        body = self._read_oauth_body()
        if body is None:
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            _err("invalid_request", "Content-Type must be application/x-www-form-urlencoded")
            return
        params = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)

        def _p(k: str) -> str:
            v = params.get(k)
            return v[0] if v else ""

        grant_type = _p("grant_type")
        code = _p("code")
        redirect_uri = _p("redirect_uri")
        code_verifier = _p("code_verifier")
        client_id = _p("client_id")
        client_secret = _p("client_secret")

        # Also accept HTTP Basic auth for client credentials.
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Basic ") and (not client_id or not client_secret):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                basic_id, _, basic_secret = decoded.partition(":")
                if not client_id:
                    client_id = urllib.parse.unquote(basic_id)
                if not client_secret:
                    client_secret = urllib.parse.unquote(basic_secret)
            except Exception:  # noqa: BLE001
                pass

        if grant_type != "authorization_code":
            _err("unsupported_grant_type", "Only authorization_code is supported")
            return
        if not _oauth_client_id_allowed(client_id, cfg):
            _err("invalid_client", "Unknown client_id")
            return
        if cfg.client_secret is not None and not secrets.compare_digest(client_secret, cfg.client_secret):
            _err("invalid_client", "Invalid client_secret")
            return
        if not code:
            _err("invalid_grant", "code is required")
            return
        if not code_verifier or not (43 <= len(code_verifier) <= 128) or not re.fullmatch(r"[A-Za-z0-9\-._~]+", code_verifier):
            _err("invalid_grant", "Invalid code_verifier")
            return

        with self.runtime._pending_codes_lock:
            code_data = self.runtime._pending_codes.pop(code, None)

        if code_data is None:
            _err("invalid_grant", "Unknown or already-used authorization code")
            return
        if time.time() > code_data["expires_at"]:
            _err("invalid_grant", "Authorization code expired")
            return
        if not secrets.compare_digest(code_data["client_id"], client_id):
            _err("invalid_grant", "client_id mismatch")
            return
        if not secrets.compare_digest(code_data["redirect_uri"], redirect_uri):
            _err("invalid_grant", "redirect_uri mismatch")
            return
        if not _verify_pkce(code_verifier, code_data["code_challenge"]):
            _err("invalid_grant", "PKCE verification failed")
            return

        server_url = str(code_data.get("server_url") or self.oauth_base_url()).rstrip("/")
        scope = str(code_data.get("scope") or "mcp")
        access_token = _create_oauth_token(cfg, server_url, scope=scope)
        self.send_json({"access_token": access_token, "token_type": "Bearer", "expires_in": cfg.token_ttl, "scope": scope})

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and is_allowed_origin(
            origin,
            auth_enabled=self.runtime.auth_enabled(),
            allowed_origins=self.runtime.allowed_origins,
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Accept, Authorization, Content-Type, MCP-Protocol-Version, Mcp-Session-Id, X-Coding-Tools-Session",
            )

    def send_json(
        self,
        payload: Any,
        *,
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        body = json_response_payload(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", self.response_http_session_id())
        self.send_cors_headers()
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


class AdminUIHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CodingToolsMCPAdmin/0.1"

    @property
    def runtime(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args, file=sys.stderr)

    def do_GET(self) -> None:  # noqa: N802
        path = posixpath.normpath(self.path.split("?", 1)[0])
        if path in {"/", "/admin"}:
            body = admin_console_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/admin/assets/"):
            self._send_admin_asset(path.removeprefix("/admin/assets/"))
            return
        if path == "/api/admin/status":
            if not self.is_admin_request():
                self.send_json({"error": "Admin token required"}, status=401)
                return
            self.send_json(self.runtime.admin_status_payload(base_url=self.oauth_base_url()))
            return
        self.send_json({"error": "Unknown endpoint"}, status=404)

    def _send_admin_asset(self, asset_name: str) -> None:
        asset = admin_asset_response(asset_name)
        if asset is None:
            self.send_json({"error": "Unknown admin asset"}, status=404)
            return
        data, content_type = asset
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any] | None:
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len or "0")
        except ValueError:
            self.send_json({"error": "Invalid Content-Length"}, status=400)
            return None
        if not (0 <= length <= MAX_HTTP_REQUEST_BYTES):
            self.send_json({"error": "Request body too large"}, status=413)
            return None
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON"}, status=400)
            return None
        if not isinstance(request, dict):
            self.send_json({"error": "Request body must be an object"}, status=400)
            return None
        return request

    def do_POST(self) -> None:  # noqa: N802
        path = posixpath.normpath(self.path.split("?", 1)[0])
        if path not in {"/api/tool", "/api/admin/tool", "/api/admin/settings", "/api/admin/runtime"}:
            self.send_json({"error": "Unknown endpoint"}, status=404)
            return
        if not self.is_admin_request():
            self.send_json({"error": "Admin token required"}, status=401)
            return
        request = self._read_json_body()
        if request is None:
            return
        if path == "/api/admin/settings":
            updates = request.get("settings") if isinstance(request.get("settings"), dict) else request
            self.send_json(self.runtime.save_startup_settings(cast(dict[str, Any], updates)))
            return
        if path == "/api/admin/runtime":
            try:
                self.send_json(self.runtime.apply_runtime_update(request))
            except ToolFailure as exc:
                self.send_json({"error": exc.message, "code": exc.code, "details": exc.details}, status=400)
            return
        if not isinstance(request.get("name"), str):
            self.send_json({"error": "name is required"}, status=400)
            return
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            self.send_json({"error": "arguments must be an object"}, status=400)
            return
        try:
            result = self.runtime.call_tool(request["name"], arguments, admin=True)
        except JsonRpcError as exc:
            self.send_json({"error": exc.message, "data": exc.data}, status=400)
            return
        self.send_json(result)

    def is_admin_request(self) -> bool:
        if not self.runtime.admin_auth_enabled() and not self.runtime.auth_enabled():
            return True
        header = self.headers.get("Authorization", "").strip()
        if self.runtime.admin_token is not None:
            if secrets.compare_digest(header, f"Bearer {self.runtime.admin_token}"):
                return True
        if self.runtime.oauth_config is not None and header.startswith("Bearer "):
            token = header[len("Bearer "):]
            claims = _decode_oauth_token(token, self.runtime.oauth_config, self.oauth_base_url())
            scope = claims.get("scope", "") if claims else ""
            if isinstance(scope, str) and self.runtime.oauth_config.admin_scope in scope.split():
                return True
        return False

    def oauth_base_url(self) -> str:
        cfg = self.runtime.oauth_config
        if cfg is not None and cfg.server_url:
            return cfg.server_url.rstrip("/")
        host = _safe_external_host(self.headers.get("Host", ""))
        if not host:
            address = cast(tuple[Any, ...], self.server.server_address)  # type: ignore[attr-defined]
            host = _http_base_for_bind_host(str(address[0]), int(address[1])).removeprefix("http://")
        return f"http://{host}".rstrip("/")

    def send_json(self, payload: Any, *, status: int = 200) -> None:
        body = json_response_payload(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class RuntimeHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[http.server.BaseHTTPRequestHandler], runtime: Runtime) -> None:
        super().__init__(address, handler)
        self.runtime = runtime


def build_runtime(
    args: argparse.Namespace,
    runtime_policy: RuntimePolicy,
    *,
    auth_token: str | None = None,
    oauth_config: OAuthConfig | None = None,
    config_dir: Path | None = None,
    upstream_config_path: Path | None = None,
    settings_path: Path | None = None,
    startup_settings: dict[str, Any] | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
    admin_ui_enabled: bool = False,
) -> Runtime:
    settings = dict(startup_settings or {})
    workspace = effective_workspace_path(args, settings)
    if config_dir is None or upstream_config_path is None or settings_path is None:
        config_dir, upstream_config_path, settings_path = resolve_config_paths(args, workspace)
        if not settings:
            settings = read_server_settings(settings_path)
    admin_manager = McpAdminManager(upstream_config_path, protocol_version=PROTOCOL_VERSION)
    secret_resolver = admin_manager.secret_vault.get_secret if admin_manager.secret_vault.enabled() else None
    upstream_manager = (
        UpstreamManager.from_config_file(
            upstream_config_path,
            protocol_version=PROTOCOL_VERSION,
            secret_resolver=secret_resolver,
        )
        if upstream_config_path.exists()
        else UpstreamManager.empty(PROTOCOL_VERSION)
    )
    runtime = Runtime(
        workspace,
        enable_view_image=args.enable_view_image,
        permission_mode=runtime_policy.permission_mode,
        shell_env_policy=runtime_policy.shell_env_policy,
        allow_network=runtime_policy.allow_network,
        tool_profile=getattr(args, "tool_profile", None) or "full",
        auth_token=auth_token,
        admin_token=args.admin_token or os.environ.get(f"{ENV_PREFIX}_ADMIN_TOKEN") or _settings_text(settings, "admin_token"),
        oauth_config=oauth_config,
        upstream_manager=upstream_manager,
        admin_manager=admin_manager,
        config_dir=config_dir,
        upstream_config_path=upstream_config_path,
        settings_path=settings_path,
        startup_settings=settings,
        server_host=server_host,
        server_port=server_port,
        admin_ui_enabled=admin_ui_enabled,
        allowed_origins=combine_allowed_origins(
            os.environ.get(f"{ENV_PREFIX}_ALLOWED_ORIGINS"),
            settings.get("allowed_origins"),
            getattr(args, "allowed_origin", None),
        ),
    )
    if runtime.capabilities.skip_all_permissions:
        print(
            "WARNING: permission_mode=dangerous disables MCP safety gates. Use only inside an isolated container or VM.",
            file=sys.stderr,
        )
    return runtime


AUTH_MODE_CHOICES = ("bearer", "noauth", "oauth")


def run_http(args: argparse.Namespace) -> int:
    workspace = effective_workspace_path(args, {})
    config_dir, upstream_config_path, settings_path = resolve_config_paths(args, workspace)
    startup_settings = read_server_settings(settings_path)
    workspace = effective_workspace_path(args, startup_settings)
    config_dir, upstream_config_path, settings_path = resolve_config_paths(args, workspace)
    startup_settings = read_server_settings(settings_path)
    apply_startup_settings(args, startup_settings)
    if getattr(args, "tool_profile", None) is None:
        args.tool_profile = "full"
    args.workspace = str(workspace)
    args.host = effective_host(args, startup_settings)
    args.port = effective_port(args, startup_settings)

    auth_mode = (os.environ.get(f"{ENV_PREFIX}_AUTH_MODE") or "").strip().lower()
    if auth_mode and auth_mode not in AUTH_MODE_CHOICES:
        supported = ", ".join(AUTH_MODE_CHOICES)
        print(f"ERROR: {ENV_PREFIX}_AUTH_MODE must be one of: {supported}.", file=sys.stderr)
        return 2
    auth_token = args.auth_token or os.environ.get(f"{ENV_PREFIX}_AUTH_TOKEN") or _settings_text(startup_settings, "auth_token")
    try:
        runtime_policy = runtime_policy_from_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    oauth_config: OAuthConfig | None = None
    oauth_mode = (
        getattr(args, "oauth_mode", False)
        or truthy_env(os.environ.get(f"{ENV_PREFIX}_OAUTH_MODE"))
        or auth_mode == "oauth"
    )
    if oauth_mode:
        client_id = os.environ.get(f"{ENV_PREFIX}_OAUTH_CLIENT_ID") or None
        client_secret = os.environ.get(f"{ENV_PREFIX}_OAUTH_CLIENT_SECRET") or None
        env_password = os.environ.get(f"{ENV_PREFIX}_OAUTH_PASSWORD")
        password = env_password or _settings_text(startup_settings, "oauth_password") or secrets.token_urlsafe(32)
        server_url = (
            os.environ.get(f"{ENV_PREFIX}_SERVER_URL") or _settings_text(startup_settings, "oauth_server_url") or ""
        ).rstrip("/") or None
        if not env_password:
            print(f"OAuth authorize password: {password}", file=sys.stderr)
        try:
            token_secret = _resolve_oauth_token_secret(startup_settings, settings_path)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        try:
            token_ttl = int(os.environ.get(f"{ENV_PREFIX}_OAUTH_TOKEN_TTL") or OAUTH_TOKEN_TTL_SECONDS)
        except ValueError:
            token_ttl = OAUTH_TOKEN_TTL_SECONDS
        oauth_config = OAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            password=password,
            server_url=server_url,
            token_secret=token_secret,
            token_ttl=token_ttl,
        )
        if auth_token:
            print(
                "Auth: dual credentials enabled — both static bearer token and OAuth 2.1 access tokens will be accepted.",
                file=sys.stderr,
            )

    if (
        not auth_token
        and not oauth_config
        and not is_loopback_bind_host(str(args.host))
        and auth_mode != "noauth"
        and truthy_env(os.environ.get(f"{ENV_PREFIX}_GENERATE_AUTH_TOKEN"))
    ):
        auth_token = secrets.token_urlsafe(32)
        print(f"Generated {ENV_PREFIX}_AUTH_TOKEN for non-loopback binding.", file=sys.stderr)
        print(f"Bearer token: {auth_token}", file=sys.stderr)

    if not auth_token and not oauth_config and not is_loopback_bind_host(str(args.host)):
        print(
            "ERROR: non-loopback HTTP binding requires --auth-token, CODING_TOOLS_MCP_AUTH_TOKEN, or --oauth-mode.",
            file=sys.stderr,
        )
        return 2
    admin_ui_enabled = not bool(getattr(args, "no_admin_ui", False)) and not falsy_env(os.environ.get(f"{ENV_PREFIX}_ADMIN_UI"))
    runtime = build_runtime(
        args,
        runtime_policy,
        auth_token=auth_token,
        oauth_config=oauth_config,
        config_dir=config_dir,
        upstream_config_path=upstream_config_path,
        settings_path=settings_path,
        startup_settings=startup_settings,
        server_host=str(args.host),
        server_port=int(args.port),
        admin_ui_enabled=admin_ui_enabled,
    )
    if admin_ui_enabled and runtime.auth_enabled() and not runtime.admin_auth_enabled():
        print(
            f"ERROR: same-port admin console requires --admin-token, {ENV_PREFIX}_ADMIN_TOKEN, or --oauth-mode when /mcp auth is enabled.",
            file=sys.stderr,
        )
        return 2

    server = RuntimeHTTPServer((args.host, args.port), MCPHandler, runtime)
    admin_server: RuntimeHTTPServer | None = None
    if oauth_config:
        url_label = oauth_config.server_url or "dynamic request URL"
        suffix = " + bearer" if runtime.auth_token else ""
        auth_label = f"oauth2{suffix} enabled (server_url={url_label})"
    elif runtime.auth_token:
        auth_label = "bearer auth enabled"
    else:
        auth_label = "no auth configured"
    base_url = _http_base_for_bind_host(str(args.host), args.port)
    print(f"{SERVER_NAME} listening on {base_url}/mcp ({auth_label}, profile={args.tool_profile})", file=sys.stderr)
    if admin_ui_enabled:
        print(f"{SERVER_NAME} admin console listening on {base_url}/admin", file=sys.stderr)
    separate_admin_enabled = bool(getattr(args, "admin_ui", False))
    if separate_admin_enabled:
        admin_host = str(getattr(args, "admin_host", "127.0.0.1"))
        admin_port = int(getattr(args, "admin_port", 8766))
        admin_server = RuntimeHTTPServer((admin_host, admin_port), AdminUIHandler, runtime)
        admin_thread = threading.Thread(target=admin_server.serve_forever, name="coding-tools-mcp-admin", daemon=True)
        admin_thread.start()
        admin_url = _http_base_for_bind_host(admin_host, admin_port)
        print(f"{SERVER_NAME} compatibility admin console listening on {admin_url}/admin", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        if admin_server is not None:
            admin_server.shutdown()
            admin_server.server_close()
        server.server_close()
    return 0


def run_stdio(args: argparse.Namespace) -> int:
    if getattr(args, "tool_profile", None) is None:
        args.tool_profile = "full"
    try:
        runtime_policy = runtime_policy_from_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    runtime = build_runtime(args, runtime_policy)
    dispatcher = StdioDispatcher(runtime)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if isinstance(request, list) and request:
                response = [item for item in (dispatcher.handle_rpc(part) if isinstance(part, dict) else invalid_request_response() for part in request) if item is not None]
            elif isinstance(request, list):
                response = invalid_request_response()
            elif isinstance(request, dict):
                response = dispatcher.handle_rpc(request)
            else:
                response = invalid_request_response()
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "error": {"code": -32603, "message": str(exc)}}) + "\n")
            sys.stdout.flush()
    return 0


class StdioDispatcher:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.initialized = False

    def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        try:
            validate_rpc_envelope(request)
            method = request["method"]
            params = rpc_params(request)
            if not self.initialized and method not in {"initialize", "ping"}:
                raise JsonRpcError(-32002, "Server not initialized")
            if method == "initialize":
                validate_initialize_params(params)
                result = self.runtime.initialize()
                self.initialized = True
            elif method == "notifications/initialized":
                return None
            elif method == "notifications/cancelled":
                session_id = params.get("session_id")
                if isinstance(session_id, str):
                    self.runtime.cancel_session(session_id)
                return None
            elif method == "ping":
                result = {}
            elif method == "logging/setLevel":
                result = self.runtime.set_logging_level(params)
            elif method == "tools/list":
                result = self.runtime.list_tools()
            elif method == "tools/call":
                if not isinstance(params.get("name"), str):
                    raise JsonRpcError(-32602, "tools/call requires a tool name")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise JsonRpcError(-32602, "tools/call arguments must be an object")
                result = self.runtime.call_tool(params["name"], arguments)
            else:
                raise JsonRpcError(-32601, f"Unknown method: {method}")
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JsonRpcError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            response: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
            if request_id is not None:
                response["id"] = request_id
            return response


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve workspace-confined coding tools over MCP.")
    parser.add_argument("--workspace", help="workspace root; defaults to CODING_TOOLS_MCP_WORKSPACE or cwd")
    parser.add_argument(
        "--host",
        default=None,
        help=f"bind host; defaults to {ENV_PREFIX}_HOST, server settings, or 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"bind port; defaults to {ENV_PREFIX}_PORT, server settings, or 8000",
    )
    parser.add_argument("--stdio", action="store_true", help="serve newline-delimited JSON-RPC over stdio")
    parser.add_argument(
        "--auth-token",
        default=None,
        help=f"require Authorization: Bearer <token> on /mcp; defaults to {ENV_PREFIX}_AUTH_TOKEN",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=None,
        help=(
            "extra exact Origin allowed for browser MCP clients; repeatable; "
            f"can also be set with comma-separated {ENV_PREFIX}_ALLOWED_ORIGINS"
        ),
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help=f"enable admin MCP management tools for this separate bearer token; defaults to {ENV_PREFIX}_ADMIN_TOKEN",
    )
    parser.add_argument(
        "--admin-ui",
        action="store_true",
        default=False,
        help="start the legacy separate admin web console in addition to same-port /admin",
    )
    parser.add_argument(
        "--no-admin-ui",
        action="store_true",
        default=False,
        help=f"disable the same-port /admin web console; can also be disabled with {ENV_PREFIX}_ADMIN_UI=0",
    )
    parser.add_argument(
        "--admin-host",
        default=os.environ.get(f"{ENV_PREFIX}_ADMIN_HOST") or "127.0.0.1",
        help=f"admin console bind host; defaults to {ENV_PREFIX}_ADMIN_HOST or 127.0.0.1",
    )
    parser.add_argument(
        "--admin-port",
        type=int,
        default=env_int(f"{ENV_PREFIX}_ADMIN_PORT", 8766),
        help=f"admin console bind port; defaults to {ENV_PREFIX}_ADMIN_PORT or 8766",
    )
    parser.add_argument(
        "--oauth-mode",
        action="store_true",
        default=False,
        help=(
            "enable OAuth 2.1 Authorization Code + PKCE; "
            f"{ENV_PREFIX}_SERVER_URL is optional; when unset OAuth metadata uses the request host; "
            "authorize password is generated when unset; client_id/client_secret are optional"
        ),
    )
    parser.add_argument(
        "--tool-profile",
        choices=TOOL_PROFILE_CHOICES,
        default=os.environ.get(f"{ENV_PREFIX}_TOOL_PROFILE") or None,
        help="tool exposure profile",
    )
    parser.add_argument(
        "--upstream-config",
        default=None,
        help=f"JSON config for upstream MCP gateway servers; defaults to {ENV_PREFIX}_UPSTREAM_CONFIG",
    )
    parser.add_argument(
        "--config-dir",
        default=None,
        help=f"directory for {UPSTREAM_CONFIG_FILENAME} and {SERVER_SETTINGS_FILENAME}; defaults to {ENV_PREFIX}_CONFIG_DIR or <workspace>/{DEFAULT_CONFIG_DIR_NAME}",
    )
    parser.add_argument(
        "--shell-env-inherit",
        choices=SHELL_ENV_INHERIT_CHOICES,
        default=None,
        help=(
            "baseline environment inheritance for exec_command subprocesses; "
            f"defaults to {ENV_PREFIX}_SHELL_ENV_INHERIT or core"
        ),
    )
    parser.add_argument(
        "--permission-mode",
        choices=PERMISSION_MODE_CHOICES,
        default=None,
        help=(
            "exec_command permission mode: safe denies network/shell-expansion/inline-script gates; "
            "trusted allows local development network, shell expansion, and inline scripts; "
            "dangerous disables permission gates"
        ),
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "compatibility alias: allow network-looking exec_command calls without changing other gates; "
            f"can also be enabled with {ENV_PREFIX}_ALLOW_NETWORK=1"
        ),
    )
    parser.add_argument(
        "--enable-view-image",
        action="store_true",
        default=os.environ.get("CODING_TOOLS_MCP_ENABLE_VIEW_IMAGE", "1") != "0",
        help="enable the P1 view_image tool",
    )
    parser.add_argument(
        "--dangerously-skip-all-permissions",
        action="store_true",
        help=(
            "compatibility alias for --permission-mode dangerous; workspace path boundaries for direct file tools still apply"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_stdio(args) if args.stdio else run_http(args)
