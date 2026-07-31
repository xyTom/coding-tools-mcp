from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .models import WorkspaceProfile
from .i18n import tr


APP_HOME = Path.home() / ".coding-tools-mcp-desktop"
PROFILES_FILE = APP_HOME / "profiles.json"
SECRETS_FILE = APP_HOME / "secrets.json"
STATE_DIR = APP_HOME / "state"
PROFILE_ID_RE = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)


def _restrict_permissions(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except (OSError, NotImplementedError):
        # Windows ultimately relies on the user's profile ACL. chmod still
        # narrows permissions where the platform supports POSIX mode bits.
        pass


def write_private_json(path: Path, payload: Any) -> None:
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
        except (OSError, NotImplementedError):
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _restrict_permissions(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


def ensure_storage() -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(APP_HOME, 0o700)
    _restrict_permissions(STATE_DIR, 0o700)
    for path in (PROFILES_FILE, SECRETS_FILE):
        if path.exists():
            _restrict_permissions(path, 0o600)


def load_profiles() -> list[WorkspaceProfile]:
    ensure_storage()
    if not PROFILES_FILE.exists():
        return []
    data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    profiles = [WorkspaceProfile.from_record(item) for item in data.get("profiles", [])]
    secrets = json.loads(SECRETS_FILE.read_text(encoding="utf-8")) if SECRETS_FILE.exists() else {}
    for profile in profiles:
        if profile.id in secrets:
            secret = secrets[profile.id]
            profile.tunnel.cloudflare_token = secret.get("cloudflare_token", profile.tunnel.cloudflare_token)
            profile.auth.oauth_password = secret.get("oauth_password", profile.auth.oauth_password)
            profile.auth.oauth_token_secret = secret.get("oauth_token_secret", profile.auth.oauth_token_secret)
            profile.auth.bearer_token = secret.get("bearer_token", profile.auth.bearer_token)
    return profiles


def save_profiles(profiles: list[WorkspaceProfile]) -> None:
    ensure_storage()
    public_records = []
    secret_records: dict[str, dict[str, str]] = {}
    for profile in profiles:
        record = profile.to_record()
        record["tunnel"]["cloudflare_token"] = ""
        record["auth"]["oauth_password"] = ""
        record["auth"]["oauth_token_secret"] = ""
        record["auth"]["bearer_token"] = ""
        public_records.append(record)
        secret_records[profile.id] = {
            "cloudflare_token": profile.tunnel.cloudflare_token,
            "oauth_password": profile.auth.oauth_password,
            "oauth_token_secret": profile.auth.oauth_token_secret,
            "bearer_token": profile.auth.bearer_token,
        }

    write_private_json(SECRETS_FILE, secret_records)
    write_private_json(PROFILES_FILE, {"profiles": public_records})


def log_dir_for_profile(profile_id: str) -> Path:
    if PROFILE_ID_RE.fullmatch(profile_id) is None:
        raise ValueError(tr("Storage", "Invalid workspace profile ID."))
    ensure_storage()
    target = STATE_DIR / profile_id
    target.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(target, 0o700)
    return target


def runtime_state_file_for_profile(profile_id: str) -> Path:
    return log_dir_for_profile(profile_id) / "runtime.json"
