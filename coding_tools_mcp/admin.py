"""Authenticated, restart-aware management services for server configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable

from .codex_sessions import CodexSessionError, CodexSessionScanner, ScanPolicy
from .oauth import (
    OAUTH_PASSWORD_SECRET,
    OAuthAuthorizationPassword,
    oauth_client_authorization_password_secret_ref,
)
from .oauth_store import OAuthAuthorizationStore, OAuthStoreError
from .operator_api import OperatorPrincipal
from .runner.credentials import RunnerCredentialError, RunnerCredentialStore
from .secret_vault import SecretVault, SecretVaultError
from .settings_definition import (
    EFFECTIVE_DEFAULTS,
    RESTART_FIELDS,
    SECRET_REFERENCE_FIELDS,
    SettingsValidationError,
    effective_startup_settings,
    normalize_startup_settings_with_warnings,
    pending_restart_fields,
    schema_payload,
)
from .settings_store import ServerSettingsStore, SettingsStoreError, sanitize_settings
from .telemetry import telemetry_mode
from .transcript import TranscriptStore, TranscriptStoreError, WorkspaceScope
from .upstream import (
    UpstreamConfigError,
    is_sensitive_env_name,
    parse_server_config,
    parse_tool_search_config,
    parse_credential_policy,
    validate_credential_policy,
)
from .workspace_catalog import WorkspaceCatalog, WorkspaceCatalogError, WorkspaceEntry

ADMIN_API_PREFIX = "/admin/api"
SERVER_SECRET_VAULT_FILENAME = "server-secrets.json"
SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(token|secret|credential|api[_-]?key|password|passwd|authorization)(?:$|[_-])",
    re.I,
)
STARTUP_OVERRIDE_SOURCES = frozenset({"cli", "desktop_cli", "environment"})


class AdminServiceError(ValueError):
    status = 400
    code = "admin_error"


class AdminConflictError(AdminServiceError):
    status = 409
    code = "stale_revision"


class AdminUnavailableError(AdminServiceError):
    status = 503
    code = "admin_unavailable"


class AdminNotFoundError(AdminServiceError):
    status = 404
    code = "not_found"


def document_revision(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except TypeError as exc:
        raise AdminServiceError(f"Value must be JSON serializable: {exc}") from exc


def _redact(value: Any, *, key: str = "") -> Any:
    if key.endswith("_secret_ref"):
        return {"configured": bool(value)}
    if isinstance(value, dict):
        if "secret_ref" in value:
            return {
                "source": "secret_vault",
                "configured": bool(value.get("secret_ref")),
            }
        if "env_ref" in value:
            ref = value.get("env_ref")
            return {
                "source": "system_environment",
                "configured": bool(ref),
                "available": bool(isinstance(ref, str) and os.environ.get(ref)),
            }
        return {str(child): _redact(item, key=str(child)) for child, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if SENSITIVE_KEY_RE.search(key):
        return {
            "source": "local_config",
            "configured": value not in (None, ""),
        }
    return value


def _redact_oauth_item(item: dict[str, Any]) -> dict[str, Any]:
    result = _json_copy(item)
    for key in (
        "client_secret_digest",
        "secret_ref",
        "token_hash",
        "refresh_token",
        "access_token",
        "signing_secret",
    ):
        result.pop(key, None)
    return _redact(result)


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
    except OSError as exc:
        raise AdminUnavailableError(f"Could not atomically save configuration: {exc}") from exc
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_gateway_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"servers": {}, "credential_policy": "local"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdminUnavailableError(f"Could not read Gateway configuration: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AdminServiceError(f"Gateway configuration is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdminServiceError("Gateway configuration must be a JSON object.")
    if "servers" in raw:
        servers = raw.get("servers")
        if not isinstance(servers, dict):
            raise AdminServiceError("Gateway configuration must contain a servers object.")
        result: dict[str, Any] = {
            "servers": _json_copy(servers),
            "credential_policy": parse_credential_policy(raw.get("credential_policy")),
        }
        if "tool_search" in raw:
            tool_search = raw.get("tool_search")
            if not isinstance(tool_search, dict):
                raise AdminServiceError("tool_search must be an object.")
            result["tool_search"] = _json_copy(tool_search)
        return result
    return {"servers": _json_copy(raw), "credential_policy": "local"}




def gateway_file_revision(path: str | Path) -> str:
    return document_revision(_read_gateway_document(Path(path).expanduser()))


def _gateway_credential_sources(
    document: dict[str, Any], vault: SecretVault
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    servers = document.get("servers")
    if not isinstance(servers, dict):
        return result
    for alias, server in servers.items():
        if not isinstance(alias, str) or not isinstance(server, dict):
            continue
        env = server.get("env")
        if not isinstance(env, dict):
            continue
        items: dict[str, dict[str, Any]] = {}
        for name, value in env.items():
            if not isinstance(name, str):
                continue
            source = "local_config"
            status = "configured"
            if isinstance(value, dict) and isinstance(value.get("secret_ref"), str):
                source = "secret_vault"
                try:
                    vault.get_secret(value["secret_ref"])
                except SecretVaultError:
                    status = "unavailable"
            elif isinstance(value, dict) and isinstance(value.get("env_ref"), str):
                source = "system_environment"
                if value["env_ref"] not in os.environ:
                    status = "missing"
            elif not isinstance(value, str) or not value:
                status = "invalid"
            items[name] = {
                "source": source,
                "status": status,
                "sensitive": is_sensitive_env_name(name),
            }
        if items:
            result[alias] = items
    return result

def _secret_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        ref = value.get("secret_ref")
        if isinstance(ref, str) and ref:
            refs.append(ref)
        for child in value.values():
            refs.extend(_secret_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_secret_refs(child))
    return refs


def _validate_gateway_document(document: dict[str, Any], vault: SecretVault) -> dict[str, Any]:
    servers = document.get("servers")
    if not isinstance(servers, dict):
        raise AdminServiceError("Gateway configuration must contain a servers object.")
    try:
        custom_synonyms = parse_tool_search_config(document.get("tool_search"))
        credential_policy = parse_credential_policy(document.get("credential_policy"))
    except UpstreamConfigError as exc:
        raise AdminServiceError(str(exc)) from exc
    normalized: dict[str, Any] = {}
    parsed_configs = []
    for alias, value in servers.items():
        if not isinstance(alias, str) or not isinstance(value, dict):
            raise AdminServiceError("Gateway server entries must use string aliases and object values.")
        try:
            parsed_configs.append(parse_server_config(alias, value))
        except UpstreamConfigError as exc:
            raise AdminServiceError(str(exc)) from exc
        refs = _secret_refs(value)
        if refs and not vault.enabled():
            raise AdminUnavailableError(
                "Gateway secret_ref requires an enabled server Secret Vault."
            )
        for ref in refs:
            try:
                vault.get_secret(ref)
            except SecretVaultError as exc:
                raise AdminUnavailableError(
                    f"Gateway secret_ref {ref!r} cannot be resolved."
                ) from exc
        headers = value.get("headers")
        if isinstance(headers, dict):
            for header_name, header_value in headers.items():
                if (
                    isinstance(header_name, str)
                    and (
                        header_name.lower() in {"authorization", "proxy-authorization"}
                        or SENSITIVE_KEY_RE.search(header_name)
                    )
                    and isinstance(header_value, str)
                    and header_value
                ):
                    raise AdminServiceError(
                        "Sensitive Gateway headers cannot be persisted as plaintext."
                    )
        normalized[alias] = _json_copy(value)
    validate_credential_policy(credential_policy, parsed_configs)
    result: dict[str, Any] = {
        "servers": normalized,
        "credential_policy": credential_policy,
    }
    if "tool_search" in document:
        result["tool_search"] = {
            "custom_synonyms": {
                key: list(values) for key, values in custom_synonyms.items()
            }
        }
    return result


class AdminService:
    """Pure service layer used by the HTTP handler; it contains no handler state."""

    def __init__(
        self,
        *,
        settings_store: ServerSettingsStore,
        active_settings: dict[str, Any],
        fallback_workspace: str | Path,
        gateway_path: str | Path,
        active_gateway_revision: str,
        secret_vault: SecretVault,
        oauth_store: OAuthAuthorizationStore | None = None,
        oauth_secret_vault: SecretVault | None = None,
        oauth_password: OAuthAuthorizationPassword | None = None,
        active_gateway_status: Callable[[], dict[str, Any]] | None = None,
        http_session_status: Callable[[], dict[str, Any]] | None = None,
        runner_credentials: RunnerCredentialStore | None = None,
        runner_status: Callable[[], dict[str, Any]] | None = None,
        transcript_store: TranscriptStore | None = None,
        session_scanner: CodexSessionScanner | None = None,
        active_sources: dict[str, str] | None = None,
        launcher: str | None = None,
        conversation_service: Any | None = None,
    ) -> None:
        self.settings_store = settings_store
        self.active_settings = _json_copy(active_settings)
        self.fallback_workspace = Path(fallback_workspace).expanduser().resolve(strict=True)
        self.gateway_path = Path(gateway_path).expanduser()
        self.active_gateway_revision = active_gateway_revision
        self.secret_vault = secret_vault
        self.oauth_store = oauth_store
        self.oauth_secret_vault = oauth_secret_vault
        self.oauth_password = oauth_password
        self.active_gateway_status = active_gateway_status
        self.http_session_status = http_session_status
        self.runner_credentials = runner_credentials
        self.runner_status = runner_status
        self.transcript_store = transcript_store
        self.session_scanner = session_scanner or CodexSessionScanner()
        self.active_sources = {
            str(field): str(source)
            for field, source in (active_sources or {}).items()
            if source
        }
        self.launcher = str(launcher or "").strip().lower() or None
        self.conversation_service = conversation_service
        self._settings_lock = threading.Lock()
        self._gateway_lock = threading.Lock()
        self._credential_audit_lock = threading.Lock()
        self.credential_audit_path = self.gateway_path.with_name(
            f"{self.gateway_path.stem}-credential-audit.jsonl"
        )

    def status_payload(self) -> dict[str, Any]:
        mode = telemetry_mode()
        payload = {
            "ok": True,
            "admin_api": 1,
            "settings": {"available": True},
            "oauth": {"available": self.oauth_store is not None},
            "gateway": {"available": True, "dynamic_reload": False},
            "chat": {"available": self.transcript_store is not None},
            "runner": {"available": self.runner_credentials is not None},
            "vault": {"enabled": self.secret_vault.enabled()},
            "telemetry": {
                "mode": mode,
                "docs": "docs/telemetry.md",
            },
        }
        if self.http_session_status is not None:
            payload["http_sessions"] = self.http_session_status()
        if self.runner_status is not None:
            payload["runner"]["status"] = self.runner_status()
        return payload

    def runner_credential_payload(self, runner_id: str) -> dict[str, Any]:
        store = self._require_runner_credentials()
        try:
            fingerprint = store.fingerprint(runner_id)
        except RunnerCredentialError as exc:
            raise AdminServiceError(str(exc)) from exc
        if fingerprint is None:
            raise AdminNotFoundError("Runner credential is not configured.")
        return {
            "ok": True,
            "runner_id": runner_id,
            "configured": True,
            "fingerprint": fingerprint,
        }

    def issue_runner_credential(self, runner_id: str) -> dict[str, Any]:
        store = self._require_runner_credentials()
        try:
            issued = store.issue(runner_id)
        except RunnerCredentialError as exc:
            raise AdminServiceError(str(exc)) from exc
        return {
            "ok": True,
            "runner_id": issued.runner_id,
            "credential": issued.credential,
            "fingerprint": issued.fingerprint,
            "created_at": issued.created_at,
            "warning": "This credential is returned only for provisioning; store it securely.",
        }

    def revoke_runner_credential(self, runner_id: str) -> dict[str, Any]:
        store = self._require_runner_credentials()
        try:
            revoked = store.revoke(runner_id)
        except RunnerCredentialError as exc:
            raise AdminServiceError(str(exc)) from exc
        return {"ok": True, "runner_id": runner_id, "revoked": revoked}

    def _require_runner_credentials(self) -> RunnerCredentialStore:
        if self.runner_credentials is None:
            raise AdminUnavailableError("Runner credential management is not configured.")
        return self.runner_credentials

    def bind_http_session_status(self, provider: Callable[[], dict[str, Any]]) -> None:
        """Bind redacted Runtime HTTP counters after the HTTP server is constructed."""

        self.http_session_status = provider

    def _settings_comparison(
        self,
        persisted: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], set[str]]:
        try:
            effective_active = effective_startup_settings(
                self.active_settings,
                self.fallback_workspace,
            )
            effective_persisted = effective_startup_settings(
                persisted,
                self.fallback_workspace,
            )
            differences = set(
                pending_restart_fields(
                    self.active_settings,
                    persisted,
                    self.fallback_workspace,
                )
            )
        except SettingsValidationError as exc:
            raise AdminServiceError(str(exc)) from exc

        field_status: dict[str, dict[str, Any]] = {}
        for field in sorted(RESTART_FIELDS):
            active_value = effective_active.get(field)
            effective_value = effective_persisted.get(field)
            persisted_explicit = field in persisted
            source = self.active_sources.get(field)
            if not source:
                if active_value == effective_value and persisted_explicit:
                    source = "persisted"
                elif (
                    not persisted_explicit
                    and field in EFFECTIVE_DEFAULTS
                    and active_value == EFFECTIVE_DEFAULTS[field]
                ):
                    source = "default"
                else:
                    source = "unknown"

            differs = field in differences
            overridden = differs and source in STARTUP_OVERRIDE_SOURCES
            restart_actionable = differs and not overridden
            if overridden:
                state = "overridden"
                if source == "desktop_cli":
                    conclusion = (
                        "Active is controlled by the Desktop launcher; restarting with the same "
                        "Desktop profile will not use the Persisted value."
                    )
                elif source == "environment":
                    conclusion = (
                        "Active is controlled by an environment variable; restarting with the same "
                        "environment will not use the Persisted value."
                    )
                else:
                    conclusion = (
                        "Active is controlled by a command-line argument; restarting with the same "
                        "command will not use the Persisted value."
                    )
            elif differs:
                state = "pending_restart"
                conclusion = "Effective Persisted differs from Active and can take effect after restart."
            elif not persisted_explicit and field in EFFECTIVE_DEFAULTS:
                state = "in_sync_default"
                conclusion = "Persisted is unset; the effective default matches Active."
            else:
                state = "in_sync"
                conclusion = "Active and effective Persisted are already in sync."

            field_status[field] = {
                "active": active_value,
                "persisted": persisted.get(field) if persisted_explicit else None,
                "persisted_explicit": persisted_explicit,
                "effective_persisted": effective_value,
                "default_value": EFFECTIVE_DEFAULTS.get(field),
                "source": source,
                "state": state,
                "restart_actionable": restart_actionable,
                "conclusion": conclusion,
            }

        actionable = {
            field
            for field in differences
            if field_status.get(field, {}).get("restart_actionable", True)
        }
        return effective_persisted, field_status, actionable

    def settings_payload(self) -> dict[str, Any]:
        result = self.settings_store.read_result()
        persisted = result.settings
        effective_persisted, field_status, pending = self._settings_comparison(persisted)
        pending.update(
            field
            for field in SECRET_REFERENCE_FIELDS
            if self.active_settings.get(field) != persisted.get(field)
        )
        schema = schema_payload()
        schema["restart_fields"] = sorted(
            set(schema.get("restart_fields", ())) | set(SECRET_REFERENCE_FIELDS)
        )
        return {
            "ok": True,
            "active": sanitize_settings(self.active_settings),
            "persisted": sanitize_settings(persisted),
            "effective_persisted": sanitize_settings(effective_persisted),
            "active_sources": dict(sorted(self.active_sources.items())),
            "field_status": field_status,
            "launcher": self.launcher,
            "managed_fields": sorted(
                field for field, source in self.active_sources.items() if source == "desktop_cli"
            ),
            "persisted_revision": document_revision(persisted),
            "pending_restart": sorted(pending),
            "restart_required": bool(pending),
            "migration_warnings": list(result.warnings),
            "schema": schema,
        }

    def validate_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        current = self.settings_store.read()
        updates = body.get("updates", body)
        if not isinstance(updates, dict):
            raise AdminServiceError("settings updates must be an object.")
        try:
            normalized, warnings = normalize_startup_settings_with_warnings(
                current, updates, self.fallback_workspace
            )
        except SettingsValidationError as exc:
            raise AdminServiceError(str(exc)) from exc
        effective_normalized, field_status, pending = self._settings_comparison(normalized)
        pending.update(
            field
            for field in SECRET_REFERENCE_FIELDS
            if self.active_settings.get(field) != normalized.get(field)
        )
        return {
            "ok": True,
            "valid": True,
            "normalized": sanitize_settings(normalized),
            "effective_normalized": sanitize_settings(effective_normalized),
            "field_status": field_status,
            "pending_restart": sorted(pending),
            "restart_required": bool(pending),
            "warnings": list(warnings),
        }

    def save_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        expected = body.get("expected_revision")
        updates = body.get("updates")
        if not isinstance(expected, str) or not expected:
            raise AdminServiceError("expected_revision is required.")
        if not isinstance(updates, dict):
            raise AdminServiceError("updates must be an object.")
        with self._settings_lock:
            current = self.settings_store.read()
            current_revision = document_revision(current)
            if not _constant_equal(expected, current_revision):
                raise AdminConflictError(
                    "Settings changed after this page was loaded; reload before saving."
                )
            try:
                normalized, warnings = normalize_startup_settings_with_warnings(
                    current, updates, self.fallback_workspace
                )
                write_warnings = self.settings_store.write(normalized)
            except (SettingsStoreError, SettingsValidationError) as exc:
                raise AdminServiceError(str(exc)) from exc
        payload = self.settings_payload()
        payload["warnings"] = list(dict.fromkeys((*warnings, *write_warnings)))
        return payload

    def gateway_payload(self) -> dict[str, Any]:
        document = _read_gateway_document(self.gateway_path)
        revision = document_revision(document)
        status = self.active_gateway_status() if self.active_gateway_status else None
        return {
            "ok": True,
            "persisted": _redact(document),
            "credential_policy": document.get("credential_policy", "local"),
            "credential_sources": _gateway_credential_sources(document, self.secret_vault),
            "persisted_revision": revision,
            "active_revision": self.active_gateway_revision,
            "pending_restart": revision != self.active_gateway_revision,
            "restart_required": revision != self.active_gateway_revision,
            "active_status": _redact(status) if isinstance(status, dict) else None,
            "activation": "new_mcp_session_or_service_restart",
            "new_server_defaults": {"expose_mode": "broker"},
            "list_changed": False,
            "dynamic_reload": False,
        }

    def save_gateway(self, body: dict[str, Any]) -> dict[str, Any]:
        expected = body.get("expected_revision")
        document = body.get("document")
        if not isinstance(expected, str) or not expected:
            raise AdminServiceError("expected_revision is required.")
        if not isinstance(document, dict):
            raise AdminServiceError("document must be an object.")
        with self._gateway_lock:
            current = _read_gateway_document(self.gateway_path)
            if not _constant_equal(expected, document_revision(current)):
                raise AdminConflictError(
                    "Gateway configuration changed after this page was loaded; reload before saving."
                )
            normalized = _validate_gateway_document(document, self.secret_vault)
            _atomic_write_json(self.gateway_path, normalized)
        return self.gateway_payload()

    def save_gateway_credential_policy(self, body: dict[str, Any]) -> dict[str, Any]:
        expected = body.get("expected_revision")
        policy = body.get("credential_policy")
        if not isinstance(expected, str) or not expected:
            raise AdminServiceError("expected_revision is required.")
        try:
            normalized_policy = parse_credential_policy(policy)
        except UpstreamConfigError as exc:
            raise AdminServiceError(str(exc)) from exc
        with self._gateway_lock:
            current = _read_gateway_document(self.gateway_path)
            if not _constant_equal(expected, document_revision(current)):
                raise AdminConflictError(
                    "Gateway configuration changed after this page was loaded; reload before saving."
                )
            current["credential_policy"] = normalized_policy
            normalized = _validate_gateway_document(current, self.secret_vault)
            _atomic_write_json(self.gateway_path, normalized)
        return self.gateway_payload()

    def save_gateway_server(self, alias: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create or patch one persisted Gateway server without exposing hidden credentials."""
        expected = body.get("expected_revision")
        config = body.get("config")
        if not isinstance(expected, str) or not expected:
            raise AdminServiceError("expected_revision is required.")
        if not isinstance(config, dict):
            raise AdminServiceError("config must be an object.")
        with self._gateway_lock:
            current = _read_gateway_document(self.gateway_path)
            if not _constant_equal(expected, document_revision(current)):
                raise AdminConflictError(
                    "Gateway configuration changed after this page was loaded; reload before saving."
                )
            servers = current.setdefault("servers", {})
            existed = alias in servers
            existing = servers.get(alias, {})
            if not isinstance(existing, dict):
                raise AdminServiceError(f"Gateway server {alias!r} is not an object.")
            merged = {**existing, **_json_copy(config)}
            if isinstance(existing.get("env"), dict) and isinstance(config.get("env"), dict):
                merged["env"] = {**existing["env"], **_json_copy(config["env"])}
            servers[alias] = merged
            normalized = _validate_gateway_document(current, self.secret_vault)
            _atomic_write_json(self.gateway_path, normalized)
        payload = self.gateway_payload()
        payload["server_alias"] = alias
        payload["created"] = not existed
        payload["affected_count"] = 1
        return payload

    def delete_gateway_server(self, alias: str, body: dict[str, Any]) -> dict[str, Any]:
        expected = body.get("expected_revision")
        if not isinstance(expected, str) or not expected:
            raise AdminServiceError("expected_revision is required.")
        with self._gateway_lock:
            current = _read_gateway_document(self.gateway_path)
            if not _constant_equal(expected, document_revision(current)):
                raise AdminConflictError(
                    "Gateway configuration changed after this page was loaded; reload before saving."
                )
            servers = current.setdefault("servers", {})
            existed = alias in servers
            if existed:
                del servers[alias]
                normalized = _validate_gateway_document(current, self.secret_vault)
                _atomic_write_json(self.gateway_path, normalized)
        payload = self.gateway_payload()
        payload["server_alias"] = alias
        payload["affected_count"] = 1 if existed else 0
        return payload

    def import_mcp_json(self, body: dict[str, Any]) -> dict[str, Any]:
        """Import standard MCP client configuration without echoing credential values."""
        expected = body.get("expected_revision")
        if not isinstance(expected, str) or not expected:
            raise AdminServiceError("expected_revision is required.")
        source = body.get("document", body)
        if not isinstance(source, dict):
            raise AdminServiceError("MCP import document must be an object.")
        servers = source.get("mcpServers")
        if not isinstance(servers, dict):
            raise AdminServiceError("MCP import requires an mcpServers object.")
        overwrite = body.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise AdminServiceError("overwrite must be a boolean.")
        converted: dict[str, Any] = {}
        for alias, value in servers.items():
            if not isinstance(alias, str) or not isinstance(value, dict):
                raise AdminServiceError("MCP server entries must be objects.")
            item = _json_copy(value)
            declared_type = item.pop("type", None)
            if "command" in item or declared_type == "stdio":
                item["transport"] = "stdio"
            elif "url" in item or declared_type in {"http", "streamable_http", "sse"}:
                item["transport"] = "streamable_http"
            else:
                raise AdminServiceError(f"MCP server {alias!r} needs command or url.")
            item.setdefault("enabled", True)
            item.setdefault("expose_mode", "broker")
            converted[alias] = item
        with self._gateway_lock:
            current = _read_gateway_document(self.gateway_path)
            if not _constant_equal(expected, document_revision(current)):
                raise AdminConflictError(
                    "Gateway configuration changed after this page was loaded; reload before importing."
                )
            existing_servers = current.setdefault("servers", {})
            conflicts = sorted(set(existing_servers) & set(converted))
            if conflicts and not overwrite:
                raise AdminConflictError(
                    "MCP import contains existing aliases; enable overwrite or rename them: "
                    + ", ".join(conflicts)
                )
            policy_value = body.get(
                "credential_policy",
                source.get("credential_policy", current.get("credential_policy")),
            )
            current["credential_policy"] = parse_credential_policy(policy_value)
            existing_servers.update(converted)
            normalized = _validate_gateway_document(current, self.secret_vault)
            _atomic_write_json(self.gateway_path, normalized)
        payload = self.gateway_payload()
        payload["imported_aliases"] = sorted(converted)
        payload["overwritten_aliases"] = conflicts
        payload["affected_count"] = len(converted)
        return payload

    def reveal_gateway_credential(self, alias: str, name: str) -> dict[str, Any]:
        value, source = self._resolve_gateway_credential(alias, name)
        self._record_credential_audit(
            "gateway_credential_revealed",
            alias=alias,
            name=name,
            source=source,
        )
        return {
            "ok": True,
            "server_alias": alias,
            "environment_name": name,
            "source": source,
            "value": value,
            "expires_in_seconds": 30,
        }

    def audit_gateway_credential_copy(self, alias: str, name: str) -> dict[str, Any]:
        _value, source = self._resolve_gateway_credential(alias, name)
        event = self._record_credential_audit(
            "gateway_credential_copied",
            alias=alias,
            name=name,
            source=source,
        )
        return {"ok": True, "audit_event_id": event["event_id"]}

    def gateway_credential_audit_payload(self, query: dict[str, str]) -> dict[str, Any]:
        try:
            limit = max(1, min(int(query.get("limit", "100")), 500))
        except ValueError as exc:
            raise AdminServiceError("credential audit limit must be an integer.") from exc
        if not self.credential_audit_path.exists():
            return {"ok": True, "items": [], "count": 0}
        try:
            lines = self.credential_audit_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AdminUnavailableError(f"Could not read credential audit log: {exc}") from exc
        items: list[dict[str, Any]] = []
        for line in reversed(lines[-limit:]):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                items.append(item)
        return {"ok": True, "items": items, "count": len(items)}

    def _resolve_gateway_credential(self, alias: str, name: str) -> tuple[str, str]:
        document = _read_gateway_document(self.gateway_path)
        servers = document.get("servers", {})
        server = servers.get(alias) if isinstance(servers, dict) else None
        if not isinstance(server, dict):
            raise AdminNotFoundError("Gateway server is not present.")
        env = server.get("env")
        if not isinstance(env, dict) or name not in env:
            raise AdminNotFoundError("Gateway environment credential is not present.")
        configured = env[name]
        if isinstance(configured, str):
            if not configured:
                raise AdminUnavailableError("Gateway credential is configured with an empty value.")
            return configured, "local_config"
        if not isinstance(configured, dict):
            raise AdminUnavailableError("Gateway credential configuration is invalid.")
        secret_ref = configured.get("secret_ref")
        if isinstance(secret_ref, str) and secret_ref:
            try:
                return self.secret_vault.get_secret(secret_ref), "secret_vault"
            except SecretVaultError as exc:
                raise AdminUnavailableError(str(exc)) from exc
        env_ref = configured.get("env_ref")
        if isinstance(env_ref, str) and env_ref:
            resolved = os.environ.get(env_ref)
            if resolved is None:
                raise AdminUnavailableError("Referenced system environment variable is not set.")
            return resolved, "system_environment"
        raise AdminUnavailableError("Gateway credential reference is invalid.")

    def _record_credential_audit(
        self,
        event_type: str,
        *,
        alias: str,
        name: str,
        source: str,
    ) -> dict[str, Any]:
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp": time.time(),
            "event_type": event_type,
            "actor_kind": "admin",
            "server_alias": alias,
            "environment_name": name,
            "source": source,
        }
        encoded = json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
        with self._credential_audit_lock:
            try:
                self.credential_audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.credential_audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name != "nt":
                    self.credential_audit_path.chmod(0o600)
            except OSError as exc:
                raise AdminUnavailableError(f"Could not write credential audit log: {exc}") from exc
        return event

    def secrets_payload(self) -> dict[str, Any]:
        if not self.secret_vault.enabled():
            raise AdminUnavailableError("Server Secret Vault is not enabled.")
        try:
            names = self.secret_vault.list_names()
            oauth_password_configured = bool(
                self.oauth_secret_vault is not None
                and self.oauth_secret_vault.enabled()
                and OAUTH_PASSWORD_SECRET in self.oauth_secret_vault.list_names()
            )
        except SecretVaultError as exc:
            raise AdminUnavailableError(str(exc)) from exc
        secrets_payload = [
            {"name": name, "configured": True}
            for name in names
            if name != OAUTH_PASSWORD_SECRET
        ]
        if oauth_password_configured:
            secrets_payload.append(
                {
                    "name": OAUTH_PASSWORD_SECRET,
                    "configured": True,
                    "usage": "oauth_authorization_password",
                    "takes_effect": "immediate",
                }
            )
        return {
            "ok": True,
            "vault_enabled": self.secret_vault.enabled(),
            "secrets": sorted(secrets_payload, key=lambda item: str(item["name"])),
        }

    def set_secret(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        value = body.get("value")
        if not isinstance(value, str) or not value:
            raise AdminServiceError("Secret value must be a non-empty string.")
        oauth_password_secret = name == OAUTH_PASSWORD_SECRET
        try:
            if oauth_password_secret:
                if self.oauth_secret_vault is None or self.oauth_password is None:
                    raise AdminUnavailableError(
                        "OAuth authorization password management is not available."
                    )
                existed = OAUTH_PASSWORD_SECRET in self.oauth_secret_vault.list_names()
                self.oauth_secret_vault.set_secret(OAUTH_PASSWORD_SECRET, value)
                self.oauth_password.rotate(value)
            else:
                existed = name in self.secret_vault.list_names()
                self.secret_vault.set_secret(name, value)
        except SecretVaultError as exc:
            raise AdminUnavailableError(str(exc)) from exc
        result = {
            "ok": True,
            "name": name,
            "configured": True,
            "created": not existed,
            "affected_count": 1,
        }
        if oauth_password_secret:
            result.update(
                {
                    "usage": "oauth_authorization_password",
                    "takes_effect": "immediate",
                    "oauth_applied_immediately": True,
                }
            )
        return result

    def delete_secret(self, name: str) -> dict[str, Any]:
        if name == OAUTH_PASSWORD_SECRET:
            raise AdminServiceError(
                "The active OAuth authorization password cannot be deleted; replace it instead."
            )
        try:
            deleted = self.secret_vault.delete_secret(name)
        except SecretVaultError as exc:
            raise AdminUnavailableError(str(exc)) from exc
        return {"ok": True, "name": name, "affected_count": 1 if deleted else 0}

    def oauth_payload(self, collection: str, query: dict[str, str]) -> dict[str, Any]:
        store = self._require_oauth_store()
        client_id = query.get("client_id") or None
        if collection == "clients":
            items = store.list_clients()
        elif collection == "grants":
            items = store.list_grants(client_id)
        elif collection == "tokens":
            items = store.list_access_tokens(client_id)
        elif collection == "refresh-families":
            items = store.list_refresh_token_families(client_id)
        elif collection == "signing-keys":
            items = store.list_signing_keys()
        elif collection == "audit":
            try:
                limit = int(query.get("limit", "100"))
            except ValueError as exc:
                raise AdminServiceError("audit limit must be an integer.") from exc
            items = store.list_audit_events(limit=limit)
        else:
            raise AdminNotFoundError("Unknown OAuth collection.")
        redacted = [_redact_oauth_item(item) for item in items]
        if collection == "clients":
            for item in redacted:
                identifier = str(item.get("client_id") or "")
                workspace_ids = item.get("workspace_ids")
                configured = bool(
                    identifier
                    and self.oauth_password is not None
                    and self.oauth_password.has_client(identifier)
                )
                item["authorize_login"] = {
                    "configured": configured,
                    "mode": "client" if configured else "global",
                }
                normalized_workspace_ids = (
                    [value for value in workspace_ids if isinstance(value, str) and value]
                    if isinstance(workspace_ids, list)
                    else []
                )
                item["workspace_access"] = {
                    "configured": bool(normalized_workspace_ids),
                    "workspace_ids": normalized_workspace_ids,
                }
        return {"ok": True, "items": redacted, "count": len(redacted)}

    def set_oauth_client_workspaces(
        self,
        client_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        workspace_ids = body.get("workspace_ids")
        if (
            not isinstance(workspace_ids, list)
            or not 1 <= len(workspace_ids) <= 256
            or any(not isinstance(value, str) or not value for value in workspace_ids)
        ):
            raise AdminServiceError(
                "workspace_ids must contain between 1 and 256 non-empty strings."
            )
        if len(set(workspace_ids)) != len(workspace_ids):
            raise AdminServiceError("workspace_ids must not contain duplicates.")
        try:
            catalog = WorkspaceCatalog.from_settings(
                self.active_settings,
                self.fallback_workspace,
            )
            for workspace_id in workspace_ids:
                catalog.get(workspace_id)
        except WorkspaceCatalogError as exc:
            raise AdminServiceError(
                "Workspace is unknown or disabled in the active server configuration."
            ) from exc

        store = self._require_oauth_store()
        client = store.get_client(client_id)
        if client is None:
            raise AdminNotFoundError("OAuth client is not present.")
        previous_workspace_ids = client.get("workspace_ids")
        try:
            applied = store.set_client_workspaces(client_id, workspace_ids)
        except (OAuthStoreError, ValueError) as exc:
            raise AdminServiceError(str(exc)) from exc
        if not applied:
            raise AdminNotFoundError("OAuth client is not present.")
        updated = store.get_client(client_id)
        normalized_workspace_ids = (
            list(updated.get("workspace_ids", [])) if updated is not None else []
        )
        return {
            "ok": True,
            "client_id": client_id,
            "workspace_access": {
                "configured": True,
                "workspace_ids": normalized_workspace_ids,
            },
            "affected_count": (
                0
                if previous_workspace_ids == normalized_workspace_ids
                else 1
            ),
            "applied_immediately": True,
        }

    def set_oauth_client_password(
        self,
        client_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        value = body.get("value")
        if not isinstance(value, str) or not value:
            raise AdminServiceError("OAuth client password must be a non-empty string.")
        store = self._require_oauth_store()
        if store.get_client(client_id) is None:
            raise AdminNotFoundError("OAuth client is not present.")
        if self.oauth_secret_vault is None or self.oauth_password is None:
            raise AdminUnavailableError(
                "OAuth client password management is not available."
            )
        reference = oauth_client_authorization_password_secret_ref(client_id)
        try:
            existed = reference in self.oauth_secret_vault.list_names()
            self.oauth_secret_vault.set_secret(reference, value)
            self.oauth_password.rotate_client(client_id, value)
        except SecretVaultError as exc:
            raise AdminUnavailableError(str(exc)) from exc
        return {
            "ok": True,
            "client_id": client_id,
            "created": not existed,
            "affected_count": 1,
            "takes_effect": "immediate",
            "authorize_login": {"configured": True, "mode": "client"},
        }

    def get_oauth_client_password(self, client_id: str) -> dict[str, Any]:
        store = self._require_oauth_store()
        if store.get_client(client_id) is None:
            raise AdminNotFoundError("OAuth client is not present.")
        if self.oauth_secret_vault is None or self.oauth_password is None:
            raise AdminUnavailableError(
                "OAuth client password management is not available."
            )
        reference = oauth_client_authorization_password_secret_ref(client_id)
        try:
            if reference not in self.oauth_secret_vault.list_names():
                raise AdminNotFoundError(
                    "This OAuth client uses the global password; no dedicated password is set."
                )
            value = self.oauth_secret_vault.get_secret(reference)
        except SecretVaultError as exc:
            raise AdminUnavailableError(str(exc)) from exc
        return {
            "ok": True,
            "client_id": client_id,
            "value": value,
            "authorize_login": {"configured": True, "mode": "client"},
        }

    def reset_oauth_client_password(self, client_id: str) -> dict[str, Any]:
        store = self._require_oauth_store()
        if store.get_client(client_id) is None:
            raise AdminNotFoundError("OAuth client is not present.")
        if self.oauth_secret_vault is None or self.oauth_password is None:
            raise AdminUnavailableError(
                "OAuth client password management is not available."
            )
        reference = oauth_client_authorization_password_secret_ref(client_id)
        try:
            deleted = self.oauth_secret_vault.delete_secret(reference)
            removed = self.oauth_password.reset_client(client_id)
        except SecretVaultError as exc:
            raise AdminUnavailableError(str(exc)) from exc
        return {
            "ok": True,
            "client_id": client_id,
            "affected_count": 1 if deleted or removed else 0,
            "takes_effect": "immediate",
            "authorize_login": {"configured": False, "mode": "global"},
        }

    def oauth_action(self, resource: str, identifier: str, action: str) -> dict[str, Any]:
        store = self._require_oauth_store()
        before_events = {
            str(item.get("event_id"))
            for item in store.list_audit_events(limit=500)
            if item.get("event_id") is not None
        }
        changed = False
        exists = False
        if resource == "clients" and action in {"enable", "disable"}:
            item = store.get_client(identifier)
            exists = item is not None
            if item is not None:
                desired = action == "enable"
                changed = (
                    bool(item.get("enabled")) is not desired
                    and store.set_client_enabled(identifier, desired)
                )
        elif resource == "grants" and action == "revoke":
            item = store.get_grant(identifier)
            exists = item is not None
            if item is not None:
                changed = (
                    item.get("revoked_at") is None
                    and store.revoke_grant(identifier)
                )
        elif resource == "tokens" and action == "revoke":
            item = next((row for row in store.list_access_tokens() if row.get("jti") == identifier), None)
            exists = item is not None
            if item is not None:
                changed = (
                    item.get("revoked_at") is None
                    and store.revoke_access_token(identifier)
                )
        elif resource == "refresh-families" and action == "revoke":
            item = next(
                (row for row in store.list_refresh_token_families() if row.get("family_id") == identifier),
                None,
            )
            exists = item is not None
            if item is not None:
                changed = (
                    item.get("revoked_at") is None
                    and store.revoke_refresh_family(identifier)
                )
        elif resource == "signing-keys" and action in {"activate", "retire", "revoke"}:
            item = next((row for row in store.list_signing_keys() if row.get("kid") == identifier), None)
            exists = item is not None
            if item is not None:
                desired_status = {"activate": "active", "retire": "retired", "revoke": "revoked"}[action]
                if action == "activate":
                    applied = store.activate_signing_key(identifier)
                elif action == "retire":
                    applied = store.retire_signing_key(identifier)
                else:
                    applied = store.revoke_signing_key(identifier)
                changed = item.get("status") != desired_status and applied
        else:
            raise AdminNotFoundError("Unknown OAuth management action.")
        audit_event_id = None
        if changed:
            for event in store.list_audit_events(limit=500):
                candidate = event.get("event_id")
                if candidate is not None and str(candidate) not in before_events:
                    audit_event_id = str(candidate)
                    break
        return {
            "ok": True,
            "resource": resource,
            "id": identifier,
            "action": action,
            "found": exists,
            "affected_count": 1 if changed else 0,
            "audit_event_id": audit_event_id,
        }

    def workspaces_payload(self) -> dict[str, Any]:
        current = self.settings_store.read()
        try:
            catalog = WorkspaceCatalog.from_settings(current, self.fallback_workspace)
        except WorkspaceCatalogError as exc:
            raise AdminServiceError(str(exc)) from exc
        return {
            "ok": True,
            **catalog.settings_payload(),
            "persisted_revision": document_revision(current),
        }

    def workspace_add(self, body: dict[str, Any]) -> dict[str, Any]:
        expected = _required_revision(body)
        entry = body.get("workspace")
        if not isinstance(entry, dict):
            raise AdminServiceError("workspace must be an object.")
        with self._settings_lock:
            current = self._checked_settings(expected)
            catalog = WorkspaceCatalog.from_settings(current, self.fallback_workspace)
            entries = [item.payload() for item in catalog.entries]
            new_entry = {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "root": entry.get("root"),
                "enabled": entry.get("enabled", True),
                "default": entry.get("default", False),
            }
            entries.append(new_entry)
            default_id = str(new_entry["id"]) if new_entry["default"] else catalog.default_id
            for item in entries:
                item["default"] = item.get("id") == default_id
            self._write_workspace_settings(current, entries, default_id)
        return self.workspaces_payload()

    def workspace_disable(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        expected = _required_revision(body)
        with self._settings_lock:
            current = self._checked_settings(expected)
            catalog = WorkspaceCatalog.from_settings(current, self.fallback_workspace)
            if identifier == catalog.default_id:
                raise AdminServiceError("The default Workspace cannot be disabled.")
            entries = [item.payload() for item in catalog.entries]
            target = next((item for item in entries if item["id"] == identifier), None)
            if target is None:
                raise AdminNotFoundError("Workspace is not present in the catalog.")
            target["enabled"] = False
            self._write_workspace_settings(current, entries, catalog.default_id)
        return self.workspaces_payload()

    def workspace_default(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        expected = _required_revision(body)
        with self._settings_lock:
            current = self._checked_settings(expected)
            catalog = WorkspaceCatalog.from_settings(current, self.fallback_workspace)
            entries = [item.payload() for item in catalog.entries]
            target = next((item for item in entries if item["id"] == identifier), None)
            if target is None:
                raise AdminNotFoundError("Workspace is not present in the catalog.")
            if not target["enabled"]:
                raise AdminServiceError("A disabled Workspace cannot become the default.")
            for item in entries:
                item["default"] = item["id"] == identifier
            self._write_workspace_settings(current, entries, identifier)
        return self.workspaces_payload()

    def workspace_check(self, identifier: str) -> dict[str, Any]:
        current = self.settings_store.read()
        catalog = WorkspaceCatalog.from_settings(current, self.fallback_workspace)
        entry = next((item for item in catalog.entries if item.id == identifier), None)
        if entry is None:
            raise AdminNotFoundError("Workspace is not present in the catalog.")
        return {
            "ok": True,
            "workspace": entry.payload(),
            "check": {
                "exists": entry.root.exists(),
                "is_directory": entry.root.is_dir(),
                "enabled": entry.enabled,
                "is_default": entry.default,
            },
        }

    def chat_conversations(self, query: dict[str, str]) -> dict[str, Any]:
        store = self._require_transcript_store()
        workspace_id = query.get("workspace_id") or None
        if workspace_id is not None:
            self._workspace_entry(workspace_id)
        page = _query_int(query, "page", 1)
        page_size = _query_int(query, "page_size", 50)
        payload = store.list_conversations(
            workspace_id,
            page=page,
            page_size=page_size,
            query=query.get("query") or None,
        )
        return {"ok": True, **payload}

    def chat_conversation_detail(self, workspace_id: str, conversation_id: str, query: dict[str, str]) -> dict[str, Any]:
        store = self._require_transcript_store()
        self._workspace_entry(workspace_id)
        payload = store.conversation_detail(
            workspace_id,
            conversation_id,
            message_page=_query_int(query, "message_page", 1),
            message_page_size=_query_int(query, "message_page_size", 100),
            context_page=_query_int(query, "context_page", 1),
            context_page_size=_query_int(query, "context_page_size", 100),
        )
        if payload is None:
            raise AdminNotFoundError("Conversation is not present in the selected Workspace.")
        return {"ok": True, **payload}

    def chat_record_messages(self, workspace_id: str, conversation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        store = self._require_transcript_store()
        self._workspace_entry(workspace_id)
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise AdminServiceError("messages must be a list.")
        return {
            "ok": True,
            **store.record_messages(
                workspace_id,
                conversation_id,
                messages,
                title=body.get("title"),
                source=body.get("source") or "admin-api",
            ),
        }

    def chat_record_context(self, workspace_id: str, conversation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        store = self._require_transcript_store()
        self._workspace_entry(workspace_id)
        entries = body.get("entries")
        if not isinstance(entries, list):
            raise AdminServiceError("entries must be a list.")
        return {
            "ok": True,
            **store.record_context(
                workspace_id,
                conversation_id,
                entries,
                title=body.get("title"),
                source=body.get("source") or "admin-api",
            ),
        }

    def chat_delete(self, resource: str, workspace_id: str, identifier: str) -> dict[str, Any]:
        store = self._require_transcript_store()
        self._workspace_entry(workspace_id)
        if resource == "messages":
            result = store.delete_message(workspace_id, identifier)
        elif resource == "context":
            result = store.delete_context(workspace_id, identifier)
        elif resource == "conversations":
            result = store.delete_conversation(workspace_id, identifier)
        elif resource == "sessions":
            result = store.delete_imported_session(workspace_id, identifier)
        else:
            raise AdminNotFoundError("Unknown chat deletion resource.")
        return {"ok": True, **result}

    def chat_clear_workspace(self, workspace_id: str) -> dict[str, Any]:
        store = self._require_transcript_store()
        self._workspace_entry(workspace_id)
        return {"ok": True, **store.clear_workspace(workspace_id)}

    def conversation_list(self, query: dict[str, str]) -> dict[str, Any]:
        service = self._require_conversation_service()
        principal = self._conversation_principal(query)
        payload = service.list_conversations(
            principal,
            principal.workspace_ids[0],
            page=_query_int(query, "page", 1),
            page_size=_query_int(query, "page_size", 50),
            query=query.get("query") or None,
        )
        return {"ok": True, **payload}

    def conversation_create(self, body: dict[str, Any]) -> dict[str, Any]:
        service = self._require_conversation_service()
        workspace_id = body.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise AdminServiceError("workspace_id is required.")
        return {"ok": True, **service.create_conversation(self._conversation_principal({"workspace_id": workspace_id}), body)}

    def conversation_detail(self, workspace_id: str, conversation_id: str) -> dict[str, Any]:
        service = self._require_conversation_service()
        self._workspace_entry(workspace_id)
        return {
            "ok": True,
            "conversation": service.get_conversation(
                OperatorPrincipal("admin", (workspace_id,)),
                workspace_id,
                conversation_id,
            ),
        }

    def conversation_execution_create(
        self,
        workspace_id: str,
        conversation_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        service = self._require_conversation_service()
        self._workspace_entry(workspace_id)
        return {
            "ok": True,
            **service.create_execution(
                OperatorPrincipal("admin", (workspace_id,)),
                workspace_id,
                conversation_id,
                body,
            ),
        }

    def conversation_turn(
        self,
        workspace_id: str,
        conversation_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        service = self._require_conversation_service()
        self._workspace_entry(workspace_id)
        return service.send_conversation_turn(
            OperatorPrincipal("admin", (workspace_id,)),
            workspace_id,
            conversation_id,
            body,
        )

    def conversation_resume(
        self,
        workspace_id: str,
        conversation_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        service = self._require_conversation_service()
        self._workspace_entry(workspace_id)
        return service.resume_conversation(
            OperatorPrincipal("admin", (workspace_id,)),
            workspace_id,
            conversation_id,
            body,
        )

    def conversation_close(
        self,
        workspace_id: str,
        conversation_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        service = self._require_conversation_service()
        self._workspace_entry(workspace_id)
        return service.close_conversation_execution(
            OperatorPrincipal("admin", (workspace_id,)),
            workspace_id,
            conversation_id,
            body,
        )

    @staticmethod
    def _conversation_principal(query: dict[str, str]) -> OperatorPrincipal:
        workspace_id = query.get("workspace_id") or ""
        if not workspace_id:
            raise AdminServiceError("workspace_id is required.")
        return OperatorPrincipal("admin", (workspace_id,))

    def _require_conversation_service(self) -> Any:
        if self.conversation_service is None:
            raise AdminUnavailableError("Conversation Center is unavailable.")
        return self.conversation_service

    def codex_scan(self, body: dict[str, Any]) -> dict[str, Any]:
        workspace_id = body.get("workspace_id")
        if not isinstance(workspace_id, str):
            raise AdminServiceError("workspace_id is required.")
        scope = self._workspace_scope(workspace_id)
        roots = body.get("roots")
        if roots is not None and (not isinstance(roots, list) or not all(isinstance(item, str) for item in roots)):
            raise AdminServiceError("roots must be a list of relative paths.")
        try:
            policy = _scan_policy(body)
            return {"ok": True, **self.session_scanner.scan(scope, roots=roots, policy=policy)}
        except CodexSessionError as exc:
            raise AdminServiceError(str(exc)) from exc

    def codex_import(self, body: dict[str, Any]) -> dict[str, Any]:
        store = self._require_transcript_store()
        workspace_id = body.get("workspace_id")
        candidate_ids = body.get("candidate_ids")
        if not isinstance(workspace_id, str):
            raise AdminServiceError("workspace_id is required.")
        if not isinstance(candidate_ids, list) or not all(isinstance(item, str) for item in candidate_ids):
            raise AdminServiceError("candidate_ids must be a list of strings.")
        roots = body.get("roots")
        if roots is not None and (not isinstance(roots, list) or not all(isinstance(item, str) for item in roots)):
            raise AdminServiceError("roots must be a list of relative paths.")
        scope = self._workspace_scope(workspace_id)
        try:
            return {
                "ok": True,
                **self.session_scanner.import_candidates(
                    store,
                    scope,
                    candidate_ids=candidate_ids,
                    roots=roots,
                    policy=_scan_policy(body),
                ),
            }
        except (CodexSessionError, TranscriptStoreError) as exc:
            raise AdminServiceError(str(exc)) from exc

    def codex_sessions(self, query: dict[str, str]) -> dict[str, Any]:
        store = self._require_transcript_store()
        workspace_id = query.get("workspace_id") or None
        if workspace_id is not None:
            self._workspace_entry(workspace_id)
        return {
            "ok": True,
            **store.list_imported_sessions(
                workspace_id,
                page=_query_int(query, "page", 1),
                page_size=_query_int(query, "page_size", 50),
            ),
        }

    def _workspace_entry(self, workspace_id: str) -> WorkspaceEntry:
        current = self.settings_store.read()
        try:
            catalog = WorkspaceCatalog.from_settings(current, self.fallback_workspace)
            return catalog.get(workspace_id)
        except WorkspaceCatalogError as exc:
            raise AdminNotFoundError("Workspace is unknown or disabled.") from exc

    def _workspace_scope(self, workspace_id: str) -> WorkspaceScope:
        entry = self._workspace_entry(workspace_id)
        if entry.target != "local" or not isinstance(entry.root, Path):
            raise AdminServiceError(
                "Remote Workspace filesystem operations must run on its Runner Data Plane."
            )
        return WorkspaceScope.create(entry.id, entry.root)

    def _require_transcript_store(self) -> TranscriptStore:
        if self.transcript_store is None:
            raise AdminUnavailableError("Chat persistence is not configured.")
        return self.transcript_store

    def dispatch(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        query: dict[str, str],
    ) -> dict[str, Any]:
        relative = path.removeprefix(ADMIN_API_PREFIX).strip("/")
        parts = [urllib.parse.unquote(part) for part in relative.split("/") if part]
        if method == "GET" and parts == ["status"]:
            return self.status_payload()
        if method == "GET" and parts == ["settings"]:
            return self.settings_payload()
        if method == "POST" and parts == ["settings", "validate"]:
            return self.validate_settings(body)
        if method == "PUT" and parts == ["settings"]:
            return self.save_settings(body)
        if method == "GET" and parts == ["gateway"]:
            return self.gateway_payload()
        if method == "PUT" and parts == ["gateway"]:
            return self.save_gateway(body)
        if method == "POST" and parts == ["gateway", "import-mcp-json"]:
            return self.import_mcp_json(body)
        if len(parts) == 4 and parts[:2] == ["gateway", "credentials"]:
            if method == "GET" and parts[3] == "reveal":
                name = query.get("name")
                if not name:
                    raise AdminServiceError("name is required.")
                return self.reveal_gateway_credential(parts[2], name)
        if method == "GET" and parts == ["gateway", "credential-audit"]:
            return self.gateway_credential_audit_payload(query)
        if method == "PUT" and parts == ["gateway", "credential-policy"]:
            return self.save_gateway_credential_policy(body)
        if method == "GET" and parts == ["gateway", "credential-audit"]:
            return self.gateway_credential_audit_payload(query)
        if (
            len(parts) == 4
            and parts[:2] == ["gateway", "credentials"]
        ):
            if method == "GET" and parts[3] == "reveal":
                return self.reveal_gateway_credential(parts[2], query.get("name", ""))
            if method == "POST" and parts[3] == "copy":
                return self.audit_gateway_credential_copy(parts[2], body.get("name", ""))
        if len(parts) == 3 and parts[:2] == ["gateway", "servers"]:
            if method == "PUT":
                return self.save_gateway_server(parts[2], body)
            if method == "DELETE":
                return self.delete_gateway_server(parts[2], body)
        if method == "GET" and parts == ["secrets"]:
            return self.secrets_payload()
        if len(parts) == 2 and parts[0] == "secrets" and method == "PUT":
            return self.set_secret(parts[1], body)
        if len(parts) == 2 and parts[0] == "secrets" and method == "DELETE":
            return self.delete_secret(parts[1])
        if method == "GET" and parts == ["workspaces"]:
            return self.workspaces_payload()
        if method == "POST" and parts == ["workspaces"]:
            return self.workspace_add(body)
        if len(parts) == 3 and parts[0] == "workspaces" and method == "POST":
            if parts[2] == "disable":
                return self.workspace_disable(parts[1], body)
            if parts[2] == "default":
                return self.workspace_default(parts[1], body)
        if len(parts) == 3 and parts[0] == "workspaces" and parts[2] == "check" and method == "GET":
            return self.workspace_check(parts[1])
        if len(parts) == 3 and parts[0] == "runners" and parts[2] == "credential":
            if method == "GET":
                return self.runner_credential_payload(parts[1])
            if method == "POST":
                return self.issue_runner_credential(parts[1])
            if method == "DELETE":
                return self.revoke_runner_credential(parts[1])
        if len(parts) == 2 and parts[0] == "oauth" and method == "GET":
            return self.oauth_payload(parts[1], query)
        if (
            len(parts) == 4
            and parts[:2] == ["oauth", "clients"]
        ):
            if parts[3] == "authorization-password" and method == "GET":
                return self.get_oauth_client_password(parts[2])
            if parts[3] == "authorization-password" and method == "PUT":
                return self.set_oauth_client_password(parts[2], body)
            if parts[3] == "authorization-password" and method == "DELETE":
                return self.reset_oauth_client_password(parts[2])
            if parts[3] == "workspaces" and method == "PUT":
                return self.set_oauth_client_workspaces(parts[2], body)
        if len(parts) == 4 and parts[0] == "oauth" and method == "POST":
            return self.oauth_action(parts[1], parts[2], parts[3])
        if method == "GET" and parts == ["chat", "conversations"]:
            return self.chat_conversations(query)
        if method == "GET" and parts == ["conversations"]:
            return self.conversation_list(query)
        if method == "POST" and parts == ["conversations"]:
            return self.conversation_create(body)
        if len(parts) == 3 and parts[:2] == ["conversations"] and method == "GET":
            return self.conversation_detail(parts[1], parts[2])
        if len(parts) == 4 and parts[:2] == ["conversations"] and parts[3] == "executions" and method == "POST":
            return self.conversation_execution_create(parts[1], parts[2], body)
        if len(parts) == 4 and parts[:2] == ["conversations"] and method == "POST":
            if parts[3] == "turns":
                return self.conversation_turn(parts[1], parts[2], body)
            if parts[3] == "resume":
                return self.conversation_resume(parts[1], parts[2], body)
            if parts[3] == "close":
                return self.conversation_close(parts[1], parts[2], body)
        if len(parts) == 4 and parts[:2] == ["chat", "conversations"] and method == "GET":
            return self.chat_conversation_detail(parts[2], parts[3], query)
        if len(parts) == 5 and parts[:2] == ["chat", "conversations"] and method == "POST":
            if parts[4] == "messages":
                return self.chat_record_messages(parts[2], parts[3], body)
            if parts[4] == "context":
                return self.chat_record_context(parts[2], parts[3], body)
        if len(parts) == 4 and parts[0] == "chat" and parts[1] in {"messages", "context", "sessions"} and method == "DELETE":
            return self.chat_delete(parts[1], parts[2], parts[3])
        if len(parts) == 4 and parts[:2] == ["chat", "conversations"] and method == "DELETE":
            return self.chat_delete("conversations", parts[2], parts[3])
        if len(parts) == 4 and parts[:2] == ["chat", "workspaces"] and parts[3] == "clear" and method == "POST":
            return self.chat_clear_workspace(parts[2])
        if method == "POST" and parts == ["codex", "sessions", "scan"]:
            return self.codex_scan(body)
        if method == "POST" and parts == ["codex", "sessions", "import"]:
            return self.codex_import(body)
        if method == "GET" and parts == ["codex", "sessions"]:
            return self.codex_sessions(query)
        if len(parts) == 4 and parts[:2] == ["codex", "sessions"] and method == "DELETE":
            return self.chat_delete("sessions", parts[2], parts[3])
        raise AdminNotFoundError("Unknown Admin API endpoint.")

    def _checked_settings(self, expected_revision: str) -> dict[str, Any]:
        current = self.settings_store.read()
        if not _constant_equal(expected_revision, document_revision(current)):
            raise AdminConflictError(
                "Settings changed after this page was loaded; reload before saving."
            )
        return current

    def _write_workspace_settings(
        self,
        current: dict[str, Any],
        entries: list[dict[str, Any]],
        default_id: str,
    ) -> None:
        try:
            normalized, _warnings = normalize_startup_settings_with_warnings(
                current,
                {
                    "workspace_catalog": entries,
                    "default_workspace_id": default_id,
                },
                self.fallback_workspace,
            )
            self.settings_store.write(normalized)
        except (SettingsStoreError, SettingsValidationError, WorkspaceCatalogError) as exc:
            raise AdminServiceError(str(exc)) from exc

    def _require_oauth_store(self) -> OAuthAuthorizationStore:
        if self.oauth_store is None:
            raise AdminUnavailableError("OAuth persistence is not configured.")
        return self.oauth_store

def _query_int(query: dict[str, str], key: str, default: int) -> int:
    raw = query.get(key)
    if raw in (None, ""):
        return default
    assert raw is not None
    try:
        return int(raw)
    except ValueError as exc:
        raise AdminServiceError(f"{key} must be an integer.") from exc


def _scan_policy(body: dict[str, Any]) -> ScanPolicy:
    values: dict[str, int] = {}
    for field in ("max_depth", "max_files", "max_file_bytes", "max_total_bytes", "max_messages"):
        if field in body:
            raw = body[field]
            if isinstance(raw, bool):
                raise AdminServiceError(f"{field} must be an integer.")
            try:
                values[field] = int(raw)
            except (TypeError, ValueError) as exc:
                raise AdminServiceError(f"{field} must be an integer.") from exc
    return ScanPolicy(**values).validated()


def _required_revision(body: dict[str, Any]) -> str:
    value = body.get("expected_revision")
    if not isinstance(value, str) or not value:
        raise AdminServiceError("expected_revision is required.")
    return value


def _constant_equal(left: str, right: str) -> bool:
    return hashlib.sha256(left.encode("utf-8")).digest() == hashlib.sha256(
        right.encode("utf-8")
    ).digest()


__all__ = [
    "ADMIN_API_PREFIX",
    "SERVER_SECRET_VAULT_FILENAME",
    "AdminConflictError",
    "AdminNotFoundError",
    "AdminService",
    "AdminServiceError",
    "AdminUnavailableError",
    "document_revision",
    "gateway_file_revision",
]
