"""Versioned server settings kept independently of a managed workspace."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class SettingsStoreError(RuntimeError):
    pass


SECRET_SETTING_KEYS = frozenset({"auth_token", "admin_token", "oauth_password", "oauth_token_secret", "oauth_refresh_token_pepper"})
SETTINGS_SCHEMA_VERSION = 1


def default_settings_dir(app_name: str = "coding-tools-mcp") -> Path:
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
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SettingsStoreError(f"Could not read server settings: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SettingsStoreError(f"Server settings JSON is corrupt; refusing to overwrite {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SettingsStoreError("Server settings must be a JSON object.")
        version = raw.get("schema_version", 0)
        if version not in (0, SETTINGS_SCHEMA_VERSION):
            raise SettingsStoreError("Server settings were written by an unsupported schema version.")
        if version == 0:
            raw["schema_version"] = SETTINGS_SCHEMA_VERSION
        return raw

    def write(self, settings: dict[str, Any]) -> None:
        if not isinstance(settings, dict):
            raise SettingsStoreError("Server settings must be a JSON object.")
        payload = dict(settings)
        payload["schema_version"] = SETTINGS_SCHEMA_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
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
        except OSError as exc:
            raise SettingsStoreError(f"Could not atomically save server settings: {exc}") from exc
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
