from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .secret_vault import SecretVault, SecretVaultError
from .upstream import UpstreamConfigError, UpstreamManager, UpstreamServerConfig, parse_server_config


ADMIN_TOOL_NAMES = (
    "mcp_catalog_list",
    "mcp_template_list",
    "mcp_template_render",
    "mcp_server_plan",
    "mcp_server_install",
    "mcp_server_update",
    "mcp_server_enable",
    "mcp_server_disable",
    "mcp_server_remove",
    "mcp_server_reload",
    "mcp_server_health",
    "mcp_server_start",
    "mcp_server_stop",
    "mcp_server_logs",
    "mcp_secret_set",
    "mcp_secret_list",
    "mcp_secret_delete",
    "mcp_transcript_sessions",
    "mcp_transcript_export",
    "mcp_codex_sessions_preview",
    "mcp_codex_sessions_import",
    "mcp_codex_sessions_sync",
    "mcp_chat_projects",
    "mcp_chat_conversations",
    "mcp_chat_messages",
    "mcp_chat_context",
    "mcp_chat_record_context",
    "mcp_chat_update_context",
    "mcp_chat_delete_context",
    "mcp_chat_recall",
    "mcp_chat_project_recall",
    "mcp_chat_export",
    "mcp_chat_context_export",
    "mcp_chat_update_message",
    "mcp_chat_delete_message",
    "mcp_chat_delete_conversation",
    "mcp_chat_clear",
    "mcp_chat_merge",
)
SERVER_CONFIG_KEYS = {
    "alias",
    "transport",
    "enabled",
    "url",
    "command",
    "args",
    "env",
    "headers",
    "authorization_env",
    "include_tools",
    "exclude_tools",
    "timeout_ms",
}
SENSITIVE_KEY_RE = re.compile(r"(token|secret|credential|api[_-]?key|password|passwd|authorization)", re.I)
SECRETS_KEY_ENV = "CODING_TOOLS_MCP_SECRETS_KEY"

MCP_SERVER_TEMPLATES: dict[str, dict[str, Any]] = {
    "filesystem": {
        "id": "filesystem",
        "title": "Filesystem",
        "category": "local",
        "description": "Expose a local directory through a filesystem MCP server.",
        "risk": "Reads and writes files in the configured directory.",
        "variables": {
            "alias": {"label": "Alias", "default": "filesystem"},
            "workspace": {"label": "Directory", "default": "."},
        },
        "config": {
            "alias": "{alias}",
            "transport": "stdio",
            "command": "uvx",
            "args": ["mcp-server-filesystem", "{workspace}"],
            "enabled": False,
        },
    },
    "github": {
        "id": "github",
        "title": "GitHub",
        "category": "code-hosting",
        "description": "Connect a GitHub MCP server using a secret vault token reference.",
        "risk": "Can read or mutate GitHub resources depending on token scope and exposed tools.",
        "variables": {
            "alias": {"label": "Alias", "default": "github"},
            "secret": {"label": "Secret name", "default": "github_token"},
        },
        "config": {
            "alias": "{alias}",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": {"secret_ref": "{secret}"}},
            "enabled": False,
        },
    },
    "browser": {
        "id": "browser",
        "title": "Browser",
        "category": "browser",
        "description": "Start a browser automation MCP server through npx.",
        "risk": "Can automate a browser and interact with pages available to that browser profile.",
        "variables": {"alias": {"label": "Alias", "default": "browser"}},
        "config": {
            "alias": "{alias}",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@browsermcp/mcp@latest"],
            "enabled": False,
        },
    },
    "playwright": {
        "id": "playwright",
        "title": "Playwright",
        "category": "browser",
        "description": "Start a Playwright MCP server for browser testing workflows.",
        "risk": "Can launch browsers and interact with local or remote web pages.",
        "variables": {"alias": {"label": "Alias", "default": "playwright"}},
        "config": {
            "alias": "{alias}",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
            "enabled": False,
        },
    },
    "fetch": {
        "id": "fetch",
        "title": "Fetch",
        "category": "web",
        "description": "Start a simple web-fetching MCP server.",
        "risk": "Can make outbound web requests from the host running the MCP server.",
        "variables": {"alias": {"label": "Alias", "default": "fetch"}},
        "config": {
            "alias": "{alias}",
            "transport": "stdio",
            "command": "uvx",
            "args": ["mcp-server-fetch"],
            "enabled": False,
        },
    },
    "local": {
        "id": "local",
        "title": "Custom local command",
        "category": "custom",
        "description": "Register a local stdio MCP command.",
        "risk": "Runs the configured executable as a child process of this server.",
        "variables": {
            "alias": {"label": "Alias", "default": "local-command"},
            "package": {"label": "Package or executable", "default": "your-mcp-package"},
        },
        "config": {
            "alias": "{alias}",
            "transport": "stdio",
            "command": "uvx",
            "args": ["{package}"],
            "enabled": False,
        },
    },
    "http": {
        "id": "http",
        "title": "Remote HTTP",
        "category": "remote",
        "description": "Proxy an existing Streamable HTTP MCP endpoint.",
        "risk": "Forwards tool calls to the configured remote MCP server.",
        "variables": {
            "alias": {"label": "Alias", "default": "remote-http"},
            "url": {"label": "MCP URL", "default": "http://127.0.0.1:3000/mcp"},
        },
        "config": {
            "alias": "{alias}",
            "transport": "http",
            "url": "{url}",
            "enabled": False,
        },
    },
}


class McpManagementError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedServerConfig:
    alias: str
    raw_config: dict[str, Any]
    parsed: UpstreamServerConfig


class McpConfigStore:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path).expanduser() if path else None

    def available(self) -> bool:
        return self.path is not None

    def read(self) -> dict[str, Any]:
        if self.path is None:
            return {"servers": {}}
        if not self.path.exists():
            return {"servers": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise McpManagementError(f"Could not read MCP config: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise McpManagementError(f"MCP config is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise McpManagementError("MCP config must be a JSON object.")
        servers = raw.get("servers")
        if servers is None:
            raw = {"servers": raw}
        elif not isinstance(servers, dict):
            raise McpManagementError("MCP config servers must be an object.")
        raw.setdefault("servers", {})
        return raw

    def write(self, document: dict[str, Any]) -> None:
        if self.path is None:
            raise McpManagementError("No MCP config path is configured.")
        servers = document.get("servers")
        if not isinstance(servers, dict):
            raise McpManagementError("MCP config servers must be an object.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        data = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp_path.write_text(data, encoding="utf-8", newline="\n")
        try:
            if os.name != "nt":
                tmp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp_path, self.path)

    def server_configs(self) -> dict[str, Any]:
        servers = self.read().get("servers", {})
        if not isinstance(servers, dict):
            raise McpManagementError("MCP config servers must be an object.")
        return servers


class McpAdminManager:
    def __init__(self, config_path: str | Path | None, *, protocol_version: str) -> None:
        self.store = McpConfigStore(config_path)
        self.protocol_version = protocol_version
        if self.store.path is None:
            self.audit_path: Path | None = None
            secret_path: Path | None = None
        else:
            self.audit_path = self.store.path.with_suffix(self.store.path.suffix + ".audit.jsonl")
            secret_path = self.store.path.with_suffix(self.store.path.suffix + ".secrets.json")
        self.secret_vault = SecretVault(secret_path, os.environ.get(SECRETS_KEY_ENV))

    def status_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.store.available(),
            "config_path": str(self.store.path) if self.store.path else None,
            "tools": list(ADMIN_TOOL_NAMES),
            "secrets": self.secret_vault.status_payload(),
        }

    def catalog_list(self, upstream_status: dict[str, Any] | None = None) -> dict[str, Any]:
        servers = self.store.server_configs()
        status_by_alias: dict[str, Any] = {}
        if isinstance(upstream_status, dict):
            raw_statuses = upstream_status.get("servers")
            if isinstance(raw_statuses, list):
                status_by_alias = {str(item.get("alias")): item for item in raw_statuses if isinstance(item, dict)}
        items = []
        for alias, config in sorted(servers.items()):
            if isinstance(config, dict):
                items.append(
                    {
                        "alias": alias,
                        "config": redact_config(config),
                        "config_raw": json_safe_copy({**config, "alias": alias}),
                        "status": status_by_alias.get(alias),
                    }
                )
        return {"ok": True, "servers": items, "server_count": len(items)}

    def template_list(self) -> dict[str, Any]:
        templates = []
        for template_id in sorted(MCP_SERVER_TEMPLATES):
            template = MCP_SERVER_TEMPLATES[template_id]
            config = render_template_config(template_id)
            templates.append(
                {
                    "id": template_id,
                    "title": template["title"],
                    "category": template["category"],
                    "description": template["description"],
                    "risk": template["risk"],
                    "variables": json_safe_copy(template.get("variables", {})),
                    "config": config,
                    "config_redacted": redact_config(config),
                }
            )
        return {"ok": True, "templates": templates, "template_count": len(templates)}

    def render_template(
        self,
        template_id: str,
        *,
        variables: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = render_template_config(template_id, variables=variables, overrides=overrides)
        plan = self.plan_server(config)
        template = MCP_SERVER_TEMPLATES[template_id]
        return {
            "ok": True,
            "template": {
                "id": template_id,
                "title": template["title"],
                "category": template["category"],
                "description": template["description"],
                "risk": template["risk"],
            },
            "config": config,
            "config_redacted": redact_config(config),
            "plan": plan,
        }

    def plan_server(self, config: dict[str, Any], *, existing_required: bool | None = None) -> dict[str, Any]:
        normalized = normalize_server_config(config)
        servers = self.store.server_configs()
        current = servers.get(normalized.alias)
        if existing_required is True and current is None:
            raise McpManagementError(f"MCP server {normalized.alias!r} is not installed.")
        if existing_required is False and current is not None:
            raise McpManagementError(f"MCP server {normalized.alias!r} already exists.")
        action = "install" if current is None else "update"
        current_config = current if isinstance(current, dict) else None
        return {
            "ok": True,
            "action": action,
            "alias": normalized.alias,
            "dry_run": True,
            "apply_required": True,
            "server": redact_config(normalized.raw_config),
            "changes": diff_configs(current_config, normalized.raw_config),
        }

    def install_server(self, config: dict[str, Any], *, apply_changes: bool = False) -> dict[str, Any]:
        normalized = normalize_server_config(config)
        plan = self.plan_server(config, existing_required=False)
        if not apply_changes:
            return plan
        document = self.store.read()
        document.setdefault("servers", {})[normalized.alias] = normalized.raw_config
        self.store.write(document)
        self._audit("install", normalized.alias, normalized.raw_config)
        return {**plan, "dry_run": False, "applied": True}

    def update_server(self, alias: str, patch: dict[str, Any], *, apply_changes: bool = False) -> dict[str, Any]:
        servers = self.store.server_configs()
        current = servers.get(alias)
        if not isinstance(current, dict):
            raise McpManagementError(f"MCP server {alias!r} is not installed.")
        if "alias" in patch and patch["alias"] != alias:
            raise McpManagementError("Updating a server alias is not supported; remove and install instead.")
        merged = {**current, **{key: value for key, value in patch.items() if key != "alias"}, "alias": alias}
        normalized = normalize_server_config(merged)
        plan = self.plan_server({**normalized.raw_config, "alias": alias}, existing_required=True)
        if not apply_changes:
            return plan
        document = self.store.read()
        document.setdefault("servers", {})[alias] = normalized.raw_config
        self.store.write(document)
        self._audit("update", alias, normalized.raw_config)
        return {**plan, "dry_run": False, "applied": True}

    def set_server_enabled(self, alias: str, enabled: bool, *, apply_changes: bool = False) -> dict[str, Any]:
        servers = self.store.server_configs()
        current = servers.get(alias)
        if not isinstance(current, dict):
            raise McpManagementError(f"MCP server {alias!r} is not installed.")
        next_config = {**current, "enabled": enabled, "alias": alias}
        normalized = normalize_server_config(next_config)
        plan = {
            "ok": True,
            "action": "enable" if enabled else "disable",
            "alias": alias,
            "dry_run": True,
            "apply_required": True,
            "server": redact_config(normalized.raw_config),
            "changes": diff_configs(current, normalized.raw_config),
        }
        if not apply_changes:
            return plan
        document = self.store.read()
        document.setdefault("servers", {})[alias] = normalized.raw_config
        self.store.write(document)
        self._audit("enable" if enabled else "disable", alias, normalized.raw_config)
        return {**plan, "dry_run": False, "applied": True}

    def remove_server(self, alias: str, *, apply_changes: bool = False) -> dict[str, Any]:
        servers = self.store.server_configs()
        current = servers.get(alias)
        if not isinstance(current, dict):
            raise McpManagementError(f"MCP server {alias!r} is not installed.")
        plan = {
            "ok": True,
            "action": "remove",
            "alias": alias,
            "dry_run": True,
            "apply_required": True,
            "server": redact_config(current),
        }
        if not apply_changes:
            return plan
        document = self.store.read()
        document.setdefault("servers", {}).pop(alias, None)
        self.store.write(document)
        self._audit("remove", alias, current)
        return {**plan, "dry_run": False, "applied": True}

    def reload_upstreams(self) -> UpstreamManager:
        if self.store.path is None:
            return UpstreamManager.empty(self.protocol_version)
        if not self.store.path.exists():
            return UpstreamManager.empty(self.protocol_version)
        resolver = self.secret_vault.get_secret if self.secret_vault.enabled() else None
        return UpstreamManager.from_config_file(
            str(self.store.path),
            protocol_version=self.protocol_version,
            secret_resolver=resolver,
        )

    def secret_set(self, name: str, value: str) -> dict[str, Any]:
        try:
            self.secret_vault.set_secret(name, value)
        except SecretVaultError as exc:
            raise McpManagementError(str(exc)) from exc
        self._audit("secret_set", name, {"value": "<redacted>"})
        return {"ok": True, "name": name}

    def secret_list(self) -> dict[str, Any]:
        return {
            "ok": True,
            "vault_enabled": self.secret_vault.enabled(),
            "secrets": self.secret_vault.list_names(),
        }

    def secret_delete(self, name: str) -> dict[str, Any]:
        try:
            existed = self.secret_vault.delete_secret(name)
        except SecretVaultError as exc:
            raise McpManagementError(str(exc)) from exc
        self._audit("secret_delete", name, {})
        return {"ok": True, "name": name, "deleted": existed}

    def _audit(self, action: str, alias: str, config: dict[str, Any]) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": int(time.time()),
            "action": action,
            "alias": alias,
            "config": redact_config(config),
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def normalize_server_config(config: dict[str, Any]) -> NormalizedServerConfig:
    if not isinstance(config, dict):
        raise McpManagementError("MCP server config must be an object.")
    unknown = set(config) - SERVER_CONFIG_KEYS
    if unknown:
        raise McpManagementError(f"Unknown MCP server config fields: {sorted(unknown)}")
    alias = config.get("alias")
    if not isinstance(alias, str) or not alias:
        raise McpManagementError("MCP server config requires alias.")
    raw_config = json_safe_copy({key: value for key, value in config.items() if key != "alias"})
    try:
        parsed = parse_server_config(alias, raw_config)
    except UpstreamConfigError as exc:
        raise McpManagementError(str(exc)) from exc
    return NormalizedServerConfig(alias=alias, raw_config=raw_config, parsed=parsed)


def render_template_config(
    template_id: str,
    *,
    variables: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template = MCP_SERVER_TEMPLATES.get(template_id)
    if template is None:
        raise McpManagementError(f"Unknown MCP server template: {template_id}")
    rendered_variables = default_template_variables(template)
    for key, value in (variables or {}).items():
        if not isinstance(key, str):
            raise McpManagementError("Template variable names must be strings.")
        rendered_variables[key] = str(value)
    config = replace_template_values(json_safe_copy(template["config"]), rendered_variables)
    if overrides:
        if not isinstance(overrides, dict):
            raise McpManagementError("Template overrides must be an object.")
        config.update(json_safe_copy(overrides))
    normalize_server_config(config)
    return config


def default_template_variables(template: dict[str, Any]) -> dict[str, str]:
    variables: dict[str, str] = {}
    raw_variables = template.get("variables", {})
    if not isinstance(raw_variables, dict):
        return variables
    for key, definition in raw_variables.items():
        if not isinstance(key, str):
            continue
        if isinstance(definition, dict):
            variables[key] = str(definition.get("default", ""))
        else:
            variables[key] = ""
    return variables


def replace_template_values(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        for key, replacement in variables.items():
            result = result.replace("{" + key + "}", replacement)
        return result
    if isinstance(value, list):
        return [replace_template_values(item, variables) for item in value]
    if isinstance(value, dict):
        return {str(key): replace_template_values(item, variables) for key, item in value.items()}
    return value


def diff_configs(current: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return {"added": sorted(new)}
    current_keys = set(current)
    new_keys = set(new)
    changed = sorted(key for key in current_keys & new_keys if current.get(key) != new.get(key))
    return {
        "added": sorted(new_keys - current_keys),
        "removed": sorted(current_keys - new_keys),
        "changed": changed,
    }


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            redacted[key] = {child_key: redact_value(child_key, child_value) for child_key, child_value in value.items()}
        elif isinstance(value, list):
            redacted[key] = list(value)
        else:
            redacted[key] = redact_value(key, value)
    return redacted


def redact_value(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        if "secret_ref" in value or "env_ref" in value:
            return dict(value)
        return {child_key: redact_value(child_key, child_value) for child_key, child_value in value.items()}
    if isinstance(value, str) and SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    return value


def json_safe_copy(value: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except TypeError as exc:
        raise McpManagementError(f"MCP server config must be JSON serializable: {exc}") from exc
