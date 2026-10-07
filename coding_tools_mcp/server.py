from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import hashlib
import html
import difflib
import fnmatch
import functools
import http.server
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast

from . import __version__
from .envutils import ENV_PREFIX, truthy_env
from .errors import JsonRpcError, ToolFailure
from .policy import IsolationConfig, ExecutionPolicy, compile_policy, ISOLATION_MODES, NETWORK_MODES
from .service_trust import ServiceImportSnapshot
from .executor import WorkspaceExecutor
from .file_broker import FileBroker
from .shells import (
    selected_windows_command_shell, shell_command, windows_command_shell_payload,
    merge_environment, powershell_scannable, powershell_dynamic_construct, cmd_dynamic_construct,
    is_windows_loader_env_name, sanitized_environment, windows_pathext, WINDOWS_CORE_ENV_NAMES,
)
from .event_log import ToolEventJournal
from .landlock_exec import libc_syscall
from .oauth import (
    OAUTH_CODE_TTL_SECONDS,
    OAUTH_GRANT_TYPE_AUTHORIZATION_CODE,
    OAUTH_GRANT_TYPES_SUPPORTED,
    OAUTH_MAX_BODY_BYTES,
    OAUTH_RESPONSE_TYPES_SUPPORTED,
    MAX_PENDING_CODES,
    OAUTH_TOKEN_TTL_SECONDS,
    OAuthConfig,
    create_access_token,
    valid_pkce_challenge,
    validate_access_token,
    verify_pkce,
)
from .breaker import REPEATED_CALL_BLOCKED, RepeatFailureBreaker, argument_fingerprint
from .changes import (
    CHANGE_ACTIONS,
    EDIT_OPERATIONS,
    MAX_CHANGES_PER_CALL,
    MAX_EDITS_PER_CHANGE,
    ChangeRequest,
    apply_line_edits,
    normalize_revision,
    parse_changes,
    reject_duplicate_paths,
)
from .patching import (
    REVISION_ALGORITHM,
    AtomicPatchCommitter,
    FileBaseline,
    StagedFile,
    apply_update_hunks_detailed,
    changed_ranges_between,
    content_revision,
    parse_patch,
    read_text_preserve_newlines,
)
from .processes import (
    HARD_KILL_SIGNAL,
    COMMAND_BUFFER_BYTES,
    COMMAND_HEAD_BUFFER_DIVISOR,
    COMMAND_OUTCOMES,
    CommandRun,
    start_reader_threads,
    start_command_watchdog,
    terminate_process_group,
)
from .protocol import (
    HEADER_MISMATCH,
    KNOWN_PROTOCOL_VERSIONS,
    LATEST_LEGACY_PROTOCOL_VERSION,
    MODERN_ERA,
    MODERN_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    RequestContext,
    dispatch_rpc,
    jsonrpc_error,
    legacy_protocol_version_is_supported,
    protocol_version_is_known,
    request_era,
    response_id,
    validate_mirror_headers,
    validate_modern_meta,
    validate_rpc_envelope,
)
from .project_context import ProjectContext, load_project_context
from .telemetry import SessionTelemetry
from .textutils import DEFAULT_MAX_LINES, TextTruncation, truncate_text_head
from .tool_results import make_tool_result
from .transport_stdio import serve_stdio


SERVER_NAME = "coding-tools-mcp"
SERVER_TITLE = "Coding Tools MCP"
MCP_ENDPOINT_PATH = "/mcp"
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
# Documented kill_command status enum; guarded by test_schema_drift.
KILL_COMMAND_STATUSES = ("terminated", "killed", "exited", "terminating", "not_found")
POSIX_CORE_ENV_NAMES = {"PATH", "LANG", "LC_ALL", "TERM"}
# Not POSIX core, but inherited under inherit="core" so git helper subprocesses and
# exec_command share the host's global git config (e.g. safe.directory entries).
GIT_ENV_NAMES = {"GIT_CONFIG_GLOBAL"}
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
EXEC_PREVIEW_BYTES = 4096
MAX_ACTIVE_COMMANDS = 16
MAX_RETAINED_OUTPUT_COMMANDS = 32
COMPLETED_COMMAND_TTL_SECONDS = 300
MAX_RUNTIME_OUTPUT_BYTES = 16 * 1024 * 1024
# timeout_ms is the total process lifetime and yield_time_ms is only how long
# the call waits before returning a command_id. They are separate budgets: a
# build that outlives the first return keeps running until the lifetime ends.
DEFAULT_PROCESS_LIFETIME_MS = 300000
MAX_PROCESS_LIFETIME_MS = 600000
DEFAULT_YIELD_MS = 10000
MAX_YIELD_MS = 30000
MAX_UNTRACKED_DIFF_FILES = 100
# A mutating call may be retried because its response was lost, not because it
# failed. Tools in this set accept an idempotency_key and replay the recorded
# result for a repeat of the same key instead of applying the work twice.
IDEMPOTENT_TOOLS = frozenset({"apply_patch", "apply_changes"})
# The write primitives. A success here changes the tree every other tool reads,
# which is what makes a previously deterministic failure worth re-attempting.
WORKSPACE_WRITE_TOOLS = frozenset({"apply_patch", "apply_changes"})
# Starting or killing a command that may write changes the tree just as
# surely, only at a moment the server cannot observe. Polling tools
# (write_stdin, read_output) are deliberately absent: a loop on those must
# still be stoppable.
BREAKER_RESET_COMMAND_TOOLS = frozenset({"exec_command", "kill_command"})
IDEMPOTENCY_CACHE_ENTRIES = 64
IDEMPOTENCY_KEY_MAX_LENGTH = 128
# Command ids remembered only to keep one finished command from being counted
# as a failed operation once per poll. Comfortably larger than the retained
# command set, so a command cannot outlive its own ledger entry.
COUNTED_OUTCOME_LEDGER_ENTRIES = 512
IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
IDEMPOTENCY_KEY_DESCRIPTION = (
    "Names this exact request so a retry after a lost response replays the recorded result "
    "(flagged idempotent_replay) instead of doing the work twice. Use a new key for new work: "
    "the key is bound to the arguments that first used it, and reusing it with any other "
    "argument — including a different dry_run — is refused with IDEMPOTENCY_KEY_REUSED. "
    "Only a successful non-dry-run result is recorded, and only the "
    f"{IDEMPOTENCY_CACHE_ENTRIES} most recently used keys are kept across all tools."
)
_COMMAND_RECOVERY_HINT = (
    "This command_id is not retained. It never existed, or it was dropped: a"
    f" finished command is kept for {COMPLETED_COMMAND_TTL_SECONDS} seconds after a"
    " response first reported its exit, finished commands are evicted beyond the"
    f" last {MAX_RETAINED_OUTPUT_COMMANDS} or {MAX_RUNTIME_OUTPUT_BYTES // (1024 * 1024)} MB"
    " of retained output, and a server restart drops them all. Retrying with the"
    " same command_id cannot succeed. Start the work again with exec_command and"
    " use the command_id it returns."
)
# Statuses with which a response reports that a command has finished. A
# kill_command `terminating` result is not one: the process is still alive.
TERMINAL_COMMAND_STATUSES = frozenset({"exited", "terminated", "timeout", "killed"})
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
RUNTIME_ROOT_DIR_NAME = "coding-tools-mcp"
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
OS_METADATA_READ_FILES = (
    "/etc/debian_version",
    "/etc/os-release",
    "/etc/lsb-release",
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

@dataclass(frozen=True)
class ShellEnvPolicy:
    inherit: str = "core"
    include_only: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    set: dict[str, str] = field(default_factory=dict)


WORKSPACE_MUTATION_CHOICES = ("unrestricted", "structured-only")


@dataclass(frozen=True)
class WorkspaceMutationPolicy:
    """Who may write to the workspace: the tools only, or commands too.

    ``structured-only`` makes the workspace read-only for `exec_command` under
    Landlock, leaving `apply_patch` and `apply_changes` as the only way in.
    Every real project needs somewhere to put build output, so `write_paths`
    is the escape hatch, and the default stays ``unrestricted`` because
    defaulting this on would break pytest, npm, cargo, gradle, and git in one
    release.
    """

    mode: str = "unrestricted"
    write_paths: tuple[str, ...] = ()

    @property
    def structured_only(self) -> bool:
        return self.mode == "structured-only"


@dataclass(frozen=True)
class RuntimePolicy:
    permission_mode: str
    shell_env_policy: ShellEnvPolicy
    allow_network: bool
    fake_readonly_annotations: bool = False
    workspace_mutation: WorkspaceMutationPolicy = WorkspaceMutationPolicy()
    isolation: IsolationConfig = IsolationConfig()


OAUTH_TOKEN_AUTH_METHODS = ("client_secret_basic", "client_secret_post", "none")


def _http_base_for_bind_host(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _first_header_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def _first_form_value(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key)
    return values[0] if values else ""


def _forwarded_header_param(value: str | None, name: str) -> str:
    first = _first_header_value(value)
    for part in first.split(";"):
        key, sep, raw = part.strip().partition("=")
        if sep and key.lower() == name:
            return raw.strip().strip('"')
    return ""


def _safe_external_host(host: str) -> str:
    host = host.strip()
    if not host or any(ch.isspace() or ch in "/\\@?#" for ch in host):
        return ""
    try:
        parsed = urllib.parse.urlsplit(f"//{host}")
        _ = parsed.port
    except ValueError:
        return ""
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return ""
    return host


def env_pattern_matches(name: str, patterns: tuple[str, ...]) -> bool:
    upper_name = name.upper()
    return any(fnmatch.fnmatchcase(upper_name, pattern.upper()) for pattern in patterns)


def is_risky_env_name(name: str) -> bool:
    upper = name.upper()
    return upper in RISKY_ENV_NAMES or upper.startswith("DYLD_") or (os.name == "nt" and is_windows_loader_env_name(name))


def is_filtered_env_var(name: str, value: str) -> bool:
    return bool(SENSITIVE_ENV_RE.search(name) or is_risky_env_name(name) or SENSITIVE_VALUE_RE.search(value))


def is_core_command_env_name(name: str) -> bool:
    upper = name.upper()
    if os.name == "nt":
        return upper in WINDOWS_CORE_ENV_NAMES
    return upper in POSIX_CORE_ENV_NAMES or upper in GIT_ENV_NAMES or upper.startswith("LC_")


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


def env_int(name: str, fallback: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


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


def fake_readonly_annotations_from_args(args: argparse.Namespace, permission_mode: str) -> bool:
    requested = bool(getattr(args, "dangerously_fake_readonly_annotations", False)) or truthy_env(
        os.environ.get(f"{ENV_PREFIX}_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS")
    )
    if requested and permission_mode != "dangerous":
        raise ValueError(
            "--dangerously-fake-readonly-annotations requires --permission-mode dangerous"
        )
    return requested


def workspace_mutation_policy_from_args(args: argparse.Namespace) -> WorkspaceMutationPolicy:
    raw_mode = (
        getattr(args, "workspace_mutation", None)
        or os.environ.get(f"{ENV_PREFIX}_WORKSPACE_MUTATION")
        or "unrestricted"
    )
    mode = str(raw_mode).strip().lower()
    if mode not in WORKSPACE_MUTATION_CHOICES:
        supported = ", ".join(WORKSPACE_MUTATION_CHOICES)
        raise ValueError(f"workspace mutation mode must be one of: {supported}")
    raw_paths = list(getattr(args, "write_path", None) or ())
    from_env = os.environ.get(f"{ENV_PREFIX}_WRITE_PATHS") or ""
    raw_paths.extend(part for part in from_env.split(os.pathsep) if part.strip())
    write_paths = tuple(dict.fromkeys(part.strip() for part in raw_paths if part.strip()))
    if write_paths and mode != "structured-only":
        raise ValueError("--write-path only applies with --workspace-mutation structured-only")
    return WorkspaceMutationPolicy(mode=mode, write_paths=write_paths)


def runtime_policy_from_args(args: argparse.Namespace) -> RuntimePolicy:
    try:
        isolation = IsolationConfig.from_args(args)
    except ToolFailure as exc:
        raise ValueError(f"{exc.code}: {exc.message}") from exc
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
        fake_readonly_annotations=fake_readonly_annotations_from_args(args, permission_mode),
        workspace_mutation=workspace_mutation_policy_from_args(args),
        isolation=isolation,
    )


@dataclass(frozen=True)
class ToolSpec:
    """Single source of truth for one tool's title, description, and annotation hints.

    Handler methods on Runtime are named exactly after the tool. Input schemas live in
    input_schemas(), keyed by the same names. `error_status` is stamped on failure
    payloads, and `content_builder` converts a success payload into extra MCP
    content blocks (beyond the rendered text).
    """

    title: str
    description: str
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    error_status: str | None = None
    content_builder: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
    gated_by: str | None = None
    """Name of a Runtime attribute that must be truthy for the tool to be exposed."""
    callable_when_hidden: bool = False
    """Whether a client that already knows the name may still call it when hidden.

    A capability gate (``view_image``) means the tool cannot work, so a hidden
    tool is an unknown tool. A mode gate means the tool works but can only ever
    report one answer, so hiding it stops advertising a guaranteed failure
    without breaking a client that calls it anyway.
    """


def _count_lines(text: str) -> int:
    """Count file lines the way ``read_file`` reports ``total_lines``."""

    # TextIO's universal-newline iteration treats CRLF, LF, and bare CR as
    # line boundaries. Normalize only this temporary counting view; the
    # caller's original bytes are preserved unchanged.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.count("\n") + (1 if normalized and not normalized.endswith("\n") else 0)


def _patch_evidence(
    content: str, changed_ranges: list[dict[str, int]], *, quality: str | None = None
) -> dict[str, Any]:
    """Post-edit evidence for one file: what the result is, not just that it worked.

    ``revision`` is the same token ``read_file`` publishes and ``apply_changes``
    requires, so a model can chain a structured edit onto a patch result
    without re-reading the file.
    """

    evidence: dict[str, Any] = {
        "revision": content_revision(content),
        "total_lines": _count_lines(content),
        "changed_ranges": changed_ranges,
    }
    if quality is not None:
        evidence["match_quality"] = quality
    return evidence


def _whole_file_range(text: str) -> list[dict[str, int]]:
    """The changed range for a file written in full."""

    lines = _count_lines(text)
    return [{"start_line": 1, "end_line": lines, "added_lines": lines, "removed_lines": 0}]


def _merge_patch_affected_file(
    affected: dict[str, dict[str, Any]], entry: dict[str, Any]
) -> None:
    """Keep one final evidence record per resolved path.

    A path staged as an earlier operation's destination can later become a
    primary update path. Merge placement quality for that chained update; once
    the chain is complete, the caller replaces block-local ranges with a diff
    from the original baseline to the final staged content. Full destination
    overwrites (Add and Move) replace evidence directly instead of using this
    helper, because evidence from bytes they overwrite is stale.
    """

    path = str(entry["path"])
    previous = affected.get(path)
    if previous is None:
        affected[path] = entry
        return
    merged = dict(entry)
    if entry.get("operation") == "delete":
        # A deleted file has no final text whose placement quality could be
        # inspected. Do not leak evidence from an earlier staged update.
        merged.pop("match_quality", None)
        affected[path] = merged
        return
    else:
        previous_ranges = previous.get("changed_ranges")
        current_ranges = entry.get("changed_ranges")
        merged["changed_ranges"] = [
            *(previous_ranges if isinstance(previous_ranges, list) else []),
            *(current_ranges if isinstance(current_ranges, list) else []),
        ]
    if merged.get("operation") == "unchanged" and previous.get("operation") != "unchanged":
        merged["operation"] = previous["operation"]
    quality_order = {"exact": 0, "trailing_ws": 1, "indent": 2}
    previous_quality = previous.get("match_quality")
    current_quality = merged.get("match_quality")
    if (
        isinstance(previous_quality, str)
        and quality_order.get(previous_quality, -1) > quality_order.get(str(current_quality), -1)
    ):
        merged["match_quality"] = previous_quality
    affected[path] = merged


def _image_content(payload: dict[str, Any]) -> list[dict[str, Any]]:
    encoded = str(payload.pop("_mcp_image_data", ""))
    return [
        {
            "type": "image",
            "data": encoded,
            "mimeType": str(payload.get("mime_type", "application/octet-stream")),
        }
    ]


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "server_info": ToolSpec(
        title="Server info",
        description="Return server, workspace, project-context, auth, policy, and fixed-tool metadata.",
        read_only=True,
        idempotent=True,
    ),
    "check_exec_environment": ToolSpec(
        title="Check exec environment",
        description="Return lightweight exec_command sandbox and environment status known to the server.",
        read_only=True,
        idempotent=True,
    ),
    "read_file": ToolSpec(
        title="Read file",
        description=(
            "Read a UTF-8 text file slice inside the configured workspace. The result text opens with a "
            "banner carrying the file's revision (the token apply_changes needs). Pass "
            "line_numbers=true to prefix each line with its number and a tab in the text, which is the "
            "numbering apply_changes edits use."
        ),
        read_only=True,
        idempotent=True,
    ),
    "list_dir": ToolSpec(
        title="List directory",
        description="List directory entries inside the configured workspace.",
        read_only=True,
        idempotent=True,
    ),
    "list_files": ToolSpec(
        title="List files",
        description="List workspace files using glob filters.",
        read_only=True,
        idempotent=True,
    ),
    "search_text": ToolSpec(
        title="Search text",
        description="Search UTF-8 workspace files for text or regex matches.",
        read_only=True,
        idempotent=True,
    ),
    "apply_patch": ToolSpec(
        title="Apply patch",
        description=(
            "Stage, validate, and atomically apply a V4A patch envelope. Hunks are located from a "
            "forward-only search cursor, in file order. `@@ <context>` is a language-agnostic text anchor "
            "that advances that cursor; it does not name a function or block. An anchored hunk takes the "
            "first match after its anchor, so use `@@` to pick between repeated blocks; consecutive `@@` "
            "lines are found in turn. An unanchored hunk that matches more than once after the cursor is "
            "PATCH_CONTEXT_AMBIGUOUS. A missing anchor fails rather than falling back before it. A "
            "pure-addition hunk validates its anchor, if any, then appends at EOF. `*** End of File` "
            "requires the hunk to match at the tail. A blank context line may be "
            "written as \"\" or as a single space. Matching is "
            "graded exact, then ignoring trailing whitespace, then ignoring indentation width, and the "
            "grade actually used comes back as match_quality. Success returns each file's revision, "
            "total_lines, and changed_ranges. Each operation's primary path may appear only once per "
            "envelope. Add File may replace an existing file; Move to may replace an existing destination, "
            "and distinct source paths may move to the same destination in order (the later write wins). "
            "Full format reference: docs/tools-and-schemas.md. Example: "
            "*** Begin Patch\n*** Update File: app.py\n@@\n-old\n+new\n*** End Patch"
        ),
        destructive=True,
    ),
    "apply_changes": ToolSpec(
        title="Apply changes",
        description=(
            "Apply line-addressed file changes atomically. Prefer this over apply_patch when you know "
            "the line numbers: nothing has to match. Each change names an action (create, write, edit, "
            "delete, move, copy), and a path. revision is the 64-hex value from read_file, or from the "
            "latest apply_changes/apply_patch result for that path (its text prints path: revision=...); "
            "a dry run's would_be_revision is not written and cannot be used. Line numbers must come "
            "from the same version of the file as the revision: after any write, use the new revision "
            "with the new line numbers, or re-read (read_file line_numbers=true shows them). write is an "
            "upsert: it needs the revision when the path exists, but may omit it when creating a missing "
            "path. edit, delete, move, and copy always need it; create rejects it and asserts absence "
            "(re-sending identical content is a no-op). edit takes line operations: replace and delete "
            "use start_line and optional end_line (inclusive, defaulting to start_line), or line "
            "for one line. If line and start_line are both supplied they must agree; explicit "
            "start_line allows end_line to extend the range. With line alone, any end_line must "
            "equal line. insert_after and insert_before use line, or start_line as the anchor; "
            "any supplied numbering fields must equal that anchor. All numbers refer to the "
            "file as read, not to the result of "
            "earlier edits in the same call. content is whole lines: \"\" is zero lines and a trailing "
            "newline adds a blank line. A path may appear once per call: combine all line edits for "
            "one file into that file's single edit change. Example, with revision set to the value "
            "read_file printed: {\"changes\":[{\"action\":\"edit\",\"path\":\"app.py\","
            "\"revision\":\"<64-hex revision from read_file>\",\"edits\":[{\"op\":\"replace\","
            "\"start_line\":10,\"end_line\":12,\"content\":\"new line\"}]}]}"
        ),
        destructive=True,
    ),
    "exec_command": ToolSpec(
        title="Execute command",
        description=(
            "Run a bounded command under runtime policy. Pass workdir explicitly for reconnect-safe paths. "
            "yield_time_ms is only how long this call waits (default 10s); timeout_ms is the total process "
            "lifetime (default 300s). A command still running when the call returns keeps running under its "
            "command_id; poll it with write_stdin (chars=\"\"), or page output with read_output using the "
            "returned output_refs. stdin is closed after the optional stdin text unless keep_stdin_open=true. "
            "Example: "
            "{\"cmd\":\"pytest -q\",\"workdir\":\".\",\"yield_time_ms\":30000}. "
            "Retained output is bounded per stream; for very large output redirect to a file "
            "(cmd > out.log 2>&1) and page it with read_file or search_text."
        ),
        destructive=True,
        open_world=True,
        error_status="failed",
    ),
    "write_stdin": ToolSpec(
        title="Write stdin",
        description=(
            "Poll or interact with a running command by command_id. Empty chars wait for output; non-empty "
            "chars writes to stdin, which requires a command started with keep_stdin_open=true or tty=true "
            "(POSIX only). Example: {\"command_id\":\"abc\",\"chars\":\"\",\"yield_time_ms\":10000}."
        ),
    ),
    "kill_command": ToolSpec(
        title="Kill command",
        description=(
            "Terminate a server-managed command by command_id. Example: "
            "{\"command_id\":\"abc\",\"signal\":\"KILL\"}."
        ),
        destructive=True,
    ),
    "read_output": ToolSpec(
        title="Read output",
        description=(
            "Read retained command output and the command's status/exit_code. output_ref is a ref from "
            "output_refs (command:<id>:stdout), or a bare command_id / command:<id> with stream "
            "(default stdout). "
            "Each stream retains the earliest output (head) plus the most recent output (rolling tail); "
            "bytes between them may be evicted and are reported via evicted_gap_bytes. "
            "Example: {\"output_ref\":\"command:abc:stdout\",\"offset\":0,\"limit\":4096}."
        ),
        read_only=True,
        idempotent=True,
    ),
    "git_status": ToolSpec(
        title="Git status",
        description="Return git working tree status for the workspace.",
        read_only=True,
        idempotent=True,
    ),
    "git_diff": ToolSpec(
        title="Git diff",
        description=(
            "Return unified git diff for workspace changes. Untracked files are included as "
            "additions by default, so a newly created file is verifiable here."
        ),
        read_only=True,
        idempotent=True,
    ),
    "git_log": ToolSpec(
        title="Git log",
        description="Return recent git commits with bounded structured metadata.",
        read_only=True,
        idempotent=True,
    ),
    "git_show": ToolSpec(
        title="Git show",
        description="Return bounded git show output for a revision.",
        read_only=True,
        idempotent=True,
    ),
    "git_blame": ToolSpec(
        title="Git blame",
        description="Return bounded git blame metadata for a workspace file.",
        read_only=True,
        idempotent=True,
    ),
    "request_permissions": ToolSpec(
        title="Request permissions",
        description=(
            "Report scoped permission-request status without silently granting operations. "
            "Advertised only in permission_mode=dangerous, the sole configuration in which it can "
            "return granted; elsewhere it can only ever return ELICITATION_UNSUPPORTED."
        ),
        read_only=True,
        gated_by="dangerously_skip_all_permissions",
        callable_when_hidden=True,
    ),
    "view_image": ToolSpec(
        title="View image",
        description="Return a workspace image as MCP image content.",
        read_only=True,
        idempotent=True,
        content_builder=_image_content,
        gated_by="enable_view_image",
    ),
}

LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
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


def json_response_payload(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@functools.lru_cache(maxsize=8)
def _configured_allowed_origins(raw: str) -> frozenset[str]:
    return frozenset(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


def is_allowed_origin(origin: str) -> bool:
    # Authentication does not replace browser Origin validation.
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    normalized = origin.rstrip("/")
    configured = _configured_allowed_origins(os.environ.get(f"{ENV_PREFIX}_ALLOWED_ORIGINS", ""))
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"} or normalized in configured


def is_loopback_bind_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", ""}


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


def truncate_line_chars(line: str, max_chars: int = GREP_MAX_LINE_CHARS) -> tuple[str, bool]:
    if len(line) <= max_chars:
        return line, False
    suffix = " ... [truncated]"
    keep = max(0, max_chars - len(suffix))
    return line[:keep] + suffix, True


def normalize_rel_display(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix()
    text = rel.as_posix()
    return "." if text == "" else text


def matches_any_glob(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) or PurePosixPath(rel).match(pattern) for pattern in patterns)


def file_entry(path: Path, rel: str, path_stat: os.stat_result) -> dict[str, Any]:
    return {
        "path": rel,
        "type": "symlink" if path.is_symlink() else "file",
        "size_bytes": path_stat.st_size,
        "modified": datetime.fromtimestamp(path_stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def search_match_item(
    rel: str,
    line_number: int,
    column: int,
    line: str,
    before: list[str],
    after: list[str],
    max_preview_bytes: int,
) -> dict[str, Any]:
    preview, line_truncated = truncate_line_chars(line)
    preview_truncation = truncate_text_head(preview, max_lines=1, max_bytes=max_preview_bytes)
    item: dict[str, Any] = {
        "path": rel,
        "line": line_number,
        "column": column,
        "preview": preview_truncation.content,
        "before": before,
        "after": after,
    }
    if line_truncated or preview_truncation.truncated:
        item["preview_truncated"] = True
        item["preview_truncated_by"] = "chars" if line_truncated else preview_truncation.truncated_by
    return item


def truncation_fields(truncation: TextTruncation) -> dict[str, Any]:
    return {
        "truncated": truncation.truncated,
        "truncated_by": truncation.truncated_by,
        "output_lines": truncation.output_lines,
        "output_bytes": truncation.output_bytes,
    }


def command_output_refs(command_id: str) -> dict[str, str]:
    """Return stable stdout and stderr paging references for a command."""
    return {stream: f"command:{command_id}:{stream}" for stream in ("stdout", "stderr")}


def read_output_action(output_ref: str, *, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    return {
        "tool": "read_output",
        "arguments": {
            "output_ref": output_ref,
            "offset": offset,
            "limit": EXEC_PREVIEW_BYTES if limit is None else limit,
        },
    }


_TOOL_PATHS: dict[str, str] = {}


def cached_which(*names: str) -> str | None:
    """shutil.which with a success-only cache: absence keeps re-probing so a
    tool installed mid-session is still picked up."""
    cached = _TOOL_PATHS.get(names[0])
    if cached:
        return cached
    for name in names:
        path = shutil.which(name)
        if path:
            _TOOL_PATHS[names[0]] = path
            return path
    return None


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


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
        self.git_path = shutil.which("git")
        self.executor: WorkspaceExecutor | None = WorkspaceExecutor(
            lambda purpose: compile_policy(IsolationConfig(), self.root, Path(tempfile.gettempdir()) / "coding-tools-context-unused", purpose=purpose),
            lambda: dict(os.environ),
        )
        self.file_broker: FileBroker | None = None

    def _reject_unsafe_text(self, raw_path: str) -> PurePosixPath:
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolFailure("INVALID_ARGUMENT", "Path must be a non-empty string.", category="validation")
        if "\x00" in raw_path:
            raise ToolFailure("INVALID_ARGUMENT", "Path contains a NUL byte.", category="validation")
        if raw_path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", raw_path):
            raise ToolFailure("ABSOLUTE_PATH_DENIED", "Absolute paths are denied.", category="security")
        pure = PurePosixPath(raw_path)
        if any(part == ".." for part in pure.parts):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "Path escapes the configured workspace.", category="security")
        return pure

    def resolve_existing(self, raw_path: str = ".") -> ResolvedPath:
        if self.file_broker is not None:
            display = self.file_broker.normalize(raw_path or ".")
            try:
                self.file_broker.stat(display)
            except FileNotFoundError as exc:
                raise ToolFailure("NOT_FOUND", f"Path not found: {raw_path}", category="not_found") from exc
            return ResolvedPath(display, self.root / display, True)
        pure = self._reject_unsafe_text(raw_path or ".")
        candidate = self.root.joinpath(*pure.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ToolFailure("NOT_FOUND", f"Path not found: {raw_path}", category="not_found") from exc
        if not is_relative_to(resolved, self.root):
            code = "SYMLINK_ESCAPE" if candidate.is_symlink() else "PATH_OUTSIDE_WORKSPACE"
            raise ToolFailure(code, "Path escapes the configured workspace.", category="security")
        return ResolvedPath(normalize_rel_display(resolved, self.root), resolved, True)

    def resolve_for_write(self, raw_path: str) -> ResolvedPath:
        if self.file_broker is not None:
            display = self.file_broker.normalize(raw_path)
            existed = self.file_broker.exists(display)
            return ResolvedPath(display, self.root / display, existed)
        pure = self._reject_unsafe_text(raw_path)
        if pure.name in {"", ".", ".."}:
            raise ToolFailure("INVALID_ARGUMENT", "Invalid write target.", category="validation")
        candidate = self.root.joinpath(*pure.parts)
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

    def reject_write_symlink(self, raw_path: str) -> None:
        pure = self._reject_unsafe_text(raw_path)
        candidate = self.root.joinpath(*pure.parts)
        if candidate.is_symlink():
            raise ToolFailure("SYMLINK_ESCAPE", "Writing through symlinks is denied.", category="security")

    def is_ignored_path(
        self,
        path: Path,
        *,
        include_hidden: bool = False,
        include_ignored: bool = False,
        git_ignored: set[str] | None = None,
    ) -> bool:
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
        rel_text = rel.as_posix()
        if rel_text in (git_ignored if git_ignored is not None else self.git_ignored_paths([rel_text])):
            return True
        return False

    def is_safe_existing_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            return False
        return is_relative_to(resolved, self.root)

    def git_ignored_paths(self, rel_paths: list[str]) -> set[str]:
        if not rel_paths or self.executor is None:
            return set()
        git = self.git_path
        if not git:
            return set()
        try:
            completed = self.executor.run(
                [git, "-C", str(self.root), "check-ignore", "--stdin", "-z"],
                input=b"\0".join(os.fsencode(path) for path in rel_paths) + b"\0",
                text=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        if completed.returncode not in {0, 1}:
            return set()
        return {os.fsdecode(path) for path in completed.stdout.split(b"\0") if path}


class WorkspaceCommandManager:
    """Own commands for one workspace independently of MCP transport sessions."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve(strict=True)
        self.server_instance_id = secrets.token_urlsafe(12)
        self.runtime_dir = runtime_dir_for_workspace(self.workspace, self.server_instance_id)
        self.fallback_runtime_dir = fallback_runtime_dir_for_workspace(
            self.workspace, self.server_instance_id
        )
        self.commands: dict[str, CommandRun] = {}
        self.output_commands: dict[str, CommandRun] = {}
        self.lock = threading.Lock()
        self.starting_commands = 0
        self.closed = False
        # Retention observability: how often output is evicted past the head
        # segment and how often clients actually ask for evicted bytes. High
        # hit rates are the signal to consider spilling output to disk.
        self._retention_stats_lock = threading.Lock()
        self._retention_stats = {
            "evict_events": 0,
            "evicted_bytes_total": 0,
            "read_output_omitted_hits": 0,
            "poll_omitted_hits": 0,
        }

    def record_output_eviction(self, stream: str, lost_bytes: int) -> None:
        with self._retention_stats_lock:
            self._retention_stats["evict_events"] += 1
            self._retention_stats["evicted_bytes_total"] += lost_bytes

    def record_omitted_read(self, kind: str) -> None:
        key = f"{kind}_omitted_hits"
        with self._retention_stats_lock:
            if key in self._retention_stats:
                self._retention_stats[key] += 1

    def retention_stats_snapshot(self) -> dict[str, int]:
        with self._retention_stats_lock:
            return dict(self._retention_stats)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            commands = list(self.commands.values())
            self.commands.clear()
            self.output_commands.clear()
        for command in commands:
            command.refresh_status()
            if command.process.poll() is None:
                terminate_process_group(command.process, signal.SIGTERM)
            command.drain_readers()
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        if self.fallback_runtime_dir is not None:
            shutil.rmtree(self.fallback_runtime_dir, ignore_errors=True)


class Runtime:
    def __init__(
        self,
        workspace: Path,
        *,
        enable_view_image: bool = True,
        permission_mode: str = "safe",
        shell_env_policy: ShellEnvPolicy | None = None,
        allow_network: bool = False,
        auth_token: str | None = None,
        oauth_config: OAuthConfig | None = None,
        project_context: ProjectContext | None = None,
        fake_readonly_annotations: bool = False,
        workspace_mutation: WorkspaceMutationPolicy | None = None,
        transport: str = "stdio",
        command_manager: WorkspaceCommandManager | None = None,
        isolation: IsolationConfig | None = None,
    ) -> None:
        self.workspace = Workspace(workspace)
        self.isolation = isolation or IsolationConfig()
        self._service_import_snapshot = ServiceImportSnapshot.capture() if self.isolation.mode == "strict" else None
        if self._service_import_snapshot is not None:
            # Structured tools can write the whole workspace even when commands
            # are structured-only or a command policy denies a nested subtree.
            self._service_import_snapshot.reject_overlapping_writes((self.workspace.root,))
        self.workspace_mutation = workspace_mutation or WorkspaceMutationPolicy()
        if self.workspace_mutation.mode not in WORKSPACE_MUTATION_CHOICES:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Unknown workspace mutation mode: {self.workspace_mutation.mode}",
                category="validation",
                details={"supported": list(WORKSPACE_MUTATION_CHOICES)},
            )
        self._workspace_write_path_roots = tuple(self._resolve_workspace_write_paths())
        self.enable_view_image = enable_view_image
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
        # Computed after the permission mode resolves: `gated_by` names a
        # runtime property, and one of the gates is the permission mode.
        self._exposed_tool_names = [
            name
            for name, spec in TOOL_REGISTRY.items()
            if spec.gated_by is None or getattr(self, spec.gated_by)
        ]
        self._exposed_tool_name_set = frozenset(self._exposed_tool_names)
        # Faking annotations is only defensible where the caller has already
        # asserted the workspace is disposable, so bind it to that assertion
        # instead of letting it be set orthogonally.
        if fake_readonly_annotations and permission_mode != "dangerous":
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "fake_readonly_annotations requires permission_mode=dangerous.",
                category="validation",
                details={"permission_mode": permission_mode},
            )
        self.fake_readonly_annotations = fake_readonly_annotations
        self.shell_env_policy = shell_env_policy or ShellEnvPolicy()
        if self.shell_env_policy.inherit not in SHELL_ENV_INHERIT_CHOICES:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Unknown shell env inherit policy: {self.shell_env_policy.inherit}",
                category="validation",
                details={"supported": list(SHELL_ENV_INHERIT_CHOICES)},
            )
        self.allow_network = allow_network or self.capabilities.network
        self.auth_token = auth_token or None
        self.oauth_config = oauth_config
        self.command_manager = command_manager or WorkspaceCommandManager(self.workspace.root)
        if self.command_manager.workspace != self.workspace.root:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "command_manager belongs to a different workspace.",
                category="validation",
            )
        if self._service_import_snapshot is not None:
            runtime_roots = (self.command_manager.runtime_dir,)
            if self.command_manager.fallback_runtime_dir is not None:
                runtime_roots += (self.command_manager.fallback_runtime_dir,)
            self._service_import_snapshot.reject_overlapping_writes(runtime_roots)
        self._owns_command_manager = command_manager is None
        self.server_instance_id = self.command_manager.server_instance_id
        self._set_runtime_dir(self.command_manager.runtime_dir)
        self.fallback_runtime_dir = self.command_manager.fallback_runtime_dir
        self._runtime_dir_lock = threading.Lock()
        self._runtime_dir_resolved = False
        self._closed = False
        self.patch_baselines: dict[str, str | None] = {}
        self.patch_lock = threading.Lock()
        service_roots = []
        log_dir = os.environ.get(f"{ENV_PREFIX}_EVENT_LOG_DIR")
        if log_dir:
            service_roots.append(Path(log_dir))
        self._service_roots = tuple(service_roots)
        self.file_broker = (
            FileBroker(self.workspace.root, denied_roots=self._execution_policy("read-helper").deny_roots,
                       writable_roots=(self.workspace.root,))
            if self.isolation.mode == "strict" else None
        )
        self.executor = WorkspaceExecutor(self._execution_policy, lambda: self._command_env({}, ensure_runtime=self.isolation.mode == "strict"), file_broker=self.file_broker)
        self.workspace.executor = self.executor
        self.workspace.file_broker = self.file_broker
        self.patch_committer = AtomicPatchCommitter(broker=self.file_broker)
        self._idempotency_results: OrderedDict[tuple[str, str], tuple[str, dict[str, Any]]] = OrderedDict()
        self._idempotency_lock = threading.Lock()
        self._idempotency_condition = threading.Condition(self._idempotency_lock)
        self._idempotency_inflight: set[tuple[str, str]] = set()
        # Every keyed caller pins its cache slot before it either executes or
        # waits. A completed owner may wake after enough unrelated completions
        # to overflow the LRU; retaining the slot until all registered waiters
        # drain guarantees they replay the result instead of executing twice.
        self._idempotency_pins: dict[tuple[str, str], int] = {}
        # Command ids whose terminal outcome telemetry has already counted. A
        # finished command answers every later poll with the same outcome, so
        # without this one failing build is counted once per poll. The outcome
        # is always assigned to exec_command, which owns the command.
        self._counted_command_outcomes: OrderedDict[str, None] = OrderedDict()
        self._counted_outcomes_lock = threading.Lock()
        # Terminal command results can also invalidate breaker verdicts, but
        # re-reading retained output must not invalidate them again.
        self._breaker_reset_commands: OrderedDict[str, None] = OrderedDict()
        self._breaker_reset_commands_lock = threading.Lock()
        self.breaker = RepeatFailureBreaker()
        # ProjectContext is frozen and derived only from the workspace tree, so
        # an embedder that builds several runtimes over one workspace can reuse
        # the discovery (git ls-files / directory walk) result.
        try:
            self.project_context: ProjectContext = (
                project_context if project_context is not None else load_project_context(
                    self.workspace.root, executor=self.executor, broker=self.file_broker
                )
            )
        except BaseException:
            if self.file_broker is not None:
                self.file_broker.close()
            if self._owns_command_manager:
                self.command_manager.close()
            raise
        self.telemetry = SessionTelemetry(permission_mode=self.permission_mode, transport=transport)
        self._tool_handlers = {name: getattr(self, name) for name in TOOL_REGISTRY}
        self._event_journal = ToolEventJournal.from_env()
        self._journal_instance_id = secrets.token_hex(16) if self._event_journal else None

    def _execution_policy(self, purpose: str = "command") -> ExecutionPolicy:
        return compile_policy(
            self.isolation, self.workspace.root, self.runtime_dir, purpose=purpose,
            structured_only=self.workspace_mutation.structured_only,
            write_paths=self._workspace_write_path_roots,
            service_roots=self._service_roots,
        )

    def _capture_baseline(self, path: Path) -> FileBaseline:
        return FileBaseline.capture(path, broker=self.file_broker) if self.file_broker else FileBaseline.capture(path)

    def _file_stat(self, path: Path) -> os.stat_result:
        return self.file_broker.stat(path) if self.file_broker else path.stat()

    def _file_is_dir(self, path: Path) -> bool:
        return self.file_broker.is_dir(path) if self.file_broker else path.is_dir()

    def _file_is_file(self, path: Path) -> bool:
        return self.file_broker.is_file(path) if self.file_broker else path.is_file()

    def _file_exists(self, path: Path) -> bool:
        return self.file_broker.exists(path) if self.file_broker else path.exists()

    def _file_bytes(self, path: Path) -> bytes:
        return self.file_broker.read_bytes(path) if self.file_broker else path.read_bytes()

    def _file_text(self, path: Path) -> str:
        return self.file_broker.read_text(path) if self.file_broker else read_text_preserve_newlines(path)

    def _walk_files(self, path: Path) -> Iterator[Path]:
        if self.file_broker:
            yield from (self.workspace.root / item for item in self.file_broker.walk_files(path, excluded_directories=DEFAULT_EXCLUDED_NAMES))
        else:
            yield from walk_files(path)

    def _file_entry(self, path: Path) -> dict[str, Any]:
        if not self.file_broker:
            return entry_for_path(path, self.workspace.root)
        info = self.file_broker.stat(path)
        return {
            "name": path.name, "path": normalize_rel_display(path, self.workspace.root),
            "type": "directory" if stat.S_ISDIR(info.st_mode) else "file",
            "size_bytes": info.st_size,
            "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
            "is_hidden": path.name.startswith("."), "is_ignored": False,
        }

    def isolation_payload(self) -> dict[str, Any]:
        return self.executor.capability_report()

    def _set_runtime_dir(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.home_dir = self.runtime_dir / "home"
        self.tmp_dir = self.runtime_dir / "tmp"
        self.cache_dir = self.runtime_dir / "cache"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._owns_command_manager:
                self.command_manager.close()
            self.telemetry.finish(output_retention=self.command_manager.retention_stats_snapshot())
        finally:
            if self._event_journal is not None:
                self._event_journal.close()
            if self.file_broker is not None:
                self.file_broker.close()

    @property
    def commands(self) -> dict[str, CommandRun]:
        return self.command_manager.commands

    @property
    def output_commands(self) -> dict[str, CommandRun]:
        return self.command_manager.output_commands

    @property
    def commands_lock(self) -> threading.Lock:
        return self.command_manager.lock

    @property
    def starting_commands(self) -> int:
        return self.command_manager.starting_commands

    @starting_commands.setter
    def starting_commands(self, value: int) -> None:
        self.command_manager.starting_commands = value

    def _create_runtime_dirs(self, runtime_dir: Path) -> str | None:
        """Create one runtime tree, reporting failure instead of raising."""

        try:
            for path in (
                runtime_dir.parent,
                runtime_dir,
                runtime_dir / "home",
                runtime_dir / "tmp",
                runtime_dir / "cache",
            ):
                if self.isolation.mode == "strict":
                    # Commands can modify their runtime tree. Never follow a
                    # replacement HOME/TEMP/cache symlink with host authority.
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                    fd = os.open(path.anchor, flags)
                    try:
                        for part in path.parts[1:]:
                            try:
                                os.mkdir(part, 0o700, dir_fd=fd)
                            except FileExistsError:
                                pass
                            child = os.open(part, flags, dir_fd=fd)
                            os.close(fd)
                            fd = child
                        os.fchmod(fd, 0o700)
                    finally:
                        os.close(fd)
                else:
                    path.mkdir(parents=True, mode=0o700, exist_ok=True)
                    if os.name != "nt":
                        try:
                            path.chmod(0o700)
                        except OSError:
                            pass
        except OSError as exc:
            return f"{runtime_dir}: {exc}"
        return None

    def _ensure_runtime_dirs(self) -> None:
        """Create the runtime directories, choosing which tree to use only once.

        The first call picks the primary directory or, if that one cannot be
        created, the fallback. Every later call re-creates that same tree and
        fails instead of switching: concurrent clients share one runtime, and
        a command reading HOME or TMPDIR must never see them move to another
        directory mid-flight.
        """

        with self._runtime_dir_lock:
            resolved = self._runtime_dir_resolved
            candidates = [self.runtime_dir]
            if not resolved and self.fallback_runtime_dir is not None and self.fallback_runtime_dir not in candidates:
                candidates.append(self.fallback_runtime_dir)
            errors: list[str] = []
            for runtime_dir in candidates:
                error = self._create_runtime_dirs(runtime_dir)
                if error is None:
                    if not resolved:
                        self._set_runtime_dir(runtime_dir)
                        self._runtime_dir_resolved = True
                    return
                errors.append(error)
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
        return self.capabilities.landlock and self.isolation.mode == "compatibility"

    def landlock_write_roots(self) -> list[Path]:
        roots = [self.runtime_dir]
        if self.workspace_mutation.structured_only:
            roots.extend(self._ensure_workspace_write_paths())
        return roots

    def _resolve_workspace_write_paths(self) -> list[Path]:
        """Resolve and validate the configured workspace write allowlist.

        Unresolvable paths and paths outside the workspace are rejected in
        strict mode. Compatibility mode drops them rather than widening the
        write grant. Resolution is deliberately side-effect free: reporting
        tools must not create directories merely by describing the policy.
        """

        resolved: list[Path] = []
        for entry in self.workspace_mutation.write_paths:
            candidate = Path(entry)
            absolute = candidate if candidate.is_absolute() else self.workspace.root / candidate
            try:
                real = absolute.resolve(strict=False)
            except (OSError, RuntimeError, ValueError) as exc:
                if self.isolation.mode == "strict":
                    raise ToolFailure("INVALID_ARGUMENT", "A configured workspace write path cannot be resolved.",
                                      category="validation", details={"path": entry}) from exc
                continue
            if not is_relative_to(real, self.workspace.root) and self.isolation.mode == "strict":
                raise ToolFailure("INVALID_ARGUMENT", "Strict write paths must stay inside the workspace.", category="security", details={"path": entry})
            if is_relative_to(real, self.workspace.root):
                existing = real
                while not existing.exists() and existing != self.workspace.root:
                    existing = existing.parent
                if existing.exists() and not existing.is_dir():
                    raise ToolFailure(
                        "INVALID_ARGUMENT",
                        f"Configured write path is not a directory: {entry}",
                        category="validation",
                        details={"path": entry},
                    )
                resolved.append(real)
        return resolved

    def workspace_write_paths(self) -> list[Path]:
        """Return the validated allowlist without mutating the workspace."""

        return list(self._workspace_write_path_roots)

    def _ensure_workspace_write_paths(self) -> list[Path]:
        """Create allowlisted directories immediately before Landlock uses them."""

        for path in self._workspace_write_path_roots:
            try:
                if self.file_broker:
                    self.file_broker.ensure_directory(path)
                else:
                    path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                display = normalize_rel_display(path, self.workspace.root)
                if exc.errno in {errno.EEXIST, errno.ENOTDIR}:
                    raise ToolFailure(
                        "INVALID_ARGUMENT",
                        f"Configured write path is not a directory: {display}",
                        category="validation",
                        details={"path": display},
                    ) from exc
                raise ToolFailure(
                    "SANDBOX_UNAVAILABLE",
                    f"Configured write path could not be created: {display}",
                    category="security",
                    details={
                        "path": display,
                        "errno": exc.errno,
                        "reason": exc.strerror or str(exc),
                    },
                ) from exc
            if not self._file_is_dir(path):
                display = normalize_rel_display(path, self.workspace.root)
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    f"Configured write path is not a directory: {display}",
                    category="validation",
                    details={"path": display},
                )
        return self.workspace_write_paths()

    def workspace_mutation_payload(self) -> dict[str, Any]:
        """Disclose who may write to the workspace, and whether it is enforced.

        `structured-only` is enforced by Landlock ABI 3 or newer. Older ABIs
        cannot mediate truncate, so calling them enforced would promise more
        than the kernel can provide.
        """

        if self.isolation.mode == "strict":
            return {
                "mode": self.workspace_mutation.mode,
                "write_paths": [normalize_rel_display(path, self.workspace.root) for path in self.workspace_write_paths()],
                "enforced": self.executor.last_launch_confirmed,
                "enforced_by": "native-sandbox-required",
                "structured_write_tools": sorted(WORKSPACE_WRITE_TOOLS),
                "warnings": [],
            }
        landlock = landlock_status_payload()
        abi = landlock.get("abi_version")
        truncate_protected = isinstance(abi, int) and abi >= 3
        enforced = (
            self.workspace_mutation.structured_only
            and self.landlock_enabled()
            and bool(landlock.get("available"))
            and truncate_protected
        )
        warnings: list[str] = []
        if self.workspace_mutation.structured_only and not enforced:
            if bool(landlock.get("available")) and self.landlock_enabled() and not truncate_protected:
                warnings.append(
                    "workspace_mutation=structured-only requires Landlock ABI 3 or newer; "
                    f"ABI {abi} cannot deny file truncation, so the policy is not fully enforced"
                )
            else:
                warnings.append(
                    "workspace_mutation=structured-only is not enforced without enabled Landlock; "
                    "exec_command can still write to the workspace"
                )
        return {
            "mode": self.workspace_mutation.mode,
            "write_paths": [
                normalize_rel_display(path, self.workspace.root) for path in self.workspace_write_paths()
            ],
            "enforced": enforced if self.workspace_mutation.structured_only else True,
            "enforced_by": "landlock" if enforced else "none",
            "structured_write_tools": sorted(WORKSPACE_WRITE_TOOLS),
            "warnings": warnings,
        }

    def is_allowed_command_tmp_path(self, candidate: str) -> bool:
        if self.capabilities.skip_all_permissions:
            return False
        try:
            resolved = Path(candidate).expanduser().resolve(strict=False)
        except OSError:
            return False
        return is_relative_to(resolved, self.runtime_dir)

    def initialize(
        self,
        client_info: dict[str, Any] | None = None,
        protocol_version: str = LATEST_LEGACY_PROTOCOL_VERSION,
    ) -> dict[str, Any]:
        self.telemetry.record_session_start(client_info, protocol_version)
        return self.initialize_result(protocol_version)

    def initialize_result(self, protocol_version: str = LATEST_LEGACY_PROTOCOL_VERSION) -> dict[str, Any]:
        """Build the handshake payload for one negotiated version.

        The version is negotiated per request rather than stored: one runtime
        serves every client of the workspace, and two of them may well have
        handshaken on different versions.
        """

        return {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self.server_identity(),
            "instructions": self.project_context.server_instructions(),
        }

    def discover_payload(self) -> dict[str, Any]:
        """Tell a client that never handshakes what this server can do.

        The modern counterpart of the handshake result, minus everything the
        handshake only needed because it was a handshake: no version is
        negotiated here, so the versions this server speaks per request are
        listed instead, and only those — a legacy version named here would
        invite a client to put one in its ``_meta``. The encoder adds the
        result envelope, so the fields returned are the answer itself.
        """

        return {
            "supportedVersions": list(MODERN_PROTOCOL_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "instructions": self.project_context.server_instructions(),
        }

    def server_identity(self) -> dict[str, Any]:
        """Name this server for the handshake and for modern result metadata.

        The protocol layer cannot import this module, so it reads the identity
        through the runtime it is already dispatching against.
        """

        return {
            "name": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": __version__,
        }

    def list_tools(self) -> dict[str, Any]:
        return {
            "tools": [
                tool_definition(name, fake_readonly=self.fake_readonly_annotations)
                for name in self.exposed_tool_names()
            ]
        }

    def exposed_tool_names(self) -> list[str]:
        return list(self._exposed_tool_names)

    def auth_enabled(self) -> bool:
        return self.auth_token is not None or self.oauth_config is not None

    def oauth_enabled(self) -> bool:
        return self.oauth_config is not None

    def resolve_existing(self, raw_path: str = ".") -> ResolvedPath:
        return self.workspace.resolve_existing(raw_path)

    def resolve_for_write(self, raw_path: str) -> ResolvedPath:
        return self.workspace.resolve_for_write(raw_path)

    def git_path_filter(self, raw_path: str) -> str:
        if raw_path == ".":
            return "."
        return self.resolve_for_write(raw_path).display

    def _exec_environment_summary(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace.root),
            "permission_mode": self.permission_mode,
            "network_allowed": self.allow_network and (self.isolation.mode != "strict" or self.isolation.network == "proxy"),
            "runtime_dir": str(self.runtime_dir),
            "home": "/tmp/home" if self.isolation.mode == "strict" else str(self.command_home_dir()),
            "tmpdir": "/tmp" if self.isolation.mode == "strict" else str(self.command_tmp_dir()),
            "cache_dir": str(self.cache_dir),
        }

    def _landlock_enforced(self, landlock: dict[str, Any]) -> bool:
        return bool(landlock.get("available")) and self.landlock_enabled()

    def server_info_payload(self) -> dict[str, Any]:
        tools = self.exposed_tool_names()
        landlock = landlock_status_payload()
        landlock["enabled"] = self._landlock_enforced(landlock)
        return {
            "server": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": __version__,
            "supported_protocol_versions": list(KNOWN_PROTOCOL_VERSIONS),
            **self._exec_environment_summary(),
            "auth_enabled": self.auth_enabled(),
            "dangerously_skip_all_permissions": self.dangerously_skip_all_permissions,
            "annotation_override": "fake_readonly" if self.fake_readonly_annotations else None,
            "landlock": landlock,
            "execution_isolation": self.isolation_payload(),
            "exec_policy": {
                "shell_expansion": self.shell_expansion_policy(),
                "inline_script": self.inline_script_policy(),
                "global_tmp_write": self.global_tmp_write_policy(),
                "secret_env_filter": self.secret_env_filter_policy(),
            },
            "workspace_mutation_policy": self.workspace_mutation_payload(),
            "shell_env_inherit": self.shell_env_policy.inherit,
            "shell_env_include_only": list(self.shell_env_policy.include_only),
            "shell_env_exclude": list(self.shell_env_policy.exclude),
            # The static budget only: how often it was actually hit is a
            # runtime-wide counter and is reported in telemetry, not to
            # whichever client happened to ask.
            "output_retention": {
                "buffer_bytes_per_stream": COMMAND_BUFFER_BYTES,
                "head_bytes_per_stream": COMMAND_BUFFER_BYTES // COMMAND_HEAD_BUFFER_DIVISOR,
                "completed_command_ttl_seconds": COMPLETED_COMMAND_TTL_SECONDS,
                "max_retained_completed_commands": MAX_RETAINED_OUTPUT_COMMANDS,
            },
            "endpoint_path": MCP_ENDPOINT_PATH,
            "project_context": {
                "root_instruction_files": [item.path for item in self.project_context.root_files],
                "nested_instruction_files": list(self.project_context.nested_files),
                "warnings": list(self.project_context.warnings),
            },
            "tools": tools,
            "tool_count": len(tools),
        }

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        journal = self._event_journal
        if journal is None:
            return self._call_tool(name, arguments, context=context)
        started = time.monotonic()
        identity = {
            "schema_version": 1,
            "runtime_id": self._journal_instance_id,
            "call_id": secrets.token_hex(16),
            "tool": name if isinstance(name, str) and name in TOOL_REGISTRY else "unknown",
        }

        def record(event: str, **fields: Any) -> None:
            journal.record({
                **identity,
                "event": event,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                **fields,
            })

        record("tool_call_started")
        try:
            result = self._call_tool(name, arguments, context=context)
        except Exception as exc:
            fields: dict[str, Any] = {"outcome": "internal_error"}
            if isinstance(exc, JsonRpcError):
                fields["outcome"] = "rpc_error"
                # Protocol codes are numbers, never arbitrary exception text.
                if exc.code in (-32700, -32600, -32601, -32602, -32603):
                    fields["rpc_error_code"] = exc.code
            record("tool_call_finished", duration_ms=int((time.monotonic() - started) * 1000), **fields)
            raise
        # BaseException deliberately leaves an unmatched start. A record of a
        # returned call does not mean the client received it, or a process exited.
        payload = result.get("structuredContent", {})
        fields = {"outcome": "tool_error" if result.get("isError") else "success"}
        for flag in ("truncated", "idempotent_replay"):
            if isinstance(payload.get(flag), bool):
                fields[flag] = payload[flag]
        operation_outcome = payload.get("operation_outcome")
        if operation_outcome in COMMAND_OUTCOMES:
            fields["operation_outcome"] = operation_outcome
        error = payload.get("error")
        if isinstance(error, dict):
            category = error.get("category")
            if category in ("validation", "permission", "not_found", "conflict", "runtime", "internal"):
                fields["error_category"] = category
        command_id = payload.get("command_id")
        if isinstance(command_id, str):
            # Only IDs actually owned by this runtime, not client strings or
            # arbitrary handler output, can enter the journal.
            with self.commands_lock:
                if command_id in self.commands or command_id in self.output_commands:
                    fields["command_id"] = command_id
        record("tool_call_finished", duration_ms=int((time.monotonic() - started) * 1000), **fields)
        return result

    def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Validate and dispatch a tool call with idempotency, breaker, and telemetry handling."""
        started_at = time.time()
        args = arguments or {}
        handler = self._tool_handlers.get(name) if self._is_callable_tool(name) else None
        if handler is None:
            self.record_rejected_tool_call(name, started_at=started_at, context=context)
            raise JsonRpcError(-32602, f"Unknown tool: {name}", {"reason": "unknown_tool"})
        spec = TOOL_REGISTRY[name]
        try:
            validate_arguments(name, args)
        except JsonRpcError:
            self.record_rejected_tool_call(name, started_at=started_at, context=context)
            raise
        fingerprint = argument_fingerprint(args)
        idempotency_slot = self._idempotency_slot(name, args)
        idempotency_fingerprint = (
            argument_fingerprint(_drop_schema_defaults(name, args)) if idempotency_slot else fingerprint
        )
        self._begin_idempotent_call(idempotency_slot)
        breaker_generation = self.breaker.generation
        try:
            # Inside the try: a key reused for different work is a tool error
            # the model can act on, not an exception that escapes the envelope.
            replayed = self._recorded_result(idempotency_slot, idempotency_fingerprint)
            if replayed is not None:
                self.emit_tool_trace(name, args, replayed, started_at, context=context)
                content = spec.content_builder(dict(replayed)) if spec.content_builder else None
                return make_tool_result(name, replayed, is_error=replayed.get("ok") is False, content=content)
            payload = handler(args)
            workspace_mutated = payload.pop("_workspace_mutated", None)
            payload.setdefault("ok", True)
            if payload.get("ok") is False:
                self._record_breaker_failure(name, fingerprint, payload, breaker_generation)
                self.emit_tool_trace(name, args, payload, started_at, context=context)
                content = spec.content_builder(payload) if spec.content_builder else None
                return make_tool_result(name, payload, is_error=True, content=content)
            self.breaker.record_success(name, fingerprint, generation=breaker_generation)
            if self._workspace_write_landed(name, payload, workspace_mutated=workspace_mutated):
                # A write landed, so every "this call can never succeed"
                # verdict the breaker holds was reached against a tree that no
                # longer exists.
                self.breaker.reset()
            else:
                self._reset_breaker_after_terminal_command(payload)
                if name in BREAKER_RESET_COMMAND_TOOLS:
                    self._reset_breaker_on_command_lifecycle(payload)
            self._record_result(idempotency_slot, idempotency_fingerprint, payload)
            self.emit_tool_trace(name, args, payload, started_at, context=context)
            content = spec.content_builder(payload) if spec.content_builder else None
            return make_tool_result(name, payload, is_error=payload.get("ok") is False, content=content)
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
            if spec.error_status:
                payload["status"] = spec.error_status
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
            self._record_breaker_failure(name, fingerprint, payload, breaker_generation)
            self.emit_tool_trace(name, args, payload, started_at, context=context)
            return make_tool_result(name, payload, is_error=True)
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
            if spec.error_status:
                payload["status"] = spec.error_status
            self._record_breaker_failure(name, fingerprint, payload, breaker_generation)
            self.emit_tool_trace(name, args, payload, started_at, context=context)
            return make_tool_result(name, payload, is_error=True)
        finally:
            self._finish_idempotent_call(idempotency_slot)

    def record_rejected_tool_call(
        self,
        name: Any,
        *,
        started_at: float | None = None,
        context: RequestContext | None = None,
    ) -> None:
        """Count a tools/call refused with -32602 before any handler ran.

        A known tool is recorded as one failed call with ``INVALID_PARAMS``.
        An unknown name only bumps a session counter: the name is whatever
        the client sent, and must not become a per-tool statistic.
        """

        if isinstance(name, str) and self._is_callable_tool(name) and name in self._tool_handlers:
            duration_ms = int((time.time() - started_at) * 1000) if started_at is not None else 0
            self.telemetry.record_tool_call(
                name,
                ok=False,
                error_code="INVALID_PARAMS",
                duration_ms=max(duration_ms, 0),
                truncated=False,
                context=context,
            )
        else:
            self.telemetry.record_unknown_tool_call()

    def _record_breaker_failure(
        self,
        name: str,
        fingerprint: str,
        payload: dict[str, Any],
        generation: int,
    ) -> None:
        """Count a structured failure whether returned or raised by a handler."""

        raw_error = payload.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        code = str(error.get("code") or "INTERNAL_ERROR")
        if code == REPEATED_CALL_BLOCKED:
            return
        repeats = self.breaker.record_failure(
            name,
            fingerprint,
            error_code=code,
            retryable=bool(error.get("retryable")),
            generation=generation,
        )
        if repeats >= self.breaker.limit:
            raw_details = error.get("details")
            details = raw_details if isinstance(raw_details, dict) else {}
            error["details"] = {
                **details,
                "recent_identical_failures": repeats,
                "repeat_warning": (
                    f"The server recently observed {repeats} failures of this request with {code}. "
                    "Check the current state or adjust the request before retrying. "
                    "Repeated failures do not block execution."
                ),
            }

    def _is_callable_tool(self, name: str) -> bool:
        if name in self._exposed_tool_name_set:
            return True
        spec = TOOL_REGISTRY.get(name)
        # A mode-gated tool disappears from tools/list but keeps answering a
        # client that already knows about it, so removing it from the catalog
        # never turns a working call into "unknown tool".
        return spec is not None and spec.callable_when_hidden

    def _idempotency_slot(self, name: str, args: dict[str, Any]) -> tuple[str, str] | None:
        if name not in IDEMPOTENT_TOOLS:
            return None
        raw = args.get("idempotency_key")
        return (name, raw) if isinstance(raw, str) and raw else None

    def _begin_idempotent_call(self, slot: tuple[str, str] | None) -> None:
        """Take the execution turn for one keyed request.

        Looking in the completed-result cache is not enough: two callers can
        both miss before either has recorded its result. Keep the slot occupied
        through execution and recording so every waiter rechecks the cache
        after the first call has made its mutation durable.
        """

        if slot is None:
            return
        with self._idempotency_condition:
            self._idempotency_pins[slot] = self._idempotency_pins.get(slot, 0) + 1
            try:
                while slot in self._idempotency_inflight:
                    self._idempotency_condition.wait()
                self._idempotency_inflight.add(slot)
            except BaseException:
                self._unpin_idempotency_slot_locked(slot)
                raise

    def _finish_idempotent_call(self, slot: tuple[str, str] | None) -> None:
        if slot is None:
            return
        with self._idempotency_condition:
            self._idempotency_inflight.discard(slot)
            self._unpin_idempotency_slot_locked(slot)
            self._prune_idempotency_results_locked()
            self._idempotency_condition.notify_all()

    def _unpin_idempotency_slot_locked(self, slot: tuple[str, str]) -> None:
        remaining = self._idempotency_pins.get(slot, 0) - 1
        if remaining > 0:
            self._idempotency_pins[slot] = remaining
        else:
            self._idempotency_pins.pop(slot, None)

    def _prune_idempotency_results_locked(self) -> None:
        while len(self._idempotency_results) > IDEMPOTENCY_CACHE_ENTRIES:
            victim = next(
                (candidate for candidate in self._idempotency_results if candidate not in self._idempotency_pins),
                None,
            )
            if victim is None:
                # A short-lived overflow is safer than evicting a result a
                # waiter has already been promised. The final waiter prunes it.
                return
            del self._idempotency_results[victim]

    def _recorded_result(self, slot: tuple[str, str] | None, fingerprint: str) -> dict[str, Any] | None:
        """Return the result a previous call under this key produced, if any.

        A key stands for one piece of work, so the recorded result is only
        replayed for the arguments that produced it. Reusing a key for a
        different request is answered with an error rather than with the first
        request's success, which would report work that was never done.
        """

        if slot is None:
            return None
        with self._idempotency_lock:
            recorded = self._idempotency_results.get(slot)
            if recorded is None:
                return None
            self._idempotency_results.move_to_end(slot)
        recorded_fingerprint, payload = recorded
        if recorded_fingerprint != fingerprint:
            raise ToolFailure(
                IDEMPOTENCY_KEY_REUSED,
                f"idempotency_key {slot[1]!r} was already used by a different {slot[0]} request. "
                "A key names one request: replay it with the same arguments, or send a new key.",
                category="validation",
                retryable=False,
                details={
                    "tool": slot[0],
                    "idempotency_key": slot[1],
                    "retry_hint": "Resend the original arguments to replay, or choose a new idempotency_key.",
                },
            )
        replay = dict(payload)
        replay["idempotent_replay"] = True
        return replay

    def _record_result(self, slot: tuple[str, str] | None, fingerprint: str, payload: dict[str, Any]) -> None:
        """Record one success under its key, with the arguments that earned it.

        A failure is never recorded: it carries its own retryability, and
        replaying one would turn a transient conflict into a permanent answer
        for as long as the key is reused. A dry run is never recorded either —
        it changed nothing, so answering a later real apply with it would
        report a write that never happened.
        """

        if slot is None or payload.get("ok") is False or payload.get("dry_run"):
            return
        with self._idempotency_lock:
            self._idempotency_results[slot] = (fingerprint, dict(payload))
            self._idempotency_results.move_to_end(slot)
            self._prune_idempotency_results_locked()

    @staticmethod
    def _workspace_write_landed(
        name: str,
        payload: dict[str, Any],
        *,
        workspace_mutated: Any = None,
    ) -> bool:
        """Whether a structured write tool changed the workspace's net state."""

        if name not in WORKSPACE_WRITE_TOOLS or any(
            payload.get(flag) for flag in ("dry_run", "already_applied", "idempotent_replay")
        ):
            return False
        if isinstance(workspace_mutated, bool):
            return workspace_mutated
        affected = payload.get("affected_files")
        if not isinstance(affected, list):
            return False
        non_mutating_operations = {"unchanged", "verify"}
        operations = [
            entry.get("operation")
            for entry in affected
            if isinstance(entry, dict) and isinstance(entry.get("operation"), str)
        ]
        return any(operation not in non_mutating_operations for operation in operations)

    def server_info(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.server_info_payload()

    def check_exec_environment(self, args: dict[str, Any]) -> dict[str, Any]:
        landlock = landlock_status_payload()
        warnings: list[str] = []
        # Landlock is the only filesystem confinement this server has, and it is
        # Linux-only. Elsewhere a command runs with the whole user account's
        # reach, which callers must be told rather than left to infer.
        if sys.platform != "linux" and self.isolation.mode == "compatibility":
            warnings.append(
                f"platform {sys.platform} has no filesystem confinement for exec_command; "
                "commands run with full user privileges outside the workspace"
            )
        if not landlock.get("available") and self.isolation.mode == "compatibility":
            warnings.append("Linux Landlock filesystem confinement is unavailable")
        mutation = self.workspace_mutation_payload()
        warnings.extend(str(item) for item in mutation.get("warnings", []))
        if self.capabilities.skip_all_permissions:
            warnings.append("permission_mode=dangerous disables MCP safety gates")
        if self.fake_readonly_annotations:
            warnings.append(
                "tools/list annotations are faked as read-only; apply_patch and exec_command still mutate and execute"
            )
        return {
            "ok": True,
            **self._exec_environment_summary(),
            "landlock_enabled": self._landlock_enforced(landlock),
            "landlock_abi": landlock.get("abi_version"),
            "execution_isolation": self.isolation_payload(),
            "global_tmp_write": self.global_tmp_write_policy(),
            "workspace_mutation_policy": mutation,
            "warnings": warnings,
        }

    def emit_tool_trace(
        self,
        name: str,
        args: dict[str, Any],
        payload: dict[str, Any],
        started_at: float,
        *,
        context: RequestContext | None = None,
    ) -> None:
        """Record call telemetry and optionally emit a trace with redacted arguments."""
        raw_error = payload.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        duration_ms = int((time.time() - started_at) * 1000)
        raw_outcome = payload.get("operation_outcome")
        outcome = self._countable_outcome(name, payload)
        # `context` is passed on as the opaque per-request fact it is: the
        # runtime neither reads the client identity in it nor branches on it.
        self.telemetry.record_tool_call(
            name,
            ok=bool(payload.get("ok")),
            error_code=error.get("code"),
            duration_ms=duration_ms,
            truncated=bool(payload.get("truncated")),
            context=context,
            outcome=outcome,
            already_applied=bool(payload.get("ok")) and payload.get("already_applied") is True,
        )
        if os.environ.get(f"{ENV_PREFIX}_TRACE") != "1":
            return
        event = {
            "event": "tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "tool": name,
            "ok": bool(payload.get("ok", False)),
            "status": payload.get("status"),
            "operation_outcome": raw_outcome if isinstance(raw_outcome, str) else None,
            "error_code": error.get("code"),
            "duration_ms": duration_ms,
            "command_id": payload.get("command_id"),
            "truncated": payload.get("truncated"),
            "args": redact_for_trace(args),
        }
        print(json.dumps(event, sort_keys=True, separators=(",", ":")), file=sys.stderr, flush=True)

    def _command_may_write(self, command_id: str | None) -> bool:
        """Whether this command (or, unknown, any command) could write the tree."""

        command = None
        if command_id:
            with self.commands_lock:
                command = self.commands.get(command_id) or self.output_commands.get(command_id)
        if command is not None:
            # This launch-specific fact handles a Landlock ruleset install
            # that failed open even when the host still advertises support.
            return bool(command.workspace_may_write)
        # Synthetic/embedder-provided command payloads have no retained
        # CommandRun. Conservatively fall back to the advertised policy.
        mutation = self.workspace_mutation_payload()
        return (
            not self.workspace_mutation.structured_only
            or mutation.get("enforced") is not True
            or bool(self._workspace_write_path_roots)
        )

    def _reset_breaker_on_command_lifecycle(self, payload: dict[str, Any]) -> None:
        """Invalidate verdicts when a command that may write starts or is killed.

        A background command can create the file a failed call was looking
        for at any moment after it starts, and a kill can leave its partial
        writes behind; neither is a terminal observation the breaker would
        otherwise see in time.
        """

        command_id = payload.get("command_id")
        if self._command_may_write(command_id if isinstance(command_id, str) else None):
            self.breaker.reset()

    def _reset_breaker_after_terminal_command(self, payload: dict[str, Any]) -> None:
        """Invalidate stale verdicts once when a command could have written."""

        outcome = payload.get("operation_outcome")
        if outcome not in set(COMMAND_OUTCOMES) - {"running", "spawn_error"}:
            return
        command_id = payload.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            return
        if not self._command_may_write(command_id):
            return
        with self._breaker_reset_commands_lock:
            if command_id in self._breaker_reset_commands:
                return
            self._breaker_reset_commands[command_id] = None
            while len(self._breaker_reset_commands) > COUNTED_OUTCOME_LEDGER_ENTRIES:
                self._breaker_reset_commands.popitem(last=False)
            self.breaker.reset()

    def _countable_outcome(self, observer: str, payload: dict[str, Any]) -> str | None:
        """Return the operation outcome telemetry should count for this call.

        A terminal outcome belongs to the ``exec_command`` call that started
        the command, not to whichever polling tool first observed it. Every
        later observer reports that same outcome again, so the command id is
        claimed once. When a poll wins the claim, its own successful call is
        recorded without the command outcome and the outcome is attached to
        the earlier ``exec_command`` summary. ``running`` is not an outcome:
        it is never counted, so each command contributes exactly one terminal
        outcome however many calls it took to finish.
        """

        outcome = payload.get("operation_outcome")
        if not isinstance(outcome, str):
            return None
        if outcome == "running":
            return None
        command_id = payload.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            return outcome
        with self._counted_outcomes_lock:
            if command_id in self._counted_command_outcomes:
                return None
            self._counted_command_outcomes[command_id] = None
            while len(self._counted_command_outcomes) > COUNTED_OUTCOME_LEDGER_ENTRIES:
                self._counted_command_outcomes.popitem(last=False)
        if observer != "exec_command":
            self.telemetry.record_deferred_operation_outcome("exec_command", outcome)
            return None
        return outcome

    def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read a bounded UTF-8 line range with its file revision and continuation metadata."""
        requested_path = str(args.get("path", ""))
        resolved = self.resolve_existing(requested_path)
        if self._file_is_dir(resolved.path):
            raise ToolFailure("IS_DIRECTORY", "Path is a directory.", category="validation")
        max_bytes = int(args.get("max_bytes", 131072))
        start_line = int(args.get("start_line", 1))
        end_line = args.get("end_line")
        max_lines = args.get("max_lines")
        if end_line is not None and max_lines is not None:
            calculated_end_line = start_line + int(max_lines) - 1
            if int(end_line) != calculated_end_line:
                raise ToolFailure("INVALID_ARGUMENT", "end_line and max_lines select different ranges.", category="validation")
        if end_line is None and max_lines is not None:
            end_line = start_line + int(max_lines) - 1
        encoding = args.get("encoding", "utf-8")
        if encoding != "utf-8":
            raise ToolFailure("UNSUPPORTED_ENCODING", "Only utf-8 is supported.", category="validation")
        total_bytes = self._file_stat(resolved.path).st_size
        with (self.file_broker.open_binary(resolved.path) if self.file_broker else resolved.path.open("rb")) as raw_handle:
            if b"\x00" in raw_handle.read(4096):
                raise ToolFailure("BINARY_FILE", "Binary file read blocked for text tool.", category="validation")
        if start_line < 1:
            raise ToolFailure("INVALID_ARGUMENT", "start_line must be >= 1.", category="validation")
        requested_end = int(end_line) if end_line is not None else None
        selected_parts: list[str] = []
        selected_bytes = 0
        total_lines = 0
        selection_complete = False
        truncated_by = None
        first_line_exceeds_limit = False
        # The revision is folded into the pass that already walks every line,
        # so it names the bytes this call decoded rather than whatever a second
        # open would have found a moment later.
        digest = hashlib.sha256()
        try:
            with (self.file_broker.open_text(resolved.path) if self.file_broker else resolved.path.open("r", encoding="utf-8", errors="strict", newline="")) as handle:
                for total_lines, line in enumerate(handle, start=1):
                    line_bytes = line.encode("utf-8")
                    digest.update(line_bytes)
                    if total_lines < start_line:
                        continue
                    if requested_end is not None and total_lines > requested_end:
                        continue
                    if selection_complete:
                        continue
                    if len(selected_parts) >= DEFAULT_MAX_LINES:
                        truncated_by = "lines"
                        selection_complete = True
                    elif selected_bytes + len(line_bytes) > max_bytes:
                        truncated_by = "bytes"
                        selection_complete = True
                        if not selected_parts:
                            # Preserve the existing oversized-first-line preview.
                            # All other pages end at a physical line boundary.
                            preview = truncate_text_head(line, max_bytes=max_bytes)
                            selected_parts.append(preview.content)
                            first_line_exceeds_limit = True
                    else:
                        selected_parts.append(line)
                        selected_bytes += len(line_bytes)
        except UnicodeDecodeError as exc:
            raise ToolFailure("UNSUPPORTED_ENCODING", "File is not valid utf-8.", category="validation") from exc
        selected = "".join(selected_parts)
        truncated = selection_complete
        end = requested_end if requested_end is not None else total_lines
        if end < start_line:
            selected = ""
        actual_end = min(end, total_lines)
        if truncated and selected_parts:
            actual_end = min(total_lines, start_line + len(selected_parts) - 1)
        next_start_line = actual_end + 1 if truncated and actual_end < min(end, total_lines) else None
        warnings = []
        if truncated:
            warnings.append("content truncated")
        if first_line_exceeds_limit:
            warnings.append("first selected line exceeds max_bytes")
        result = {
            "path": resolved.display,
            "content": selected,
            "encoding": "utf-8",
            "revision": digest.hexdigest(),
            "revision_algorithm": REVISION_ALGORITHM,
            "max_bytes": max_bytes,
            "start_line": start_line,
            "end_line": actual_end,
            "total_lines": total_lines,
            "total_bytes": total_bytes,
            "bytes_read": len(selected.encode("utf-8")),
            "truncated": truncated,
            "truncated_by": truncated_by,
            "first_line_exceeds_limit": first_line_exceeds_limit,
            "output_lines": len(selected_parts),
            "output_bytes": len(selected.encode("utf-8")),
            "next_start_line": next_start_line,
            "warnings": warnings,
        }
        if args.get("line_numbers") is True:
            # Echoed so the text renderer can number lines; content itself
            # stays the file's bytes.
            result["line_numbers"] = True
        if next_start_line is not None:
            continuation: dict[str, Any] = {
                "path": requested_path,
                "start_line": next_start_line,
                "max_bytes": max_bytes,
            }
            if requested_end is not None:
                continuation["end_line"] = requested_end
            if result.get("line_numbers"):
                continuation["line_numbers"] = True
            result["next_action"] = {"tool": "read_file", "arguments": continuation}
        return result

    def list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_existing(str(args.get("path", ".")))
        if not self._file_is_dir(resolved.path):
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
                children = ([self.workspace.root / item.display for item in self.file_broker.list_dir(directory)]
                            if self.file_broker else list(directory.iterdir()))
            except OSError:
                return
            child_rel_paths = [normalize_rel_display(child, self.workspace.root) for child in children]
            ignored = set() if include_ignored else self.workspace.git_ignored_paths(child_rel_paths)
            for child in children:
                if self.workspace.is_ignored_path(
                    child,
                    include_hidden=include_hidden,
                    include_ignored=include_ignored,
                    git_ignored=ignored,
                ):
                    continue
                entries.append(self._file_entry(child))
                if len(entries) >= max_entries:
                    truncated = True
                    return
                if recursive and depth < max_depth and self._file_is_dir(child) and (self.file_broker is not None or not child.is_symlink()):
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
        if not self._file_is_dir(resolved.path):
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
        for batch in path_batches(self._walk_files(resolved.path), 256):
            # Filter by glob first so git check-ignore only sees candidates.
            candidates = [
                (path, rel)
                for path, rel in ((path, normalize_rel_display(path, self.workspace.root)) for path in batch)
                if matches_any_glob(rel, patterns) and not matches_any_glob(rel, exclude_patterns)
            ]
            ignored = set() if include_ignored else self.workspace.git_ignored_paths([rel for _, rel in candidates])
            for path, rel in candidates:
                if path.is_symlink() and not self.workspace.is_safe_existing_path(path):
                    continue
                if self.workspace.is_ignored_path(
                    path,
                    include_hidden=include_hidden,
                    include_ignored=include_ignored,
                    git_ignored=ignored,
                ):
                    continue
                files.append(file_entry(path, rel, self._file_stat(path) if self.file_broker else path.lstat()))
                if len(files) >= max_results:
                    truncated = True
                    break
            if truncated:
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
        fd = cached_which("fd", "fdfind")
        if not fd or not self._file_is_dir(resolved.path):
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
                completed = self.executor.run(
                    args,
                    cwd=str(resolved.path),
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                if completed.returncode in {1, 2} and "--no-require-git" in completed.stderr:
                    # Older fd (including Ubuntu 22.04's 8.3) lacks this
                    # optional flag; clap 2 uses exit 1 and clap 3+ uses 2.
                    # Retry through the same policy boundary;
                    # git check-ignore below still enforces repository ignores.
                    args_base.remove("--no-require-git")
                    args.remove("--no-require-git")
                    completed = self.executor.run(
                        args, cwd=str(resolved.path), text=True, encoding="utf-8", errors="strict",
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                    )
            except ToolFailure:
                raise
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
                rel = normalize_rel_display(path, self.workspace.root)
                if matches_any_glob(rel, exclude_patterns):
                    continue
                paths[rel] = path
                if len(paths) >= max_results:
                    break
            if len(paths) >= max_results:
                break
        ignored = set() if include_ignored else self.workspace.git_ignored_paths(list(paths))
        files: list[dict[str, Any]] = []
        for rel, path in paths.items():
            if self.workspace.is_ignored_path(
                path,
                include_hidden=include_hidden,
                include_ignored=include_ignored,
                git_ignored=ignored,
            ):
                continue
            try:
                info = self.file_broker.stat(path) if self.file_broker else path.lstat()
            except (OSError, ToolFailure):
                continue
            files.append(file_entry(path, rel, info))
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
        needle = query if case_sensitive else query.lower()

        roots = [resolved.path] if self._file_is_file(resolved.path) else self._walk_files(resolved.path)
        for batch in path_batches(roots, 256):
            # Filter by glob first so git check-ignore runs once per batch of
            # candidates instead of once per walked file.
            candidates = []
            for path in batch:
                if self._file_is_dir(path):
                    continue
                if path.is_symlink() and not self.workspace.is_safe_existing_path(path):
                    continue
                rel = normalize_rel_display(path, self.workspace.root)
                if include_globs and not matches_any_glob(rel, include_globs):
                    continue
                if matches_any_glob(rel, exclude_globs):
                    continue
                candidates.append((path, rel))
            ignored = self.workspace.git_ignored_paths([rel for _, rel in candidates])
            for path, rel in candidates:
                if self.workspace.is_ignored_path(path, git_ignored=ignored):
                    continue
                try:
                    data = self._file_bytes(path)
                except OSError:
                    continue
                if b"\x00" in data[:4096]:
                    continue
                try:
                    lines = data.decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    continue
                for index, line in enumerate(lines):
                    if compiled:
                        found = compiled.search(line)
                        if not found:
                            continue
                        column = found.start() + 1
                    else:
                        literal_index = find_literal(line, needle, case_sensitive)
                        if literal_index < 0:
                            continue
                        column = literal_index + 1
                    total += 1
                    if len(matches) >= max_results:
                        continue
                    before = lines[max(0, index - context_lines) : index]
                    after = lines[index + 1 : index + 1 + context_lines]
                    matches.append(search_match_item(rel, index + 1, column, line, before, after, max_preview_bytes))
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
        rg = cached_which("rg")
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
            process = self.executor.popen(
                args,
                cwd=str(self.workspace.root),
                text=True,
                encoding="utf-8",
                errors="strict",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return None
        timed_out = threading.Event()

        def stop_timed_out_search() -> None:
            timed_out.set()
            try:
                terminate_process_group(process, HARD_KILL_SIGNAL)
            except OSError:
                pass

        timeout = threading.Timer(10, stop_timed_out_search)
        timeout.daemon = True
        timeout.start()
        matches: list[dict[str, Any]] = []
        total = 0
        truncated = False
        file_cache: dict[str, list[str]] = {}
        assert process.stdout is not None
        try:
            for raw in process.stdout:
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
                    truncated = True
                    process.terminate()
                    break
                rel = normalize_rel_display((self.workspace.root / path_text).resolve(), self.workspace.root)
                submatches = data.get("submatches") if isinstance(data.get("submatches"), list) else []
                first_submatch = submatches[0] if submatches and isinstance(submatches[0], dict) else {}
                column = int(first_submatch.get("start", 0)) + 1
                sanitized = str(line_text).replace("\r\n", "\n").replace("\r", "").rstrip("\n")
                lines: list[str] = []
                if context_lines > 0:
                    lines = file_cache.get(rel, [])
                    if rel not in file_cache:
                        try:
                            lines = self._file_text(self.workspace.root / rel).splitlines()
                        except OSError:
                            lines = []
                        file_cache[rel] = lines
                index = line_number - 1
                before = lines[max(0, index - context_lines) : index] if lines else []
                after = lines[index + 1 : index + 1 + context_lines] if lines else []
                matches.append(search_match_item(rel, line_number, column, sanitized, before, after, max_preview_bytes))
        finally:
            timeout.cancel()
            try:
                process.stdout.close()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if timed_out.is_set():
            return None
        if not truncated and process.returncode not in {0, 1}:
            return None
        return {
            "query": query,
            "matches": matches,
            "total_matches": total,
            "total_matches_exact": not truncated,
            "truncated": truncated,
            "engine": "rg",
            "warnings": ["result limit reached; search stopped early"] if truncated else [],
        }

    def apply_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate and stage patch operations, committing atomically unless this is a dry run."""
        patch = str(args.get("patch", ""))
        dry_run = bool(args.get("dry_run", False))
        with self.patch_lock:
            operations = parse_patch(patch)
            # Match the Codex tool-entry contract: an operation's primary
            # path may appear only once in one envelope. Move destinations are
            # deliberately not primary paths, so distinct sources may still
            # move to the same destination and the later write wins.
            primary_paths: dict[str, int] = {}
            for operation_index, operation in enumerate(operations):
                display = self._resolve_patch_path(operation.path, require_existing=False)
                if display in primary_paths:
                    raise ToolFailure(
                        "PATCH_FAILED",
                        f"Patch names the primary path {display} more than once.",
                        category="validation",
                        details={
                            "path": display,
                            "operation_indexes": [primary_paths[display], operation_index],
                        },
                    )
                primary_paths[display] = operation_index
            staged: dict[str, StagedFile] = {}
            summaries: list[str] = []
            affected: dict[str, dict[str, Any]] = {}
            warnings: list[str] = []
            already_applied_operations = 0
            additions = 0
            removals = 0
            for op in operations:
                target = self.workspace.resolve_for_write(op.path)
                prior = staged.get(target.display)
                if op.kind in {"update", "delete"}:
                    # A previous move can create this path only in staging.
                    # Require an on-disk source only when it has no staged state.
                    if prior is None:
                        target = self.workspace.resolve_existing(op.path)
                    elif prior.content is None:
                        raise ToolFailure(
                            "PATCH_FAILED", f"Cannot {op.kind} a deleted file.", category="validation"
                        )
                if op.kind in {"add", "update", "delete"}:
                    self.workspace.reject_write_symlink(op.path)
                if op.move_to:
                    self._validate_patch_path(op.move_to, require_existing=False)
                    self.workspace.reject_write_symlink(op.move_to)
                if op.kind == "add":
                    baseline = prior.baseline if prior is not None else self._capture_baseline(target.path)
                    staged[target.display] = StagedFile(
                        target.display,
                        target.path,
                        op.add_content or "",
                        baseline,
                        prior.mode if prior is not None else baseline.mode,
                    )
                    added_text = op.add_content or ""
                    # Add is a full destination overwrite. Evidence must
                    # describe the original destination bytes -> final bytes,
                    # not retain ranges or match quality from an earlier move
                    # that happened to stage the same destination first.
                    affected[target.display] = {
                        "path": target.display,
                        "operation": "add",
                        **_patch_evidence(
                            added_text,
                            changed_ranges_between(
                                baseline.text(target.display), added_text
                            ),
                        ),
                    }
                    summaries.append(f"A {target.display}")
                    additions += len(added_text.splitlines())
                    if baseline.data is not None:
                        removals += len(baseline.data.splitlines())
                elif op.kind == "delete":
                    if self._file_is_dir(target.path):
                        raise ToolFailure("PATCH_FAILED", "Cannot delete a directory.", category="validation")
                    baseline = prior.baseline if prior is not None else self._capture_baseline(target.path)
                    staged[target.display] = StagedFile(
                        target.display, target.path, None, baseline, baseline.mode, action="delete"
                    )
                    _merge_patch_affected_file(
                        affected,
                        {"path": target.display, "operation": "delete", "total_lines": 0},
                    )
                    summaries.append(f"D {target.display}")
                    removals += len((baseline.data or b"").splitlines())
                elif op.kind == "update":
                    source = target
                    if self._file_is_dir(source.path):
                        raise ToolFailure("PATCH_FAILED", "Cannot update a directory.", category="validation")
                    baseline = prior.baseline if prior is not None else self._capture_baseline(source.path)
                    content = prior.content if prior is not None else baseline.text(source.display)
                    assert content is not None
                    outcome = apply_update_hunks_detailed(content, op.hunks, op.path)
                    updated = outcome.content
                    warnings.extend(f"{source.display}: {item}" for item in outcome.warnings)
                    operation_already_applied = (
                        outcome.applied_hunks == 0 and bool(outcome.already_applied_hunks)
                    )
                    # Skipped (already-applied) hunks wrote nothing, so they
                    # contribute no additions or removals.
                    skipped_hunks = set(outcome.already_applied_hunks)
                    for hunk_index, hunk in enumerate(op.hunks):
                        if hunk_index in skipped_hunks:
                            continue
                        for line in hunk.lines:
                            additions += line.startswith("+")
                            removals += line.startswith("-")
                    evidence = _patch_evidence(updated, outcome.changed_ranges, quality=outcome.match_quality)
                    source_mode = prior.mode if prior is not None else baseline.mode
                    if op.move_to:
                        dest = self.workspace.resolve_for_write(op.move_to)
                        # Already-matching hunks make the content update a
                        # no-op, but relocating the file is still a write.
                        if operation_already_applied and dest.display == source.display:
                            already_applied_operations += 1
                        dest_prior = staged.get(dest.display)
                        # Keep the first snapshot for the entire transaction;
                        # recapturing a shared destination could accept and then
                        # overwrite an intervening external edit.
                        dest_baseline = (
                            dest_prior.baseline if dest_prior is not None else
                            baseline if dest.display == source.display else self._capture_baseline(dest.path)
                        )
                        destination_evidence = _patch_evidence(
                            updated,
                            changed_ranges_between(
                                dest_baseline.text(dest.display), updated
                            ),
                            quality=outcome.match_quality,
                        )
                        staged[source.display] = StagedFile(
                            source.display,
                            source.path,
                            None,
                            baseline,
                            source_mode,
                        )
                        staged[dest.display] = StagedFile(
                            dest.display,
                            dest.path,
                            updated,
                            dest_baseline,
                            source_mode,
                        )
                        if dest.display != source.display:
                            # If this source was an earlier operation's
                            # destination, its old evidence no longer describes
                            # the final workspace state. Re-add it below as an
                            # explicit deletion after recording the destination.
                            affected.pop(source.display, None)
                        # A move is a full destination overwrite. Replace any
                        # evidence already recorded for the destination and
                        # describe the original destination baseline -> final
                        # staged bytes. This keeps revision/ranges/quality from
                        # referring to different writes when destinations are
                        # shared by several sources.
                        affected[dest.display] = {
                            "path": dest.display,
                            "old_path": source.display,
                            "operation": "move",
                            **destination_evidence,
                        }
                        if dest.display != source.display:
                            affected[source.display] = {
                                "path": source.display,
                                "operation": "delete",
                                "total_lines": 0,
                            }
                        summaries.append(f"R {source.display} -> {dest.display}")
                    else:
                        if operation_already_applied:
                            already_applied_operations += 1
                        # The final staged state, rather than only this block's
                        # input, decides whether a write is necessary. Content
                        # that returns to the baseline can still require a
                        # write when a prior move carried a different mode.
                        block_unchanged = updated == content
                        baseline_content = baseline.text(source.display)
                        net_unchanged = updated == baseline_content and source_mode == baseline.mode
                        staged[source.display] = StagedFile(
                            source.display,
                            source.path,
                            updated,
                            baseline,
                            source_mode,
                            action="verify" if net_unchanged else "write",
                        )
                        _merge_patch_affected_file(
                            affected,
                            {
                                "path": source.display,
                                "operation": "unchanged" if net_unchanged else "update",
                                **evidence,
                            },
                        )
                        if prior is not None:
                            net_ranges = changed_ranges_between(baseline_content, updated)
                            affected[source.display]["changed_ranges"] = net_ranges
                            affected[source.display]["operation"] = (
                                "unchanged" if net_unchanged else "update"
                            )
                        summaries.append(f"{'=' if block_unchanged else 'M'} {source.display}")
            if not affected:
                raise ToolFailure("PATCH_FAILED", "No files were modified.", category="validation")
            workspace_mutated = any(change.action != "verify" for change in staged.values())
            if not dry_run:
                self._commit_staged_files(list(staged.values()))
        return {
            "dry_run": dry_run,
            "clean": True,
            "already_applied": (
                already_applied_operations > 0
                and already_applied_operations == len(operations)
            ),
            "revision_algorithm": REVISION_ALGORITHM,
            "summary": "\n".join(summaries),
            "affected_files": list(affected.values()),
            "additions": additions,
            "removals": removals,
            "warnings": warnings,
            "_workspace_mutated": workspace_mutated and not dry_run,
        }

    def apply_changes(self, args: dict[str, Any]) -> dict[str, Any]:
        dry_run = bool(args.get("dry_run", False))
        changes = parse_changes(args.get("changes"))
        staged: dict[str, StagedFile] = {}
        summaries: list[str] = []
        affected: list[dict[str, Any]] = []
        warnings: list[str] = []
        additions = 0
        removals = 0
        unchanged_files = 0
        with self.patch_lock:
            # Every path in the array is resolved before anything is staged, so
            # a rejected path in the last change cannot leave the earlier ones
            # half prepared, and so the duplicate check below compares the
            # names staging will actually use.
            targets: list[tuple[int, str]] = []
            for change in changes:
                targets.extend(self._resolve_change_paths(change))
            reject_duplicate_paths(targets)
            for change in changes:
                if change.action in {"create", "write"}:
                    result = self._stage_written_file(change, staged)
                elif change.action == "edit":
                    result = self._stage_edited_file(change, staged)
                elif change.action == "delete":
                    result = self._stage_deleted_file(change, staged)
                else:
                    result = self._stage_relocated_file(change, staged)
                entry, summary, added, removed = result
                affected.append(entry)
                summaries.append(summary)
                additions += added
                removals += removed
                unchanged_files += entry["operation"] == "unchanged"
            if not affected:
                raise ToolFailure("PATCH_FAILED", "No files were modified.", category="validation")
            workspace_mutated = any(change.action != "verify" for change in staged.values())
            if not dry_run:
                self._commit_staged_files(list(staged.values()))
        return {
            "dry_run": dry_run,
            "clean": True,
            "already_applied": unchanged_files == len(changes),
            "revision_algorithm": REVISION_ALGORITHM,
            "summary": "\n".join(summaries),
            "affected_files": affected,
            "additions": additions,
            "removals": removals,
            "warnings": warnings,
            "_workspace_mutated": workspace_mutated and not dry_run,
        }

    def _resolve_change_paths(self, change: ChangeRequest) -> list[tuple[int, str]]:
        """Validate one change's paths and return the display names it claims."""

        requires_existing = change.action in {"edit", "delete", "move", "copy"}
        self.workspace.reject_write_symlink(change.path)
        targets = [(change.index, self._resolve_patch_path(change.path, require_existing=requires_existing))]
        if change.destination is not None:
            self.workspace.reject_write_symlink(change.destination)
            targets.append((change.index, self._resolve_patch_path(change.destination, require_existing=False)))
        return targets

    def _stage_written_file(
        self, change: ChangeRequest, staged: dict[str, StagedFile]
    ) -> tuple[dict[str, Any], str, int, int]:
        """Stage a create or revision-checked write and return file evidence and line counts."""
        target = self.workspace.resolve_for_write(change.path)
        content = change.content or ""
        if target.existed and self._file_is_dir(target.path):
            raise ToolFailure(
                "PATCH_FAILED",
                f"Cannot {change.action} {target.display}: it is a directory. create and write "
                "only produce files; choose a file path inside it instead.",
                category="validation",
                details={"path": target.display, "is_directory": True},
            )
        baseline = self._capture_baseline(target.path)
        if change.action == "create" and target.existed:
            if baseline.data != content.encode("utf-8"):
                raise ToolFailure(
                    "PATCH_FAILED",
                    f"Cannot create {target.display}: it already exists with different content. "
                    "To replace it, read_file it and send action \"write\" with that revision.",
                    category="validation",
                    details={
                        "path": target.display,
                        "exists": True,
                        "next_action": {"tool": "read_file", "arguments": {"path": target.display}},
                    },
                )
            # Byte-identical: most likely a retry after a lost response. The
            # requested state already holds, so this is an idempotent success
            # that writes nothing, exactly like any other unchanged change.
            current = baseline.text(target.display)
        elif target.existed:
            current = baseline.text(target.display)
            self._check_revision(change, target.display, current)
        else:
            current = None
            if change.revision is not None:
                normalize_revision(change.revision, f"changes[{change.index}]")
                raise ToolFailure(
                    "REVISION_MISMATCH",
                    f"{target.display} does not exist, so it has no revision to match.",
                    category="conflict",
                    retryable=True,
                    details={"path": target.display, "expected_revision": change.revision, "exists": False},
                )
        unchanged = current == content
        staged[target.display] = StagedFile(
            target.display,
            target.path,
            content,
            baseline,
            baseline.mode,
            action="verify" if unchanged else "write",
        )
        operation = "unchanged" if unchanged else ("create" if current is None else "write")
        ranges: list[dict[str, int]] = (
            [] if unchanged else changed_ranges_between(current or "", content)
        )
        entry = {"path": target.display, "operation": operation, **_patch_evidence(content, ranges)}
        marker = {"unchanged": "=", "create": "A"}.get(operation, "M")
        return entry, f"{marker} {target.display}", 0 if unchanged else _count_lines(content), (
            0 if unchanged or current is None else _count_lines(current)
        )

    def _stage_edited_file(
        self, change: ChangeRequest, staged: dict[str, StagedFile]
    ) -> tuple[dict[str, Any], str, int, int]:
        """Stage revision-checked line edits and report only the lines that actually changed."""
        source = self.workspace.resolve_existing(change.path)
        if self._file_is_dir(source.path):
            raise ToolFailure("PATCH_FAILED", "Cannot edit a directory.", category="validation")
        baseline = self._capture_baseline(source.path)
        current = baseline.text(source.display)
        self._check_revision(change, source.display, current)
        outcome = apply_line_edits(current, change.edits, source.display)
        unchanged = outcome.content == current
        staged[source.display] = StagedFile(
            source.display,
            source.path,
            current if unchanged else outcome.content,
            baseline,
            baseline.mode,
            action="verify" if unchanged else "write",
        )
        # Count what actually differs: a replace that rewrites a line with its
        # own text is +0 -0, not +1 -1.
        added = sum(item["added_lines"] for item in outcome.changed_ranges)
        removed = sum(item["removed_lines"] for item in outcome.changed_ranges)
        entry = {
            "path": source.display,
            "operation": "unchanged" if unchanged else "edit",
            **_patch_evidence(outcome.content, outcome.changed_ranges),
        }
        return entry, f"{'=' if unchanged else 'M'} {source.display}", added, removed

    def _stage_deleted_file(
        self, change: ChangeRequest, staged: dict[str, StagedFile]
    ) -> tuple[dict[str, Any], str, int, int]:
        target = self.workspace.resolve_existing(change.path)
        if self._file_is_dir(target.path):
            raise ToolFailure("PATCH_FAILED", "Cannot delete a directory.", category="validation")
        baseline = self._capture_baseline(target.path)
        self._check_revision(change, target.display, baseline.text(target.display))
        staged[target.display] = StagedFile(
            target.display, target.path, None, baseline, baseline.mode, action="delete"
        )
        entry = {"path": target.display, "operation": "delete", "total_lines": 0, "changed_ranges": []}
        return entry, f"D {target.display}", 0, len((baseline.data or b"").splitlines())

    def _stage_relocated_file(
        self, change: ChangeRequest, staged: dict[str, StagedFile]
    ) -> tuple[dict[str, Any], str, int, int]:
        source = self.workspace.resolve_existing(change.path)
        if self._file_is_dir(source.path):
            raise ToolFailure("PATCH_FAILED", f"Cannot {change.action} a directory.", category="validation")
        destination = str(change.destination)
        dest = self.workspace.resolve_for_write(destination)
        if dest.existed:
            raise ToolFailure(
                "PATCH_FAILED",
                f"Cannot {change.action} onto {dest.display}: it already exists.",
                category="validation",
            )
        baseline = self._capture_baseline(source.path)
        content = baseline.text(source.display)
        self._check_revision(change, source.display, content)
        # A copy must still fail if its source changed underneath us, so the
        # source is staged as a baseline assertion that writes nothing.
        staged[source.display] = StagedFile(
            source.display,
            source.path,
            None if change.action == "move" else content,
            baseline,
            baseline.mode,
            action="delete" if change.action == "move" else "verify",
        )
        staged[dest.display] = StagedFile(
            dest.display,
            dest.path,
            content,
            self._capture_baseline(dest.path),
            baseline.mode,
            action="write",
        )
        entry = {
            "path": dest.display,
            "old_path": source.display,
            "operation": change.action,
            **_patch_evidence(content, _whole_file_range(content)),
        }
        marker = "R" if change.action == "move" else "C"
        return entry, f"{marker} {source.display} -> {dest.display}", _count_lines(content), 0

    def _check_revision(self, change: ChangeRequest, display: str, current: str) -> None:
        """Refuse to write over bytes the caller has not seen.

        The revision is the whole point of the tool: line numbers are only
        meaningful against a known file version, and a stale request that
        still parses is exactly the failure this refuses to commit.
        """

        actual = content_revision(current)
        if change.revision is None:
            raise ToolFailure(
                "REVISION_REQUIRED",
                f"changes[{change.index}] must carry the revision of {display}. "
                f"Read the file first; its current revision is {actual}.",
                category="validation",
                retryable=True,
                details={
                    "path": display,
                    "current_revision": actual,
                    "revision_algorithm": REVISION_ALGORITHM,
                    "next_action": {"tool": "read_file", "arguments": {"path": display}},
                },
            )
        expected = normalize_revision(change.revision, f"changes[{change.index}]")
        if expected != actual:
            # The current revision is deliberately withheld: pasted back with
            # line numbers from the old version, it would pass this check and
            # edit the wrong lines. read_file returns it together with the
            # line numbers that belong to it.
            raise ToolFailure(
                "REVISION_MISMATCH",
                f"{display} has changed since revision {expected} was read (by another write, or by "
                "an earlier apply_changes/apply_patch call). Re-read it with read_file, which returns "
                "the new revision and the current line numbers, and rebuild the change against those.",
                category="conflict",
                retryable=True,
                details={
                    "path": display,
                    "expected_revision": expected,
                    "revision_algorithm": REVISION_ALGORITHM,
                    "total_lines": _count_lines(current),
                    "next_action": {"tool": "read_file", "arguments": {"path": display, "line_numbers": True}},
                },
            )

    def _validate_patch_path(self, raw_path: str, *, require_existing: bool) -> None:
        self._resolve_patch_path(raw_path, require_existing=require_existing)

    def _resolve_patch_path(self, raw_path: str, *, require_existing: bool) -> str:
        """Validate one patch target and return its workspace-relative display name."""

        if require_existing:
            return self.workspace.resolve_existing(raw_path).display
        return self.workspace.resolve_for_write(raw_path).display

    def _commit_staged_files(self, staged: list[StagedFile]) -> None:
        self.patch_committer.commit(staged)
        for change in staged:
            if change.display in self.patch_baselines:
                continue
            self.patch_baselines[change.display] = (
                None if change.baseline.data is None else change.baseline.data.decode("utf-8", errors="replace")
            )

    def exec_command(self, args: dict[str, Any]) -> dict[str, Any]:
        """Start a policy-checked managed command and return output after exit or the yield limit."""
        self._prune_commands()
        cmd = str(args.get("cmd", ""))
        if not cmd:
            raise ToolFailure("INVALID_ARGUMENT", "cmd is required.", category="validation")
        workdir_arg = args.get("workdir", args.get("cwd", "."))
        if "workdir" in args and "cwd" in args and str(args["workdir"]) != str(args["cwd"]):
            raise ToolFailure("INVALID_ARGUMENT", "workdir and cwd refer to different directories.", category="validation")
        workdir = self.resolve_existing(str(workdir_arg))
        if not self._file_is_dir(workdir.path):
            raise ToolFailure("NOT_A_DIRECTORY", "workdir is not a directory.", category="validation")
        self._check_command_policy(cmd, args)
        timeout_ms = int(args.get("timeout_ms", DEFAULT_PROCESS_LIFETIME_MS))
        yield_ms = int(args.get("yield_time_ms", DEFAULT_YIELD_MS))
        max_output_bytes = int(args.get("max_output_bytes", 65536))
        tty = bool(args.get("tty", False))
        keep_stdin_open = bool(args.get("keep_stdin_open", False))
        stdin_text = str(args.get("stdin", ""))
        env = self._command_env(args.get("env", {}))
        start = time.time()
        deadline = start + (timeout_ms / 1000.0)
        landlock_fd: int | None = None
        landlock_warning: str | None = None
        landlock_confined = False
        popen_cmd: Any = cmd
        popen_shell = True
        popen_extra = process_group_popen_kwargs()
        selected_shell = None
        if os.name == "nt":
            selected_shell = selected_windows_command_shell(str(self.workspace.root))
            popen_cmd = shell_command(cmd, selected_shell)
            popen_shell = False
        if self.isolation.mode == "strict":
            self._ensure_workspace_write_paths()
        if self.landlock_enabled():
            try:
                landlock_fd = open_landlock_ruleset(
                    self.workspace.root,
                    guard_allow_roots(),
                    write_roots=self.landlock_write_roots(),
                    workspace_writable=not self.workspace_mutation.structured_only,
                )
                popen_cmd = landlock_exec_argv(landlock_fd, cmd)
                popen_shell = False
                popen_extra["pass_fds"] = (landlock_fd,)
                landlock_confined = True
            except ToolFailure as exc:
                if exc.code != "SANDBOX_UNAVAILABLE":
                    raise
                landlock_warning = landlock_unavailable_warning(exc)
        mutation = self.workspace_mutation_payload()
        workspace_may_write = (
            not self.workspace_mutation.structured_only
            or mutation.get("enforced") is not True
            or bool(self._workspace_write_path_roots)
            or (not landlock_confined and self.isolation.mode != "strict")
        )
        with self.commands_lock:
            if self._closed or self.command_manager.closed:
                if landlock_fd is not None:
                    os.close(landlock_fd)
                raise ToolFailure("COMMAND_CLOSED", "Workspace command manager is closed.", category="runtime")
            if len(self.commands) + self.starting_commands >= MAX_ACTIVE_COMMANDS:
                if landlock_fd is not None:
                    os.close(landlock_fd)
                raise ToolFailure(
                    "COMMAND_LIMIT_REACHED",
                    "Too many commands are already running or starting.",
                    category="runtime",
                    retryable=True,
                    details={"max_active_commands": MAX_ACTIVE_COMMANDS},
                )
            self.starting_commands += 1
        process: subprocess.Popen[bytes] | None = None
        command: CommandRun | None = None
        registered = False
        slot_released = False
        try:
            process, pty_master_fd = self.executor.spawn_managed(
                popen_cmd,
                cwd=str(workdir.path),
                shell=popen_shell,
                env=env,
                tty=tty,
                popen_kwargs=popen_extra,
            )
            if self.isolation.mode == "strict":
                workspace_may_write = not self.workspace_mutation.structured_only or bool(self._workspace_write_path_roots)
            command = self._make_command(
                process,
                timeout_at=deadline,
                warnings=[landlock_warning] if landlock_warning else None,
                pty_master_fd=pty_master_fd,
                landlock_confined=landlock_confined,
                workspace_may_write=workspace_may_write,
            )
            with self.commands_lock:
                self.starting_commands -= 1
                slot_released = True
                if not self._closed and not self.command_manager.closed:
                    self.commands[command.command_id] = command
                    registered = True
            if not registered:
                raise ToolFailure("COMMAND_CLOSED", "Runtime closed while the command was starting.", category="runtime")
        except Exception as exc:
            with self.commands_lock:
                if not registered and not slot_released:
                    self.starting_commands -= 1
            if process is not None and process.poll() is None:
                terminate_process_group(process, signal.SIGTERM)
            if isinstance(exc, OSError) and process is None:
                return {
                    "ok": False,
                    "status": "failed",
                    "operation_outcome": "spawn_error",
                    "error": {
                        "code": "COMMAND_SPAWN_FAILED",
                        "message": f"Failed to start command: {exc}",
                        "category": "runtime",
                        "retryable": exc.errno in {errno.EAGAIN, errno.ENOMEM},
                        "details": {
                            "errno": exc.errno,
                            "reason": exc.strerror or str(exc),
                        },
                    },
                }
            raise
        finally:
            if landlock_fd is not None:
                try:
                    os.close(landlock_fd)
                except OSError:
                    pass
        assert command is not None
        start_reader_threads(command)
        start_command_watchdog(command)
        try:
            if stdin_text:
                command.write_input(stdin_text.encode("utf-8"))
        except ToolFailure:
            if process.poll() is None:
                raise
        finally:
            # Closing stdin gives a command that reads it EOF instead of a
            # hang; keep_stdin_open is the explicit opt-in to interact.
            if not tty and not keep_stdin_open:
                command.close_stdin()
        initial_wait = max(0, min(yield_ms, MAX_YIELD_MS)) / 1000.0

        def finish() -> dict[str, Any]:
            # snapshot_since_cursor owns the status mapping (running/exited/
            # terminated/timeout) so exec, polling, and kill paths agree.
            payload = command.snapshot_since_cursor(max_output_bytes)
            payload["elapsed_ms"] = int((time.time() - start) * 1000)
            if selected_shell is not None:
                payload["shell"] = windows_command_shell_payload(selected_shell)
            if self.isolation.mode == "strict":
                payload["execution_isolation"] = {"mode": "strict", "confirmed": True, "network": self.isolation.network}
            self._add_exec_diagnostics(payload)
            return self._format_command_output(command, payload, args)

        while True:
            if process.poll() is not None:
                command.refresh_status()
                command.drain_readers()
                return finish()
            now = time.time()
            if not tty and now >= deadline:
                command.timed_out = True
                terminate_process_group(process, signal.SIGTERM)
                command.refresh_status()
                command.drain_readers()
                return finish()
            with command.lock:
                tty_has_initial_output = bool(
                    len(command.stdout) > command.stdout_cursor
                    or len(command.stderr) > command.stderr_cursor
                )
            if now - start >= initial_wait or (tty and tty_has_initial_output):
                return finish()
            time.sleep(0.02)

    def _check_command_policy(self, cmd: str, args: dict[str, Any]) -> None:
        if self.dangerously_skip_all_permissions:
            return
        selected_shell = selected_windows_command_shell(str(self.workspace.root)) if os.name == "nt" else None
        scannable = powershell_scannable(cmd) if selected_shell and selected_shell.kind == "pwsh" else cmd
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
            inline_script = inline_script_command(scannable)
            if inline_script is not None:
                raise ToolFailure(
                    "PERMISSION_REQUIRED",
                    "Inline interpreter or shell code requires explicit permission because network and filesystem effects cannot be verified statically.",
                    category="permission",
                    details={"permission": INLINE_SCRIPT_PERMISSION, **inline_script},
                )
        compact = " ".join(cmd.split()).lower()
        dynamic = (
            powershell_dynamic_construct(cmd) if selected_shell and selected_shell.kind == "pwsh"
            else cmd_dynamic_construct(cmd) if selected_shell else SHELL_EXPANSION_RE.search(scannable)
        )
        if not self.capabilities.shell_expansion and dynamic:
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
        if DESTRUCTIVE_RE.search(scannable):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Destructive commands are blocked without explicit permission.",
                category="permission",
                details={"permission": "destructive_command", "command": compact},
            )
        if not self.allow_network and NETWORK_RE.search(scannable) and not is_literal_network_reference_command(cmd):
            raise ToolFailure(
                "PERMISSION_REQUIRED",
                "Network access is denied by default.",
                category="permission",
                details={"permission": "network", "command": compact},
            )

    def _add_exec_diagnostics(self, payload: dict[str, Any]) -> None:
        diagnostics = exec_output_diagnostics(payload)
        if diagnostics:
            payload["diagnostics"] = diagnostics

    def _check_command_paths(self, cmd: str) -> None:
        scannable = strip_heredoc_payloads(cmd)
        try:
            tokens = shlex_split(scannable)
        except ValueError:
            tokens = scannable.split()
        for executable in command_executables(tokens):
            self._reject_setuid_executable(executable)
        for candidate in explicit_command_path_candidates(tokens):
            self._check_command_path_candidate(candidate)

    def _check_command_path_candidate(self, candidate: str) -> None:
        """Reject command path candidates that escape the workspace or cannot be checked safely."""
        candidate = candidate.strip()
        if not candidate or candidate in {"-", "--"}:
            return

        def escape_failure() -> ToolFailure:
            return ToolFailure(
                "PERMISSION_REQUIRED",
                "Command path escapes the workspace and is blocked.",
                category="permission",
                details={"permission": "filesystem_escape", "path": candidate},
            )

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
            # Drive-relative `C:file` resolves against that drive's cwd, not
            # the workspace. UNC `\\server\share` normalizes to `//server`.
            or (windows_shell_syntax() and re.match(r"^[A-Za-z]:", normalized))
            or any(part == ".." for part in PurePosixPath(normalized).parts)
        ):
            raise escape_failure()
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
                        raise escape_failure() from write_exc
                    raise
                return
            if exc.code in {"PATH_OUTSIDE_WORKSPACE", "ABSOLUTE_PATH_DENIED", "SYMLINK_ESCAPE"}:
                raise escape_failure() from exc

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

    def _command_env(self, extra: Any, *, ensure_runtime: bool = True) -> dict[str, str]:
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
        env = merge_environment(env, {str(key): str(value) for key, value in self.shell_env_policy.set.items()}, windows=os.name == "nt")
        if ensure_runtime:
            self._ensure_runtime_dirs()
        tmp_dir = self.command_tmp_dir()
        env["HOME"] = str(self.command_home_dir())
        env["TMPDIR"] = str(tmp_dir)
        if os.name == "nt":
            env["TEMP"] = str(tmp_dir)
            env["TMP"] = str(tmp_dir)
        if isinstance(extra, dict):
            for key, value in extra.items():
                key_text = str(key)
                value_text = str(value)
                if not self.dangerously_skip_all_permissions and is_filtered_env_var(key_text, value_text):
                    continue
                env = merge_environment(env, {key_text: value_text}, windows=os.name == "nt")
        if not self.dangerously_skip_all_permissions or self.isolation.mode == "strict":
            env = sanitized_environment(env, windows=os.name == "nt", is_filtered=is_filtered_env_var)
        if os.name == "nt":
            env = merge_environment(env, {"PATHEXT": ";".join(windows_pathext(env))}, windows=True)
            env = merge_environment(env, {"HOME": str(self.command_home_dir()), "TEMP": str(tmp_dir), "TMP": str(tmp_dir)}, windows=True)
        return env

    def _git_env(self) -> dict[str, str]:
        return self._command_env({})

    def _run_git_text(
        self, cmd: list[str], *, timeout: int | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        # Decode in the caller: Windows text-pipe reader threads can otherwise
        # lose a UnicodeDecodeError and leave stdout/stderr as None.
        completed = self._run_git_bytes(
            [cmd[0], "-c", "i18n.logOutputEncoding=UTF-8", *cmd[1:]],
            timeout=timeout,
            env=env,
        )
        try:
            stdout = completed.stdout.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            stderr = completed.stderr.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            raise ToolFailure(
                "GIT_ERROR",
                "Git output is not valid UTF-8; refusing to replace undecodable bytes.",
                category="runtime",
            ) from exc
        return subprocess.CompletedProcess(
            completed.args, completed.returncode, stdout, stderr
        )

    def _run_git_bytes(
        self, cmd: list[str], *, timeout: int | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return self.executor.run(
            cmd,
            text=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=self._git_env() if env is None else env,
        )

    def _git_status_not_repo(self, completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        warnings = []
        stderr = completed.stderr.strip()
        if stderr:
            warnings.append(f"git rev-parse failed: {stderr}")
        return {"is_repo": False, "clean": True, "entries": [], "truncated": False, "warnings": warnings}

    def _is_git_repo(self, path: Path, *, env: dict[str, str] | None = None) -> bool:
        completed = self._run_git_text(
            [require_git(), "-C", str(path), "rev-parse", "--is-inside-work-tree"], env=env
        )
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    def _git_rev_parse(self, path: Path, rev: str, *, env: dict[str, str] | None = None) -> str:
        completed = self._run_git_text([require_git(), "-C", str(path), "rev-parse", rev], env=env)
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def _git_path_filters(self, args: dict[str, Any]) -> list[str]:
        path_filters: list[str] = []
        if isinstance(args.get("path"), str):
            path_filters.append(str(args["path"]))
        if isinstance(args.get("paths"), list):
            path_filters.extend(str(item) for item in args["paths"])
        return [self.git_path_filter(path) for path in path_filters]

    def _base_command_env(self) -> dict[str, str]:
        if self.shell_env_policy.inherit == "none":
            return {}
        if self.shell_env_policy.inherit == "all":
            return {str(key): str(value) for key, value in os.environ.items()}
        return {
            str(key): str(value)
            for key, value in os.environ.items()
            if is_core_command_env_name(str(key))
        }

    def _make_command(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout_at: float | None = None,
        warnings: list[str] | None = None,
        pty_master_fd: int | None = None,
        landlock_confined: bool = False,
        workspace_may_write: bool = True,
    ) -> CommandRun:
        return CommandRun(
            command_id=secrets.token_urlsafe(18),
            process=process,
            timeout_at=timeout_at,
            warnings=warnings or [],
            pty_master_fd=pty_master_fd,
            landlock_confined=landlock_confined,
            workspace_may_write=workspace_may_write,
            execution_isolation=({"mode": "strict", "confirmed": True, "network": self.isolation.network}
                                 if self.isolation.mode == "strict" else None),
            on_evict=self.command_manager.record_output_eviction,
        )

    def _remember_output_command(self, command: CommandRun) -> None:
        command.refresh_status()
        with self.commands_lock:
            self.output_commands.pop(command.command_id, None)
            self.output_commands[command.command_id] = command
            self._evict_retained_locked()

    def _retained_output_bytes_locked(self) -> int:
        return sum(command.retained_bytes for command in self.commands.values()) + sum(
            command.retained_bytes for command in self.output_commands.values()
        )

    def _evict_retained_locked(self) -> None:
        retained = self._retained_output_bytes_locked()
        while self.output_commands and (
            len(self.output_commands) > MAX_RETAINED_OUTPUT_COMMANDS
            or retained > MAX_RUNTIME_OUTPUT_BYTES
        ):
            oldest = self.output_commands.pop(next(iter(self.output_commands)))
            retained -= oldest.retained_bytes

    def _complete_command(self, command: CommandRun) -> None:
        command.refresh_status()
        if command.process.poll() is None:
            return
        with self.commands_lock:
            self.commands.pop(command.command_id, None)
        self._remember_output_command(command)

    def _prune_commands(self) -> None:
        """Collect completed commands and evict retained output by observation TTL and capacity."""
        with self.commands_lock:
            active = list(self.commands.values())
        for command in active:
            command.refresh_status()
            if command.process.poll() is not None:
                self._complete_command(command)
        cutoff = time.time() - COMPLETED_COMMAND_TTL_SECONDS
        with self.commands_lock:
            expired = [
                command_id
                for command_id, command in self.output_commands.items()
                # TTL starts when a response first reported the exit. A command
                # nobody has seen finish stays until capacity eviction.
                if command.observed_at is not None and command.observed_at < cutoff
            ]
            for command_id in expired:
                self.output_commands.pop(command_id, None)
            self._evict_retained_locked()

    def _get_output_command(self, command_id: str) -> CommandRun:
        self._prune_commands()
        with self.commands_lock:
            command = self.commands.get(command_id) or self.output_commands.get(command_id)
        if command is None:
            raise ToolFailure(
                "COMMAND_NOT_FOUND",
                "Output command not found.",
                category="runtime",
                details={"retry_hint": _COMMAND_RECOVERY_HINT},
            )
        return command

    @staticmethod
    def _mark_terminal_observed(command: CommandRun, status: Any) -> None:
        """Start the retention clock when a completed command is first reported as terminal."""
        if status in TERMINAL_COMMAND_STATUSES and command.completed_at is not None and command.observed_at is None:
            command.observed_at = time.time()

    def _format_command_output(self, command: CommandRun, payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        """Attach paging references and continuations, updating command retention on completion."""
        terminal = payload.get("status") != "running"
        if terminal:
            self._complete_command(command)
        self._mark_terminal_observed(command, payload.get("status"))
        if payload.get("status") == "running":
            payload["next_action"] = {
                "tool": "write_stdin",
                "arguments": {
                    "command_id": command.command_id,
                    "chars": "",
                    "yield_time_ms": DEFAULT_YIELD_MS,
                },
            }
        output_refs = command_output_refs(command.command_id)
        # Always present so a client can page either stream with read_output
        # without first having to hit truncation.
        payload["output_refs"] = dict(output_refs)
        truncated_streams: list[str] = []
        cursor_skipped_drop = False
        for stream in ("stdout", "stderr"):
            omitted = payload.get(f"{stream}_omitted_bytes")
            if isinstance(omitted, int) and omitted > 0:
                cursor_skipped_drop = True
            if payload.get(f"{stream}_truncated") or (
                isinstance(omitted, int) and omitted > 0
            ):
                truncated_streams.append(stream)
        if cursor_skipped_drop:
            self.command_manager.record_omitted_read("poll")
        output_stream = (
            truncated_streams[0]
            if truncated_streams
            else "stderr"
            if not payload.get("stdout") and payload.get("stderr")
            else "stdout"
        )
        output_ref = output_refs[output_stream]
        truncated = bool(payload.get("truncated"))
        if truncated:
            if not truncated_streams:
                truncated_streams.append(output_stream)
            if terminal:
                self._remember_output_command(command)
            payload["output_ref"] = output_ref
            payload["output_stream"] = output_stream
            payload["output_refs"] = output_refs
            payload["output_truncated"] = True
            payload["truncated_output_streams"] = truncated_streams
            read_actions = [read_output_action(output_refs[stream]) for stream in truncated_streams]
            payload["next_actions"] = read_actions
            if terminal:
                payload["next_action"] = read_actions[0]
        verbosity = str(args.get("verbosity", "")).strip().lower()
        if not verbosity:
            return payload
        if verbosity not in {"summary", "preview", "full"}:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "verbosity must be one of: summary, preview, full.",
                category="validation",
            )
        if terminal and not truncated:
            self._remember_output_command(command)
        payload["summary"] = self._command_output_summary(command, payload)
        payload["output_ref"] = output_ref
        payload["output_stream"] = output_stream
        payload["output_refs"] = output_refs
        if verbosity == "full":
            return payload
        compact = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "stdout",
                "stderr",
                "stdout_truncated",
                "stderr_truncated",
                "stdout_truncated_by",
                "stderr_truncated_by",
                "stdout_output_lines",
                "stderr_output_lines",
                "stdout_output_bytes",
                "stderr_output_bytes",
                "stdout_omitted_bytes",
                "stderr_omitted_bytes",
            }
        }
        if verbosity == "preview":
            preview_limit = int(args.get("preview_bytes", EXEC_PREVIEW_BYTES))
            preview, preview_truncated = truncate_bytes(command.retained_output_bytes(), preview_limit)
            compact["preview"] = preview
            compact["preview_truncated"] = preview_truncated
            compact["truncated"] = bool(compact.get("truncated") or preview_truncated)
            if preview_truncated and not compact.get("truncated_output_streams"):
                preview_streams = [
                    stream
                    for stream in ("stdout", "stderr")
                    if command.retained_stream_segments(stream)[3] > 0
                ]
                compact["truncated_output_streams"] = preview_streams
                preview_actions = [read_output_action(output_refs[stream]) for stream in preview_streams]
                compact["next_actions"] = preview_actions
                if terminal and preview_actions:
                    compact["next_action"] = preview_actions[0]
        return compact

    def _command_output_summary(self, command: CommandRun, payload: dict[str, Any]) -> str:
        retained = command.retained_output_bytes().decode("utf-8", errors="replace")
        lines = retained.splitlines()
        tail = next((line.strip() for line in reversed(lines) if line.strip()), "")
        if len(tail) > 120:
            tail = tail[:117] + "..."
        elapsed = float(payload.get("elapsed_ms") or 0) / 1000.0
        exit_code = payload.get("exit_code")
        status = f"exit {exit_code}" if exit_code is not None else str(payload.get("status", "running"))
        parts = [status, f"{elapsed:.1f}s", f"{len(lines)} lines"]
        if tail:
            parts.append(f"tail: {tail!r}")
        return " | ".join(parts)

    def read_output(self, args: dict[str, Any]) -> dict[str, Any]:
        """Page retained command output by byte offset, reporting status and any evicted gaps."""
        output_ref = str(args.get("output_ref", "")).strip()
        requested_stream = str(args.get("stream", "") or "")
        if requested_stream and requested_stream not in {"stdout", "stderr"}:
            raise ToolFailure("INVALID_ARGUMENT", "stream must be stdout or stderr.", category="validation")
        # Accept the full ref, `command:<id>`, or a bare command_id; the last
        # two take their stream from `stream` (default stdout).
        match = re.fullmatch(r"command:([^:]+):(stdout|stderr)", output_ref)
        if match:
            command_id, stream = match.group(1), match.group(2)
            if requested_stream and requested_stream != stream:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "stream does not match output_ref.",
                    category="validation",
                    details={"output_ref_stream": stream, "stream": requested_stream},
                )
        else:
            bare = re.fullmatch(r"(?:command:)?([^:\s]+)", output_ref)
            if not bare:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "output_ref must be a command_id, command:<id>, or command:<id>:stdout|stderr.",
                    category="validation",
                )
            command_id, stream = bare.group(1), requested_stream or "stdout"
            output_ref = f"command:{command_id}:{stream}"
        command = self._get_output_command(command_id)
        command.refresh_status()
        head, tail, tail_start_offset, total_stream_bytes, dropped_bytes = command.retained_stream_segments(stream)
        requested_offset = max(0, int(args.get("offset", 0)))
        limit = max(1, min(int(args.get("limit", EXEC_PREVIEW_BYTES)), COMMAND_BUFFER_BYTES))
        head_len = len(head)
        evicted_gap_bytes = max(0, tail_start_offset - head_len)
        # The retained set is the frozen head [0, head_len) plus the rolling
        # tail [tail_start_offset, total). Serve from whichever segment holds
        # the requested offset; offsets inside the evicted gap clamp forward
        # to the tail. Chunks never span the gap so offsets stay stable.
        if requested_offset >= tail_start_offset:
            offset = requested_offset
            buffer_offset = offset - tail_start_offset
            chunk = tail[buffer_offset : buffer_offset + limit]
        elif requested_offset < head_len:
            offset = requested_offset
            chunk = head[offset : min(head_len, offset + limit)]
        else:
            offset = tail_start_offset
            chunk = tail[:limit]
        next_offset = offset + len(chunk) if offset + len(chunk) < total_stream_bytes else None
        omitted_bytes = offset - requested_offset
        warnings: list[str] = []
        if omitted_bytes:
            warnings.append(f"{stream} offset skipped dropped bytes")
        if evicted_gap_bytes:
            warnings.append(
                f"{stream} output between the retained head and the rolling tail was evicted; "
                "redirect large output to a file (cmd > out.log 2>&1) to keep everything"
            )
        if omitted_bytes:
            self.command_manager.record_omitted_read("read_output")
        status = command.status()
        self._mark_terminal_observed(command, status)
        result = {
            "command_id": command.command_id,
            "status": status,
            "exit_code": command.exit_code if status != "running" else None,
            "operation_outcome": command.operation_outcome(status),
            "output_ref": output_ref,
            "stream_output_ref": f"command:{command.command_id}:{stream}",
            "stream": stream,
            "offset": offset,
            "requested_offset": requested_offset,
            "limit": limit,
            "content": chunk.decode("utf-8", errors="replace"),
            "next_offset": next_offset,
            "total_retained_bytes": len(tail) + min(head_len, tail_start_offset),
            "head_retained_bytes": head_len,
            "evicted_gap_bytes": evicted_gap_bytes,
            "retained_start_offset": tail_start_offset,
            "total_stream_bytes": total_stream_bytes,
            "stdout_dropped_bytes": command.stdout_dropped_bytes,
            "stderr_dropped_bytes": command.stderr_dropped_bytes,
            "stream_dropped_bytes": dropped_bytes,
            "omitted_bytes": omitted_bytes,
            "truncated": next_offset is not None,
            "ok": True,
            "warnings": warnings,
        }
        if next_offset is not None:
            result["next_action"] = read_output_action(
                str(result["stream_output_ref"]), offset=next_offset, limit=limit
            )
        return result

    def write_stdin(self, args: dict[str, Any]) -> dict[str, Any]:
        """Write command input or poll with empty chars, then return the next output snapshot."""
        command_id = str(args.get("command_id", ""))
        command = self._get_command(command_id)
        command.refresh_status()
        chars = str(args.get("chars", ""))
        if command.process.poll() is not None:
            if chars:
                self._raise_exited_stdin(command)
            payload = command.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))
            return self._format_command_output(command, payload, args)
        if chars:
            if command.stdin_closed:
                raise ToolFailure(
                    "COMMAND_CLOSED",
                    "Command is still running but its stdin was closed at start; input cannot be written.",
                    category="runtime",
                    details={
                        "reason": "stdin_closed",
                        "command_id": command.command_id,
                        "retry_hint": (
                            "To interact, start the command again with exec_command keep_stdin_open=true "
                            "(or tty=true on POSIX). To stop it, use kill_command. For one-shot input, "
                            "pass it as exec_command's stdin. Polling with empty chars still works."
                        ),
                    },
                )
            try:
                command.write_input(chars.encode("utf-8"))
            except ToolFailure:
                command.refresh_status()
                if command.process.poll() is not None:
                    self._raise_exited_stdin(command)
                raise
        wait_until = time.time() + (int(args.get("yield_time_ms", DEFAULT_YIELD_MS)) / 1000.0)
        first_output_at: float | None = None
        while time.time() < wait_until and command.process.poll() is None:
            time.sleep(0.02)
            with command.lock:
                has_new_output = len(command.stdout) > command.stdout_cursor or len(command.stderr) > command.stderr_cursor
                if has_new_output and not chars:
                    break
                if has_new_output and chars:
                    if first_output_at is None:
                        first_output_at = time.time()
                    if time.time() - first_output_at >= 0.05:
                        break
        payload = command.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))
        return self._format_command_output(command, payload, args)

    def _raise_exited_stdin(self, command: CommandRun) -> None:
        """Record terminal observation and reject stdin writes with the command's exit details."""
        command.refresh_status()
        status = command.status()
        self._complete_command(command)
        self._mark_terminal_observed(command, status)
        raise ToolFailure(
            "COMMAND_CLOSED",
            "Command is closed; stdin write blocked.",
            category="runtime",
            details={
                "reason": "exited",
                "command_id": command.command_id,
                "status": status,
                "exit_code": command.exit_code,
                "signal": command.signal_name,
                "operation_outcome": command.operation_outcome(status),
                "retry_hint": (
                    "The command has finished; read its remaining output with write_stdin "
                    "chars=\"\" or read_output."
                ),
            },
        )

    def _wait_for_command_exit(self, command: CommandRun, wait_seconds: float) -> bool:
        try:
            command.process.wait(timeout=max(0.0, wait_seconds))
        except subprocess.TimeoutExpired:
            pass
        command.refresh_status()
        command.drain_readers()
        return command.process.poll() is not None

    def kill_command(self, args: dict[str, Any]) -> dict[str, Any]:
        """Terminate a live command with escalation, or report its existing exit without signaling."""
        command_id = str(args.get("command_id", ""))
        command = self._get_command(command_id)
        signal_name = str(args.get("signal", "TERM"))
        force = signal_name == "KILL"
        signum = {"TERM": signal.SIGTERM, "KILL": HARD_KILL_SIGNAL, "INT": signal.SIGINT}.get(
            signal_name,
            signal.SIGTERM,
        )
        evict = True
        signal_sent: str | None = None
        if command.process.poll() is None:
            command.terminating = True
            command.killed_by_client = True
            signal_sent = "SIGKILL" if force else signal.Signals(signum).name
            terminate_process_group(command.process, signum, force=force)
            exited = self._wait_for_command_exit(command, int(args.get("wait_ms", 5000)) / 1000.0)
            if not exited and not force:
                force = True
                signal_sent = "SIGKILL"
                terminate_process_group(command.process, HARD_KILL_SIGNAL, force=True)
                exited = self._wait_for_command_exit(command, int(args.get("kill_wait_ms", 2000)) / 1000.0)
            if exited:
                killed = True
                status = "killed" if force else "terminated"
            else:
                killed = False
                evict = False
                status = "terminating"
        else:
            # Nothing was sent; exit_code/signal report the exit it already had.
            killed = False
            command.refresh_status()
            status = "exited"
        payload = command.snapshot_since_cursor(int(args.get("max_output_bytes", 65536)))
        payload.update({"killed": killed, "status": status, "evicted": evict, "signal_sent": signal_sent})
        payload = self._format_command_output(command, payload, args)
        if status == "terminating":
            warnings = list(payload.get("warnings", []))
            warnings.append("Process did not exit after TERM/SIGKILL; command retained for retry or watchdog cleanup.")
            payload["warnings"] = warnings
            payload["next_action"] = "retry kill_command or wait for watchdog cleanup"
        if evict:
            with self.commands_lock:
                self.commands.pop(command_id, None)
        return payload

    def _get_command(self, command_id: str) -> CommandRun:
        self._prune_commands()
        with self.commands_lock:
            command = self.commands.get(command_id) or self.output_commands.get(command_id)
        if command is None:
            raise ToolFailure(
                "COMMAND_NOT_FOUND",
                "Command not found; stdin access denied.",
                category="not_found",
                details={"retry_hint": _COMMAND_RECOVERY_HINT},
            )
        return command

    def git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_existing(str(args.get("path", ".")))
        max_entries = int(args.get("max_entries", 1000))
        include_untracked = bool(args.get("include_untracked", True))
        git = require_git()
        git_env = self._git_env()
        root_check = self._run_git_text(
            [git, "-C", str(resolved.path), "rev-parse", "--show-toplevel"], env=git_env
        )
        if root_check.returncode != 0:
            return self._git_status_not_repo(root_check)
        status_cmd = [git, "-C", str(resolved.path), "status", "--porcelain=v1", "-b"]
        if not include_untracked:
            status_cmd.append("--untracked-files=no")
        completed = self._run_git_text(status_cmd, timeout=10, env=git_env)
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
            "head": self._git_rev_parse(resolved.path, "HEAD", env=git_env),
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "clean": not entries,
            "entries": entries,
            "truncated": len(entries) >= max_entries and len(lines) > max_entries + 1,
        }

    def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        git_env = self._git_env()
        staged = bool(args.get("staged", False))
        unstaged = bool(args.get("unstaged", True))
        include_untracked = bool(args.get("include_untracked", True))
        context = int(args.get("context_lines", 3))
        max_bytes = int(args.get("max_bytes", 262144))
        path_filters = self._git_path_filters(args)
        if not self._is_git_repo(self.workspace.root, env=git_env):
            return self._fallback_diff(path_filters, max_bytes)
        chunks: list[bytes] = []
        if unstaged:
            chunks.append(self._run_git_diff(git, context, path_filters, cached=False, env=git_env))
        if staged:
            chunks.append(self._run_git_diff(git, context, path_filters, cached=True, env=git_env))
        warnings: list[str] = []
        # A file created by apply_patch is untracked, so plain `git diff` cannot
        # confirm it exists. Diff each untracked path against an empty file so
        # an added file is verifiable from the same output as an edited one.
        if include_untracked and unstaged:
            untracked_chunks, untracked_warnings = self._untracked_diffs(
                git, context, path_filters, max_bytes, env=git_env
            )
            chunks.extend(untracked_chunks)
            warnings.extend(untracked_warnings)
        combined = b""
        for chunk in chunks:
            if combined and chunk and not combined.endswith(b"\n"):
                combined += b"\n"
            combined += chunk
        diff_truncation = truncate_text_head(combined.decode("utf-8", errors="replace"), max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes)
        diff_text = diff_truncation.content
        truncated = diff_truncation.truncated
        if truncated:
            warnings.append("diff truncated")
        return {
            "diff": diff_text,
            "files": parse_diff_files(diff_text),
            "include_untracked": include_untracked and unstaged,
            **truncation_fields(diff_truncation),
            "warnings": warnings,
        }

    def _untracked_diffs(
        self,
        git: str,
        context: int,
        path_filters: list[str],
        max_bytes: int,
        *,
        env: dict[str, str] | None = None,
    ) -> tuple[list[bytes], list[str]]:
        listing = [git, "-C", str(self.workspace.root), "ls-files", "--others", "--exclude-standard", "-z"]
        if path_filters:
            listing.append("--")
            listing.extend(path_filters)
        completed = self._run_git_bytes(listing, timeout=10, env=env)
        if completed.returncode != 0:
            return [], ["untracked files could not be listed"]
        paths = [entry for entry in completed.stdout.decode("utf-8", errors="replace").split("\0") if entry]
        chunks: list[bytes] = []
        warnings: list[str] = []
        if len(paths) > MAX_UNTRACKED_DIFF_FILES:
            warnings.append(
                f"only the first {MAX_UNTRACKED_DIFF_FILES} of {len(paths)} untracked files are diffed"
            )
            paths = paths[:MAX_UNTRACKED_DIFF_FILES]
        spent = 0
        for rel in paths:
            if spent >= max_bytes:
                warnings.append("untracked diff truncated")
                break
            chunk = self._run_git_bytes(
                [
                    git,
                    "-C",
                    str(self.workspace.root),
                    "diff",
                    "--no-index",
                    f"--unified={context}",
                    "--",
                    os.devnull,
                    rel,
                ],
                timeout=10,
                env=env,
            )
            # --no-index reports a difference with exit 1; anything else is a
            # per-file problem (unreadable path, vanished file) that must not
            # fail the whole diff.
            if chunk.returncode not in {0, 1}:
                warnings.append(f"untracked file could not be diffed: {rel}")
                continue
            chunks.append(chunk.stdout)
            spent += len(chunk.stdout)
        return chunks, warnings

    def _run_git_diff(
        self, git: str, context: int, path_filters: list[str], *, cached: bool, env: dict[str, str] | None = None
    ) -> bytes:
        cmd = [git, "-C", str(self.workspace.root), "diff", f"--unified={context}"]
        if cached:
            cmd.append("--cached")
        if path_filters:
            cmd.append("--")
            cmd.extend(path_filters)
        completed = self._run_git_bytes(cmd, timeout=10, env=env)
        if completed.returncode not in {0, 1}:
            raise ToolFailure("GIT_ERROR", completed.stderr.decode("utf-8", errors="replace"), category="runtime")
        return completed.stdout

    def _fallback_diff(self, path_filters: list[str], max_bytes: int) -> dict[str, Any]:
        selected = set(path_filters)
        chunks: list[str] = []
        files: list[dict[str, Any]] = []
        with self.patch_lock:
            baselines = sorted(self.patch_baselines.items())
        for rel, before in baselines:
            if selected and rel not in selected:
                continue
            current_path = self.workspace.resolve_for_write(rel).path
            after = self._file_text(current_path) if self._file_exists(current_path) and not self._file_is_dir(current_path) else None
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
            **truncation_fields(diff_truncation),
            "warnings": ["non-git diff fallback"] + (["diff truncated"] if truncated else []),
        }

    def git_log(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        git_env = self._git_env()
        requested_path = str(args.get("path", "."))
        resolved = self.resolve_existing(requested_path)
        if not self._is_git_repo(resolved.path, env=git_env):
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
        completed = self._run_git_text(cmd, timeout=10, env=git_env)
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
        result = {
            "is_repo": True,
            "ref": ref,
            "path": path_filter,
            "max_count": max_count,
            "skip": skip,
            "commits": commits[:max_count],
            "truncated": truncated,
            "warnings": ["commit limit reached"] if truncated else [],
        }
        if truncated:
            result["next_action"] = {
                "tool": "git_log",
                "arguments": {
                    "path": requested_path,
                    "ref": ref,
                    "max_count": max_count,
                    "skip": skip + max_count,
                },
            }
        return result

    def git_show(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        git_env = self._git_env()
        if not self._is_git_repo(self.workspace.root, env=git_env):
            return {"is_repo": False, "content": "", "files": [], "truncated": False, "warnings": []}
        rev = validate_git_ref(str(args.get("rev", "HEAD")))
        context = int(args.get("context_lines", 3))
        max_bytes = int(args.get("max_bytes", 262144))
        include_diff = bool(args.get("include_diff", True))
        normalized_filters = self._git_path_filters(args)
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
        completed = self._run_git_bytes(cmd, timeout=10, env=git_env)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.decode("utf-8", errors="replace").strip() or "git show failed", category="runtime")
        truncation = truncate_text_head(completed.stdout.decode("utf-8", errors="replace"), max_lines=DEFAULT_MAX_LINES, max_bytes=max_bytes)
        content = truncation.content
        return {
            "is_repo": True,
            "rev": rev,
            "content": content,
            "files": parse_diff_files(content),
            **truncation_fields(truncation),
            "warnings": ["output truncated"] if truncation.truncated else [],
        }

    def git_blame(self, args: dict[str, Any]) -> dict[str, Any]:
        git = require_git()
        git_env = self._git_env()
        requested_path = str(args.get("path", ""))
        resolved = self.resolve_existing(requested_path)
        if self._file_is_dir(resolved.path):
            raise ToolFailure("IS_DIRECTORY", "Path is a directory.", category="validation")
        if not self._is_git_repo(self.workspace.root, env=git_env):
            return {"is_repo": False, "path": resolved.display, "lines": [], "truncated": False, "warnings": []}
        ref_arg = args.get("rev")
        ref = validate_git_ref(str(ref_arg)) if isinstance(ref_arg, str) and ref_arg else None
        start_line = int(args.get("start_line", 1))
        end_line = args.get("end_line")
        max_lines = int(args.get("max_lines", 200))
        if end_line is None:
            requested_final_line = start_line + max_lines - 1
        else:
            requested_final_line = int(end_line)
        if requested_final_line < start_line:
            raise ToolFailure("INVALID_ARGUMENT", "end_line must be >= start_line.", category="validation")
        requested_lines = requested_final_line - start_line + 1
        truncated = requested_lines > max_lines
        final_line = min(requested_final_line, start_line + max_lines - 1)
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
        completed = self._run_git_text(cmd, timeout=10, env=git_env)
        if completed.returncode != 0:
            raise ToolFailure("GIT_ERROR", completed.stderr.strip() or "git blame failed", category="runtime")
        lines = parse_git_blame_porcelain(completed.stdout)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated = True
        result = {
            "is_repo": True,
            "path": resolved.display,
            "rev": ref,
            "start_line": start_line,
            "end_line": final_line,
            "max_lines": max_lines,
            "lines": lines,
            "truncated": truncated,
            "warnings": ["line limit reached"] if truncated else [],
        }
        if truncated and final_line < requested_final_line:
            next_arguments: dict[str, Any] = {
                "path": requested_path,
                "start_line": final_line + 1,
                "end_line": requested_final_line,
                "max_lines": max_lines,
            }
            if ref:
                next_arguments["rev"] = ref
            result["next_action"] = {
                "tool": "git_blame",
                "arguments": next_arguments,
            }
        return result

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
        data = self._file_bytes(resolved.path)
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
        payload: dict[str, Any] = {
            "path": resolved.display,
            "mime_type": mime_type,
            "bytes": len(data),
            "width": width,
            "height": height,
            "resized": resized,
            "original": original,
            "_mcp_image_data": base64.b64encode(data).decode("ascii"),
            "warnings": warnings,
        }
        return payload


def walk_files(root: Path) -> Iterator[Path]:
    if root.is_file() or root.is_symlink():
        yield root
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in DEFAULT_EXCLUDED_NAMES]
        current_path = Path(current)
        for name in files:
            yield current_path / name


def path_batches(paths: Iterator[Path], size: int) -> Iterator[list[Path]]:
    batch: list[Path] = []
    for path in paths:
        batch.append(path)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def find_literal(line: str, needle: str, case_sensitive: bool) -> int:
    """Return the match index of a pre-normalized needle (lowered unless
    case_sensitive) in line, or -1."""
    haystack = line if case_sensitive else line.lower()
    return haystack.find(needle)


def windows_shell_syntax() -> bool:
    """Whether commands run under a Windows shell, where ``\\`` separates paths.

    A function rather than a constant so tests can simulate Windows.
    """

    return os.name == "nt"


def shlex_split(command: str, *, windows: bool | None = None) -> list[str]:
    """Tokenize shell syntax for policy checks, preserving backslashes on Windows."""
    if windows is None:
        windows = windows_shell_syntax()
    if windows:
        # Windows shells have no backslash escape, so a POSIX lexer would eat
        # path separators: `..\..\secret` became `....secret` and slipped
        # past the workspace check. Doubling each backslash makes the lexer
        # yield it literally, outside and inside double quotes alike.
        command = command.replace("\\", "\\\\")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def parse_heredoc_delimiter(command: str, start: int) -> tuple[int, str, bool]:
    index = start
    length = len(command)
    strip_tabs = False
    if index < length and command[index] == "-":
        strip_tabs = True
        index += 1
    while index < length and command[index] in " \t":
        index += 1
    delimiter: list[str] = []
    while index < length:
        char = command[index]
        if char in "'\"":
            quote = char
            index += 1
            while index < length and command[index] != quote:
                delimiter.append(command[index])
                index += 1
            if index < length:
                index += 1
            continue
        if char == "\\" and index + 1 < length:
            delimiter.append(command[index + 1])
            index += 2
            continue
        if char.isspace() or char in ";&|<>()":
            break
        delimiter.append(char)
        index += 1
    return index, "".join(delimiter), strip_tabs


def strip_heredoc_payloads(command: str) -> str:
    """Drop heredoc body lines so command scanning sees only live shell code.

    Heredoc bodies are stdin data, not code: scanning XML payloads produces fake
    escape candidates such as ``/modelVersion`` from ``</modelVersion>``. Bash
    starts the body on the line after the operator, so everything else stays
    visible to the scanner: redirections on the operator's own line
    (``cat <<EOF > /etc/cron.d/evil``) and commands after the closing delimiter.
    ``<<`` inside quotes or inside ``((...))`` arithmetic never opens a heredoc,
    which keeps fake heredocs from hiding live commands; an unterminated heredoc
    swallows the remaining lines exactly as bash treats them (as body).
    """
    if "<<" not in command:
        return command
    live: list[str] = []
    pending: list[tuple[str, bool]] = []
    index = 0
    length = len(command)
    in_single = False
    in_double = False
    arith_parens = 0
    while index < length:
        char = command[index]
        if in_single:
            live.append(char)
            in_single = char != "'"
            index += 1
            continue
        if in_double:
            if char == "\\" and index + 1 < length:
                live.append(command[index : index + 2])
                index += 2
                continue
            live.append(char)
            in_double = char != '"'
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            live.append(command[index : index + 2])
            index += 2
            continue
        if char == "'":
            in_single = True
            live.append(char)
            index += 1
            continue
        if char == '"':
            in_double = True
            live.append(char)
            index += 1
            continue
        if arith_parens:
            if char == "(":
                arith_parens += 1
            elif char == ")":
                arith_parens -= 1
            live.append(char)
            index += 1
            continue
        if char == "(" and command[index : index + 2] == "((":
            arith_parens = 2
            live.append("((")
            index += 2
            continue
        if char == "<" and command[index : index + 3] == "<<<":
            live.append("<<<")
            index += 3
            continue
        if char == "<" and command[index : index + 2] == "<<":
            operator_end, delimiter, strip_tabs = parse_heredoc_delimiter(command, index + 2)
            live.append(command[index:operator_end])
            index = operator_end
            if delimiter:
                pending.append((delimiter, strip_tabs))
            continue
        if char == "\n":
            live.append(char)
            index += 1
            for delimiter, strip_tabs in pending:
                while index < length:
                    line_end = command.find("\n", index)
                    if line_end < 0:
                        line_end = length
                    line = command[index:line_end].rstrip("\r")
                    index = line_end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
            pending = []
            continue
        live.append(char)
        index += 1
    return "".join(live)


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
    if name in PATH_ARGUMENT_COMMANDS or (windows_shell_syntax() and name in {
        "get-content", "gc", "get-item", "get-childitem", "gci", "set-content", "add-content",
        "remove-item", "copy-item", "move-item", "new-item", "test-path", "resolve-path",
    }):
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


def require_git() -> str:
    git = cached_which("git")
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


class LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


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


def open_landlock_ruleset(
    workspace: Path,
    read_roots: list[str],
    *,
    write_roots: list[Path] | None = None,
    workspace_writable: bool = True,
) -> int:
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
        # Under structured-only the workspace itself is read-only for the
        # command, so the write roots below are the complete list of places a
        # command may still write.
        add_landlock_path(
            ruleset_fd, workspace, workspace_access if workspace_writable else readonly_access
        )
        for write_root in write_roots or []:
            add_landlock_path(ruleset_fd, write_root, workspace_access, required=False)
        for read_root in read_roots:
            add_landlock_path(ruleset_fd, Path(read_root), readonly_access, required=False)
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
        fd = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
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
    # Keyed on the env vars the computation reads, so repeated exec_command
    # calls skip the dozens of Path.resolve()/is_dir() syscalls while env
    # changes still invalidate the cache.
    return list(
        _guard_allow_roots_cached(
            os.environ.get("JAVA_HOME", ""),
            os.environ.get("PATH", ""),
            os.environ.get(f"{ENV_PREFIX}_EXEC_ALLOW_ROOTS", ""),
        )
    )


@functools.lru_cache(maxsize=8)
def _guard_allow_roots_cached(java_home: str, path_env: str, extra_roots: str) -> tuple[str, ...]:
    roots = set(TOOLCHAIN_READ_ROOTS)
    roots.update(OS_METADATA_READ_FILES)
    roots.update(GIT_READ_ROOTS)
    roots.update(DNS_RESOLVER_READ_ROOTS)
    roots.update(
        {
            str(Path(sys.executable).resolve().parent),
            str(Path(sys.prefix).resolve()),
            str(Path(sys.base_prefix).resolve()),
        }
    )
    if java_home:
        try:
            resolved_java_home = Path(java_home).expanduser().resolve()
        except OSError:
            pass
        else:
            roots.add(str(resolved_java_home))
    for item in path_env.split(os.pathsep):
        if not item:
            continue
        try:
            resolved = Path(item).resolve()
        except OSError:
            continue
        if resolved.is_dir() and is_default_system_path_root(resolved):
            roots.add(str(resolved))
    for item in extra_roots.split(os.pathsep):
        if not item:
            continue
        try:
            resolved = Path(item).expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir():
            roots.add(str(resolved))
    return tuple(sorted(root for root in roots if root and Path(root).is_absolute()))


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
        image_width, image_height = identify_jpeg_size(data)
        return "image/jpeg", image_width, image_height
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        image_width, image_height = identify_webp_size(data)
        return "image/webp", image_width, image_height
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


def object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def tool_output_schema(name: str | None = None) -> dict[str, Any]:
    """Declare what one tool actually returns, not just that it returns `ok`.

    One generic schema for eighteen tools told a client nothing: every field a
    caller needed — `command_id`, `exit_code`, `output_ref`, patch evidence —
    was undeclared. Each entry below adds the tool's own fields on top of the
    shared envelope. `additionalProperties` stays open because payloads still
    carry advisory fields (warnings, next_action) that are not part of the
    contract.
    """

    schema: dict[str, Any] = {
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
    if name is not None:
        schema["properties"] = {**schema["properties"], **output_schemas().get(name, {})}
    return schema


@functools.cache
def output_schemas() -> dict[str, dict[str, Any]]:
    """Return the structured result property schemas keyed by tool name."""
    string: dict[str, Any] = {"type": "string"}
    nullable_string: dict[str, Any] = {"type": ["string", "null"]}
    integer: dict[str, Any] = {"type": "integer"}
    nullable_integer: dict[str, Any] = {"type": ["integer", "null"]}
    boolean: dict[str, Any] = {"type": "boolean"}
    string_array: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    object_array: dict[str, Any] = {"type": "array", "items": {"type": "object", "additionalProperties": True}}
    truncation: dict[str, Any] = {
        "truncated": boolean,
        "truncated_by": nullable_string,
        "output_lines": integer,
        "output_bytes": integer,
        "warnings": string_array,
    }
    command_result: dict[str, Any] = {
        "command_id": string,
        "status": string,
        "operation_outcome": {**string, "enum": list(COMMAND_OUTCOMES)},
        "exit_code": nullable_integer,
        "signal": nullable_string,
        "timed_out": boolean,
        "stdout": string,
        "stderr": string,
        "stdout_truncated": boolean,
        "stderr_truncated": boolean,
        "stdout_truncated_by": nullable_string,
        "stderr_truncated_by": nullable_string,
        "stdout_output_lines": integer,
        "stderr_output_lines": integer,
        "stdout_output_bytes": integer,
        "stderr_output_bytes": integer,
        "stdout_dropped_bytes": integer,
        "stderr_dropped_bytes": integer,
        "stdout_omitted_bytes": integer,
        "stderr_omitted_bytes": integer,
        "output_ref": nullable_string,
        "output_stream": string,
        "output_refs": {"type": "object", "additionalProperties": {"type": "string"}},
        "output_truncated": boolean,
        "truncated_output_streams": string_array,
        "truncated": boolean,
        "next_action": {"type": ["object", "null"], "additionalProperties": True},
        "next_actions": object_array,
        "summary": string,
        "preview": string,
        "preview_truncated": boolean,
        "warnings": string_array,
    }
    patch_result: dict[str, Any] = {
        "dry_run": boolean,
        "clean": boolean,
        "already_applied": boolean,
        "idempotent_replay": boolean,
        "revision_algorithm": {**string, "enum": [REVISION_ALGORITHM]},
        "summary": string,
        "affected_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": string,
                    "old_path": string,
                    "operation": string,
                    "revision": string,
                    "total_lines": integer,
                    "match_quality": string,
                    "changed_ranges": object_array,
                },
                "additionalProperties": True,
            },
        },
        "additions": integer,
        "removals": integer,
        "warnings": string_array,
    }
    git_text: dict[str, Any] = {"truncated": boolean, "warnings": string_array}
    return {
        "server_info": {
            "server": string,
            "version": string,
            "workspace": string,
            "permission_mode": string,
            "workspace_mutation_policy": {"type": "object", "additionalProperties": True},
            "tools": string_array,
            "tool_count": integer,
        },
        "check_exec_environment": {
            "workspace": string,
            "permission_mode": string,
            "landlock_enabled": boolean,
            "landlock_abi": nullable_integer,
            "warnings": string_array,
        },
        "read_file": {
            "path": string,
            "content": string,
            "encoding": string,
            "revision": string,
            "revision_algorithm": {**string, "enum": [REVISION_ALGORITHM]},
            "start_line": integer,
            "end_line": integer,
            "total_lines": integer,
            "total_bytes": integer,
            "bytes_read": integer,
            "next_start_line": nullable_integer,
            "first_line_exceeds_limit": boolean,
            "line_numbers": boolean,
            **truncation,
        },
        "list_dir": {"path": string, "entries": object_array, **truncation},
        "list_files": {"path": string, "files": object_array, **truncation},
        "search_text": {
            "query": string,
            "matches": object_array,
            "total_matches": integer,
            "total_matches_exact": boolean,
            "engine": string,
            **truncation,
        },
        "apply_patch": patch_result,
        "apply_changes": patch_result,
        "exec_command": {**command_result, "elapsed_ms": integer},
        "write_stdin": command_result,
        "kill_command": {
            **command_result,
            "killed": boolean,
            "evicted": boolean,
            "signal_sent": nullable_string,
            "next_action": {
                "type": ["object", "string", "null"],
                "additionalProperties": True,
            },
        },
        # Paging a retained stream, not running one: this result has none of
        # the head-truncation fields the `truncation` block declares, and its
        # byte counts are per stream rather than one `total_bytes`.
        "read_output": {
            "command_id": string,
            "status": {**string, "enum": ["running", "exited", "terminated", "timeout"]},
            "exit_code": nullable_integer,
            "operation_outcome": {**string, "enum": list(COMMAND_OUTCOMES)},
            "output_ref": string,
            "stream_output_ref": string,
            "stream": string,
            "content": string,
            "offset": integer,
            "requested_offset": integer,
            "limit": integer,
            "next_offset": nullable_integer,
            "total_stream_bytes": integer,
            "total_retained_bytes": integer,
            "head_retained_bytes": integer,
            "retained_start_offset": integer,
            "evicted_gap_bytes": integer,
            "omitted_bytes": integer,
            "stream_dropped_bytes": integer,
            "stdout_dropped_bytes": integer,
            "stderr_dropped_bytes": integer,
            "truncated": boolean,
            "next_action": {"type": ["object", "null"], "additionalProperties": True},
            "warnings": string_array,
        },
        "git_status": {"is_repo": boolean, "branch": nullable_string, "entries": object_array, **git_text},
        "git_diff": {"diff": string, "files": object_array, "include_untracked": boolean, **git_text},
        "git_log": {"is_repo": boolean, "commits": object_array, **git_text},
        "git_show": {"content": string, "files": object_array, **git_text},
        "git_blame": {"path": string, "lines": object_array, **git_text},
        "request_permissions": {
            "status": string,
            "grant_id": nullable_string,
            "expires_at": nullable_string,
            "constraints": {"type": "object", "additionalProperties": True},
            "warnings": string_array,
        },
        "view_image": {
            "path": string,
            "mime_type": string,
            "width": nullable_integer,
            "height": nullable_integer,
            "bytes": integer,
            "resized": boolean,
            "warnings": string_array,
        },
    }


def _drop_schema_defaults(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return ``args`` without top-level arguments equal to their schema default.

    Spelling out a default (``"dry_run": false``) asks for exactly the same
    work as leaving it out, so the idempotency fingerprint must not tell the
    two apart. The type must match too: ``0`` is not the default ``false``.
    """

    properties = input_schemas().get(tool_name, {}).get("properties", {})
    normalized: dict[str, Any] = {}
    for key, value in args.items():
        spec = properties.get(key)
        if isinstance(spec, dict) and "default" in spec:
            default = spec["default"]
            if type(value) is type(default) and value == default:
                continue
        normalized[key] = value
    return normalized


def validate_arguments(tool_name: str, args: dict[str, Any]) -> None:
    schema = input_schemas()[tool_name]
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
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} is longer than {max_length}.", category="validation")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be one of {schema['enum']!r}.", category="validation")

    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be >= {minimum}.", category="validation")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be <= {maximum}.", category="validation")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} needs at least {min_items} items.", category="validation")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} holds more than {max_items} items.", category="validation")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
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


def tool_definition(name: str, *, fake_readonly: bool = False) -> dict[str, Any]:
    schemas = input_schemas()
    annotations = tool_annotations(name, fake_readonly=fake_readonly)
    return {
        "name": name,
        "title": annotations["title"],
        "description": TOOL_REGISTRY[name].description,
        "inputSchema": schemas[name],
        "outputSchema": tool_output_schema(name),
        "annotations": annotations,
    }


def tool_annotations(name: str, *, fake_readonly: bool = False) -> dict[str, Any]:
    """Return a tool's MCP annotations.

    ``fake_readonly`` serves clients that refuse to call, or prompt on every call
    to, a tool annotated as mutating, which no server-side permission mode can
    influence. It reports every tool as read-only and non-destructive even though
    `apply_patch` and `exec_command` still mutate and still execute. Only
    `tools/list` may pass it: `server_info` and the server card must keep
    reporting the real annotations so the override stays discoverable.
    """
    spec = TOOL_REGISTRY[name]
    if fake_readonly:
        return {
            "title": spec.title,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": spec.idempotent,
            "openWorldHint": False,
        }
    return {
        "title": spec.title,
        "readOnlyHint": spec.read_only,
        "destructiveHint": spec.destructive,
        "idempotentHint": spec.idempotent,
        "openWorldHint": spec.open_world,
    }


@functools.cache
def input_schemas() -> dict[str, dict[str, Any]]:
    """Return cached tool argument schemas; callers must treat the tree as read-only."""
    # Cached: callers only read the returned tree, and rebuilding the full
    # ~190-line schema dict on every tools/call dispatch is measurable.
    string = {"type": "string"}
    integer = {"type": "integer"}
    boolean = {"type": "boolean"}
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "server_info": object_schema(),
        "check_exec_environment": object_schema(),
        "read_file": object_schema(
            {
                "path": {**string, "minLength": 1},
                "start_line": {**integer, "minimum": 1, "default": 1},
                "end_line": {**integer, "minimum": 1},
                "max_lines": {**integer, "minimum": 1},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 131072},
                "encoding": {**string, "enum": ["utf-8"], "default": "utf-8"},
                "line_numbers": {
                    **boolean,
                    "default": False,
                    "description": (
                        "Prefix each line of the model-facing text with its 1-based number and a tab "
                        "(the numbers apply_changes edits use). structuredContent.content is unchanged."
                    ),
                },
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
                "idempotency_key": {
                    **string,
                    "minLength": 1,
                    "maxLength": IDEMPOTENCY_KEY_MAX_LENGTH,
                    "description": IDEMPOTENCY_KEY_DESCRIPTION,
                },
            },
            ["patch"],
        ),
        "apply_changes": object_schema(
            {
                "changes": {
                    "type": "array",
                    # No minItems: an empty array is answered by the handler with
                    # the same readable PATCH_FAILED apply_patch gives an empty
                    # envelope, rather than a transport-level invalid-params
                    # error the model cannot act on.
                    "maxItems": MAX_CHANGES_PER_CALL,
                    "description": (
                        "One entry per file, at least one. A path may appear once per call; combine "
                        "several line edits for one file into that file's single edit change. The whole "
                        "request must fit in 1 MiB, so keep it to roughly 20 files per call."
                    ),
                    "items": object_schema(
                        {
                            "action": {
                                **string,
                                "enum": list(CHANGE_ACTIONS),
                                "description": (
                                    "create writes a file that must not exist; write upserts one; edit "
                                    "applies line operations; delete removes; move and copy need a "
                                    "destination that must not exist."
                                ),
                            },
                            "path": {
                                **string,
                                "minLength": 1,
                                "description": "Workspace-relative file path; used by every action.",
                            },
                            "revision": {
                                **string,
                                "minLength": 1,
                                "description": (
                                    "The 64-character hex revision of this path, from read_file or from "
                                    "the latest apply_changes/apply_patch result for it (not a dry run). "
                                    "Line numbers in edits must come from that same version of the file. "
                                    "Required for write when the path exists, and always for edit, "
                                    "delete, move, and copy; write may omit it when creating a missing "
                                    "path, and create rejects it. A file that changed since is refused "
                                    "with REVISION_MISMATCH: re-read it and rebuild the edits."
                                ),
                            },
                            "content": {
                                **string,
                                "description": (
                                    "Full file text; used by create and write only (edit takes "
                                    "edits[].content)."
                                ),
                            },
                            "destination": {
                                **string,
                                "minLength": 1,
                                "description": (
                                    "Target path; used by move and copy only, and must not already exist."
                                ),
                            },
                            "edits": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": MAX_EDITS_PER_CHANGE,
                                "description": (
                                    "Line operations; used by action=edit only. Every line number refers "
                                    "to the file version named by revision, never to the result of "
                                    "another edit in the same call, and no two edits may address the "
                                    "same lines. replace: start_line, optional end_line, content. delete: "
                                    "start_line, optional end_line. insert_after / insert_before: line, "
                                    "content. See the numbering field descriptions for accepted aliases."
                                ),
                                "items": object_schema(
                                    {
                                        "op": {
                                            **string,
                                            "enum": list(EDIT_OPERATIONS),
                                            "description": (
                                                "replace and delete address start_line..end_line; "
                                                "insert_after and insert_before address line."
                                            ),
                                        },
                                        "start_line": {
                                            **integer,
                                            "minimum": 1,
                                            "description": (
                                                "Used by replace and delete: first line of the range, "
                                                "1-based and inclusive. If line is also supplied it "
                                                "must equal start_line; end_line may extend the range. "
                                                "insert_after and insert_before accept it in place of "
                                                "line; both must agree when supplied together."
                                            ),
                                        },
                                        "end_line": {
                                            **integer,
                                            "minimum": 1,
                                            "description": (
                                                "Used by replace and delete: last line of the range, "
                                                "inclusive and >= start_line; defaults to start_line. "
                                                "With line but no start_line, it must equal line. "
                                                "insert_after and insert_before accept it only when "
                                                "equal to their line or start_line anchor."
                                            ),
                                        },
                                        "line": {
                                            **integer,
                                            "minimum": 0,
                                            "description": (
                                                "Used by insert_after (0 to total_lines, where 0 "
                                                "inserts at the beginning) and insert_before (1 to "
                                                "total_lines + 1, where total_lines + 1 appends). "
                                                "replace and delete accept it as shorthand for one "
                                                "line: without start_line, end_line must be omitted or "
                                                "equal line. With explicit start_line, line must equal "
                                                "start_line and end_line may extend the range."
                                            ),
                                        },
                                        "content": {
                                            **string,
                                            "description": (
                                                "Used by replace, insert_after, and insert_before "
                                                "(required); not accepted for delete. Whole lines: \"\" "
                                                "is zero lines, which makes replace with empty content "
                                                "a deletion; a trailing newline adds a blank line."
                                            ),
                                        },
                                    },
                                    ["op"],
                                ),
                            },
                        },
                        ["action", "path"],
                    ),
                },
                "dry_run": {**boolean, "default": False},
                "idempotency_key": {
                    **string,
                    "minLength": 1,
                    "maxLength": IDEMPOTENCY_KEY_MAX_LENGTH,
                    "description": IDEMPOTENCY_KEY_DESCRIPTION,
                },
            },
            ["changes"],
        ),
        "exec_command": object_schema(
            {
                "cmd": {**string, "minLength": 1},
                "workdir": {**string, "default": "."},
                "cwd": {**string},
                "timeout_ms": {
                    **integer,
                    "minimum": 1,
                    "maximum": MAX_PROCESS_LIFETIME_MS,
                    "default": DEFAULT_PROCESS_LIFETIME_MS,
                    "description": (
                        "Total process lifetime in milliseconds. The command is killed when this "
                        "elapses, whether or not the call has already returned."
                    ),
                },
                "yield_time_ms": {
                    **integer,
                    "minimum": 0,
                    "maximum": MAX_YIELD_MS,
                    "default": DEFAULT_YIELD_MS,
                    "description": (
                        "How long this call waits before returning. A command still running at that "
                        "point keeps running and returns a command_id; it is not killed."
                    ),
                },
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "verbosity": {**string, "enum": ["summary", "preview", "full"]},
                "preview_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
                "stdin": {
                    **string,
                    "default": "",
                    "description": (
                        "One-shot input written to the command's stdin at start. stdin is then closed "
                        "(EOF) unless keep_stdin_open is true."
                    ),
                },
                "tty": {
                    **boolean,
                    "default": False,
                    "description": (
                        "Run under a real pseudo-terminal (POSIX only; Windows returns TTY_UNSUPPORTED). "
                        "stdin stays writable with write_stdin."
                    ),
                },
                "keep_stdin_open": {
                    **boolean,
                    "default": False,
                    "description": (
                        "Keep the stdin pipe open so write_stdin can send input to a non-tty command "
                        "(works on Windows). Default false closes stdin so commands that read it get EOF "
                        "instead of hanging."
                    ),
                },
                "env": {"type": "object", "additionalProperties": {"type": "string"}, "default": {}},
            },
            ["cmd"],
        ),
        "write_stdin": object_schema(
            {
                "command_id": {**string, "minLength": 1},
                "chars": {
                    **string,
                    "default": "",
                    "description": (
                        "Empty polls for output. Non-empty is written to stdin and requires a command "
                        "started with keep_stdin_open=true or tty=true; otherwise COMMAND_CLOSED."
                    ),
                },
                "yield_time_ms": {**integer, "minimum": 0, "maximum": MAX_YIELD_MS, "default": DEFAULT_YIELD_MS},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "verbosity": {**string, "enum": ["summary", "preview", "full"]},
                "preview_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
            },
            ["command_id"],
        ),
        "kill_command": object_schema(
            {
                "command_id": {**string, "minLength": 1},
                "signal": {**string, "enum": ["TERM", "KILL", "INT"], "default": "TERM"},
                "wait_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 5000},
                "kill_wait_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 2000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "verbosity": {**string, "enum": ["summary", "preview", "full"]},
                "preview_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
            },
            ["command_id"],
        ),
        "read_output": object_schema(
            {
                "output_ref": {
                    **string,
                    "minLength": 1,
                    "description": (
                        "command:<id>:stdout or command:<id>:stderr from output_refs, or a bare command_id "
                        "or command:<id> (stream then selects the stream)."
                    ),
                },
                "stream": {
                    **string,
                    "enum": ["stdout", "stderr"],
                    "description": (
                        "Stream for a bare command_id or command:<id> (default stdout). With a full ref it "
                        "must match the ref's stream."
                    ),
                },
                "offset": {**integer, "minimum": 0, "default": 0},
                "limit": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
            },
            ["output_ref"],
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
                "include_untracked": {
                    **boolean,
                    "default": True,
                    "description": (
                        "Diff untracked files against an empty file so newly created files appear "
                        "as additions. Applies to the unstaged pass only."
                    ),
                },
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
    names = runtime.exposed_tool_names()
    # Always the real annotations, never the tools/list override: this card is
    # what an operator fetches to find out what the endpoint actually does.
    annotations = {name: tool_annotations(name, fake_readonly=False) for name in names}
    read_only = [name for name in names if annotations[name].get("readOnlyHint") is True]
    mutating = [name for name in names if annotations[name].get("readOnlyHint") is not True]
    payload = {
        "supportedProtocolVersions": list(KNOWN_PROTOCOL_VERSIONS),
        "server": {
            "name": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": __version__,
        },
        "transport": {
            "type": "streamable_http",
            "endpoint": MCP_ENDPOINT_PATH,
            "methods": ["POST", "OPTIONS"],
        },
        "auth": _server_card_auth(runtime, oauth_base_url=oauth_base_url),
        "tools": {
            "count": len(names),
            "names": names,
            "readOnlyHintTrue": read_only,
            "readOnlyHintFalse": mutating,
            "annotationOverride": ("fake_readonly" if runtime.fake_readonly_annotations else None),
        },
        "capabilities": {
            "tools": {"listChanged": False},
        },
    }
    return payload


# The headers a modern request mirrors its body in, each of which may appear
# exactly once.
MIRROR_HEADERS = ("MCP-Protocol-Version", "Mcp-Method", "Mcp-Name")

# A modern client reads the HTTP status as well as the JSON-RPC error, so the
# protocol errors that name a fault in the request are reported as such. Every
# other code — including -32603, which says the request was fine and we were
# not — stays a 200 carrying a JSON-RPC error, as the legacy era always does.
MODERN_ERROR_STATUSES = {
    -32601: 404,
    -32602: 400,
    HEADER_MISMATCH: 400,
    UNSUPPORTED_PROTOCOL_VERSION: 400,
}


def rpc_response_status(era: str, response: dict[str, Any]) -> int:
    if era != MODERN_ERA:
        return 200
    error = response.get("error")
    if not isinstance(error, dict):
        return 200
    return MODERN_ERROR_STATUSES.get(error.get("code"), 200)


class MCPHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"CodingToolsMCP/{__version__}"

    @property
    def runtime(self) -> Runtime:
        return cast(Runtime, self.server.runtime)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args, file=sys.stderr)

    def send_rpc_error(
        self,
        code: int,
        message: str,
        *,
        status: int = 400,
        request_id: str | int | None = None,
        data: Any = None,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_json(
            jsonrpc_error(request_id, code, message, data),
            status=status,
            extra_headers=extra_headers,
            head_only=head_only,
        )

    def do_GET(self) -> None:
        self.handle_metadata_request(head_only=False)

    def do_HEAD(self) -> None:
        self.handle_metadata_request(head_only=True)

    def do_DELETE(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if posixpath.normpath(request_path) != MCP_ENDPOINT_PATH:
            self.send_json({"error": "Unknown endpoint"}, status=404)
            return
        if not self.is_authorized():
            self.send_unauthorized()
            return
        # There is no session to terminate: every request is served by the one
        # workspace runtime, which outlives any single client.
        self.send_rpc_error(
            -32601,
            "DELETE is not supported: this endpoint has no sessions to terminate",
            status=405,
            extra_headers={"Allow": "POST"},
        )

    def do_OPTIONS(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if posixpath.normpath(request_path) not in {
            MCP_ENDPOINT_PATH,
            "/.well-known/mcp.json",
            "/.well-known/mcp/server-card.json",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/oauth/authorize",
            "/oauth/token",
            "/oauth/register",
        }:
            self.send_json({"error": "Unknown endpoint"}, status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not is_allowed_origin(origin):
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
        if normalized == MCP_ENDPOINT_PATH:
            origin = self.headers.get("Origin")
            if origin and not is_allowed_origin(origin):
                self.send_json({"error": "Origin denied"}, status=403, head_only=head_only)
                return
            if not self.is_authorized():
                self.send_unauthorized(head_only=head_only)
                return
            self.send_rpc_error(
                -32000,
                "SSE GET stream is not supported",
                status=405,
                extra_headers={"Allow": "POST"},
                head_only=head_only,
            )
            return
        if normalized in {"/.well-known/mcp.json", "/.well-known/mcp/server-card.json"}:
            self.send_json(server_card_payload(self.runtime, oauth_base_url=self.oauth_base_url()), head_only=head_only)
            return
        self.send_json({"error": "Unknown endpoint"}, status=404, head_only=head_only)

    def do_POST(self) -> None:
        request_path = self.path.split("?", 1)[0]
        normalized = posixpath.normpath(request_path)
        if normalized == "/oauth/authorize":
            self.handle_oauth_authorize_post()
            return
        if normalized == "/oauth/token":
            self.handle_oauth_token()
            return
        if normalized == "/oauth/register":
            self.handle_oauth_register()
            return
        if normalized != MCP_ENDPOINT_PATH:
            self.send_rpc_error(-32601, "Unknown endpoint", status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not is_allowed_origin(origin):
            self.send_rpc_error(-32600, "Origin denied", status=403)
            return
        if not self.is_authorized():
            self.send_unauthorized()
            return
        if self.headers.get_content_type().lower() != "application/json":
            self.send_rpc_error(-32600, "Content-Type must be application/json", status=415)
            return
        protocol_version = self.headers.get("MCP-Protocol-Version")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_rpc_error(-32600, "Content-Length is required", status=411)
            return
        try:
            length = int(raw_length)
        except ValueError:
            self.send_rpc_error(-32600, "Content-Length must be a non-negative integer")
            return
        if length < 0:
            self.send_rpc_error(-32600, "Content-Length must be a non-negative integer")
            return
        if length > MAX_HTTP_REQUEST_BYTES:
            self.close_connection = True
            self.send_rpc_error(
                -32600,
                "Request body exceeds maximum size",
                status=413,
                data={"max_bytes": MAX_HTTP_REQUEST_BYTES},
            )
            return
        body = self.rfile.read(length)
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            # RecursionError included: a deeply nested document is a document
            # this server cannot parse, not a reason to unwind the handler.
            self.send_rpc_error(-32700, "Parse error")
            return
        if isinstance(request, list):
            self.send_rpc_error(-32600, "JSON-RPC batch requests are not supported by Streamable HTTP")
            return
        if not isinstance(request, dict):
            self.send_rpc_error(-32600, "Invalid Request")
            return
        try:
            validate_rpc_envelope(request)
        except JsonRpcError as exc:
            self.send_rpc_error(
                exc.code, exc.message, status=200, request_id=response_id(request), data=exc.data
            )
            return
        # Every request is served by the one workspace runtime, and a client
        # that still echoes an ``Mcp-Session-Id`` from an older server is
        # served like any other rather than rejected. What the request must
        # carry beyond that depends on its era, which only its body can decide.
        method = str(request["method"])
        raw_params = request.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        era = request_era(method, params)
        if era == MODERN_ERA:
            duplicate = self.duplicated_mirror_header()
            if duplicate is not None:
                self.send_rpc_error(
                    HEADER_MISMATCH,
                    f"{duplicate} must appear exactly once",
                    request_id=response_id(request),
                    data={"header": duplicate, "reason": "duplicate"},
                )
                return
            # The body's own contract comes first: a version this server does
            # not speak, or a mistyped ``_meta`` field, is the same fault here
            # as it is over stdio, and mirror headers that faithfully repeat a
            # wrong body must not answer for it instead. A notification is
            # exempt — nothing may be sent back for one — and is left to the
            # dispatcher, which stays silent.
            if "id" in request:
                try:
                    validate_modern_meta(params)
                except JsonRpcError as exc:
                    self.send_rpc_error(
                        exc.code,
                        exc.message,
                        status=MODERN_ERROR_STATUSES.get(exc.code, 200),
                        request_id=response_id(request),
                        data=exc.data,
                    )
                    return
        elif protocol_version and not protocol_version_is_known(protocol_version):
            # A handshake-era request naming a version from neither era: the
            # body cannot decide it, so the transport refuses it and offers
            # everything this server speaks.
            self.send_rpc_error(
                -32600,
                "Unsupported MCP protocol version",
                data={"supported": list(KNOWN_PROTOCOL_VERSIONS), "received": protocol_version},
            )
            return
        try:
            validate_mirror_headers(
                era,
                method,
                params,
                version_header=protocol_version,
                method_header=self.headers.get("Mcp-Method"),
                name_header=self.headers.get("Mcp-Name"),
            )
        except JsonRpcError as exc:
            self.send_rpc_error(exc.code, exc.message, request_id=response_id(request), data=exc.data)
            return
        response = self.handle_rpc(request, transport_protocol_version=protocol_version)
        if response is None:
            self.send_response(202)
            self.send_cors_headers()
            self.end_headers()
            return
        self.send_json(response, status=rpc_response_status(era, response))

    def duplicated_mirror_header(self) -> str | None:
        """Name the first mirror header that was sent more than once, if any.

        A gateway routes on these headers alone, and which of two values it
        reads is its own business, so a request that states its version,
        method, or subject twice has no single mirror to check the body
        against and is refused rather than resolved.
        """

        for header in MIRROR_HEADERS:
            if len(self.headers.get_all(header) or ()) > 1:
                return header
        return None

    def handle_rpc(
        self,
        request: dict[str, Any],
        *,
        transport_protocol_version: str | None = None,
    ) -> dict[str, Any] | None:
        legacy_version = (
            transport_protocol_version
            if legacy_protocol_version_is_supported(transport_protocol_version)
            else None
        )
        try:
            return dispatch_rpc(self.runtime, request, transport_protocol_version=legacy_version)
        except Exception as exc:  # noqa: BLE001 - HTTP must always answer with JSON-RPC
            return jsonrpc_error(response_id(request), -32603, str(exc))

    def is_authorized(self) -> bool:
        if not self.runtime.auth_enabled():
            return True
        header = self.headers.get("Authorization", "").strip()
        if self.runtime.auth_token is not None:
            if secrets.compare_digest(header, f"Bearer {self.runtime.auth_token}"):
                return True
        if self.runtime.oauth_config is not None and header.startswith("Bearer "):
            token = header[len("Bearer "):]
            if validate_access_token(token, self.runtime.oauth_config, self.oauth_base_url()):
                return True
        return False

    def oauth_base_url(self) -> str:
        cfg = self.runtime.oauth_config
        if cfg is not None and cfg.server_url:
            return cfg.server_url.rstrip("/")
        trust_proxy = truthy_env(os.environ.get(f"{ENV_PREFIX}_TRUST_PROXY_HEADERS"))
        proto = _first_header_value(self.headers.get("X-Forwarded-Proto")) if trust_proxy else ""
        if trust_proxy and not proto:
            proto = _forwarded_header_param(self.headers.get("Forwarded"), "proto")
        host = _safe_external_host(_first_header_value(self.headers.get("X-Forwarded-Host"))) if trust_proxy else ""
        if trust_proxy and not host:
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
        self.send_rpc_error(
            -32000,
            "Unauthorized",
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
                "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": list(OAUTH_RESPONSE_TYPES_SUPPORTED),
                "grant_types_supported": list(OAUTH_GRANT_TYPES_SUPPORTED),
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": list(OAUTH_TOKEN_AUTH_METHODS),
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
            {"resource": base, "authorization_servers": [base], "bearer_methods_supported": ["header"]},
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

    def _oauth_login_page(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        state: str,
        resource: str,
        error: str = "",
    ) -> str:
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
            f"<input type='hidden' name='resource' value='{esc(resource)}'>"
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
        _p = functools.partial(_first_form_value, params)
        client_id = _p("client_id")
        redirect_uri = _p("redirect_uri")
        code_challenge = _p("code_challenge")
        code_challenge_method = _p("code_challenge_method")
        state = _p("state")
        resource = _p("resource")

        if _p("response_type") != "code":
            self._send_html("<h2>Error</h2><p>response_type must be 'code'</p>", status=400)
            return
        if cfg.registry.get(client_id) is None:
            self._send_html("<h2>Error</h2><p>Unknown client_id</p>", status=400)
            return
        if not cfg.registry.accepts_redirect(client_id, redirect_uri):
            self._send_html("<h2>Error</h2><p>redirect_uri is not registered for this client</p>", status=400)
            return
        if code_challenge_method != "S256" or not valid_pkce_challenge(code_challenge):
            self._send_html("<h2>Error</h2><p>code_challenge_method must be S256 and code_challenge is required</p>", status=400)
            return
        if resource.rstrip("/") != self.oauth_base_url():
            self._send_html("<h2>Error</h2><p>resource must identify this MCP server</p>", status=400)
            return

        self._send_html(self._oauth_login_page(
            client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
            code_challenge_method=code_challenge_method, state=state, resource=resource,
        ))

    def handle_oauth_authorize_post(self) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404)
            return
        body = self._read_oauth_body()
        if body is None:
            return
        if self.headers.get_content_type().lower() != "application/x-www-form-urlencoded":
            self.send_json({"error": "invalid_request", "error_description": "Content-Type must be application/x-www-form-urlencoded"}, status=400)
            return
        params = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        _p = functools.partial(_first_form_value, params)
        client_id = _p("client_id")
        redirect_uri = _p("redirect_uri")
        code_challenge = _p("code_challenge")
        code_challenge_method = _p("code_challenge_method")
        state = _p("state")
        resource = _p("resource")
        password = _p("password")

        def fail(error: str, status: int = 400) -> None:
            self._send_html(self._oauth_login_page(
                client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
                code_challenge_method=code_challenge_method, state=state, resource=resource,
                error=error,
            ), status=status)

        if cfg.registry.get(client_id) is None or not cfg.registry.accepts_redirect(client_id, redirect_uri):
            fail("Invalid client or redirect URI")
            return
        if code_challenge_method != "S256" or not valid_pkce_challenge(code_challenge):
            fail("Invalid PKCE parameters")
            return
        if resource.rstrip("/") != self.oauth_base_url():
            fail("Invalid resource")
            return
        if not secrets.compare_digest(password, cfg.password):
            fail("Invalid password", status=401)
            return

        code = secrets.token_urlsafe(32)
        now = time.time()
        with cfg.pending_codes_lock:
            expired = [k for k, v in cfg.pending_codes.items() if v["expires_at"] < now]
            for k in expired:
                del cfg.pending_codes[k]
            while len(cfg.pending_codes) >= MAX_PENDING_CODES:
                cfg.pending_codes.pop(next(iter(cfg.pending_codes)))
            cfg.pending_codes[code] = {
                "code_challenge": code_challenge,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "expires_at": now + OAUTH_CODE_TTL_SECONDS,
                "server_url": self.oauth_base_url(),
                "resource": resource.rstrip("/"),
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
        _p = functools.partial(_first_form_value, params)
        grant_type = _p("grant_type")
        code = _p("code")
        redirect_uri = _p("redirect_uri")
        code_verifier = _p("code_verifier")
        client_id = _p("client_id")
        client_secret = _p("client_secret")
        resource = _p("resource").rstrip("/")
        presented_auth_method = "client_secret_post" if client_secret else "none"

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
                presented_auth_method = "client_secret_basic"
            except Exception:  # noqa: BLE001
                pass

        if grant_type != OAUTH_GRANT_TYPE_AUTHORIZATION_CODE:
            _err("unsupported_grant_type", "Only authorization_code is supported")
            return
        if cfg.registry.get(client_id) is None:
            _err("invalid_client", "Unknown client_id")
            return
        if not cfg.registry.authenticates(client_id, client_secret, presented_auth_method):
            _err("invalid_client", "Invalid client_secret")
            return
        if not code:
            _err("invalid_grant", "code is required")
            return
        if not code_verifier or not (43 <= len(code_verifier) <= 128) or not re.fullmatch(r"[A-Za-z0-9\-._~]+", code_verifier):
            _err("invalid_grant", "Invalid code_verifier")
            return

        with cfg.pending_codes_lock:
            code_data = cfg.pending_codes.pop(code, None)

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
        if not resource or not secrets.compare_digest(str(code_data.get("resource") or ""), resource):
            _err("invalid_target", "resource mismatch")
            return
        if not verify_pkce(code_verifier, code_data["code_challenge"]):
            _err("invalid_grant", "PKCE verification failed")
            return

        server_url = resource
        access_token = create_access_token(cfg, server_url, client_id=client_id)
        self.send_json({"access_token": access_token, "token_type": "Bearer", "expires_in": cfg.token_ttl})

    def handle_oauth_register(self) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404)
            return
        body = self._read_oauth_body()
        if body is None:
            return
        if self.headers.get_content_type().lower() != "application/json":
            self.send_json({"error": "invalid_client_metadata", "error_description": "Content-Type must be application/json"}, status=400)
            return
        try:
            metadata = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "invalid_client_metadata", "error_description": "Body must be valid JSON"}, status=400)
            return
        if not isinstance(metadata, dict):
            self.send_json({"error": "invalid_client_metadata", "error_description": "Metadata must be an object"}, status=400)
            return
        try:
            registered = cfg.registry.register(metadata)
        except ValueError as exc:
            self.send_json({"error": "invalid_client_metadata", "error_description": str(exc)}, status=400)
            return
        self.send_json(registered, status=201)

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and is_allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Accept, Authorization, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name",
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
        self.send_header("Cache-Control", "no-store")
        self.send_cors_headers()
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


class RuntimeHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[MCPHandler],
        runtime: Runtime,
    ) -> None:
        super().__init__(address, handler)
        self.runtime = runtime

    def server_close(self) -> None:
        self.runtime.close()
        super().server_close()


def build_runtime(
    args: argparse.Namespace,
    runtime_policy: RuntimePolicy,
    *,
    auth_token: str | None = None,
    oauth_config: OAuthConfig | None = None,
    emit_warning: bool = True,
    project_context: ProjectContext | None = None,
    transport: str = "stdio",
    command_manager: WorkspaceCommandManager | None = None,
) -> Runtime:
    workspace = Path(args.workspace or os.environ.get(f"{ENV_PREFIX}_WORKSPACE") or os.getcwd())
    runtime = Runtime(
        workspace,
        enable_view_image=args.enable_view_image,
        permission_mode=runtime_policy.permission_mode,
        shell_env_policy=runtime_policy.shell_env_policy,
        allow_network=runtime_policy.allow_network,
        auth_token=auth_token,
        oauth_config=oauth_config,
        project_context=project_context,
        fake_readonly_annotations=runtime_policy.fake_readonly_annotations,
        workspace_mutation=runtime_policy.workspace_mutation,
        transport=transport,
        command_manager=command_manager,
        isolation=runtime_policy.isolation,
    )
    if emit_warning and runtime.capabilities.skip_all_permissions:
        print(
            "WARNING: permission_mode=dangerous disables MCP safety gates. Use only inside an isolated container or VM.",
            file=sys.stderr,
        )
    mutation = runtime.workspace_mutation_payload()
    if emit_warning and runtime.workspace_mutation.structured_only and not mutation["enforced"]:
        for warning in mutation.get("warnings", []):
            print(f"WARNING: {warning}.", file=sys.stderr)
    if emit_warning and runtime.fake_readonly_annotations:
        print(
            "WARNING: tools/list reports every tool as read-only and non-destructive. "
            "apply_patch and exec_command still mutate the workspace and still run commands. "
            "server_info and the server card keep reporting the real annotations.",
            file=sys.stderr,
        )
    return runtime


AUTH_MODE_CHOICES = ("bearer", "noauth", "oauth")


def run_http(args: argparse.Namespace) -> int:
    auth_mode = (os.environ.get(f"{ENV_PREFIX}_AUTH_MODE") or "").strip().lower()
    if auth_mode and auth_mode not in AUTH_MODE_CHOICES:
        supported = ", ".join(AUTH_MODE_CHOICES)
        print(f"ERROR: {ENV_PREFIX}_AUTH_MODE must be one of: {supported}.", file=sys.stderr)
        return 2
    auth_token = args.auth_token or os.environ.get(f"{ENV_PREFIX}_AUTH_TOKEN") or None
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
        password = env_password or secrets.token_urlsafe(32)
        server_url = (os.environ.get(f"{ENV_PREFIX}_SERVER_URL") or "").rstrip("/") or None
        if not env_password:
            print(f"OAuth authorize password: {password}", file=sys.stderr)
        raw_secret = os.environ.get(f"{ENV_PREFIX}_OAUTH_TOKEN_SECRET") or ""
        if raw_secret:
            try:
                token_secret = bytes.fromhex(raw_secret)
            except ValueError:
                print(
                    f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_SECRET must be hex-encoded bytes.",
                    file=sys.stderr,
                )
                return 2
            if len(token_secret) < 32:
                print(
                    f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_SECRET must contain at least 32 bytes.",
                    file=sys.stderr,
                )
                return 2
        else:
            token_secret = secrets.token_bytes(32)
        try:
            token_ttl = int(os.environ.get(f"{ENV_PREFIX}_OAUTH_TOKEN_TTL") or OAUTH_TOKEN_TTL_SECONDS)
        except ValueError:
            print(f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_TTL must be an integer.", file=sys.stderr)
            return 2
        if not 60 <= token_ttl <= 604_800:
            print(f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_TTL must be between 60 and 604800 seconds.", file=sys.stderr)
            return 2
        oauth_config = OAuthConfig(
            password=password,
            server_url=server_url,
            token_secret=token_secret,
            token_ttl=token_ttl,
        )
        if client_id:
            raw_redirects = os.environ.get(f"{ENV_PREFIX}_OAUTH_REDIRECT_URIS") or "http://127.0.0.1/callback"
            redirect_uris = tuple(item.strip() for item in raw_redirects.split(",") if item.strip())
            try:
                oauth_config.registry.add_preregistered(
                    client_id,
                    redirect_uris,
                    client_secret=client_secret,
                )
            except ValueError as exc:
                print(f"ERROR: invalid OAuth redirect URI configuration: {exc}", file=sys.stderr)
                return 2
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

    # A tunnel forwards to a loopback bind, so the bind host cannot tell a private
    # sandbox apart from a publicly reachable one. Gate on authentication instead:
    # over HTTP, only callers the operator admitted may be told a false catalog.
    if runtime_policy.fake_readonly_annotations and not auth_token and not oauth_config:
        print(
            "ERROR: --dangerously-fake-readonly-annotations over HTTP requires --auth-token, "
            f"{ENV_PREFIX}_AUTH_TOKEN, or --oauth-mode. "
            "Use stdio for an unauthenticated local sandbox.",
            file=sys.stderr,
        )
        return 2

    try:
        runtime = build_runtime(
            args,
            runtime_policy,
            auth_token=auth_token,
            oauth_config=oauth_config,
            transport="http",
        )
    except ToolFailure as exc:
        print(f"ERROR: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    server = RuntimeHTTPServer((args.host, args.port), MCPHandler, runtime)
    if oauth_config:
        url_label = oauth_config.server_url or "dynamic request URL"
        suffix = " + bearer" if runtime.auth_token else ""
        auth_label = f"oauth2{suffix} enabled (server_url={url_label})"
    elif runtime.auth_token:
        auth_label = "bearer auth enabled"
    else:
        auth_label = "no auth configured"
    base_url = _http_base_for_bind_host(str(args.host), args.port)
    print(f"{SERVER_NAME} listening on {base_url}/mcp ({auth_label})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def run_stdio(args: argparse.Namespace) -> int:
    try:
        runtime_policy = runtime_policy_from_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        runtime = build_runtime(args, runtime_policy)
    except ToolFailure as exc:
        print(f"ERROR: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    return serve_stdio(runtime)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve workspace-confined coding tools over MCP.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("--workspace", help="workspace root; defaults to CODING_TOOLS_MCP_WORKSPACE or cwd")
    parser.add_argument(
        "--host",
        default=os.environ.get(f"{ENV_PREFIX}_HOST") or "127.0.0.1",
        help=f"bind host; defaults to {ENV_PREFIX}_HOST or 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_int(f"{ENV_PREFIX}_PORT", 8000),
        help=f"bind port; defaults to {ENV_PREFIX}_PORT or 8000",
    )
    parser.add_argument("--execution-isolation", choices=ISOLATION_MODES, default=None,
                        help="strict requires native file/network isolation and fails closed; compatibility preserves legacy modes")
    parser.add_argument("--sandbox-network", choices=NETWORK_MODES, default=None,
                        help="strict network policy; unsupported capabilities fail closed")
    parser.add_argument("--sandbox-allow-destination", action="append", default=None, help="exact HOST:PORT allowed through enforced proxy; repeatable")
    parser.add_argument("--sandbox-helper", default=None, help="absolute trusted native-helper installation path")
    parser.add_argument("--sandbox-helper-sha256", default=None, help="pinned SHA-256 of the native helper")
    parser.add_argument("--sandbox-read-root", action="append", default=None, help="explicit additional trusted toolchain read root")
    parser.add_argument("--sandbox-deny-root", action="append", default=None, help="service-private path hidden from tools and commands")
    parser.add_argument("--stdio", action="store_true", help="serve newline-delimited JSON-RPC over stdio")
    parser.add_argument(
        "--auth-token",
        default=None,
        help=f"require Authorization: Bearer <token> on /mcp; defaults to {ENV_PREFIX}_AUTH_TOKEN",
    )
    parser.add_argument(
        "--oauth-mode",
        action="store_true",
        default=False,
        help=(
            "enable OAuth 2.1 Authorization Code + PKCE; "
            f"{ENV_PREFIX}_SERVER_URL is optional; when unset OAuth metadata uses the request host; "
            "authorize password is generated when unset; RFC 7591 dynamic registration is enabled"
        ),
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
        "--workspace-mutation",
        choices=WORKSPACE_MUTATION_CHOICES,
        default=None,
        help=(
            "who may write to the workspace: unrestricted (default) lets exec_command write; "
            "structured-only makes the workspace read-only for commands under Landlock, leaving "
            "apply_patch and apply_changes as the only way in. Experimental: it breaks any command "
            "that writes into the tree (pytest caches, npm, cargo, gradle, git) unless every such "
            f"directory is listed with --write-path; also settable with {ENV_PREFIX}_WORKSPACE_MUTATION"
        ),
    )
    parser.add_argument(
        "--write-path",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "workspace-relative directory that stays writable under "
            "--workspace-mutation structured-only; repeatable, and also settable as an "
            f"os.pathsep-separated {ENV_PREFIX}_WRITE_PATHS"
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
    parser.add_argument(
        "--dangerously-fake-readonly-annotations",
        action="store_true",
        help=(
            "report every tool in tools/list as read-only and non-destructive for clients that gate on "
            "annotations; mutation and execution still happen; requires --permission-mode dangerous, and "
            "requires auth over HTTP; server_info and the server card keep reporting the real annotations; "
            f"can also be enabled with {ENV_PREFIX}_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS=1"
        ),
    )
    return parser


def install_sigterm_handler() -> None:
    """Exit cleanly on SIGTERM (128 + 15), matching the KeyboardInterrupt path.

    Essential as PID 1 in a container: without a handler the kernel ignores
    SIGTERM for init, so `docker stop` hangs for its grace period and then
    SIGKILLs the server instead of letting it shut down.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _terminate(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    install_sigterm_handler()
    return run_stdio(args) if args.stdio else run_http(args)


if __name__ == "__main__":
    raise SystemExit(main())
