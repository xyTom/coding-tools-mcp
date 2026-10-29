"""Keep strict write authority away from the service's Python import trees.

This is an administrator configuration check, not a Python import sandbox.
Import paths must remain administrator-controlled for the service lifetime.
In particular, a currently empty search directory is still trusted: a command
must not be able to plant a module there for a later lazy import.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ToolFailure


def _cached_package_paths(value: object) -> tuple[str, ...]:
    # Iterating importlib's dynamic namespace __path__ can rerun import hooks.
    # Inspect its already-cached paths instead; reject unfamiliar path objects.
    if type(value).__name__ == "_NamespacePath" and type(value).__module__ == "_frozen_importlib_external":
        value = vars(value).get("_path")
    if not isinstance(value, (list, tuple)) or type(value) not in (list, tuple) or any(not isinstance(path, str) for path in value):
        raise ToolFailure("SANDBOX_POLICY_INVALID", "Unsupported service package search paths.", category="security")
    return tuple(value)


def _absolute_roots(value: str | os.PathLike[str]) -> tuple[Path, ...]:
    """Protect both the lookup spelling and its target, including symlinks."""
    try:
        absolute = Path(os.path.abspath(value))
        return tuple(dict.fromkeys((absolute, absolute.resolve(strict=False))))
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ToolFailure(
            "SANDBOX_POLICY_INVALID", "A service import path cannot be resolved safely.", category="security",
        ) from exc


@dataclass(frozen=True)
class ServiceImportSnapshot:
    roots: tuple[Path, ...]

    @classmethod
    def capture(cls) -> ServiceImportSnapshot:
        # Capture before accepting work; never import/find_spec a package merely
        # to discover where it lives. Loaded editable finders can redirect imports
        # outside sys.path, and optional dependencies may not have been imported.
        modules = tuple(sys.modules.items())
        paths: list[str | os.PathLike[str]] = [Path(__file__).parent]
        placeholders: set[str] = set()
        for name, module in modules:
            attributes = vars(module) if module is not None else {}
            filename = attributes.get("__file__")
            if isinstance(filename, str) and not filename.startswith("<"):
                paths.append(filename)
            paths.extend(_cached_package_paths(attributes.get("__path__", ())))
            if name.startswith("__editable__"):
                # Setuptools' PEP 660 finder stores real package targets here;
                # its path-hook sentinel is not a filesystem search directory.
                mapping = attributes.get("MAPPING", {})
                namespaces = attributes.get("NAMESPACES", {})
                if not isinstance(mapping, dict) or not isinstance(namespaces, dict):
                    raise ToolFailure("SANDBOX_POLICY_INVALID", "Unsupported editable import paths.", category="security")
                paths.extend(mapping.values())
                for namespace_paths in namespaces.values():
                    paths.extend(_cached_package_paths(namespace_paths))
                placeholder = attributes.get("PATH_PLACEHOLDER")
                if isinstance(placeholder, str):
                    placeholders.add(placeholder)
        # Empty and relative entries are resolved against the startup cwd. Do
        # not discard missing paths: commands could create them after startup.
        paths.extend(entry for entry in tuple(sys.path) if entry not in placeholders)
        roots = tuple(dict.fromkeys(root for path in paths if path not in placeholders for root in _absolute_roots(path)))
        return cls(roots)

    def reject_overlapping_writes(self, write_roots: Iterable[Path]) -> None:
        for write_root in write_roots:
            for writable in _absolute_roots(write_root):
                for trusted in self.roots:
                    # Both directions matter: a workspace beneath an import
                    # tree can create a previously absent package/namespace.
                    if writable.is_relative_to(trusted) or trusted.is_relative_to(writable):
                        raise ToolFailure(
                            "SANDBOX_POLICY_INVALID",
                            "Strict writable roots must be disjoint from service source and Python import paths.",
                            category="security",
                            details={"write_root": str(writable), "trusted_import_root": str(trusted)},
                        )
