"""Immutable Workspace selection for stdio and Streamable HTTP runtimes."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .oauth import OAuthIdentity
from .workspace_catalog import WorkspaceCatalog, WorkspaceCatalogError


class WorkspaceBindingError(RuntimeError):
    """A request identity cannot be safely bound to an enabled Workspace."""


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace_id: str
    root: Path
    authorization_method: str
    client_id: str | None = None
    grant_id: str | None = None

    def authorization_key(self) -> tuple[str, str | None, str | None, str]:
        return (
            self.authorization_method,
            self.client_id,
            self.grant_id,
            self.workspace_id,
        )


class WorkspaceBindingResolver:
    """Resolve a startup Catalog snapshot into immutable per-Runtime bindings.

    Replacing the Catalog affects only future resolutions. Existing Runtime
    instances retain their frozen binding and Workspace adapter until closed.
    """

    def __init__(self, catalog: WorkspaceCatalog) -> None:
        self._catalog = catalog
        self._lock = threading.Lock()

    def catalog(self) -> WorkspaceCatalog:
        with self._lock:
            return self._catalog

    def update_catalog(self, catalog: WorkspaceCatalog) -> None:
        with self._lock:
            self._catalog = catalog

    def resolve_stdio(self) -> WorkspaceBinding:
        catalog = self.catalog()
        entry = catalog.default()
        return WorkspaceBinding(entry.id, entry.root, "stdio")

    def resolve_http(
        self,
        authorization_method: str,
        identity: OAuthIdentity | None,
    ) -> WorkspaceBinding:
        catalog = self.catalog()
        if authorization_method == "oauth":
            if identity is None:
                raise WorkspaceBindingError("OAuth identity is required for Workspace binding.")
            try:
                entry = catalog.get(identity.workspace_id)
            except WorkspaceCatalogError as exc:
                raise WorkspaceBindingError(
                    "OAuth identity has no enabled Workspace mapping."
                ) from exc
            return WorkspaceBinding(
                entry.id,
                entry.root,
                "oauth",
                client_id=identity.client_id,
                grant_id=identity.grant_id,
            )
        if identity is not None:
            raise WorkspaceBindingError(
                "OAuth identity cannot be combined with a non-OAuth authorization method."
            )
        entry = catalog.default()
        return WorkspaceBinding(entry.id, entry.root, authorization_method)
