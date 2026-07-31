#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


def placeholders(value: str) -> Counter[str]:
    return Counter(PLACEHOLDER_RE.findall(value))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate desktop Qt translation catalogs."
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("apps/desktop-client/mcp_desktop_client/locales/app_zh_CN.ts"),
    )
    parser.add_argument(
        "--compiled",
        type=Path,
        default=Path("apps/desktop-client/mcp_desktop_client/locales/app_zh_CN.qm"),
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("apps/desktop-client/mcp_desktop_client"),
    )
    args = parser.parse_args()

    root = ET.parse(args.catalog).getroot()
    errors: list[str] = []
    message_count = 0
    catalog_messages: set[tuple[str, str]] = set()
    for context in root.findall("context"):
        context_name = context.findtext("name") or "<unknown>"
        for message in context.findall("message"):
            message_count += 1
            source = message.findtext("source") or ""
            catalog_messages.add((context_name, source))
            translation = message.find("translation")
            translated = "" if translation is None else translation.text or ""
            if (
                translation is None
                or translation.get("type") == "unfinished"
                or not translated.strip()
            ):
                errors.append(f"{context_name}: unfinished translation for {source!r}")
                continue
            if placeholders(source) != placeholders(translated):
                errors.append(f"{context_name}: placeholder mismatch for {source!r}")

    if root.get("sourcelanguage") != "en_US" or root.get("language") != "zh_CN":
        errors.append("catalog must declare sourcelanguage=en_US and language=zh_CN")
    if not args.compiled.is_file() or args.compiled.stat().st_size == 0:
        errors.append(f"compiled catalog is missing or empty: {args.compiled}")
    if message_count == 0:
        errors.append("catalog contains no messages")

    source_messages: set[tuple[str, str]] = set()
    for source_path in sorted(args.source_dir.rglob("*.py")):
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id != "tr"
            ):
                continue
            if (
                len(node.args) < 2
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
                or not isinstance(node.args[1], ast.Constant)
                or not isinstance(node.args[1].value, str)
            ):
                errors.append(
                    f"{source_path}:{node.lineno}: tr() requires literal context and source strings"
                )
                continue
            source_messages.add((node.args[0].value, node.args[1].value))

    for context_name, source in sorted(source_messages - catalog_messages):
        errors.append(
            f"{context_name}: source string is missing from catalog: {source!r}"
        )
    for context_name, source in sorted(catalog_messages - source_messages):
        errors.append(
            f"{context_name}: catalog contains obsolete source string: {source!r}"
        )

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"Desktop i18n catalog OK: {message_count} translated messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
