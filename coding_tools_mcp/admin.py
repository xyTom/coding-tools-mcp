"""Authenticated, restart-aware management services for server configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .codex_sessions import CodexSessionError, CodexSessionScanner, ScanPolicy
from .oauth_store import OAuthAuthorizationStore
from .secret_vault import SecretVault, SecretVaultError
from .settings_definition import (
    SECRET_REFERENCE_FIELDS,
    SettingsValidationError,
    normalize_startup_settings_with_warnings,
    pending_restart_fields,
    schema_payload,
)
from .settings_store import ServerSettingsStore, SettingsStoreError, sanitize_settings
from .telemetry import telemetry_mode
from .transcript import TranscriptStore, TranscriptStoreError, WorkspaceScope
from .upstream import UpstreamConfigError, parse_server_config
from .workspace_catalog import WorkspaceCatalog, WorkspaceCatalogError

ADMIN_API_PREFIX = "/admin/api"
SERVER_SECRET_VAULT_FILENAME = "server-secrets.json"
SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(token|secret|credential|api[_-]?key|password|passwd|authorization)(?:$|[_-])",
    re.I,
)


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
            return {"source": "secret_ref", "configured": bool(value.get("secret_ref"))}
        if "env_ref" in value:
            return {"source": "env_ref", "configured": bool(value.get("env_ref"))}
        return {str(child): _redact(item, key=str(child)) for child, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if SENSITIVE_KEY_RE.search(key):
        return "<redacted>" if value not in (None, "") else value
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
        return {"servers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdminUnavailableError(f"Could not read Gateway configuration: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AdminServiceError(f"Gateway configuration is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdminServiceError("Gateway configuration must be a JSON object.")
    servers = raw.get("servers", raw)
    if not isinstance(servers, dict):
        raise AdminServiceError("Gateway configuration must contain a servers object.")
    return {"servers": _json_copy(servers)}




def gateway_file_revision(path: str | Path) -> str:
    return document_revision(_read_gateway_document(Path(path).expanduser()))

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
    normalized: dict[str, Any] = {}
    for alias, value in servers.items():
        if not isinstance(alias, str) or not isinstance(value, dict):
            raise AdminServiceError("Gateway server entries must use string aliases and object values.")
        try:
            parse_server_config(alias, value)
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
        env = value.get("env")
        if isinstance(env, dict):
            for env_name, env_value in env.items():
                if (
                    isinstance(env_name, str)
                    and SENSITIVE_KEY_RE.search(env_name)
                    and isinstance(env_value, str)
                ):
                    raise AdminServiceError(
                        f"Sensitive Gateway environment value {env_name!r} must use env_ref or secret_ref."
                    )
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
    return {"servers": normalized}


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
        active_gateway_status: Callable[[], dict[str, Any]] | None = None,
        transcript_store: TranscriptStore | None = None,
        session_scanner: CodexSessionScanner | None = None,
    ) -> None:
        self.settings_store = settings_store
        self.active_settings = _json_copy(active_settings)
        self.fallback_workspace = Path(fallback_workspace).expanduser().resolve(strict=True)
        self.gateway_path = Path(gateway_path).expanduser()
        self.active_gateway_revision = active_gateway_revision
        self.secret_vault = secret_vault
        self.oauth_store = oauth_store
        self.active_gateway_status = active_gateway_status
        self.transcript_store = transcript_store
        self.session_scanner = session_scanner or CodexSessionScanner()
        self._settings_lock = threading.Lock()
        self._gateway_lock = threading.Lock()

    def status_payload(self) -> dict[str, Any]:
        mode = telemetry_mode()
        return {
            "ok": True,
            "admin_api": 1,
            "settings": {"available": True},
            "oauth": {"available": self.oauth_store is not None},
            "gateway": {"available": True, "dynamic_reload": False},
            "chat": {"available": self.transcript_store is not None},
            "vault": {"enabled": self.secret_vault.enabled()},
            "telemetry": {
                "mode": mode,
                "docs": "docs/telemetry.md",
            },
        }

    def settings_payload(self) -> dict[str, Any]:
        result = self.settings_store.read_result()
        persisted = result.settings
        pending = set(pending_restart_fields(self.active_settings, persisted))
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
        pending = set(pending_restart_fields(self.active_settings, normalized))
        pending.update(
            field
            for field in SECRET_REFERENCE_FIELDS
            if self.active_settings.get(field) != normalized.get(field)
        )
        return {
            "ok": True,
            "valid": True,
            "normalized": sanitize_settings(normalized),
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
            "persisted_revision": revision,
            "active_revision": self.active_gateway_revision,
            "pending_restart": revision != self.active_gateway_revision,
            "restart_required": revision != self.active_gateway_revision,
            "active_status": _redact(status) if isinstance(status, dict) else None,
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

    def secrets_payload(self) -> dict[str, Any]:
        if not self.secret_vault.enabled():
            raise AdminUnavailableError("Server Secret Vault is not enabled.")
        try:
            names = self.secret_vault.list_names()
        except SecretVaultError as exc:
            raise AdminUnavailableError(str(exc)) from exc
        return {
            "ok": True,
            "vault_enabled": self.secret_vault.enabled(),
            "secrets": [{"name": name, "configured": True} for name in names],
        }

    def set_secret(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        value = body.get("value")
        if not isinstance(value, str) or not value:
            raise AdminServiceError("Secret value must be a non-empty string.")
        try:
            existed = name in self.secret_vault.list_names()
            self.secret_vault.set_secret(name, value)
        except SecretVaultError as exc:
            raise AdminUnavailableError(str(exc)) from exc
        return {
            "ok": True,
            "name": name,
            "configured": True,
            "created": not existed,
            "affected_count": 1,
        }

    def delete_secret(self, name: str) -> dict[str, Any]:
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
        return {"ok": True, "items": redacted, "count": len(redacted)}

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
            self._workspace_scope(workspace_id)
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
        self._workspace_scope(workspace_id)
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
        self._workspace_scope(workspace_id)
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
        self._workspace_scope(workspace_id)
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
        self._workspace_scope(workspace_id)
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
        self._workspace_scope(workspace_id)
        return {"ok": True, **store.clear_workspace(workspace_id)}

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
            self._workspace_scope(workspace_id)
        return {
            "ok": True,
            **store.list_imported_sessions(
                workspace_id,
                page=_query_int(query, "page", 1),
                page_size=_query_int(query, "page_size", 50),
            ),
        }

    def _workspace_scope(self, workspace_id: str) -> WorkspaceScope:
        current = self.settings_store.read()
        try:
            catalog = WorkspaceCatalog.from_settings(current, self.fallback_workspace)
            entry = catalog.get(workspace_id)
        except WorkspaceCatalogError as exc:
            raise AdminNotFoundError("Workspace is unknown or disabled.") from exc
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
        parts = [part for part in relative.split("/") if part]
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
        if len(parts) == 2 and parts[0] == "oauth" and method == "GET":
            return self.oauth_payload(parts[1], query)
        if len(parts) == 4 and parts[0] == "oauth" and method == "POST":
            return self.oauth_action(parts[1], parts[2], parts[3])
        if method == "GET" and parts == ["chat", "conversations"]:
            return self.chat_conversations(query)
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
