"""Validated catalog of independently selectable workspace roots."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class WorkspaceCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceEntry:
    id: str
    name: str
    root: Path
    enabled: bool = True
    default: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "root": str(self.root),
            "enabled": self.enabled,
            "default": self.default,
        }


class WorkspaceCatalog:
    def __init__(self, entries: list[WorkspaceEntry], default_id: str) -> None:
        if not entries:
            raise WorkspaceCatalogError("Workspace catalog must contain at least one workspace.")
        if not isinstance(default_id, str) or not default_id:
            raise WorkspaceCatalogError("Workspace catalog requires a default workspace id.")

        normalized = [
            WorkspaceEntry(
                id=_validated_id(entry.id),
                name=_validated_name(entry.name),
                root=_validated_root(entry.root),
                enabled=bool(entry.enabled),
                default=bool(entry.default),
            )
            for entry in entries
        ]
        default_flags = [entry.id for entry in normalized if entry.default]
        if len(default_flags) > 1:
            raise WorkspaceCatalogError("Workspace catalog must contain only one default workspace.")
        if default_flags and default_flags[0] != default_id:
            raise WorkspaceCatalogError("Default workspace flag conflicts with default_workspace_id.")

        by_id = {entry.id: entry for entry in normalized}
        if len(by_id) != len(normalized):
            raise WorkspaceCatalogError("Workspace IDs must be unique.")
        if default_id not in by_id:
            raise WorkspaceCatalogError("Default workspace id is not present in the catalog.")
        if not by_id[default_id].enabled:
            raise WorkspaceCatalogError("The default workspace must be enabled.")

        canonical = [replace(entry, default=entry.id == default_id) for entry in normalized]
        self._validate_roots(canonical)
        self.entries = tuple(canonical)
        self.default_id = default_id
        self._by_id = {entry.id: entry for entry in canonical}

    @classmethod
    def single(cls, root: str | Path) -> "WorkspaceCatalog":
        resolved = _validated_root(root)
        identifier = "ws-" + hashlib.sha256(_root_identity(resolved).encode("utf-8")).hexdigest()[:16]
        return cls(
            [WorkspaceEntry(identifier, resolved.name or str(resolved), resolved, True, True)],
            identifier,
        )

    @classmethod
    def from_settings(cls, settings: dict[str, Any], fallback_root: str | Path) -> "WorkspaceCatalog":
        raw = settings.get("workspace_catalog")
        explicit_default = settings.get("default_workspace_id")
        if not isinstance(raw, list) or not raw:
            return cls.single(settings.get("workspace") or fallback_root)

        entries: list[WorkspaceEntry] = []
        flagged_defaults: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                raise WorkspaceCatalogError("Each workspace catalog entry must be an object.")
            entry = WorkspaceEntry(
                id=_validated_id(item.get("id")),
                name=_validated_name(item.get("name")),
                root=_validated_root(item.get("root")),
                enabled=_validated_bool(item.get("enabled", True), "enabled"),
                default=_validated_bool(item.get("default", False), "default"),
            )
            entries.append(entry)
            if entry.default:
                flagged_defaults.append(entry.id)

        if len(flagged_defaults) > 1:
            raise WorkspaceCatalogError("Workspace catalog must contain only one default workspace.")
        if explicit_default is not None and not isinstance(explicit_default, str):
            raise WorkspaceCatalogError("default_workspace_id must be a string.")
        if isinstance(explicit_default, str) and explicit_default and flagged_defaults:
            if explicit_default != flagged_defaults[0]:
                raise WorkspaceCatalogError("Default workspace flag conflicts with default_workspace_id.")
        chosen = (
            explicit_default
            if isinstance(explicit_default, str) and explicit_default
            else flagged_defaults[0]
            if flagged_defaults
            else entries[0].id
        )
        return cls(entries, chosen)

    def default(self) -> WorkspaceEntry:
        return self._by_id[self.default_id]

    def get(self, identifier: str) -> WorkspaceEntry:
        entry = self._by_id.get(identifier)
        if entry is None or not entry.enabled:
            raise WorkspaceCatalogError("Workspace is unknown or disabled.")
        return entry

    def enabled_entries(self) -> tuple[WorkspaceEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled)

    def payload(self) -> dict[str, Any]:
        return {
            "default_workspace_id": self.default_id,
            "workspaces": [entry.payload() for entry in self.enabled_entries()],
        }

    def settings_payload(self) -> dict[str, Any]:
        return {
            "workspace_catalog": [entry.payload() for entry in self.entries],
            "default_workspace_id": self.default_id,
        }

    @staticmethod
    def _validate_roots(entries: list[WorkspaceEntry]) -> None:
        roots: list[Path] = []
        identities: set[str] = set()
        for entry in entries:
            root = entry.root
            identity = _root_identity(root)
            if identity in identities:
                raise WorkspaceCatalogError("Workspace roots must be unique.")
            for other in roots:
                if _relative_to(root, other) or _relative_to(other, root):
                    raise WorkspaceCatalogError("Nested workspace roots are not allowed.")
            roots.append(root)
            identities.add(identity)


def _validated_id(raw: Any) -> str:
    if not isinstance(raw, str) or WORKSPACE_ID_PATTERN.fullmatch(raw) is None:
        raise WorkspaceCatalogError(
            "Workspace id must contain 1-128 ASCII letters, digits, dots, underscores, or hyphens."
        )
    return raw


def _validated_name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 200:
        raise WorkspaceCatalogError("Workspace name must contain 1-200 characters.")
    return raw.strip()


def _validated_bool(raw: Any, field: str) -> bool:
    if not isinstance(raw, bool):
        raise WorkspaceCatalogError(f"Workspace {field} must be a boolean.")
    return raw


def _validated_root(raw: Any) -> Path:
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise WorkspaceCatalogError("Workspace root is required.")
    try:
        root = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceCatalogError(f"Workspace root cannot be resolved: {exc}") from exc
    if not root.is_dir():
        raise WorkspaceCatalogError("Workspace root must be an existing directory.")
    if root == Path(root.anchor):
        raise WorkspaceCatalogError("Filesystem roots cannot be managed as workspaces.")
    return root


def _root_identity(root: Path) -> str:
    return os.path.normcase(str(root))


def _relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
