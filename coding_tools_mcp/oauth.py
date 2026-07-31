from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

import jwt

from .oauth_store import (
    OAuthAuthorizationStore,
    OAuthStoreError,
    RefreshTokenClientMismatchError,
)
from .secret_vault import SecretVault, SecretVaultError


OAUTH_CODE_TTL_SECONDS = 300
OAUTH_TOKEN_TTL_SECONDS = 24 * 60 * 60
OAUTH_MAX_BODY_BYTES = 8_192
OAUTH_GRANT_TYPE_AUTHORIZATION_CODE = "authorization_code"
OAUTH_GRANT_TYPE_REFRESH_TOKEN = "refresh_token"
# Shared by AS metadata, DCR narrowing, and token-endpoint dispatch. A grant
# type belongs here only after its endpoint branch and focused tests are complete.
OAUTH_GRANT_TYPES_SUPPORTED = (
    OAUTH_GRANT_TYPE_AUTHORIZATION_CODE,
    OAUTH_GRANT_TYPE_REFRESH_TOKEN,
)
OAUTH_RESPONSE_TYPES_SUPPORTED = ("code",)
MAX_REDIRECT_URIS = 10
MAX_REGISTERED_CLIENTS = 1_024
MAX_PENDING_CODES = 256


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str
    client_name: str | None = None
    secret_digest: str | None = None
    workspace_id: str | None = None
    issued_at: int = field(default_factory=lambda: int(time.time()))

    def accepts_redirect(self, redirect_uri: str) -> bool:
        return redirect_uri in self.redirect_uris

    def verifies_secret(self, secret: str) -> bool:
        if self.token_endpoint_auth_method == "none":
            return not secret
        if self.secret_digest is None or not secret:
            return False
        return secrets.compare_digest(self.secret_digest, _secret_digest(secret))


@dataclass(frozen=True)
class OAuthIdentity:
    client_id: str
    grant_id: str
    workspace_id: str
    jti: str


@dataclass(frozen=True)
class AccessTokenIssue:
    token: str
    jti: str
    client_id: str
    grant_id: str
    signing_kid: str
    scopes: str
    issued_at: int
    expires_at: int
    token_mode: str


class OAuthClientRegistry:
    """Thread-safe RFC 7591 client registry for one server process."""

    def __init__(self) -> None:
        self._clients: dict[str, OAuthClient] = {}
        self._lock = threading.Lock()

    def add_preregistered(
        self,
        client_id: str,
        redirect_uris: tuple[str, ...],
        *,
        client_secret: str | None,
    ) -> None:
        redirects = validate_redirect_uris(list(redirect_uris))
        method = "client_secret_post" if client_secret is not None else "none"
        client = OAuthClient(
            client_id=client_id,
            redirect_uris=redirects,
            token_endpoint_auth_method=method,
            secret_digest=_secret_digest(client_secret) if client_secret is not None else None,
        )
        with self._lock:
            self._clients[client_id] = client

    def register(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirects, grant_types, response_types, method, client_name = _validated_registration(metadata)
        with self._lock:
            if len(self._clients) >= MAX_REGISTERED_CLIENTS:
                raise ValueError("dynamic client registration limit reached")
            client_id = secrets.token_urlsafe(24)
            while client_id in self._clients:
                client_id = secrets.token_urlsafe(24)
            client_secret = secrets.token_urlsafe(32) if method != "none" else None
            client = OAuthClient(
                client_id=client_id,
                redirect_uris=redirects,
                token_endpoint_auth_method=method,
                client_name=client_name,
                secret_digest=_secret_digest(client_secret) if client_secret is not None else None,
            )
            self._clients[client_id] = client
        return _registration_response(client, grant_types, response_types, client_secret)

    def get(self, client_id: str) -> OAuthClient | None:
        with self._lock:
            return self._clients.get(client_id)

    def accepts_redirect(self, client_id: str, redirect_uri: str) -> bool:
        client = self.get(client_id)
        return client is not None and client.accepts_redirect(redirect_uri)

    def authenticates(self, client_id: str, client_secret: str, auth_method: str) -> bool:
        client = self.get(client_id)
        return (
            client is not None
            and client.token_endpoint_auth_method == auth_method
            and client.verifies_secret(client_secret)
        )


class PersistentOAuthClientRegistry(OAuthClientRegistry):
    """Store-backed registry preserving the upstream registry interface.

    Store errors propagate so callers can fail closed instead of silently
    falling back to an in-memory registry.
    """

    def __init__(
        self,
        store: OAuthAuthorizationStore,
        *,
        registration_workspace_id: str | None = "default",
    ) -> None:
        self.store = store
        self.registration_workspace_id = registration_workspace_id

    def add_preregistered(
        self,
        client_id: str,
        redirect_uris: tuple[str, ...],
        *,
        client_secret: str | None,
        workspace_id: str | None = None,
    ) -> None:
        redirects = validate_redirect_uris(list(redirect_uris))
        method = "client_secret_post" if client_secret is not None else "none"
        resolved_workspace_id = workspace_id or self.registration_workspace_id
        self.store.upsert_client(
            client_id,
            display_name=client_id,
            scopes="mcp",
            redirect_uris=redirects,
            client_type="confidential" if client_secret is not None else "public_pkce",
            token_endpoint_auth_method=method,
            client_secret_digest=(
                _secret_digest(client_secret) if client_secret is not None else None
            ),
            workspace_id=resolved_workspace_id,
        )

    def register(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirects, grant_types, response_types, method, client_name = _validated_registration(metadata)
        if len(self.store.list_clients()) >= MAX_REGISTERED_CLIENTS:
            raise ValueError("dynamic client registration limit reached")
        client_id = secrets.token_urlsafe(24)
        while self.store.get_client(client_id) is not None:
            client_id = secrets.token_urlsafe(24)
        client_secret = secrets.token_urlsafe(32) if method != "none" else None
        client = OAuthClient(
            client_id=client_id,
            redirect_uris=redirects,
            token_endpoint_auth_method=method,
            client_name=client_name,
            secret_digest=_secret_digest(client_secret) if client_secret is not None else None,
        )
        self.store.upsert_client(
            client.client_id,
            display_name=client.client_name or client.client_id,
            scopes="mcp",
            redirect_uris=client.redirect_uris,
            client_type="confidential" if client_secret is not None else "public_pkce",
            token_endpoint_auth_method=client.token_endpoint_auth_method,
            client_secret_digest=client.secret_digest,
            workspace_id=self.registration_workspace_id,
        )
        return _registration_response(client, grant_types, response_types, client_secret)

    def get(self, client_id: str) -> OAuthClient | None:
        record = self.store.get_client(client_id)
        if record is None or not bool(record.get("enabled")) or record.get("revoked_at") is not None:
            return None
        redirects = record.get("redirect_uris")
        if not isinstance(redirects, list) or not all(isinstance(item, str) for item in redirects):
            return None
        method = record.get("token_endpoint_auth_method")
        if method not in {"none", "client_secret_post", "client_secret_basic"}:
            return None
        digest = record.get("client_secret_digest")
        if digest is not None and not isinstance(digest, str):
            return None
        created_at = record.get("created_at")
        return OAuthClient(
            client_id=client_id,
            redirect_uris=tuple(redirects),
            token_endpoint_auth_method=method,
            client_name=str(record.get("display_name") or client_id),
            secret_digest=digest,
            workspace_id=(
                str(record["workspace_id"])
                if isinstance(record.get("workspace_id"), str) and record["workspace_id"]
                else None
            ),
            issued_at=int(created_at) if isinstance(created_at, (int, float)) else int(time.time()),
        )


@dataclass(frozen=True)
class OAuthConfig:
    password: str
    server_url: str | None
    token_secret: bytes
    token_ttl: int = OAUTH_TOKEN_TTL_SECONDS
    registry: OAuthClientRegistry = field(default_factory=OAuthClientRegistry)
    store: OAuthAuthorizationStore | None = None
    secret_vault: SecretVault | None = None
    refresh_token_ttl: int = 60 * 60 * 24 * 90
    signing_kid: str | None = None
    signing_keys: dict[str, bytes] = field(default_factory=dict)
    pending_codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_codes_lock: threading.Lock = field(default_factory=threading.Lock)


class OAuthServiceError(RuntimeError):
    """Persistent OAuth state cannot safely complete the requested operation."""


def create_authorization_grant(
    config: OAuthConfig,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: str,
) -> str:
    if config.store is None:
        raise OAuthServiceError("OAuth authorization store is not configured.")
    client = config.registry.get(client_id)
    if client is None or not client.accepts_redirect(redirect_uri):
        raise OAuthServiceError("OAuth client or redirect URI is not active.")
    try:
        return config.store.create_grant(client_id, scopes)
    except (OAuthStoreError, ValueError) as exc:
        raise OAuthServiceError("OAuth authorization store is unavailable.") from exc


class OAuthClientAuthenticationError(OAuthServiceError):
    pass


class OAuthInvalidGrantError(OAuthServiceError):
    pass


def _active_grant(
    config: OAuthConfig,
    *,
    grant_id: str,
    client_id: str,
) -> dict[str, Any]:
    if config.store is None:
        raise OAuthServiceError("OAuth authorization store is not configured.")
    try:
        grant = config.store.get_grant(grant_id)
    except OAuthStoreError as exc:
        raise OAuthServiceError("OAuth authorization store is unavailable.") from exc
    if (
        grant is None
        or grant.get("client_id") != client_id
        or not bool(grant.get("enabled"))
        or grant.get("revoked_at") is not None
    ):
        raise OAuthInvalidGrantError("OAuth grant is not active.")
    return grant


def issue_refresh_token(
    config: OAuthConfig,
    *,
    grant_id: str,
    client_id: str,
    scopes: str,
) -> str:
    if config.store is None:
        raise OAuthServiceError("OAuth authorization store is not configured.")
    _active_grant(config, grant_id=grant_id, client_id=client_id)
    try:
        _family_id, token = config.store.issue_refresh_token(
            grant_id,
            client_id,
            scopes,
            expires_at=time.time() + config.refresh_token_ttl,
        )
    except OAuthStoreError as exc:
        raise OAuthServiceError("OAuth refresh-token state could not be persisted.") from exc
    return token


def exchange_refresh_token(
    config: OAuthConfig,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    auth_method: str,
    server_url: str,
) -> dict[str, Any]:
    if config.store is None:
        raise OAuthServiceError("OAuth authorization store is not configured.")
    try:
        authenticated = config.registry.authenticates(
            client_id,
            client_secret,
            auth_method,
        )
    except OAuthStoreError as exc:
        raise OAuthServiceError("OAuth client registry is unavailable.") from exc
    if not authenticated:
        raise OAuthClientAuthenticationError("OAuth client authentication failed.")
    try:
        binding = config.store.refresh_token_binding(refresh_token)
    except OAuthStoreError as exc:
        raise OAuthServiceError("OAuth refresh-token store is unavailable.") from exc
    if binding is None:
        raise OAuthInvalidGrantError("Refresh token is invalid, expired, or reused.")
    if not secrets.compare_digest(binding.client_id, client_id):
        raise OAuthClientAuthenticationError("Refresh token client mismatch.")

    access_issue = _prepare_access_token(
        config,
        server_url,
        client_id=binding.client_id,
        grant_id=binding.grant_id,
        scope=binding.scopes,
    )
    try:
        rotated = config.store.rotate_refresh_token_and_record_access_token(
            refresh_token,
            expected_client_id=client_id,
            refresh_expires_at=time.time() + config.refresh_token_ttl,
            access_jti=access_issue.jti,
            access_signing_kid=access_issue.signing_kid,
            access_scopes=access_issue.scopes,
            access_issued_at=access_issue.issued_at,
            access_expires_at=access_issue.expires_at,
            token_mode=access_issue.token_mode,
        )
    except RefreshTokenClientMismatchError as exc:
        raise OAuthClientAuthenticationError("Refresh token client mismatch.") from exc
    except OAuthStoreError as exc:
        raise OAuthServiceError("OAuth refresh-token exchange could not be persisted.") from exc
    if rotated is None:
        raise OAuthInvalidGrantError("Refresh token is invalid, expired, or reused.")
    return {
        "access_token": access_issue.token,
        "token_type": "Bearer",
        "expires_in": config.token_ttl,
        "scope": rotated.scopes,
        "refresh_token": rotated.token,
    }


def validate_redirect_uris(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_REDIRECT_URIS:
        raise ValueError(f"redirect_uris must contain between 1 and {MAX_REDIRECT_URIS} entries")
    redirects: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 2048:
            raise ValueError("redirect_uri must be a string of at most 2048 characters")
        parsed = urllib.parse.urlsplit(item)
        if parsed.fragment or not parsed.scheme or not parsed.netloc or not parsed.hostname:
            raise ValueError("redirect_uri must be an absolute URI without a fragment")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("redirect_uri must not contain user information")
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("HTTP redirect_uri is allowed only for loopback hosts")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("redirect_uri must use HTTPS or loopback HTTP")
        redirects.append(item)
    if len(set(redirects)) != len(redirects):
        raise ValueError("redirect_uris must be unique")
    return tuple(redirects)


def _validated_registration(
    metadata: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str, str | None]:
    redirects = validate_redirect_uris(metadata.get("redirect_uris"))
    requested_grant_types = metadata.get("grant_types", list(OAUTH_GRANT_TYPES_SUPPORTED))
    requested_response_types = metadata.get("response_types", list(OAUTH_RESPONSE_TYPES_SUPPORTED))
    if not isinstance(requested_grant_types, list) or not all(
        isinstance(item, str) for item in requested_grant_types
    ):
        raise ValueError("grant_types must be an array of strings")
    grant_types = tuple(item for item in OAUTH_GRANT_TYPES_SUPPORTED if item in requested_grant_types)
    if not grant_types:
        raise ValueError("grant_types must include at least one supported value")
    if not isinstance(requested_response_types, list) or not all(
        isinstance(item, str) for item in requested_response_types
    ):
        raise ValueError("response_types must be an array of strings")
    response_types = tuple(item for item in OAUTH_RESPONSE_TYPES_SUPPORTED if item in requested_response_types)
    if not response_types:
        raise ValueError("response_types must include at least one supported value")
    method = str(metadata.get("token_endpoint_auth_method") or "none")
    if method not in {"none", "client_secret_post", "client_secret_basic"}:
        raise ValueError("unsupported token_endpoint_auth_method")
    return redirects, grant_types, response_types, method, _optional_text(metadata.get("client_name"), 200)


def _registration_response(
    client: OAuthClient,
    grant_types: tuple[str, ...],
    response_types: tuple[str, ...],
    client_secret: str | None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "client_id": client.client_id,
        "client_id_issued_at": client.issued_at,
        "redirect_uris": list(client.redirect_uris),
        "grant_types": list(grant_types),
        "response_types": list(response_types),
        "token_endpoint_auth_method": client.token_endpoint_auth_method,
    }
    if client.client_name:
        response["client_name"] = client.client_name
    if client_secret is not None:
        response["client_secret"] = client_secret
        response["client_secret_expires_at"] = 0
    return response


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", code_verifier):
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


def valid_pkce_challenge(code_challenge: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge) is not None


def signing_key_id(secret: bytes) -> str:
    return f"key-{hashlib.sha256(secret).hexdigest()[:16]}"


def signing_key_secret_ref(kid: str) -> str:
    return f"oauth/signing/{kid}"


def initialize_signing_key_ring(
    store: OAuthAuthorizationStore,
    vault: SecretVault,
    initial_secret: bytes,
    *,
    legacy_secret_ref: str,
) -> tuple[str, bytes, dict[str, bytes]]:
    if not vault.enabled():
        raise OAuthServiceError("OAuth Secret Vault is not enabled.")
    records = store.list_signing_keys()
    initial_kid = signing_key_id(initial_secret)
    if not records:
        reference = signing_key_secret_ref(initial_kid)
        vault.set_secret(reference, initial_secret.hex())
        store.register_signing_key(
            initial_kid,
            hashlib.sha256(initial_secret).hexdigest(),
            secret_ref=reference,
        )
        records = store.list_signing_keys()

    keys: dict[str, bytes] = {}
    active: list[tuple[str, bytes]] = []
    for record in records:
        status = record.get("status")
        if status not in {"active", "retired"}:
            continue
        raw_kid = record.get("kid")
        raw_reference = record.get("secret_ref")
        if not isinstance(raw_kid, str) or not raw_kid:
            raise OAuthServiceError("OAuth signing-key metadata is invalid.")
        kid = raw_kid
        if not isinstance(raw_reference, str) or not raw_reference:
            raise OAuthServiceError(f"OAuth signing key {kid!r} has no Vault reference.")
        reference = raw_reference
        try:
            secret = bytes.fromhex(vault.get_secret(reference))
        except (ValueError, SecretVaultError) as exc:
            raise OAuthServiceError(
                f"OAuth signing key {kid!r} cannot be loaded from Secret Vault."
            ) from exc
        fingerprint = hashlib.sha256(secret).hexdigest()
        if record.get("fingerprint") != fingerprint or signing_key_id(secret) != kid:
            raise OAuthServiceError(f"OAuth signing key {kid!r} metadata does not match its secret.")
        if reference == legacy_secret_ref:
            migrated_ref = signing_key_secret_ref(kid)
            vault.set_secret(migrated_ref, secret.hex())
            store.register_signing_key(
                kid,
                fingerprint,
                secret_ref=migrated_ref,
                active=status == "active",
            )
        keys[kid] = secret
        if status == "active":
            active.append((kid, secret))
    if len(active) != 1:
        raise OAuthServiceError("OAuth signing-key ring must contain exactly one active key.")
    active_kid, active_secret = active[0]
    return active_kid, active_secret, keys


def rotate_signing_key(config: OAuthConfig) -> OAuthConfig:
    if config.store is None or config.secret_vault is None:
        raise OAuthServiceError("OAuth signing-key persistence is not configured.")
    if not config.secret_vault.enabled():
        raise OAuthServiceError("OAuth Secret Vault is not enabled.")
    secret = secrets.token_bytes(32)
    kid = signing_key_id(secret)
    reference = signing_key_secret_ref(kid)
    try:
        config.secret_vault.set_secret(reference, secret.hex())
        config.store.register_signing_key(
            kid,
            hashlib.sha256(secret).hexdigest(),
            secret_ref=reference,
        )
    except (OAuthStoreError, SecretVaultError, ValueError) as exc:
        raise OAuthServiceError("OAuth signing-key rotation failed.") from exc
    keys = dict(config.signing_keys)
    keys[kid] = secret
    return replace(
        config,
        token_secret=secret,
        signing_kid=kid,
        signing_keys=keys,
    )


def revoke_signing_key(config: OAuthConfig, kid: str) -> bool:
    if config.store is None:
        raise OAuthServiceError("OAuth signing-key persistence is not configured.")
    try:
        return config.store.revoke_signing_key(kid)
    except OAuthStoreError as exc:
        raise OAuthServiceError("OAuth signing-key revocation failed.") from exc


def oauth_signing_kid(config: OAuthConfig) -> str:
    return config.signing_kid or signing_key_id(config.token_secret)


def _oauth_signing_key(config: OAuthConfig, kid: str) -> bytes | None:
    if kid in config.signing_keys:
        return config.signing_keys[kid]
    if secrets.compare_digest(kid, oauth_signing_kid(config)):
        return config.token_secret
    return None


def _prepare_access_token(
    config: OAuthConfig,
    server_url: str,
    *,
    client_id: str,
    grant_id: str,
    scope: str = "mcp",
    token_mode: str = "standard",
) -> AccessTokenIssue:
    now = int(time.time())
    expires_at = now + config.token_ttl
    jti = str(uuid.uuid4())
    kid = oauth_signing_kid(config)
    key = _oauth_signing_key(config, kid)
    if key is None:
        raise OAuthServiceError("OAuth signing key is unavailable.")
    token = jwt.encode(
        {
            "iss": server_url,
            "aud": server_url,
            "sub": grant_id,
            "client_id": client_id,
            "grant_id": grant_id,
            "iat": now,
            "exp": expires_at,
            "scope": scope,
            "jti": jti,
        },
        key,
        algorithm="HS256",
        headers={"kid": kid},
    )
    return AccessTokenIssue(
        token=token,
        jti=jti,
        client_id=client_id,
        grant_id=grant_id,
        signing_kid=kid,
        scopes=scope,
        issued_at=now,
        expires_at=expires_at,
        token_mode=token_mode,
    )


def create_access_token(
    config: OAuthConfig,
    server_url: str,
    *,
    client_id: str,
    grant_id: str,
    scope: str = "mcp",
    token_mode: str = "standard",
) -> str:
    if config.store is None:
        raise OAuthServiceError("OAuth authorization store is not configured.")
    issue = _prepare_access_token(
        config,
        server_url,
        client_id=client_id,
        grant_id=grant_id,
        scope=scope,
        token_mode=token_mode,
    )
    try:
        config.store.record_access_token(
            issue.jti,
            issue.grant_id,
            issue.client_id,
            issue.signing_kid,
            issue.scopes,
            issued_at=issue.issued_at,
            expires_at=issue.expires_at,
            token_mode=issue.token_mode,
        )
    except OAuthStoreError as exc:
        raise OAuthServiceError("OAuth access-token state could not be persisted.") from exc
    return issue.token


def authenticate_access_token(
    token: str,
    config: OAuthConfig,
    server_url: str,
) -> OAuthIdentity | None:
    if config.store is None:
        return None
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not isinstance(kid, str):
            return None
        key = _oauth_signing_key(config, kid)
        if key is None:
            return None
        claims = jwt.decode(
            token,
            key,
            algorithms=["HS256"],
            audience=server_url,
            issuer=server_url,
            options={
                "require": [
                    "iss",
                    "aud",
                    "client_id",
                    "grant_id",
                    "iat",
                    "exp",
                    "jti",
                ]
            },
        )
    except jwt.PyJWTError:
        return None
    client_id = claims.get("client_id")
    grant_id = claims.get("grant_id")
    jti = claims.get("jti")
    if not isinstance(client_id, str) or not client_id:
        return None
    if not isinstance(grant_id, str) or not grant_id:
        return None
    if not isinstance(jti, str) or not jti:
        return None
    if claims.get("sub") != grant_id:
        return None
    persisted = config.store.active_access_token_identity(jti)
    if persisted is None:
        return None
    if persisted["client_id"] != client_id or persisted["grant_id"] != grant_id:
        return None
    return OAuthIdentity(
        client_id=client_id,
        grant_id=grant_id,
        workspace_id=persisted["workspace_id"],
        jti=jti,
    )


def validate_access_token(token: str, config: OAuthConfig, server_url: str) -> bool:
    return authenticate_access_token(token, config, server_url) is not None


def _secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _optional_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]
