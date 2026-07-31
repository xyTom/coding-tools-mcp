"""Canonical validation and migration rules for persisted startup settings."""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from pathlib import Path
from typing import Any

from .workspace_catalog import WorkspaceCatalog, WorkspaceCatalogError


PERMISSION_MODE_CHOICES = ("safe", "trusted", "dangerous")
SHELL_ENV_INHERIT_CHOICES = ("core", "all", "none")
LEGACY_TOOL_PROFILE_WARNING = "legacy_tool_profile_ignored"
SECRET_REFERENCE_FIELDS = frozenset(
    {
        "auth_token_secret_ref",
        "admin_token_secret_ref",
        "oauth_authorization_password_secret_ref",
        "oauth_active_key_secret_ref",
        "oauth_refresh_token_pepper_secret_ref",
    }
)
RESTART_FIELDS = frozenset(
    {
        "host",
        "port",
        "workspace_catalog",
        "default_workspace_id",
        "oauth_server_url",
        "oauth_compatibility_mode",
        "oauth_client_workspace_bindings",
        "permission_mode",
        "shell_env_inherit",
        "allowed_origins",
    }
)


class SettingsValidationError(ValueError):
    def __init__(
        self,
        field_errors: dict[str, str] | None = None,
        form_errors: list[str] | None = None,
    ) -> None:
        self.field_errors = field_errors or {}
        self.form_errors = form_errors or []
        message = (
            next(iter(self.field_errors.values()), None)
            or "; ".join(self.form_errors)
            or "Invalid settings."
        )
        super().__init__(message)


def migrate_persisted_settings(settings: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Return settings with obsolete fields removed and stable warning codes.

    Legacy ``tool_profile`` values are accepted as migration input only. Known
    and unknown values have the same outcome: the value is ignored, the fixed
    tool catalog remains authoritative, and the field is omitted on the next
    successful settings write.
    """

    migrated = dict(settings)
    warnings: list[str] = []
    if "tool_profile" in migrated:
        migrated.pop("tool_profile", None)
        warnings.append(LEGACY_TOOL_PROFILE_WARNING)
    return migrated, tuple(warnings)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in re.split(r"[\s,]+", value) if item]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def normalize_allowed_origins(value: Any) -> list[str]:
    normalized: list[str] = []
    for item in _items(value):
        raw = _text(item).rstrip("/")
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            parsed = None
        if (
            not raw
            or raw in {"*", "null"}
            or parsed is None
            or not parsed.scheme
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or parsed.hostname is None
        ):
            raise SettingsValidationError(
                {"allowed_origins": f"Unsupported allowed origin: {raw or 'empty value'}"}
            )
        host = parsed.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError as exc:
            raise SettingsValidationError(
                {"allowed_origins": f"Allowed origin has an invalid port: {raw}"}
            ) from exc
        origin = urllib.parse.urlunsplit(
            (parsed.scheme.lower(), f"{host}:{port}" if port is not None else host, "", "", "")
        )
        if origin not in normalized:
            normalized.append(origin)
    return normalized


def _normalize_host(value: Any) -> str:
    host = _text(value)
    if not host:
        raise SettingsValidationError({"host": "Host is required."})
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if len(host) > 253 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) is None:
        raise SettingsValidationError({"host": "Host must be a valid IP address or hostname."})
    return host.lower()


def _normalize_url(value: Any) -> str:
    url = _text(value)
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SettingsValidationError({"oauth_server_url": "OAuth server URL is invalid."}) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise SettingsValidationError(
            {"oauth_server_url": "OAuth server URL must use http:// or https:// without user information."}
        )
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


def _normalize_choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    normalized = _text(value).lower()
    if normalized not in choices:
        raise SettingsValidationError(
            {field: f"Unsupported value; expected one of: {', '.join(choices)}."}
        )
    return normalized


def normalize_oauth_client_workspace_bindings(
    value: Any,
    catalog: WorkspaceCatalog,
) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsValidationError(
            {"oauth_client_workspace_bindings": "OAuth client Workspace bindings must be an object."}
        )
    normalized: dict[str, str] = {}
    for raw_client_id, raw_workspace_id in value.items():
        if (
            not isinstance(raw_client_id, str)
            or not 1 <= len(raw_client_id) <= 128
            or not all(char.isalnum() or char in "-._~" for char in raw_client_id)
        ):
            raise SettingsValidationError(
                {"oauth_client_workspace_bindings": "OAuth client binding contains an invalid client_id."}
            )
        if not isinstance(raw_workspace_id, str) or not raw_workspace_id:
            raise SettingsValidationError(
                {"oauth_client_workspace_bindings": "OAuth client binding requires a Workspace id."}
            )
        try:
            workspace = catalog.get(raw_workspace_id)
        except WorkspaceCatalogError as exc:
            raise SettingsValidationError(
                {
                    "oauth_client_workspace_bindings": (
                        f"OAuth client {raw_client_id!r} references an unknown or disabled Workspace."
                    )
                }
            ) from exc
        normalized[raw_client_id] = workspace.id
    return dict(sorted(normalized.items()))


def _canonicalize_catalog(settings: dict[str, Any], fallback_workspace: str | Path) -> None:
    try:
        catalog = WorkspaceCatalog.from_settings(settings, fallback_workspace)
    except WorkspaceCatalogError as exc:
        raise SettingsValidationError({"workspace_catalog": str(exc)}) from exc
    settings.update(catalog.settings_payload())
    settings["workspace"] = str(catalog.default().root)


def normalize_startup_settings_with_warnings(
    current: dict[str, Any],
    updates: dict[str, Any],
    fallback_workspace: str | Path,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    settings, current_warnings = migrate_persisted_settings(current)
    clean_updates, update_warnings = migrate_persisted_settings(updates)
    accepted = {
        "host",
        "port",
        "workspace",
        "workspace_catalog",
        "default_workspace_id",
        "oauth_server_url",
        "oauth_compatibility_mode",
        "oauth_client_workspace_bindings",
        "permission_mode",
        "shell_env_inherit",
        "allowed_origins",
        *SECRET_REFERENCE_FIELDS,
    }
    for key, value in clean_updates.items():
        if key not in accepted:
            continue
        if key == "allowed_origins":
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = normalize_allowed_origins(value)
        elif key in {
            "workspace_catalog",
            "oauth_compatibility_mode",
            "oauth_client_workspace_bindings",
        }:
            settings[key] = value
        elif value is None or value == "":
            settings.pop(key, None)
        else:
            settings[key] = value

    if "host" in settings:
        settings["host"] = _normalize_host(settings["host"])
    if "port" in settings:
        try:
            port = int(settings["port"])
        except (TypeError, ValueError) as exc:
            raise SettingsValidationError({"port": "Port must be an integer from 1 to 65535."}) from exc
        if not 1 <= port <= 65535:
            raise SettingsValidationError({"port": "Port must be an integer from 1 to 65535."})
        settings["port"] = port
    if "oauth_server_url" in settings:
        settings["oauth_server_url"] = _normalize_url(settings["oauth_server_url"])
    for key, choices in (
        ("permission_mode", PERMISSION_MODE_CHOICES),
        ("shell_env_inherit", SHELL_ENV_INHERIT_CHOICES),
    ):
        if key in settings:
            settings[key] = _normalize_choice(settings[key], key, choices)
    if "oauth_compatibility_mode" in settings and not isinstance(
        settings["oauth_compatibility_mode"], bool
    ):
        raise SettingsValidationError(
            {"oauth_compatibility_mode": "OAuth compatibility mode must be a boolean."}
        )
    if (
        "workspace_catalog" in settings
        or "default_workspace_id" in settings
        or "workspace" in clean_updates
    ):
        _canonicalize_catalog(settings, fallback_workspace)
    if "oauth_client_workspace_bindings" in settings:
        try:
            catalog = WorkspaceCatalog.from_settings(settings, fallback_workspace)
        except WorkspaceCatalogError as exc:
            raise SettingsValidationError({"workspace_catalog": str(exc)}) from exc
        settings["oauth_client_workspace_bindings"] = normalize_oauth_client_workspace_bindings(
            settings["oauth_client_workspace_bindings"],
            catalog,
        )

    warnings = tuple(dict.fromkeys((*current_warnings, *update_warnings)))
    return settings, warnings


def normalize_startup_settings(
    current: dict[str, Any],
    updates: dict[str, Any],
    fallback_workspace: str | Path,
) -> dict[str, Any]:
    settings, _warnings = normalize_startup_settings_with_warnings(
        current,
        updates,
        fallback_workspace,
    )
    return settings


def pending_restart_fields(
    active: dict[str, Any],
    persisted: dict[str, Any],
) -> list[str]:
    return sorted(
        field
        for field in RESTART_FIELDS
        if active.get(field) != persisted.get(field)
    )


def schema_payload() -> dict[str, Any]:
    return {
        "permission_mode": list(PERMISSION_MODE_CHOICES),
        "shell_env_inherit": list(SHELL_ENV_INHERIT_CHOICES),
        "restart_fields": sorted(RESTART_FIELDS),
        "migration_warnings": [LEGACY_TOOL_PROFILE_WARNING],
    }
