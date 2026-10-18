"""Immutable, per-launch authority for workspace processes and structured files.

Strict isolation is an explicit additive setting. Compatibility retains the
previous permission modes and must never be reported as strict containment.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ToolFailure

ISOLATION_MODES = ("compatibility", "strict")
NETWORK_MODES = ("offline", "proxy")


@dataclass(frozen=True)
class IsolationConfig:
    mode: str = "compatibility"
    network: str = "offline"
    read_roots: tuple[Path, ...] = ()
    deny_roots: tuple[Path, ...] = ()
    helper_path: Path | None = None
    helper_sha256: str | None = None
    allowed_destinations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in ISOLATION_MODES or self.network not in NETWORK_MODES:
            raise ToolFailure("INVALID_ARGUMENT", "Invalid isolation or network mode.", category="validation")
        if self.helper_path is not None and not self.helper_path.is_absolute():
            raise ToolFailure("INVALID_ARGUMENT", "The native helper path must be absolute.", category="validation")
        if self.allowed_destinations:
            from .network_proxy import parse_destination
            try:
                tuple(parse_destination(value) for value in self.allowed_destinations)
            except ValueError as exc:
                raise ToolFailure("INVALID_ARGUMENT", f"Invalid proxy destination: {exc}", category="validation") from exc
        if self.helper_sha256 is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", self.helper_sha256):
            raise ToolFailure("INVALID_ARGUMENT", "The helper SHA-256 must contain 64 hexadecimal characters.", category="validation")

    @classmethod
    def from_args(cls, args: Any) -> IsolationConfig:
        prefix = "CODING_TOOLS_MCP_"

        def value(name: str, default: str = "") -> str:
            return str(getattr(args, name.lower(), None) or os.environ.get(prefix + name) or default)

        def roots(name: str) -> tuple[Path, ...]:
            entries = getattr(args, name.lower(), None)
            if entries is None:
                entries = [p for p in os.environ.get(prefix + name + "S", "").split(os.pathsep) if p]
            try:
                return tuple(Path(p).expanduser().absolute() for p in entries)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ToolFailure("INVALID_ARGUMENT", f"A configured {name.lower()} path cannot be expanded.",
                                  category="validation") from exc

        helper = value("SANDBOX_HELPER")
        return cls(
            mode=value("EXECUTION_ISOLATION", "compatibility"),
            network=value("SANDBOX_NETWORK", "offline"),
            read_roots=roots("SANDBOX_READ_ROOT"),
            deny_roots=roots("SANDBOX_DENY_ROOT"),
            helper_path=Path(helper) if helper else None,
            helper_sha256=value("SANDBOX_HELPER_SHA256") or None,
            allowed_destinations=tuple(getattr(args, "sandbox_allow_destination", None) or filter(None, value("SANDBOX_ALLOW_DESTINATIONS").split(","))),
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    workspace: Path
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    deny_roots: tuple[Path, ...]
    mode: str = "compatibility"
    network: str = "offline"
    purpose: str = "command"
    helper_path: Path | None = None
    helper_sha256: str | None = None
    allowed_destinations: tuple[str, ...] = ()

    @property
    def strict(self) -> bool:
        return self.mode == "strict"

    def permits_read(self, path: Path) -> bool:
        target = path.resolve(strict=False)
        return not any(target.is_relative_to(root) for root in self.deny_roots) and any(
            target.is_relative_to(root) for root in self.read_roots + self.write_roots
        )


def system_read_roots() -> tuple[Path, ...]:
    """Toolchain roots, never inferred from workspace configuration or PATH."""
    names = (
        "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/ld.so.cache",
        "/etc/alternatives", "/etc/localtime", "/etc/ssl/certs", "/etc/ca-certificates",
        "/System", "/Library/Apple", "/private/etc/localtime",
    )
    roots = [Path(name).resolve() for name in names if Path(name).exists()]
    # The interpreter running this installed service is a static administrator
    # choice, unlike PATH/JAVA_HOME inherited from a caller or repository.
    roots.extend(Path(p).resolve() for p in (sys.prefix, sys.base_prefix))
    return tuple(dict.fromkeys(roots))


def compile_policy(
    config: IsolationConfig,
    workspace: Path,
    runtime_dir: Path,
    *,
    purpose: str = "command",
    structured_only: bool = False,
    write_paths: tuple[Path, ...] = (),
    service_roots: tuple[Path, ...] = (),
) -> ExecutionPolicy:
    if purpose not in {"command", "read-helper"}:
        raise ValueError("Unknown process purpose")

    def resolve_root(root: Path, *, must_exist: bool = False) -> Path:
        try:
            return root.resolve(strict=must_exist)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolFailure("INVALID_ARGUMENT", "A configured sandbox root cannot be resolved.",
                              category="validation", details={"path": str(root)}) from exc

    workspace = resolve_root(workspace, must_exist=True)
    runtime_dir = resolve_root(runtime_dir)
    if runtime_dir.is_relative_to(workspace):
        raise ToolFailure("INVALID_ARGUMENT", "Command runtime state must live outside the workspace.", category="security")
    try:
        read_roots = (workspace, *system_read_roots(), *(resolve_root(p, must_exist=True) for p in config.read_roots))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolFailure("INVALID_ARGUMENT", "A configured sandbox read root is unavailable.", category="validation") from exc
    write_roots = (runtime_dir,)
    if purpose == "command":
        write_roots += write_paths if structured_only else (workspace,)
    helper_roots = (config.helper_path.parent,) if config.helper_path is not None else ()
    deny_roots = tuple(dict.fromkeys(resolve_root(p) for p in (*config.deny_roots, *service_roots, *helper_roots)))
    for root in deny_roots:
        if workspace.is_relative_to(root) or runtime_dir.is_relative_to(root):
            raise ToolFailure("INVALID_ARGUMENT", "A denied root contains the workspace or command runtime.", category="security")
    return ExecutionPolicy(
        workspace=workspace,
        read_roots=tuple(dict.fromkeys(read_roots)),
        write_roots=tuple(dict.fromkeys(resolve_root(p) for p in write_roots)),
        deny_roots=deny_roots,
        mode=config.mode,
        network="offline" if purpose == "read-helper" else config.network,
        purpose=purpose,
        helper_path=config.helper_path,
        helper_sha256=config.helper_sha256,
        allowed_destinations=config.allowed_destinations if purpose == "command" else (),
    )
