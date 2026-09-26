from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_tools_mcp.oauth import (
    OAuthClientRegistry,
    OAuthConfig,
    create_access_token,
    create_refresh_token,
    validate_access_token,
    validate_refresh_token,
)


class OAuthRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.registry_path = Path(self.tempdir.name) / "oauth_clients.json"
        self.env = mock.patch.dict(
            os.environ,
            {"CODING_TOOLS_MCP_OAUTH_CLIENT_REGISTRY": str(self.registry_path)},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tempdir.cleanup()

    def test_dynamic_public_client_survives_registry_restart(self) -> None:
        first = OAuthClientRegistry()
        result = first.register(
            {
                "redirect_uris": ["https://client.example/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
        )

        second = OAuthClientRegistry()
        client = second.get(result["client_id"])
        self.assertIsNotNone(client)
        self.assertTrue(
            second.accepts_redirect(result["client_id"], "https://client.example/callback")
        )

    def test_confidential_client_secret_digest_survives_restart(self) -> None:
        first = OAuthClientRegistry()
        result = first.register(
            {
                "redirect_uris": ["https://client.example/callback"],
                "token_endpoint_auth_method": "client_secret_post",
            }
        )

        second = OAuthClientRegistry()
        self.assertTrue(
            second.authenticates(
                result["client_id"], result["client_secret"], "client_secret_post"
            )
        )

    def test_refresh_rotation_preserves_absolute_expiry(self) -> None:
        registry = OAuthClientRegistry()
        registry.add_preregistered(
            "client-1", ("https://client.example/callback",), client_secret=None
        )
        config = OAuthConfig(
            password="test-password",
            server_url="https://mcp.example.com",
            token_secret=b"x" * 32,
            token_ttl=60,
            refresh_token_ttl=120,
            registry=registry,
        )

        refresh = create_refresh_token(config, config.server_url, client_id="client-1")
        refresh_data = validate_refresh_token(refresh, config, config.server_url)
        self.assertIsNotNone(refresh_data)
        assert refresh_data is not None
        client_id, original_expiry = refresh_data

        rotated = create_refresh_token(
            config,
            config.server_url,
            client_id=client_id,
            expires_at=original_expiry,
        )
        rotated_data = validate_refresh_token(rotated, config, config.server_url)
        self.assertEqual(rotated_data, (client_id, original_expiry))

        access = create_access_token(config, config.server_url, client_id=client_id)
        self.assertTrue(validate_access_token(access, config, config.server_url))
        self.assertFalse(validate_access_token(refresh, config, config.server_url))
        self.assertIsNone(validate_refresh_token(access, config, config.server_url))


if __name__ == "__main__":
    unittest.main()
