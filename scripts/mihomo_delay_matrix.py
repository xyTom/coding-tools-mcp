#!/usr/bin/env python3
"""Probe selected Mihomo proxies through the live named-pipe controller.

This is a diagnostics helper for Windows Clash Verge deployments.  It asks
Mihomo itself to run delay tests, so the measured path matches production
proxy dialing rather than being intercepted a second time by TUN.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from windows_pipe_http import parse_response, request  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def get_json(pipe: str, target: str) -> tuple[int, dict[str, object]]:
    status, body = parse_response(request(pipe, "GET", target))
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"raw": body.decode("utf-8", errors="replace")}
    return status, payload if isinstance(payload, dict) else {"value": payload}


def probe(pipe: str, name: str, url: str, timeout_ms: int, expected: int | None) -> dict[str, object]:
    quoted_name = urllib.parse.quote(name, safe="")
    query = {
        "timeout": str(timeout_ms),
        "url": url,
    }
    if expected is not None:
        query["expected"] = str(expected)
    target = f"/proxies/{quoted_name}/delay?{urllib.parse.urlencode(query)}"
    try:
        status, payload = get_json(pipe, target)
        delay = payload.get("delay")
        message = payload.get("message")
        return {"name": name, "status": status, "delay": delay, "message": message}
    except Exception as exc:  # noqa: BLE001 - diagnostic output must continue
        return {"name": name, "status": 0, "delay": None, "message": type(exc).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipe", default=r"\\.\pipe\verge-mihomo")
    parser.add_argument("--url", default="https://api.openai.com/v1/models")
    parser.add_argument("--expected", type=int, default=401)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--prefix", default="WS-")
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--group", help="probe exactly the members of this live proxy group")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    status, payload = get_json(args.pipe, "/proxies")
    if status != 200:
        raise SystemExit(f"could not list proxies: HTTP {status}")
    raw = payload.get("proxies")
    if not isinstance(raw, dict):
        raise SystemExit("Mihomo /proxies response did not contain a proxy map")
    if args.group:
        group = raw.get(args.group)
        members = group.get("all") if isinstance(group, dict) else None
        if not isinstance(members, list):
            raise SystemExit(f"group not found or has no members: {args.group}")
        names = [name for name in members if isinstance(name, str)]
    else:
        names = sorted(
            name
            for name in raw
            if isinstance(name, str) and (name.startswith(args.prefix) or name in set(args.include))
        )
    if not names:
        raise SystemExit("no matching proxies")

    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as executor:
        futures = {
            executor.submit(
                probe,
                args.pipe,
                name,
                args.url,
                args.timeout_ms,
                args.expected,
            ): name
            for name in names
        }
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(
        key=lambda item: (
            item.get("status") != 200,
            item.get("delay") if isinstance(item.get("delay"), (int, float)) else 10**9,
            str(item.get("name")),
        )
    )
    for item in results:
        delay = item.get("delay")
        delay_text = f"{delay}ms" if isinstance(delay, (int, float)) else "-"
        message = item.get("message") or ""
        print(f"{item['name']}\tHTTP {item['status']}\t{delay_text}\t{message}")
    good = sum(1 for item in results if item.get("status") == 200)
    print(f"SUMMARY\t{good}/{len(results)} usable")
    return 0 if good else 2


if __name__ == "__main__":
    raise SystemExit(main())
