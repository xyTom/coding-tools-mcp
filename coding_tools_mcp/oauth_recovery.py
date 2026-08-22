"""Operator CLI for recovering durable public OAuth Client registrations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .oauth_store import OAuthStoreError, recover_public_client_registrations
from .settings_store import default_settings_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recover active public PKCE OAuth Client registrations from a clean "
            "backup without restoring Grants, tokens, or signing keys."
        )
    )
    parser.add_argument("--source", required=True, help="cleanly closed source oauth.sqlite3")
    parser.add_argument(
        "--target",
        default=str(default_settings_dir() / "oauth.sqlite3"),
        help="active oauth.sqlite3; defaults to the stable configuration directory",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the import; required to make changes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("--apply is required; no changes were made")
    try:
        result = recover_public_client_registrations(
            Path(args.source),
            Path(args.target),
        )
    except (OAuthStoreError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "imported_client_ids": list(result.imported_client_ids),
                "existing_client_ids": list(result.existing_client_ids),
                "skipped_client_ids": list(result.skipped_client_ids),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
