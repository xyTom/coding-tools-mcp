from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from coding_tools_mcp.oauth_store import OAuthAuthorizationStore
from coding_tools_mcp.server import (
    MCPHandler,
    Runtime,
    RuntimeHTTPServer,
    build_persistent_oauth_config,
)


@contextmanager
def oauth_root() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp())
    try:
        yield root
    finally:
        database = root / "oauth.sqlite3"
        if database.exists():
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()
        for attempt in range(20):
            try:
                shutil.rmtree(root)
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                retryable = os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 145}
                if not retryable or attempt == 19:
                    raise
                time.sleep(0.05)


def start_server(root: Path, config):  # type: ignore[no-untyped-def]
    runtime = Runtime(root, oauth_config=config, transport="http")
    server = RuntimeHTTPServer(
        ("127.0.0.1", 0),
        MCPHandler,
        runtime,
        lambda: Runtime(root, oauth_config=config, transport="http"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def stop_server(server: RuntimeHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def post_form(base: str, path: str, payload: dict[str, str]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=urllib.parse.urlencode(payload).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class RefreshTokenHTTPTests(unittest.TestCase):
    def test_rotation_survives_rebuild_and_reuse_revokes_family(self) -> None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
                return None

        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=None,
                token_ttl=86_400,
            )
            server, thread, base = start_server(root, config)
            verifier = "r" * 43
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            registration = urllib.request.Request(
                f"{base}/oauth/register",
                data=json.dumps(
                    {
                        "client_name": "Refresh Agent",
                        "redirect_uris": ["http://127.0.0.1/callback"],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                metadata = json.loads(
                    urllib.request.urlopen(
                        f"{base}/.well-known/oauth-authorization-server",
                        timeout=5,
                    ).read()
                )
                self.assertEqual(
                    metadata["grant_types_supported"],
                    ["authorization_code", "refresh_token"],
                )
                registered = json.loads(
                    urllib.request.urlopen(registration, timeout=5).read()
                )
                self.assertEqual(
                    registered["grant_types"],
                    ["authorization_code", "refresh_token"],
                )
                authorize = urllib.request.Request(
                    f"{base}/oauth/authorize",
                    data=urllib.parse.urlencode(
                        {
                            "client_id": str(registered["client_id"]),
                            "redirect_uri": "http://127.0.0.1/callback",
                            "code_challenge": challenge,
                            "code_challenge_method": "S256",
                            "state": "state-refresh",
                            "resource": base,
                            "password": "synthetic-authorize-password",
                        }
                    ).encode("ascii"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                opener = urllib.request.build_opener(NoRedirect)
                with self.assertRaises(urllib.error.HTTPError) as redirected:
                    opener.open(authorize, timeout=5)
                self.assertEqual(redirected.exception.code, 302)
                location = redirected.exception.headers["Location"]
                code = urllib.parse.parse_qs(
                    urllib.parse.urlparse(location).query
                )["code"][0]
                token_status, issued = post_form(
                    base,
                    "/oauth/token",
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code_verifier": verifier,
                        "client_id": str(registered["client_id"]),
                        "resource": base,
                    },
                )
                self.assertEqual(token_status, 200)
                self.assertTrue(issued["access_token"])
                original_refresh = str(issued["refresh_token"])
            finally:
                stop_server(server, thread)

            reopened, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=None,
                token_ttl=86_400,
            )
            restarted, restarted_thread, restarted_base = start_server(root, reopened)
            try:
                original_audit = OAuthAuthorizationStore._audit

                def fail_access_audit(
                    conn: sqlite3.Connection,
                    event_type: str,
                    **kwargs: object,
                ) -> None:
                    if event_type == "access_token_issued":
                        raise sqlite3.IntegrityError("injected access-token audit failure")
                    original_audit(conn, event_type, **kwargs)  # type: ignore[arg-type]

                with patch.object(
                    OAuthAuthorizationStore,
                    "_audit",
                    staticmethod(fail_access_audit),
                ):
                    failed_status, failed = post_form(
                        restarted_base,
                        "/oauth/token",
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": original_refresh,
                            "client_id": str(registered["client_id"]),
                        },
                    )
                self.assertEqual(failed_status, 503)
                self.assertEqual(failed["error"], "server_error")

                refresh_status, refreshed = post_form(
                    restarted_base,
                    "/oauth/token",
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": original_refresh,
                        "client_id": str(registered["client_id"]),
                    },
                )
                self.assertEqual(refresh_status, 200)
                replacement = str(refreshed["refresh_token"])
                self.assertNotEqual(replacement, original_refresh)
                self.assertTrue(refreshed["access_token"])

                replay_status, replay = post_form(
                    restarted_base,
                    "/oauth/token",
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": original_refresh,
                        "client_id": str(registered["client_id"]),
                    },
                )
                self.assertEqual(replay_status, 400)
                self.assertEqual(replay["error"], "invalid_grant")

                replacement_status, replacement_error = post_form(
                    restarted_base,
                    "/oauth/token",
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": replacement,
                        "client_id": str(registered["client_id"]),
                    },
                )
                self.assertEqual(replacement_status, 400)
                self.assertEqual(replacement_error["error"], "invalid_grant")
                families = reopened.store.list_refresh_token_families(
                    str(registered["client_id"])
                )
                self.assertEqual(len(families), 1)
                self.assertIsNotNone(families[0]["revoked_at"])
                self.assertEqual(families[0]["revoke_reason"], "refresh_token_reuse")
            finally:
                stop_server(restarted, restarted_thread)


if __name__ == "__main__":
    unittest.main()
