"""Small Workspace-scoped CLI for recording and reading chat persistence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .transcript import TranscriptStore, TranscriptStoreError, WorkspaceScope


class CliError(ValueError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record Workspace-scoped chat data.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--workspace-root", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    record_message = sub.add_parser("record-message")
    record_message.add_argument("--conversation-id")
    record_message.add_argument("--title")
    record_message.add_argument("--message-id")
    record_message.add_argument("--role")
    record_message.add_argument("--content")
    record_message.add_argument("--timestamp")
    record_message.add_argument("--source")
    record_message.add_argument("--stdin-json", action="store_true")

    record_context = sub.add_parser("record-context")
    record_context.add_argument("--conversation-id")
    record_context.add_argument("--title")
    record_context.add_argument("--context-id")
    record_context.add_argument("--kind")
    record_context.add_argument("--content")
    record_context.add_argument("--timestamp")
    record_context.add_argument("--source")
    record_context.add_argument("--stdin-json", action="store_true")

    listing = sub.add_parser("list")
    listing.add_argument("--page", type=int, default=1)
    listing.add_argument("--page-size", type=int, default=50)
    listing.add_argument("--query")

    detail = sub.add_parser("detail")
    detail.add_argument("--conversation-id", required=True)
    detail.add_argument("--page", type=int, default=1)
    detail.add_argument("--page-size", type=int, default=100)
    return parser


def _read_stdin_json() -> dict[str, Any]:
    stream = getattr(sys.stdin, "buffer", None)
    raw = stream.read() if stream is not None else sys.stdin.read().encode("utf-8", errors="replace")
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = raw.decode(encoding)
            value = json.loads(text)
            if not isinstance(value, dict):
                raise CliError("stdin JSON must be an object.")
            return value
        except UnicodeDecodeError:
            continue
        except json.JSONDecodeError as exc:
            raise CliError(f"stdin is not valid JSON: {exc}") from exc
    raise CliError("stdin is not valid UTF-8, UTF-16, or GB18030 text.")


def _value(args: argparse.Namespace, payload: dict[str, Any], name: str, default: Any = None) -> Any:
    value = getattr(args, name, None)
    return value if value not in (None, "") else payload.get(name, default)


def run(args: argparse.Namespace) -> dict[str, Any]:
    scope = WorkspaceScope.create(args.workspace_id, args.workspace_root)
    store = TranscriptStore(Path(args.db_path))
    service = store.scoped(scope.workspace_id, scope.workspace_root)
    payload = _read_stdin_json() if getattr(args, "stdin_json", False) else {}
    if args.command == "record-message":
        conversation_id = _value(args, payload, "conversation_id")
        if not conversation_id:
            raise CliError("conversation_id is required.")
        message = {
            "message_id": _value(args, payload, "message_id"),
            "role": _value(args, payload, "role", "unknown"),
            "content": _value(args, payload, "content", ""),
            "timestamp": _value(args, payload, "timestamp"),
            "source": _value(args, payload, "source", "chat-cli"),
            "metadata": payload.get("metadata", payload.get("metadata_json", {})),
        }
        return service.record_messages(
            str(conversation_id),
            [message],
            title=_value(args, payload, "title"),
            source=_value(args, payload, "source", "chat-cli"),
        )
    if args.command == "record-context":
        conversation_id = _value(args, payload, "conversation_id")
        if not conversation_id:
            raise CliError("conversation_id is required.")
        entry = {
            "context_id": _value(args, payload, "context_id", payload.get("entry_id")),
            "kind": _value(args, payload, "kind", "note"),
            "content": _value(args, payload, "content", ""),
            "timestamp": _value(args, payload, "timestamp"),
            "source": _value(args, payload, "source", "chat-cli"),
            "metadata": payload.get("metadata", payload.get("metadata_json", {})),
        }
        return service.record_context(
            str(conversation_id),
            [entry],
            title=_value(args, payload, "title"),
            source=_value(args, payload, "source", "chat-cli"),
        )
    if args.command == "list":
        return service.list_conversations(page=args.page, page_size=args.page_size, query=args.query)
    if args.command == "detail":
        result = service.conversation_detail(
            args.conversation_id,
            message_page=args.page,
            message_page_size=args.page_size,
            context_page=args.page,
            context_page_size=args.page_size,
        )
        if result is None:
            raise CliError("Conversation was not found in this Workspace.")
        return result
    raise CliError("Unknown command.")


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(build_parser().parse_args(argv))
        text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        sys.stdout.write(text.encode("utf-8", errors="replace").decode("utf-8") + "\n")
        return 0
    except (CliError, TranscriptStoreError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
