from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Mapping
from urllib.parse import urlparse

from .i18n import tr
from .models import MCP_ENDPOINT_PATH, WorkspaceProfile


OPERATOR_APP_PATH = "/app"
ADMIN_APP_PATH = "/admin"

HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)
HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)


def normalize_public_origin(value: str, *, field_name: str = "Public URL") -> str:
    """Validate a shareable HTTPS origin without credentials or path data."""

    raw = value.strip()
    parsed = urlparse(raw)
    try:
        has_custom_port = parsed.port is not None
    except ValueError:
        has_custom_port = True
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or has_custom_port
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            tr(
                "Connectivity",
                "{field} must be an HTTPS URL containing only a domain and no credentials.",
            ).format(field=field_name)
        )
    return raw.rstrip("/")


@dataclass(frozen=True)
class TunnelLaunchSpec:
    provider: str
    public_url: str = ""
    command: tuple[str, ...] = ()
    readiness: str = "external"
    externally_managed: bool = False

    @property
    def managed_process(self) -> bool:
        return bool(self.command)


class TunnelProvider:
    kind = ""
    externally_managed = False

    def validate(self, profile: WorkspaceProfile, *, cloudflared_command: str | None = None) -> None:
        raise NotImplementedError

    def launch_spec(
        self,
        profile: WorkspaceProfile,
        *,
        cloudflared_command: str | None = None,
    ) -> TunnelLaunchSpec:
        raise NotImplementedError

    def resolved_public_url(
        self,
        profile: WorkspaceProfile,
        *,
        state: Mapping[str, object] | None = None,
    ) -> str:
        raise NotImplementedError

    def server_url(self, profile: WorkspaceProfile) -> str:
        return self.resolved_public_url(profile)


class FrpExternalProvider(TunnelProvider):
    kind = "frp"
    externally_managed = True

    def validate(self, profile: WorkspaceProfile, *, cloudflared_command: str | None = None) -> None:
        del cloudflared_command
        if not profile.tunnel.frp_server.strip() or not profile.tunnel.frp_subdomain.strip():
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "FRP mode requires a server domain and subdomain, and an external FRP client must be running.",
                )
            )
        if HOSTNAME_RE.fullmatch(profile.tunnel.frp_server.strip()) is None:
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "The FRP server domain is invalid. Enter a domain without a scheme or path.",
                )
            )
        if HOST_LABEL_RE.fullmatch(profile.tunnel.frp_subdomain.strip()) is None:
            raise RuntimeError(tr("RuntimeManager", "The FRP subdomain is invalid."))

    def launch_spec(
        self,
        profile: WorkspaceProfile,
        *,
        cloudflared_command: str | None = None,
    ) -> TunnelLaunchSpec:
        del cloudflared_command
        return TunnelLaunchSpec(
            provider=self.kind,
            public_url=profile.effective_public_url,
            externally_managed=True,
        )

    def resolved_public_url(
        self,
        profile: WorkspaceProfile,
        *,
        state: Mapping[str, object] | None = None,
    ) -> str:
        del state
        return profile.effective_public_url


class ExternalTunnelProvider(TunnelProvider):
    kind = "external"
    externally_managed = True

    def validate(self, profile: WorkspaceProfile, *, cloudflared_command: str | None = None) -> None:
        del cloudflared_command
        normalize_public_origin(profile.tunnel.public_url, field_name="External public URL")

    def launch_spec(
        self,
        profile: WorkspaceProfile,
        *,
        cloudflared_command: str | None = None,
    ) -> TunnelLaunchSpec:
        del cloudflared_command
        return TunnelLaunchSpec(
            provider=self.kind,
            public_url=normalize_public_origin(
                profile.tunnel.public_url,
                field_name="External public URL",
            ),
            externally_managed=True,
        )

    def resolved_public_url(
        self,
        profile: WorkspaceProfile,
        *,
        state: Mapping[str, object] | None = None,
    ) -> str:
        del state
        value = profile.tunnel.public_url.strip()
        return value.rstrip("/") if value else ""


class CloudflareTunnelProvider(TunnelProvider):
    kind = "cloudflare"

    def validate(self, profile: WorkspaceProfile, *, cloudflared_command: str | None = None) -> None:
        if not cloudflared_command:
            raise RuntimeError(
                tr(
                    "RuntimeManager",
                    "cloudflared was not found. Install the Cloudflare Tunnel CLI first.\n"
                    "On Windows, run: winget install Cloudflare.cloudflared",
                )
            )
        if profile.tunnel.cloudflare_mode not in {"quick", "named"}:
            raise RuntimeError(tr("Connectivity", "Unsupported Cloudflare tunnel mode."))
        if profile.tunnel.cloudflare_mode != "named":
            return
        if not profile.tunnel.cloudflare_token.strip():
            raise RuntimeError(tr("RuntimeManager", "Cloudflare fixed-domain mode requires a Tunnel Token."))
        if not profile.tunnel.public_url.strip():
            raise RuntimeError(tr("RuntimeManager", "Cloudflare fixed-domain mode requires a public URL."))
        normalize_public_origin(profile.tunnel.public_url, field_name="Cloudflare public URL")

    def launch_spec(
        self,
        profile: WorkspaceProfile,
        *,
        cloudflared_command: str | None = None,
    ) -> TunnelLaunchSpec:
        self.validate(profile, cloudflared_command=cloudflared_command)
        assert cloudflared_command is not None
        if profile.tunnel.cloudflare_mode == "named":
            return TunnelLaunchSpec(
                provider=self.kind,
                public_url=normalize_public_origin(
                    profile.tunnel.public_url,
                    field_name="Cloudflare public URL",
                ),
                command=(
                    cloudflared_command,
                    "tunnel",
                    "run",
                    "--token",
                    profile.tunnel.cloudflare_token.strip(),
                ),
                readiness="cloudflare_named",
            )
        return TunnelLaunchSpec(
            provider=self.kind,
            command=(
                cloudflared_command,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{profile.runtime.local_port}",
            ),
            readiness="cloudflare_quick",
        )

    def resolved_public_url(
        self,
        profile: WorkspaceProfile,
        *,
        state: Mapping[str, object] | None = None,
    ) -> str:
        if profile.tunnel.cloudflare_mode == "named":
            return profile.tunnel.public_url.strip().rstrip("/")
        if state is None:
            return ""
        value = state.get("public_url")
        return str(value).rstrip("/") if isinstance(value, str) and value.strip() else ""

    def server_url(self, profile: WorkspaceProfile) -> str:
        if profile.tunnel.cloudflare_mode == "named":
            return profile.tunnel.public_url.strip().rstrip("/")
        return ""


class TunnelProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, TunnelProvider] = {
            provider.kind: provider
            for provider in (
                FrpExternalProvider(),
                CloudflareTunnelProvider(),
                ExternalTunnelProvider(),
            )
        }

    def get(self, kind: str) -> TunnelProvider:
        provider = self._providers.get(kind)
        if provider is None:
            raise RuntimeError(
                tr("Connectivity", "Unsupported tunnel provider: {provider}").format(provider=kind)
            )
        return provider

    def for_profile(self, profile: WorkspaceProfile) -> TunnelProvider:
        return self.get(profile.tunnel.type)


@dataclass(frozen=True)
class MobileOnboarding:
    provider: str
    runtime_status: str
    runner_status: str
    auth_mode: str
    auth_required: bool
    externally_managed: bool
    public_base_url: str
    operator_app_url: str
    mcp_url: str
    admin_url: str

    def to_record(self) -> dict[str, object]:
        return asdict(self)


def build_mobile_onboarding(
    profile: WorkspaceProfile,
    *,
    public_url: str,
    runtime_status: str,
    runner_status: str,
) -> MobileOnboarding:
    base_url = public_url.strip().rstrip("/")
    provider = TunnelProviderRegistry().for_profile(profile)
    if base_url:
        # All shareable onboarding links are origins only. This also rejects
        # accidental bearer/password material embedded in userinfo or query strings.
        base_url = normalize_public_origin(base_url, field_name="Public URL")
    return MobileOnboarding(
        provider=provider.kind,
        runtime_status=runtime_status,
        runner_status=runner_status or "unknown",
        auth_mode=profile.auth.type,
        auth_required=profile.auth.type != "noauth",
        externally_managed=provider.externally_managed,
        public_base_url=base_url,
        operator_app_url=f"{base_url}{OPERATOR_APP_PATH}" if base_url else "",
        mcp_url=f"{base_url}{MCP_ENDPOINT_PATH}" if base_url else "",
        admin_url=f"{base_url}{ADMIN_APP_PATH}" if base_url else "",
    )
