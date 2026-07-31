from __future__ import annotations

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

from coding_tools_mcp.oauth_store import OAuthStoreError
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


class OAuthPersistenceFailClosedTests(unittest.TestCase):
    def test_dcr_authorize_and_token_reject_store_failure_without_fallback(self) -> None:
        with oauth_root() as root:
            config, _created = build_persistent_oauth_config(
                root,
                master_key="synthetic-master-key",
                password="synthetic-authorize-password",
                server_url=None,
                token_ttl=86_400,
                client_id="fail-closed-agent",
                redirect_uris=("http://127.0.0.1/callback",),
            )
            runtime = Runtime(root, oauth_config=config, transport="http")
            server = RuntimeHTTPServer(
                ("127.0.0.1", 0),
                MCPHandler,
                runtime,
                lambda: Runtime(root, oauth_config=config, transport="http"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                registration = urllib.request.Request(
                    f"{base}/oauth/register",
                    data=json.dumps(
                        {
                            "redirect_uris": ["http://127.0.0.1/callback"],
                            "grant_types": ["authorization_code", "refresh_token"],
                            "response_types": ["code"],
                            "token_endpoint_auth_method": "none",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with patch.object(
                    config.store,
                    "list_clients",
                    side_effect=OAuthStoreError("synthetic store failure"),
                ):
                    with self.assertRaises(urllib.error.HTTPError) as dcr_error:
                        urllib.request.urlopen(registration, timeout=5)
                self.assertEqual(dcr_error.exception.code, 503)
                self.assertEqual(json.loads(dcr_error.exception.read())["error"], "server_error")

                query = urllib.parse.urlencode(
                    {
                        "response_type": "code",
                        "client_id": "fail-closed-agent",
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code_challenge": "A" * 43,
                        "code_challenge_method": "S256",
                        "resource": base,
                    }
                )
                with patch.object(
                    config.store,
                    "get_client",
                    side_effect=OAuthStoreError("synthetic store failure"),
                ):
                    with self.assertRaises(urllib.error.HTTPError) as authorize_error:
                        urllib.request.urlopen(f"{base}/oauth/authorize?{query}", timeout=5)
                self.assertEqual(authorize_error.exception.code, 503)

                token_request = urllib.request.Request(
                    f"{base}/oauth/token",
                    data=urllib.parse.urlencode(
                        {
                            "grant_type": "authorization_code",
                            "client_id": "fail-closed-agent",
                            "code": "not-reached",
                            "redirect_uri": "http://127.0.0.1/callback",
                            "code_verifier": "v" * 43,
                            "resource": base,
                        }
                    ).encode("ascii"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with patch.object(
                    config.store,
                    "get_client",
                    side_effect=OAuthStoreError("synthetic store failure"),
                ):
                    with self.assertRaises(urllib.error.HTTPError) as token_error:
                        urllib.request.urlopen(token_request, timeout=5)
                self.assertEqual(token_error.exception.code, 503)
                self.assertEqual(json.loads(token_error.exception.read())["error"], "server_error")
                self.assertEqual(
                    [item["client_id"] for item in config.store.list_clients()],
                    ["fail-closed-agent"],
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
