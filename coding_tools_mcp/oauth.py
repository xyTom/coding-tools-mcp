from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt


OAUTH_CODE_TTL_SECONDS = 300
OAUTH_TOKEN_TTL_SECONDS = 24 * 60 * 60
OAUTH_REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
OAUTH_MAX_BODY_BYTES = 8_192
OAUTH_GRANT_TYPE_AUTHORIZATION_CODE = "authorization_code"
OAUTH_GRANT_TYPE_REFRESH_TOKEN = "refresh_token"
OAUTH_GRANT_TYPES_SUPPORTED = (OAUTH_GRANT_TYPE_AUTHORIZATION_CODE, OAUTH_GRANT_TYPE_REFRESH_TOKEN)
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
        configured = os.environ.get("CODING_TOOLS_MCP_OAUTH_CLIENT_REGISTRY", "").strip()
        self._storage_path = Path(configured) if configured else Path.home() / ".coding-tools-mcp" / "oauth_clients.json"
        self._load_persisted()

    def _load_persisted(self) -> None:
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        for item in payload.get("clients", []):
            try:
                client = OAuthClient(client_id=str(item["client_id"]), redirect_uris=validate_redirect_uris(list(item["redirect_uris"])), token_endpoint_auth_method=str(item.get("token_endpoint_auth_method") or "none"), client_name=item.get("client_name") or None, secret_digest=item.get("secret_digest") or None, issued_at=int(item.get("issued_at") or time.time()))
            except (KeyError, TypeError, ValueError):
                continue
            self._clients[client.client_id] = client

    def _persist_locked(self) -> None:
        payload = {"clients": [{"client_id": c.client_id, "redirect_uris": list(c.redirect_uris), "token_endpoint_auth_method": c.token_endpoint_auth_method, "client_name": c.client_name, "secret_digest": c.secret_digest, "issued_at": c.issued_at} for c in self._clients.values()]}
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._storage_path.with_suffix(self._storage_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._storage_path)
        try:
            os.chmod(self._storage_path, 0o600)
        except OSError:
            pass

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
            self._persist_locked()

    def register(self, metadata: dict[str, Any]) -> dict[str, Any]:
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
                client_name=_optional_text(metadata.get("client_name"), 200),
                secret_digest=_secret_digest(client_secret) if client_secret is not None else None,
            )
            self._clients[client_id] = client
            self._persist_locked()
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


@dataclass(frozen=True)
class OAuthConfig:
    password: str
    server_url: str | None
    token_secret: bytes
    token_ttl: int = OAUTH_TOKEN_TTL_SECONDS
    refresh_token_ttl: int = OAUTH_REFRESH_TOKEN_TTL_SECONDS
    registry: OAuthClientRegistry = field(default_factory=OAuthClientRegistry)
    pending_codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_codes_lock: threading.Lock = field(default_factory=threading.Lock)


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


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", code_verifier):
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


def valid_pkce_challenge(code_challenge: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge) is not None


def create_access_token(config: OAuthConfig, server_url: str, *, client_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": server_url,
            "aud": server_url,
            "sub": client_id,
            "client_id": client_id,
            "iat": now,
            "exp": now + config.token_ttl,
            "scope": "mcp",
            "token_use": "access",
        },
        config.token_secret,
        algorithm="HS256",
    )


def validate_access_token(token: str, config: OAuthConfig, server_url: str) -> bool:
    try:
        claims = jwt.decode(
            token,
            config.token_secret,
            algorithms=["HS256"],
            audience=server_url,
            issuer=server_url,
        )
    except jwt.PyJWTError:
        return False
    client_id = claims.get("client_id")
    return (
        claims.get("token_use") == "access"
        and isinstance(client_id, str)
        and config.registry.get(client_id) is not None
    )


def create_refresh_token(
    config: OAuthConfig, server_url: str, *, client_id: str, expires_at: int | None = None
) -> str:
    now = int(time.time())
    absolute_exp = min(expires_at or (now + config.refresh_token_ttl), now + config.refresh_token_ttl)
    return jwt.encode(
        {
            "iss": server_url, "aud": server_url, "sub": client_id, "client_id": client_id,
            "iat": now, "exp": absolute_exp, "scope": "mcp", "token_use": "refresh",
            "jti": secrets.token_urlsafe(16),
        },
        config.token_secret, algorithm="HS256",
    )


def validate_refresh_token(token: str, config: OAuthConfig, server_url: str) -> tuple[str, int] | None:
    try:
        claims = jwt.decode(token, config.token_secret, algorithms=["HS256"], audience=server_url, issuer=server_url)
    except jwt.PyJWTError:
        return None
    client_id = claims.get("client_id")
    exp = claims.get("exp")
    if claims.get("token_use") != "refresh" or not isinstance(client_id, str) or not isinstance(exp, int):
        return None
    if config.registry.get(client_id) is None:
        return None
    return client_id, exp


def _secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _optional_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]
