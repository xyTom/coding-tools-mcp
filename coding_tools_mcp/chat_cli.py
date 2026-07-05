from __future__ import annotations

import argparse
import json
import locale
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from .transcript import TranscriptStore
except ImportError:  # pragma: no cover - exercised when run as a file from the workspace root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from coding_tools_mcp.transcript import TranscriptStore


ENV_PREFIX = "CODING_TOOLS_MCP"
DEFAULT_CONFIG_DIR_NAME = ".coding-tools-mcp"
TRANSCRIPT_DB_FILENAME = "transcripts.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = run(args)
    except CliError as exc:
        emit_json({"ok": False, "error": exc.message}, stream=sys.stderr)
        return exc.exit_code
    emit_json(payload)
    return 0


class CliError(Exception):
    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def emit_json(payload: dict[str, Any], *, stream: Any = None) -> None:
    stream = stream or sys.stdout
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=stream)


def add_project_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--project-path", default=None)
    parser.add_argument("--project-workspace", default=None)
    parser.add_argument("--project-metadata-json", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record and recall coding-tools-mcp chat transcripts.")
    parser.add_argument("--workspace", default=None, help="Workspace root; defaults to CODING_TOOLS_MCP_WORKSPACE or cwd.")
    parser.add_argument("--config-dir", default=None, help="Config directory containing transcripts.sqlite3.")
    parser.add_argument("--db-path", default=None, help="Exact transcript SQLite path; overrides workspace/config-dir.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_message = subparsers.add_parser("record-message", help="Record one chat message.")
    record_message.add_argument("--stdin-json", action="store_true", help="Read message payload from stdin as JSON.")
    record_message.add_argument("--conversation-id", default=None)
    record_message.add_argument("--conversation-title", default=None)
    record_message.add_argument("--conversation-uid", default=None)
    record_message.add_argument("--message-id", default=None)
    record_message.add_argument("--role", default=None)
    record_message.add_argument("--timestamp", default=None)
    record_message.add_argument("--content", default=None)
    record_message.add_argument("--source", default=None)
    record_message.add_argument("--metadata-json", default=None)
    add_project_args(record_message)

    record_transcript = subparsers.add_parser("record-transcript", help="Record multiple chat messages from stdin JSON.")
    record_transcript.add_argument("--stdin-json", action="store_true", help="Read transcript payload from stdin as JSON.")
    record_transcript.add_argument("--conversation-id", default=None)
    record_transcript.add_argument("--conversation-title", default=None)
    record_transcript.add_argument("--conversation-uid", default=None)
    record_transcript.add_argument("--source", default=None)
    add_project_args(record_transcript)

    record_context = subparsers.add_parser("record-context", help="Record one context retention entry.")
    record_context.add_argument("--stdin-json", action="store_true", help="Read context payload from stdin as JSON.")
    record_context.add_argument("--conversation-id", default=None)
    record_context.add_argument("--conversation-title", default=None)
    record_context.add_argument("--conversation-uid", default=None)
    record_context.add_argument("--entry-id", default=None)
    record_context.add_argument("--kind", default=None)
    record_context.add_argument("--timestamp", default=None)
    record_context.add_argument("--content", default=None)
    record_context.add_argument("--source", default=None)
    record_context.add_argument("--metadata-json", default=None)
    add_project_args(record_context)

    recall = subparsers.add_parser("recall-context", help="Recall persisted chat context as JSON and Markdown.")
    recall.add_argument("--conversation-id", required=True)
    recall.add_argument("--max-messages", type=int, default=200)
    recall.add_argument("--max-context-entries", type=int, default=200)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    store = transcript_store_from_args(args)
    if args.command == "record-message":
        return record_message(store, args)
    if args.command == "record-transcript":
        return record_transcript(store, args)
    if args.command == "record-context":
        return record_context(store, args)
    if args.command == "recall-context":
        return recall_context(store, args)
    raise CliError(f"Unknown command: {args.command}")


def transcript_store_from_args(args: argparse.Namespace) -> TranscriptStore:
    if args.db_path:
        return TranscriptStore(Path(str(args.db_path)).expanduser())
    workspace = Path(str(args.workspace or os.environ.get(f"{ENV_PREFIX}_WORKSPACE") or os.getcwd())).expanduser()
    config_dir = Path(str(args.config_dir or os.environ.get(f"{ENV_PREFIX}_CONFIG_DIR") or workspace / DEFAULT_CONFIG_DIR_NAME)).expanduser()
    return TranscriptStore(config_dir / TRANSCRIPT_DB_FILENAME)


def project_kwargs_from_inputs(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("project_metadata")
    metadata_json = first_text(args.project_metadata_json, payload.get("project_metadata_json"))
    if metadata is None and metadata_json:
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise CliError(f"project_metadata_json must be valid JSON: {exc}") from exc
    if metadata is not None and not isinstance(metadata, dict):
        raise CliError("project_metadata must be a JSON object.")
    return {
        "project_id": first_text(args.project_id, payload.get("project_id")),
        "project_name": first_text(args.project_name, payload.get("project_name")),
        "project_path": first_text(args.project_path, payload.get("project_path")),
        "project_workspace": first_text(args.project_workspace, payload.get("project_workspace")),
        "project_metadata": metadata if isinstance(metadata, dict) else None,
    }


def record_message(store: TranscriptStore, args: argparse.Namespace) -> dict[str, Any]:
    payload = read_stdin_object() if args.stdin_json else {}
    title = first_text(args.conversation_title, payload.get("conversation_title"), payload.get("title"))
    uid = first_text(args.conversation_uid, payload.get("conversation_uid"), payload.get("uid"))
    conversation_id = conversation_id_from_inputs(first_text(args.conversation_id, payload.get("conversation_id")), title, uid)
    content = first_text(args.content, payload.get("content"))
    if not conversation_id:
        raise CliError("conversation_id is required.")
    if content is None:
        raise CliError("content is required.")
    message: dict[str, Any] = {
        "role": first_text(args.role, payload.get("role")) or "message",
        "content": content,
    }
    for field, value in {
        "message_id": first_text(args.message_id, payload.get("message_id"), payload.get("id")),
        "timestamp": first_text(args.timestamp, payload.get("timestamp")),
        "source": first_text(args.source, payload.get("source")),
    }.items():
        if value:
            message[field] = value
    metadata = payload.get("metadata")
    metadata_json = first_text(args.metadata_json, payload.get("metadata_json"))
    if metadata is None and metadata_json:
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise CliError(f"metadata_json must be valid JSON: {exc}") from exc
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise CliError("metadata must be a JSON object.")
        message["metadata"] = metadata
    return store.record_chat_messages(
        conversation_id=conversation_id,
        messages=[message],
        source=first_text(args.source, payload.get("source")),
        conversation_title=title,
        conversation_uid=uid,
        **project_kwargs_from_inputs(args, payload),
    )


def record_transcript(store: TranscriptStore, args: argparse.Namespace) -> dict[str, Any]:
    if not args.stdin_json:
        raise CliError("record-transcript requires --stdin-json.")
    payload = read_stdin_object()
    title = first_text(args.conversation_title, payload.get("conversation_title"), payload.get("title"))
    uid = first_text(args.conversation_uid, payload.get("conversation_uid"), payload.get("uid"))
    conversation_id = conversation_id_from_inputs(first_text(args.conversation_id, payload.get("conversation_id")), title, uid)
    if not conversation_id:
        raise CliError("conversation_id is required.")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise CliError("messages must be an array.")
    return store.record_chat_messages(
        conversation_id=conversation_id,
        messages=messages,
        source=first_text(args.source, payload.get("source")),
        conversation_title=title,
        conversation_uid=uid,
        **project_kwargs_from_inputs(args, payload),
    )


def record_context(store: TranscriptStore, args: argparse.Namespace) -> dict[str, Any]:
    payload = read_stdin_object() if args.stdin_json else {}
    title = first_text(args.conversation_title, payload.get("conversation_title"), payload.get("title"))
    uid = first_text(args.conversation_uid, payload.get("conversation_uid"), payload.get("uid"))
    conversation_id = conversation_id_from_inputs(first_text(args.conversation_id, payload.get("conversation_id")), title, uid)
    content = first_text(args.content, payload.get("content"))
    if not conversation_id:
        raise CliError("conversation_id is required, or provide conversation_title and conversation_uid.")
    if content is None:
        raise CliError("content is required.")
    entry: dict[str, Any] = {
        "kind": first_text(args.kind, payload.get("kind")) or "checkpoint",
        "content": content,
    }
    for field, value in {
        "entry_id": first_text(args.entry_id, payload.get("entry_id"), payload.get("id")),
        "timestamp": first_text(args.timestamp, payload.get("timestamp")),
        "source": first_text(args.source, payload.get("source")),
    }.items():
        if value:
            entry[field] = value
    metadata = payload.get("metadata")
    metadata_json = first_text(args.metadata_json, payload.get("metadata_json"))
    if metadata is None and metadata_json:
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise CliError(f"metadata_json must be valid JSON: {exc}") from exc
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise CliError("metadata must be a JSON object.")
        entry["metadata"] = metadata
    return store.record_context_entries(
        conversation_id=conversation_id,
        entries=[entry],
        source=first_text(args.source, payload.get("source")),
        conversation_title=title,
        conversation_uid=uid,
        **project_kwargs_from_inputs(args, payload),
    )


def recall_context(store: TranscriptStore, args: argparse.Namespace) -> dict[str, Any]:
    try:
        messages_payload = store.list_chat_messages(conversation_id=str(args.conversation_id), limit=int(args.max_messages))
        context_payload = store.list_context_entries(conversation_id=str(args.conversation_id), limit=int(args.max_context_entries))
        context_export = store.export_context_markdown(
            conversation_id=str(args.conversation_id),
            max_entries=int(args.max_context_entries),
            write_file=False,
        )
        if messages_payload.get("message_count", 0):
            chat_export = store.export_chat_markdown(
                conversation_id=str(args.conversation_id),
                max_messages=int(args.max_messages),
                write_file=False,
            )
        else:
            chat_export = {"conversations": context_export.get("conversations", []), "markdown": ""}
    except ValueError as exc:
        raise CliError(str(exc), exit_code=1) from exc
    context_markdown = str(context_export.get("markdown") or "")
    chat_markdown = str(chat_export.get("markdown") or "")
    context_text = context_markdown if context_payload.get("entry_count", 0) else chat_markdown
    return {
        "ok": True,
        "conversation_id": messages_payload.get("conversation_id"),
        "message_count": messages_payload.get("message_count", 0),
        "context_entry_count": context_payload.get("entry_count", 0),
        "max_messages": int(args.max_messages),
        "max_context_entries": int(args.max_context_entries),
        "messages": messages_payload.get("messages", []),
        "context_entries": context_payload.get("entries", []),
        "conversations": context_export.get("conversations") or chat_export.get("conversations", []),
        "markdown": context_text,
        "context_text": context_text,
        "context_markdown": context_markdown,
        "chat_markdown": chat_markdown,
    }


def read_stdin_object() -> dict[str, Any]:
    raw = read_stdin_text()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"stdin must contain valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CliError("stdin JSON must be an object.")
    return payload


def read_stdin_text() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        raw = buffer.read()
        if isinstance(raw, bytes):
            encodings = [
                "utf-8",
                getattr(sys.stdin, "encoding", None),
                locale.getpreferredencoding(False),
                "gb18030",
            ]
            for encoding in dict.fromkeys(item for item in encodings if item):
                try:
                    return raw.decode(str(encoding))
                except (LookupError, UnicodeDecodeError):
                    continue
            return raw.decode("utf-8", errors="replace")
        return str(raw)
    return sys.stdin.read()


def first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return None


def conversation_id_from_inputs(conversation_id: str | None, title: str | None, uid: str | None) -> str | None:
    if conversation_id:
        return conversation_id
    if not title or not uid:
        return None
    compact_title = re.sub(r"\s+", "-", title.strip())
    return f"{compact_title}--{uid.strip()}"[:192]


if __name__ == "__main__":
    raise SystemExit(main())
