"""Canonical validation and comparison rules for persisted startup settings.

The server keeps settings as JSON for compatibility, but all writers share this
module so invalid values cannot be persisted merely because they came from the
admin UI instead of command-line parsing.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from pathlib import Path
from typing import Any

from .workspace_catalog import WorkspaceCatalog, WorkspaceCatalogError


PERMISSION_MODE_CHOICES = ("safe", "trusted", "dangerous")
TOOL_PROFILE_CHOICES = ("full", "read-only", "compat-readonly-all")
SHELL_ENV_INHERIT_CHOICES = ("core", "all", "none")
RESTART_FIELDS = frozenset(
    {
        "host",
        "port",
        "workspace_catalog",
        "default_workspace_id",
        "oauth_server_url",
        "oauth_compatibility_mode",
        "permission_mode",
        "tool_profile",
        "shell_env_inherit",
        "allowed_origins",
    }
)


class SettingsValidationError(ValueError):
    def __init__(self, field_errors: dict[str, str] | None = None, form_errors: list[str] | None = None) -> None:
        self.field_errors = field_errors or {}
        self.form_errors = form_errors or []
        super().__init__(next(iter(self.field_errors.values()), None) or "; ".join(self.form_errors) or "Invalid settings.")


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
            raise SettingsValidationError({"allowed_origins": f"不支持的访问来源：{raw or '空值'}"})
        host = parsed.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError as exc:
            raise SettingsValidationError({"allowed_origins": f"访问来源端口无效：{raw}"}) from exc
        origin = urllib.parse.urlunsplit((parsed.scheme.lower(), f"{host}:{port}" if port is not None else host, "", "", ""))
        if origin not in normalized:
            normalized.append(origin)
    return normalized


def _normalize_host(value: Any) -> str:
    host = _text(value)
    if not host:
        raise SettingsValidationError({"host": "请输入监听地址。"})
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise SettingsValidationError({"host": "监听地址必须是有效的 IP 地址或主机名。"})
    return host.lower()


def _normalize_url(value: Any) -> str:
    url = _text(value)
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SettingsValidationError({"oauth_server_url": "公开访问地址格式无效。"}) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise SettingsValidationError({"oauth_server_url": "公开访问地址必须是 http:// 或 https:// URL。"})
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def _normalize_choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    normalized = _text(value).lower()
    if normalized not in choices:
        raise SettingsValidationError({field: f"不支持的值；可选值：{', '.join(choices)}。"})
    return normalized


def _canonicalize_catalog(settings: dict[str, Any], fallback_workspace: str | Path) -> None:
    try:
        catalog = WorkspaceCatalog.from_settings(settings, fallback_workspace)
    except WorkspaceCatalogError as exc:
        raise SettingsValidationError({"workspace_catalog": str(exc)}) from exc
    settings["workspace_catalog"] = [
        {**entry.payload(), "default": entry.id == catalog.default_id} for entry in catalog.entries
    ]
    settings["default_workspace_id"] = catalog.default_id
    settings["workspace"] = str(catalog.default().root)


def normalize_startup_settings(current: dict[str, Any], updates: dict[str, Any], fallback_workspace: str | Path) -> dict[str, Any]:
    """Merge, validate and canonicalize persisted startup settings.

    Empty values retain the old endpoint behaviour: they remove an optional
    persisted setting. Secrets are intentionally not interpreted here.
    """
    settings = dict(current)
    accepted = {
        "host", "port", "workspace", "workspace_catalog", "default_workspace_id",
        "auth_token", "admin_token", "oauth_password", "oauth_server_url",
        "oauth_token_secret", "oauth_compatibility_mode", "permission_mode",
        "tool_profile", "shell_env_inherit", "allowed_origins",
    }
    for key, value in updates.items():
        if key not in accepted:
            continue
        if key == "allowed_origins":
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = normalize_allowed_origins(value)
        elif key in {"workspace_catalog", "oauth_compatibility_mode"}:
            settings[key] = value
        elif value is None or value == "":
            settings.pop(key, None)
        else:
            settings[key] = value

    try:
        if "host" in settings:
            settings["host"] = _normalize_host(settings["host"])
        if "port" in settings:
            try:
                port = int(settings["port"])
            except (TypeError, ValueError) as exc:
                raise SettingsValidationError({"port": "端口必须是 1 到 65535 之间的整数。"}) from exc
            if not 1 <= port <= 65535:
                raise SettingsValidationError({"port": "端口必须是 1 到 65535 之间的整数。"})
            settings["port"] = port
        if "oauth_server_url" in settings:
            settings["oauth_server_url"] = _normalize_url(settings["oauth_server_url"])
        for key, choices in (
            ("permission_mode", PERMISSION_MODE_CHOICES),
            ("tool_profile", TOOL_PROFILE_CHOICES),
            ("shell_env_inherit", SHELL_ENV_INHERIT_CHOICES),
        ):
            if key in settings:
                settings[key] = _normalize_choice(settings[key], key, choices)
        if "oauth_compatibility_mode" in settings:
            if not isinstance(settings["oauth_compatibility_mode"], bool):
                raise SettingsValidationError({"oauth_compatibility_mode": "兼容模式必须是布尔值。"})
        if "workspace_catalog" in settings or "default_workspace_id" in settings or "workspace" in updates:
            _canonicalize_catalog(settings, fallback_workspace)
    except SettingsValidationError:
        raise
    return settings


def pending_restart_fields(active: dict[str, Any], persisted: dict[str, Any]) -> list[str]:
    return sorted(field for field in RESTART_FIELDS if active.get(field) != persisted.get(field))


def schema_payload() -> dict[str, Any]:
    return {
        "permission_mode": list(PERMISSION_MODE_CHOICES),
        "tool_profile": list(TOOL_PROFILE_CHOICES),
        "shell_env_inherit": list(SHELL_ENV_INHERIT_CHOICES),
        "restart_fields": sorted(RESTART_FIELDS),
    }
