from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from .i18n import tr


MCP_ENDPOINT_PATH = "/mcp"


def _new_secret() -> str:
    return uuid4().hex + uuid4().hex


@dataclass
class TunnelConfig:
    type: str = "frp"
    public_url: str = ""
    frp_server: str = ""
    frp_subdomain: str = ""
    cloudflare_mode: str = "quick"
    cloudflare_token: str = ""

    def computed_public_url(self) -> str:
        if self.type == "frp" and self.frp_server and self.frp_subdomain:
            return f"https://{self.frp_subdomain}.{self.frp_server}"
        return self.public_url


@dataclass
class AuthConfig:
    type: str = "oauth"
    oauth_password: str = field(default_factory=_new_secret)
    oauth_token_secret: str = field(default_factory=_new_secret)
    bearer_token: str = field(default_factory=_new_secret)


@dataclass
class RuntimeConfig:
    local_port: int = 28766
    permission_mode: str = "trusted"
    runtime_command: str = ""


@dataclass
class WorkspaceProfile:
    id: str
    name: str
    path: str
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    @property
    def endpoint(self) -> str:
        return f"{self.effective_public_url.rstrip('/')}{MCP_ENDPOINT_PATH}"

    @property
    def local_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.runtime.local_port}{MCP_ENDPOINT_PATH}"

    @property
    def effective_public_url(self) -> str:
        return self.tunnel.computed_public_url().rstrip("/")

    def frp_proxy_snippet(self) -> str:
        proxy_name = re.sub(r"[^a-z0-9_-]+", "-", self.name.lower()).strip("-_") or "workspace"
        return "\n".join(
            [
                "[[proxies]]",
                f'name = "{proxy_name}-mcp"',
                'type = "http"',
                'localIP = "127.0.0.1"',
                f"localPort = {self.runtime.local_port}",
                f"subdomain = {json.dumps(self.tunnel.frp_subdomain, ensure_ascii=False)}",
            ]
        )

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "WorkspaceProfile":
        def known_fields(config_cls: type, key: str) -> dict[str, Any]:
            # Drop keys removed or renamed in newer releases so stale
            # profiles.json records keep loading.
            data = dict(record.get(key, {}))
            return {name: value for name, value in data.items() if name in config_cls.__dataclass_fields__}

        return cls(
            id=record["id"],
            name=record["name"],
            path=record["path"],
            tunnel=TunnelConfig(**known_fields(TunnelConfig, "tunnel")),
            auth=AuthConfig(**known_fields(AuthConfig, "auth")),
            runtime=RuntimeConfig(**known_fields(RuntimeConfig, "runtime")),
        )


@dataclass
class RuntimeStatus:
    state: str = "stopped"
    pid: int | None = None
    local_message: str = field(default_factory=lambda: tr("Models", "Not started"))
    public_message: str = field(default_factory=lambda: tr("Models", "Unknown"))


def build_profile(path: str, name: str | None = None) -> WorkspaceProfile:
    if not path.strip():
        raise ValueError(tr("Models", "Workspace path cannot be empty."))
    cleaned = os.path.normpath(path)
    label = name or cleaned.replace("\\", "/").split("/")[-1]
    return WorkspaceProfile(id=uuid4().hex, name=label or tr("Models", "Workspace"), path=cleaned)
