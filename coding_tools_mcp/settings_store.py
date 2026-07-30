"""Versioned server settings kept independently of a managed workspace."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .envutils import ENV_PREFIX
from .settings_definition import migrate_persisted_settings


class SettingsStoreError(RuntimeError):
    pass


SECRET_SETTING_KEYS = frozenset(
    {
        "auth_token",
        "admin_token",
        "oauth_password",
        "oauth_token_secret",
        "oauth_refresh_token_pepper",
    }
)
SETTINGS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SettingsReadResult:
    settings: dict[str, Any]
    warnings: tuple[str, ...]
    migrated: bool


def default_settings_dir(app_name: str = "coding-tools-mcp") -> Path:
    configured = os.environ.get(f"{ENV_PREFIX}_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / app_name


def sanitize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    result = dict(settings)
    for key in SECRET_SETTING_KEYS:
        if result.get(key):
            result[f"{key}_configured"] = True
        result.pop(key, None)
    for key in list(result):
        if key.endswith("_secret_ref"):
            result[key] = {"configured": bool(result[key])}
    return result


class ServerSettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def read(self) -> dict[str, Any]:
        return self.read_result().settings

    def read_result(self) -> SettingsReadResult:
        if not self.path.exists():
            return SettingsReadResult({}, (), False)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SettingsStoreError(f"Could not read server settings: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SettingsStoreError(
                f"Server settings JSON is corrupt; refusing to overwrite {self.path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise SettingsStoreError("Server settings must be a JSON object.")

        version = raw.get("schema_version", 0)
        if isinstance(version, bool) or not isinstance(version, int):
            raise SettingsStoreError("Server settings schema_version must be an integer.")
        if version not in (0, SETTINGS_SCHEMA_VERSION):
            raise SettingsStoreError("Server settings were written by an unsupported schema version.")

        migrated, warnings = migrate_persisted_settings(raw)
        changed = migrated != raw or version == 0
        migrated["schema_version"] = SETTINGS_SCHEMA_VERSION
        return SettingsReadResult(migrated, warnings, changed)

    def write(self, settings: dict[str, Any]) -> tuple[str, ...]:
        if not isinstance(settings, dict):
            raise SettingsStoreError("Server settings must be a JSON object.")
        payload, warnings = migrate_persisted_settings(settings)
        plaintext_keys = sorted(
            key for key in SECRET_SETTING_KEYS if payload.get(key) not in (None, "")
        )
        if plaintext_keys:
            raise SettingsStoreError(
                "Secret values must be stored in SecretVault and referenced from settings: "
                + ", ".join(plaintext_keys)
            )
        payload["schema_version"] = SETTINGS_SCHEMA_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                tmp_path.chmod(0o600)
            os.replace(tmp_path, self.path)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            raise SettingsStoreError(f"Could not atomically save server settings: {exc}") from exc
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return warnings


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
