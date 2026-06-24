from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any


class SecretVaultError(ValueError):
    pass


class SecretVault:
    def __init__(self, path: str | Path | None, master_key: str | None) -> None:
        self.path = Path(path).expanduser() if path else None
        self.master_key = master_key or None

    def enabled(self) -> bool:
        return self.path is not None and self.master_key is not None

    def status_payload(self) -> dict[str, Any]:
        return {"enabled": self.enabled(), "path": str(self.path) if self.path else None, "secret_count": len(self.list_names())}

    def list_names(self) -> list[str]:
        raw = self._read_raw(require_key=False)
        secrets = raw.get("secrets", {})
        return sorted(secrets) if isinstance(secrets, dict) else []

    def set_secret(self, name: str, value: str) -> None:
        self._require_enabled()
        validate_secret_name(name)
        if not isinstance(value, str):
            raise SecretVaultError("Secret value must be a string.")
        assert self.master_key is not None
        raw = self._read_raw(require_key=True)
        raw.setdefault("version", 1)
        raw.setdefault("secrets", {})[name] = encrypt_value(value, self.master_key)
        self._write_raw(raw)

    def get_secret(self, name: str) -> str:
        self._require_enabled()
        validate_secret_name(name)
        assert self.master_key is not None
        raw = self._read_raw(require_key=True)
        record = raw.get("secrets", {}).get(name) if isinstance(raw.get("secrets"), dict) else None
        if not isinstance(record, dict):
            raise SecretVaultError(f"Secret {name!r} is not set.")
        return decrypt_value(record, self.master_key)

    def delete_secret(self, name: str) -> bool:
        self._require_enabled()
        validate_secret_name(name)
        raw = self._read_raw(require_key=True)
        secrets = raw.setdefault("secrets", {})
        if not isinstance(secrets, dict):
            raise SecretVaultError("Secret vault is corrupt.")
        existed = name in secrets
        secrets.pop(name, None)
        self._write_raw(raw)
        return existed

    def _require_enabled(self) -> None:
        if self.path is None:
            raise SecretVaultError("Secret vault path is not configured.")
        if self.master_key is None:
            raise SecretVaultError("CODING_TOOLS_MCP_SECRETS_KEY is required to write or read real token values.")

    def _read_raw(self, *, require_key: bool) -> dict[str, Any]:
        if require_key:
            self._require_enabled()
        if self.path is None or not self.path.exists():
            return {"version": 1, "secrets": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SecretVaultError(f"Could not read secret vault: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise SecretVaultError(f"Secret vault is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SecretVaultError("Secret vault must be a JSON object.")
        raw.setdefault("version", 1)
        raw.setdefault("secrets", {})
        return raw

    def _write_raw(self, raw: dict[str, Any]) -> None:
        if self.path is None:
            raise SecretVaultError("Secret vault path is not configured.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            if os.name != "nt":
                tmp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp_path, self.path)


def validate_secret_name(name: str) -> None:
    if not isinstance(name, str) or not name or len(name) > 128:
        raise SecretVaultError("Secret name must be a non-empty string up to 128 characters.")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-/")
    if any(char not in allowed for char in name):
        raise SecretVaultError("Secret name contains unsupported characters.")


def encrypt_value(value: str, master_key: str) -> dict[str, str | int]:
    salt = os.urandom(16)
    nonce = os.urandom(16)
    enc_key, mac_key = derive_keys(master_key, salt)
    plaintext = value.encode("utf-8")
    ciphertext = xor_bytes(plaintext, key_stream(enc_key, nonce, len(plaintext)))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return {
        "version": 1,
        "kdf": "pbkdf2-sha256",
        "salt": b64e(salt),
        "nonce": b64e(nonce),
        "ciphertext": b64e(ciphertext),
        "tag": b64e(tag),
    }


def decrypt_value(record: dict[str, Any], master_key: str) -> str:
    try:
        salt = b64d(str(record["salt"]))
        nonce = b64d(str(record["nonce"]))
        ciphertext = b64d(str(record["ciphertext"]))
        tag = b64d(str(record["tag"]))
    except (KeyError, ValueError) as exc:
        raise SecretVaultError("Secret record is corrupt.") from exc
    enc_key, mac_key = derive_keys(master_key, salt)
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise SecretVaultError("Secret vault key is incorrect or the record was modified.")
    plaintext = xor_bytes(ciphertext, key_stream(enc_key, nonce, len(ciphertext)))
    return plaintext.decode("utf-8")


def derive_keys(master_key: str, salt: bytes) -> tuple[bytes, bytes]:
    key_material = hashlib.pbkdf2_hmac("sha256", master_key.encode("utf-8"), salt, 200_000, dklen=64)
    return key_material[:32], key_material[32:]


def key_stream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(output[:length])


def xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right, strict=True))


def b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))
