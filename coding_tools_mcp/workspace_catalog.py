"""Validated catalog of independently selectable workspace roots."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
        return {"id": self.id, "name": self.name, "root": str(self.root), "enabled": self.enabled, "default": self.default}


class WorkspaceCatalog:
    def __init__(self, entries: list[WorkspaceEntry], default_id: str) -> None:
        if not entries:
            raise WorkspaceCatalogError("Workspace catalog must contain at least one workspace.")
        self.entries = tuple(entries)
        self.default_id = default_id
        self._by_id = {entry.id: entry for entry in entries}
        if len(self._by_id) != len(entries) or default_id not in self._by_id:
            raise WorkspaceCatalogError("Workspace IDs must be unique and include the default workspace.")
        if not self._by_id[default_id].enabled:
            raise WorkspaceCatalogError("The default workspace must be enabled.")
        self._validate_entries()

    @classmethod
    def single(cls, root: str | Path) -> "WorkspaceCatalog":
        resolved = _validated_root(root)
        identifier = "ws-" + hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
        return cls([WorkspaceEntry(identifier, resolved.name or str(resolved), resolved, True, True)], identifier)

    @classmethod
    def from_settings(cls, settings: dict[str, Any], fallback_root: str | Path) -> "WorkspaceCatalog":
        raw = settings.get("workspace_catalog")
        default_id = settings.get("default_workspace_id")
        if not isinstance(raw, list) or not raw:
            return cls.single(settings.get("workspace") or fallback_root)
        entries: list[WorkspaceEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                raise WorkspaceCatalogError("Each workspace catalog entry must be an object.")
            identifier = item.get("id")
            name = item.get("name")
            root = item.get("root")
            if not isinstance(identifier, str) or not identifier or not isinstance(name, str) or not name.strip():
                raise WorkspaceCatalogError("Workspace entries require a stable id and name.")
            entries.append(WorkspaceEntry(identifier, name.strip(), _validated_root(root), bool(item.get("enabled", True)), bool(item.get("default", False))))
        chosen = default_id if isinstance(default_id, str) else next((item.id for item in entries if item.default), entries[0].id)
        return cls(entries, chosen)

    def default(self) -> WorkspaceEntry:
        return self._by_id[self.default_id]

    def get(self, identifier: str) -> WorkspaceEntry:
        entry = self._by_id.get(identifier)
        if entry is None or not entry.enabled:
            raise WorkspaceCatalogError("Workspace is unknown or disabled.")
        return entry

    def payload(self) -> dict[str, Any]:
        return {"default_workspace_id": self.default_id, "workspaces": [entry.payload() for entry in self.entries]}

    def settings_payload(self) -> dict[str, Any]:
        return {"workspace_catalog": [entry.payload() for entry in self.entries], "default_workspace_id": self.default_id}

    def _validate_entries(self) -> None:
        roots: list[Path] = []
        for entry in self.entries:
            if not entry.id or len(entry.id) > 128:
                raise WorkspaceCatalogError("Workspace id must contain 1-128 characters.")
            root = entry.root
            if root == root.anchor:
                raise WorkspaceCatalogError("Filesystem roots cannot be managed as workspaces.")
            for other in roots:
                if root == other:
                    raise WorkspaceCatalogError("Workspace roots must be unique.")
                if _relative_to(root, other) or _relative_to(other, root):
                    raise WorkspaceCatalogError("Nested workspace roots are not allowed.")
            roots.append(root)


def _validated_root(raw: Any) -> Path:
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise WorkspaceCatalogError("Workspace root is required.")
    try:
        root = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkspaceCatalogError(f"Workspace root cannot be resolved: {exc}") from exc
    if not root.is_dir():
        raise WorkspaceCatalogError("Workspace root must be an existing directory.")
    return root


def _relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
