from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt

from .oauth_store import OAuthAuthorizationStore, OAuthStoreError
from .secret_vault import SecretVault


OAUTH_CODE_TTL_SECONDS = 300
OAUTH_TOKEN_TTL_SECONDS = 24 * 60 * 60
OAUTH_MAX_BODY_BYTES = 8_192
OAUTH_GRANT_TYPE_AUTHORIZATION_CODE = "authorization_code"
# Advertised in AS metadata and used to narrow DCR requests. The token endpoint
# implements authorization_code only — adding an entry here requires a matching
# branch in handle_oauth_token, not just a wider check.
OAUTH_GRANT_TYPES_SUPPORTED = (OAUTH_GRANT_TYPE_AUTHORIZATION_CODE,)
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
    issued_at: int = field(default_factory=lambda: int(time.time()))

    def accepts_redirect(self, redirect_uri: str) -> bool:
        return redirect_uri in self.redirect_uris

    def verifies_secret(self, secret: str) -> bool:
        if self.token_endpoint_auth_method == "none":
            return not secret
        if self.secret_digest is None or not secret:
            return False
        return secrets.compare_digest(self.secret_digest, _secret_digest(secret))


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

    def __init__(self, store: OAuthAuthorizationStore) -> None:
        self.store = store

    def add_preregistered(
        self,
        client_id: str,
        redirect_uris: tuple[str, ...],
        *,
        client_secret: str | None,
    ) -> None:
        redirects = validate_redirect_uris(list(redirect_uris))
        method = "client_secret_post" if client_secret is not None else "none"
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


def oauth_signing_kid(config: OAuthConfig) -> str:
    return config.signing_kid or f"key-{hashlib.sha256(config.token_secret).hexdigest()[:16]}"


def _oauth_signing_key(config: OAuthConfig, kid: str) -> bytes | None:
    if kid in config.signing_keys:
        return config.signing_keys[kid]
    if secrets.compare_digest(kid, oauth_signing_kid(config)):
        return config.token_secret
    return None


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
    try:
        config.store.record_access_token(
            jti,
            grant_id,
            client_id,
            kid,
            scope,
            issued_at=now,
            expires_at=expires_at,
            token_mode=token_mode,
        )
    except OAuthStoreError as exc:
        raise OAuthServiceError("OAuth access-token state could not be persisted.") from exc
    return token


def validate_access_token(token: str, config: OAuthConfig, server_url: str) -> bool:
    if config.store is None:
        return False
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not isinstance(kid, str):
            return False
        key = _oauth_signing_key(config, kid)
        if key is None:
            return False
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
        return False
    client_id = claims.get("client_id")
    grant_id = claims.get("grant_id")
    jti = claims.get("jti")
    if not isinstance(client_id, str) or not client_id:
        return False
    if not isinstance(grant_id, str) or not grant_id:
        return False
    if not isinstance(jti, str) or not jti:
        return False
    if claims.get("sub") != grant_id:
        return False
    return config.store.access_token_is_active(jti)


def _secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _optional_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]
