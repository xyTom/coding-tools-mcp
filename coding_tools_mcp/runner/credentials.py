"""Runner-specific credential primitives.

Runner credentials intentionally do not reuse MCP OAuth or Admin credentials.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from ..secret_vault import SecretVault, SecretVaultError
from .protocol import validate_identifier


class RunnerCredentialError(ValueError):
    pass


@dataclass(frozen=True)
class IssuedRunnerCredential:
    runner_id: str
    credential: str
    fingerprint: str
    created_at: str


@dataclass(frozen=True)
class AuthenticatedRunnerCredential:
    runner_id: str
    fingerprint: str


class RunnerCredentialStore:
    """Runner credential registry with optional SecretVault persistence."""

    def __init__(self, vault: SecretVault | None = None) -> None:
        self._vault = vault
        self._credentials: dict[str, tuple[str, str]] = {}

    def issue(self, runner_id: str) -> IssuedRunnerCredential:
        validate_identifier(runner_id, "runner_id")
        secret = secrets.token_urlsafe(48)
        fingerprint = _fingerprint(secret)
        if self._vault is not None:
            try:
                self._vault.set_secret(_secret_name(runner_id), secret)
            except SecretVaultError as exc:
                raise RunnerCredentialError("runner credential store is unavailable") from exc
        else:
            self._credentials[runner_id] = (secret, fingerprint)
        return IssuedRunnerCredential(
            runner_id=runner_id,
            credential=secret,
            fingerprint=fingerprint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def authenticate(self, runner_id: str, credential: str) -> AuthenticatedRunnerCredential:
        validate_identifier(runner_id, "runner_id")
        if not isinstance(credential, str) or not credential:
            raise RunnerCredentialError("runner credential is required")
        if self._vault is not None:
            try:
                expected = self._vault.get_secret(_secret_name(runner_id))
            except SecretVaultError as exc:
                raise RunnerCredentialError("unknown runner") from exc
            fingerprint = _fingerprint(expected)
        else:
            stored = self._credentials.get(runner_id)
            if stored is None:
                raise RunnerCredentialError("unknown runner")
            expected, fingerprint = stored
        if not secrets.compare_digest(expected, credential):
            raise RunnerCredentialError("invalid runner credential")
        return AuthenticatedRunnerCredential(runner_id, fingerprint)

    def revoke(self, runner_id: str) -> bool:
        validate_identifier(runner_id, "runner_id")
        if self._vault is not None:
            try:
                return self._vault.delete_secret(_secret_name(runner_id))
            except SecretVaultError as exc:
                raise RunnerCredentialError("runner credential store is unavailable") from exc
        return self._credentials.pop(runner_id, None) is not None

    def fingerprint(self, runner_id: str) -> str | None:
        validate_identifier(runner_id, "runner_id")
        if self._vault is not None:
            try:
                return _fingerprint(self._vault.get_secret(_secret_name(runner_id)))
            except SecretVaultError:
                return None
        record = self._credentials.get(runner_id)
        return record[1] if record else None


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _secret_name(runner_id: str) -> str:
    identity = hashlib.sha256(runner_id.encode("utf-8")).hexdigest()[:32]
    return f"runner/credential/{identity}"
