#!/usr/bin/env python3
"""Apply the narrow Clash Verge reliability patch used by the local MCP tunnel.

The patch is intentionally text based so it preserves the user's profile and
does not require PyYAML.  It is idempotent and backs up every changed file.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


OPENAI_GROUP = """- name: OpenAI-Control
  type: fallback
  url: https://api.openai.com/v1/models
  interval: 60
  proxies:
  - OpenAI-Airport
  - Azure-PL-Xray
  lazy: false
  timeout: 5000
  max-failed-times: 1
  expected-status: 401
  hidden: true
"""

AIRPORT_PROVIDER_BLOCK = """proxy-providers:
  OpenAI-Airport-Provider:
    type: file
    path: ./proxy_providers/user3-airport.yaml
    filter: '^(❇️猎户座-(A|C|E|G)|🇯🇵日本-X|🇯🇵东京-E\\(通用\\)|🇸🇬狮城-E\\(通用\\)|🇺🇸西美-(E|F)\\(通用\\))$'
    health-check:
      enable: true
      url: https://api.openai.com/v1/models
      interval: 120
      timeout: 5000
      lazy: false
      expected-status: 401
"""

AIRPORT_CANDIDATE_GROUP = """- name: OpenAI-Airport
  type: fallback
  use:
  - OpenAI-Airport-Provider
  url: https://api.openai.com/v1/models
  interval: 120
  timeout: 5000
  lazy: false
  max-failed-times: 1
  expected-status: 401
  hidden: true
"""


def patched_text(text: str) -> str:
    updated = text
    updated = updated.replace("  strict-route: true\n", "  strict-route: false\n")
    updated = updated.replace(
        "  url: http://cp.cloudflare.com/generate_204\n",
        "  url: https://cp.cloudflare.com/generate_204\n",
    )
    updated = updated.replace("- PROCESS-NAME,tailscale-ipn.exe,DIRECT\n", "")

    if "OpenAI-Airport-Provider:" not in updated:
        proxy_marker = "proxies:\n"
        if proxy_marker not in updated:
            raise RuntimeError("top-level proxies marker not found")
        if "proxy-providers:\n" not in updated:
            updated = updated.replace(proxy_marker, AIRPORT_PROVIDER_BLOCK + proxy_marker, 1)
        else:
            updated = updated.replace(
                "proxy-providers:\n",
                "proxy-providers:\n"
                "  OpenAI-Airport-Provider:\n"
                "    type: file\n"
                "    path: ./proxy_providers/user3-airport.yaml\n"
                "    filter: '^(❇️猎户座-(A|C|E|G)|🇯🇵日本-X|🇯🇵东京-E\\(通用\\)|🇸🇬狮城-E\\(通用\\)|🇺🇸西美-(E|F)\\(通用\\))$'\n"
                "    health-check:\n"
                "      enable: true\n"
                "      url: https://api.openai.com/v1/models\n"
                "      interval: 120\n"
                "      timeout: 5000\n"
                "      lazy: false\n"
                "      expected-status: 401\n",
                1,
            )
    else:
        provider_start = updated.index("  OpenAI-Airport-Provider:\n")
        proxy_marker = updated.find("proxies:\n", provider_start)
        if proxy_marker < 0:
            raise RuntimeError("top-level proxies marker not found after OpenAI airport provider")
        provider_block = AIRPORT_PROVIDER_BLOCK.removeprefix("proxy-providers:\n")
        updated = updated[:provider_start] + provider_block + updated[proxy_marker:]

    if "- name: OpenAI-Airport-Candidates\n" in updated:
        start = updated.index("- name: OpenAI-Airport-Candidates\n")
        next_group = updated.find("- name: ", start + 1)
        if next_group < 0:
            raise RuntimeError("airport candidate group has no following group marker")
        updated = updated[:start] + AIRPORT_CANDIDATE_GROUP + updated[next_group:]
    elif "- name: OpenAI-Airport\n" not in updated:
        group_marker = (
            "- name: OpenAI-Control\n"
            if "- name: OpenAI-Control\n" in updated
            else "- name: Azure极速\n"
        )
        if group_marker not in updated:
            raise RuntimeError("proxy group insertion marker not found")
        updated = updated.replace(group_marker, AIRPORT_CANDIDATE_GROUP + group_marker, 1)
    else:
        start = updated.index("- name: OpenAI-Airport\n")
        next_group = updated.find("- name: ", start + 1)
        if next_group < 0:
            raise RuntimeError("OpenAI-Airport has no following proxy group marker")
        updated = updated[:start] + AIRPORT_CANDIDATE_GROUP + updated[next_group:]

    if "- name: OpenAI-Control\n" not in updated:
        marker = "- name: Azure极速\n"
        if marker not in updated:
            raise RuntimeError("Azure极速 proxy group marker not found")
        updated = updated.replace(marker, OPENAI_GROUP + marker, 1)
    else:
        start = updated.index("- name: OpenAI-Control\n")
        next_group = updated.find("- name: ", start + len("- name: OpenAI-Control\n"))
        if next_group < 0:
            raise RuntimeError("OpenAI-Control has no following proxy group marker")
        updated = updated[:start] + OPENAI_GROUP + updated[next_group:]

    rule = "- DOMAIN,api.openai.com,OpenAI-Control\n"
    if rule not in updated:
        marker = "- DOMAIN-SUFFIX,openai.com,Azure极速\n"
        if marker not in updated:
            raise RuntimeError("OpenAI rule marker not found")
        updated = updated.replace(marker, rule + marker, 1)
    return updated


def patch_file(path: Path, *, dry_run: bool, stamp: str) -> str:
    original = path.read_text(encoding="utf-8")
    updated = patched_text(original)
    if updated == original:
        return f"unchanged\t{path}"
    if dry_run:
        return f"would-change\t{path}"
    backup = path.with_name(f"{path.name}.bak-mcp-reliability-{stamp}")
    shutil.copy2(path, backup)
    temporary = path.with_name(path.name + ".mcp-reliability.tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return f"changed\t{path}\tbackup={backup}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--config-root",
        type=Path,
        required=True,
    )
    args = parser.parse_args()
    root = args.config_root.expanduser().resolve()
    targets = (
        root / "profiles/AzSeal0824V3_2SimpleWin.yaml",
        root / "clash-verge.yaml",
        root / "clash-verge-check.yaml",
    )
    missing = [str(path) for path in targets if not path.is_file()]
    if missing:
        raise SystemExit("missing target(s): " + ", ".join(missing))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for target in targets:
        print(patch_file(target, dry_run=args.dry_run, stamp=stamp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
