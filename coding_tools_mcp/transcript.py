"""Workspace-partitioned chat, context, and imported-session persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class TranscriptStoreError(RuntimeError):
    pass


MAX_PAGE_SIZE = 200
MAX_CONTENT_CHARS = 2_000_000
SCHEMA_VERSION = 1


def _now() -> float:
    return time.time()


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise TranscriptStoreError(f"{field} must be a non-empty string up to 256 characters.")
    if value in {".", ".."} or any(char in value for char in "\x00/\\"):
        raise TranscriptStoreError(f"{field} contains unsupported characters.")
    return value


def _text(value: Any, *, limit: int = MAX_CONTENT_CHARS) -> str:
    if value is None:
        return ""
    result = str(value).replace("\x00", "\ufffd")
    result = result.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    if len(result) > limit:
        raise TranscriptStoreError(f"Text exceeds the {limit}-character limit.")
    return result


def _json(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:
        raise TranscriptStoreError(f"Metadata must be JSON serializable: {exc}") from exc


def _page(page: int, page_size: int) -> tuple[int, int]:
    try:
        normalized_page = max(1, int(page))
        normalized_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    except (TypeError, ValueError) as exc:
        raise TranscriptStoreError("page and page_size must be integers.") from exc
    return normalized_page, normalized_size


def _summary(text: str, limit: int = 240) -> str:
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "\u2026"


@dataclass(frozen=True)
class WorkspaceScope:
    workspace_id: str
    workspace_root: Path

    @classmethod
    def create(cls, workspace_id: str, workspace_root: str | Path) -> "WorkspaceScope":
        identifier = _require_id(workspace_id, "workspace_id")
        try:
            root = Path(workspace_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise TranscriptStoreError(f"Workspace root cannot be resolved: {exc}") from exc
        if not root.is_dir():
            raise TranscriptStoreError("Workspace root must be an existing directory.")
        return cls(identifier, root)


class TranscriptStore:
    """SQLite store where every key is partitioned by explicit Workspace identity."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._migrate()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._write_lock, self._connection(write=True) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise TranscriptStoreError("Transcript database was written by a newer version.")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_conversations(
                    workspace_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    title TEXT,
                    source TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id, conversation_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages(
                    workspace_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    timestamp TEXT,
                    content TEXT NOT NULL,
                    source TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id, message_id),
                    FOREIGN KEY(workspace_id, conversation_id)
                      REFERENCES chat_conversations(workspace_id, conversation_id)
                      ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_context_entries(
                    workspace_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    timestamp TEXT,
                    content TEXT NOT NULL,
                    source TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id, context_id),
                    FOREIGN KEY(workspace_id, conversation_id)
                      REFERENCES chat_conversations(workspace_id, conversation_id)
                      ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS imported_sessions(
                    workspace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    conversation_id TEXT,
                    relative_path TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    title TEXT,
                    summary TEXT,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    parse_error_count INTEGER NOT NULL DEFAULT 0,
                    parse_errors_json TEXT NOT NULL DEFAULT '[]',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    file_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    imported_at REAL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id, session_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_updated ON chat_conversations(workspace_id, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON chat_messages(workspace_id, conversation_id, created_at, message_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_conversation ON chat_context_entries(workspace_id, conversation_id, created_at, context_id)"
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def scoped(self, workspace_id: str, workspace_root: str | Path) -> "WorkspaceTranscriptService":
        return WorkspaceTranscriptService(self, WorkspaceScope.create(workspace_id, workspace_root))

    def record_messages(
        self,
        workspace_id: str,
        conversation_id: str,
        messages: list[dict[str, Any]],
        *,
        title: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = _require_id(workspace_id, "workspace_id")
        conversation_id = _require_id(conversation_id, "conversation_id")
        if not isinstance(messages, list) or len(messages) > 10_000:
            raise TranscriptStoreError("messages must be a list with at most 10000 items.")
        inserted = 0
        duplicates = 0
        now = _now()
        with self._write_lock, self._connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO chat_conversations(workspace_id, conversation_id, title, source, created_at, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(workspace_id, conversation_id) DO UPDATE SET
                  title=COALESCE(excluded.title, chat_conversations.title),
                  source=COALESCE(excluded.source, chat_conversations.source),
                  updated_at=excluded.updated_at
                """,
                (workspace_id, conversation_id, _text(title, limit=500) or None, _text(source, limit=200) or None, now, now),
            )
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    raise TranscriptStoreError("Each message must be an object.")
                message_id = _require_id(
                    message.get("message_id") or message.get("id") or f"msg-{uuid.uuid4().hex}",
                    "message_id",
                )
                role = _text(message.get("role") or "unknown", limit=64).lower()
                if role not in {"user", "assistant", "system", "tool", "developer", "unknown"}:
                    role = "unknown"
                content = _text(message.get("content"))
                timestamp = _text(message.get("timestamp"), limit=128) or None
                item_source = _text(message.get("source") or source, limit=200) or None
                metadata = message.get("metadata", message.get("metadata_json", {}))
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {"raw": _text(metadata, limit=20_000)}
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO chat_messages(
                      workspace_id, message_id, conversation_id, role, timestamp,
                      content, source, metadata_json, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (workspace_id, message_id, conversation_id, role, timestamp, content, item_source, _json(metadata), now + index / 1_000_000, now),
                )
                if cursor.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
            conn.execute(
                "UPDATE chat_conversations SET updated_at=? WHERE workspace_id=? AND conversation_id=?",
                (now, workspace_id, conversation_id),
            )
        return {
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
            "inserted_count": inserted,
            "duplicate_count": duplicates,
        }

    def record_context(
        self,
        workspace_id: str,
        conversation_id: str,
        entries: list[dict[str, Any]],
        *,
        title: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = _require_id(workspace_id, "workspace_id")
        conversation_id = _require_id(conversation_id, "conversation_id")
        if not isinstance(entries, list) or len(entries) > 10_000:
            raise TranscriptStoreError("entries must be a list with at most 10000 items.")
        inserted = 0
        duplicates = 0
        now = _now()
        with self._write_lock, self._connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO chat_conversations(workspace_id, conversation_id, title, source, created_at, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(workspace_id, conversation_id) DO UPDATE SET
                  title=COALESCE(excluded.title, chat_conversations.title),
                  source=COALESCE(excluded.source, chat_conversations.source),
                  updated_at=excluded.updated_at
                """,
                (workspace_id, conversation_id, _text(title, limit=500) or None, _text(source, limit=200) or None, now, now),
            )
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise TranscriptStoreError("Each context entry must be an object.")
                context_id = _require_id(
                    entry.get("context_id") or entry.get("entry_id") or entry.get("id") or f"ctx-{uuid.uuid4().hex}",
                    "context_id",
                )
                kind = _text(entry.get("kind") or "note", limit=64).lower()
                content = _text(entry.get("content"))
                timestamp = _text(entry.get("timestamp"), limit=128) or None
                item_source = _text(entry.get("source") or source, limit=200) or None
                metadata = entry.get("metadata", entry.get("metadata_json", {}))
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {"raw": _text(metadata, limit=20_000)}
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO chat_context_entries(
                      workspace_id, context_id, conversation_id, kind, timestamp,
                      content, source, metadata_json, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (workspace_id, context_id, conversation_id, kind, timestamp, content, item_source, _json(metadata), now + index / 1_000_000, now),
                )
                if cursor.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
            conn.execute(
                "UPDATE chat_conversations SET updated_at=? WHERE workspace_id=? AND conversation_id=?",
                (now, workspace_id, conversation_id),
            )
        return {
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
            "inserted_count": inserted,
            "duplicate_count": duplicates,
        }

    def list_conversations(
        self,
        workspace_id: str | None,
        *,
        page: int = 1,
        page_size: int = 50,
        query: str | None = None,
    ) -> dict[str, Any]:
        if workspace_id is not None:
            workspace_id = _require_id(workspace_id, "workspace_id")
        page, page_size = _page(page, page_size)
        clauses: list[str] = []
        args: list[Any] = []
        if workspace_id is not None:
            clauses.append("c.workspace_id=?")
            args.append(workspace_id)
        if query:
            clauses.append("(c.conversation_id LIKE ? OR c.title LIKE ? OR c.source LIKE ?)")
            pattern = f"%{_text(query, limit=200)}%"
            args.extend((pattern, pattern, pattern))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM chat_conversations c{where}", args).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT c.workspace_id, c.conversation_id, c.title, c.source,
                       c.created_at, c.updated_at,
                       COUNT(DISTINCT m.message_id) AS message_count,
                       COUNT(DISTINCT x.context_id) AS context_count,
                       MAX(CASE WHEN m.role='user' THEN substr(m.content,1,240) END) AS preview
                FROM chat_conversations c
                LEFT JOIN chat_messages m ON m.workspace_id=c.workspace_id AND m.conversation_id=c.conversation_id
                LEFT JOIN chat_context_entries x ON x.workspace_id=c.workspace_id AND x.conversation_id=c.conversation_id
                {where}
                GROUP BY c.workspace_id, c.conversation_id
                ORDER BY c.updated_at DESC, c.workspace_id, c.conversation_id
                LIMIT ? OFFSET ?
                """,
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["preview"] = _summary(item.get("preview") or "")
        return {"items": items, "count": len(items), "total": total, "page": page, "page_size": page_size}

    def conversation_detail(
        self,
        workspace_id: str,
        conversation_id: str,
        *,
        message_page: int = 1,
        message_page_size: int = 100,
        context_page: int = 1,
        context_page_size: int = 100,
    ) -> dict[str, Any] | None:
        workspace_id = _require_id(workspace_id, "workspace_id")
        conversation_id = _require_id(conversation_id, "conversation_id")
        message_page, message_page_size = _page(message_page, message_page_size)
        context_page, context_page_size = _page(context_page, context_page_size)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM chat_conversations WHERE workspace_id=? AND conversation_id=?",
                (workspace_id, conversation_id),
            ).fetchone()
            if row is None:
                return None
            messages_total = int(conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE workspace_id=? AND conversation_id=?",
                (workspace_id, conversation_id),
            ).fetchone()[0])
            contexts_total = int(conn.execute(
                "SELECT COUNT(*) FROM chat_context_entries WHERE workspace_id=? AND conversation_id=?",
                (workspace_id, conversation_id),
            ).fetchone()[0])
            messages = conn.execute(
                """
                SELECT workspace_id, message_id, conversation_id, role, timestamp,
                       content, source, metadata_json, created_at, updated_at
                FROM chat_messages
                WHERE workspace_id=? AND conversation_id=?
                ORDER BY created_at, message_id LIMIT ? OFFSET ?
                """,
                (workspace_id, conversation_id, message_page_size, (message_page - 1) * message_page_size),
            ).fetchall()
            contexts = conn.execute(
                """
                SELECT workspace_id, context_id, conversation_id, kind, timestamp,
                       content, source, metadata_json, created_at, updated_at
                FROM chat_context_entries
                WHERE workspace_id=? AND conversation_id=?
                ORDER BY created_at, context_id LIMIT ? OFFSET ?
                """,
                (workspace_id, conversation_id, context_page_size, (context_page - 1) * context_page_size),
            ).fetchall()
        message_items = [_decode_metadata(dict(item)) for item in messages]
        context_items = [_decode_metadata(dict(item)) for item in contexts]
        return {
            "conversation": dict(row),
            "messages": message_items,
            "messages_total": messages_total,
            "message_page": message_page,
            "message_page_size": message_page_size,
            "contexts": context_items,
            "contexts_total": contexts_total,
            "context_page": context_page,
            "context_page_size": context_page_size,
        }

    def delete_message(self, workspace_id: str, message_id: str) -> dict[str, Any]:
        return self._delete_by_id("chat_messages", "message_id", workspace_id, message_id)

    def delete_context(self, workspace_id: str, context_id: str) -> dict[str, Any]:
        return self._delete_by_id("chat_context_entries", "context_id", workspace_id, context_id)

    def delete_conversation(self, workspace_id: str, conversation_id: str) -> dict[str, Any]:
        workspace_id = _require_id(workspace_id, "workspace_id")
        conversation_id = _require_id(conversation_id, "conversation_id")
        with self._write_lock, self._connection(write=True) as conn:
            message_count = int(conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE workspace_id=? AND conversation_id=?",
                (workspace_id, conversation_id),
            ).fetchone()[0])
            context_count = int(conn.execute(
                "SELECT COUNT(*) FROM chat_context_entries WHERE workspace_id=? AND conversation_id=?",
                (workspace_id, conversation_id),
            ).fetchone()[0])
            cursor = conn.execute(
                "DELETE FROM chat_conversations WHERE workspace_id=? AND conversation_id=?",
                (workspace_id, conversation_id),
            )
        return {
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
            "affected_count": int(cursor.rowcount),
            "deleted_message_count": message_count if cursor.rowcount else 0,
            "deleted_context_count": context_count if cursor.rowcount else 0,
        }

    def clear_workspace(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = _require_id(workspace_id, "workspace_id")
        with self._write_lock, self._connection(write=True) as conn:
            conversations = int(conn.execute("SELECT COUNT(*) FROM chat_conversations WHERE workspace_id=?", (workspace_id,)).fetchone()[0])
            messages = int(conn.execute("SELECT COUNT(*) FROM chat_messages WHERE workspace_id=?", (workspace_id,)).fetchone()[0])
            contexts = int(conn.execute("SELECT COUNT(*) FROM chat_context_entries WHERE workspace_id=?", (workspace_id,)).fetchone()[0])
            sessions = int(conn.execute("SELECT COUNT(*) FROM imported_sessions WHERE workspace_id=?", (workspace_id,)).fetchone()[0])
            conn.execute("DELETE FROM chat_conversations WHERE workspace_id=?", (workspace_id,))
            conn.execute("DELETE FROM imported_sessions WHERE workspace_id=?", (workspace_id,))
        return {
            "workspace_id": workspace_id,
            "affected_count": conversations + messages + contexts + sessions,
            "deleted_conversation_count": conversations,
            "deleted_message_count": messages,
            "deleted_context_count": contexts,
            "deleted_session_count": sessions,
        }

    def upsert_imported_session(self, workspace_id: str, item: dict[str, Any]) -> None:
        workspace_id = _require_id(workspace_id, "workspace_id")
        session_id = _require_id(item.get("session_id"), "session_id")
        errors = item.get("parse_errors") or []
        with self._write_lock, self._connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO imported_sessions(
                  workspace_id, session_id, conversation_id, relative_path, source_kind,
                  title, summary, message_count, parse_error_count, parse_errors_json,
                  file_size, file_mtime_ns, imported_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace_id, session_id) DO UPDATE SET
                  conversation_id=excluded.conversation_id,
                  relative_path=excluded.relative_path,
                  source_kind=excluded.source_kind,
                  title=excluded.title,
                  summary=excluded.summary,
                  message_count=excluded.message_count,
                  parse_error_count=excluded.parse_error_count,
                  parse_errors_json=excluded.parse_errors_json,
                  file_size=excluded.file_size,
                  file_mtime_ns=excluded.file_mtime_ns,
                  imported_at=COALESCE(excluded.imported_at, imported_sessions.imported_at),
                  updated_at=excluded.updated_at
                """,
                (
                    workspace_id,
                    session_id,
                    item.get("conversation_id"),
                    _text(item.get("relative_path"), limit=2000),
                    _text(item.get("source_kind") or "codex", limit=100),
                    _text(item.get("title"), limit=500) or None,
                    _text(item.get("summary"), limit=1000) or None,
                    int(item.get("message_count") or 0),
                    len(errors),
                    _json(errors),
                    int(item.get("file_size") or 0),
                    int(item.get("file_mtime_ns") or 0),
                    item.get("imported_at"),
                    _now(),
                ),
            )

    def list_imported_sessions(
        self,
        workspace_id: str | None,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if workspace_id is not None:
            workspace_id = _require_id(workspace_id, "workspace_id")
        page, page_size = _page(page, page_size)
        where = " WHERE workspace_id=?" if workspace_id else ""
        args: tuple[Any, ...] = (workspace_id,) if workspace_id else ()
        with self._connection() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM imported_sessions{where}", args).fetchone()[0])
            rows = conn.execute(
                f"SELECT * FROM imported_sessions{where} ORDER BY updated_at DESC, workspace_id, session_id LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["parse_errors"] = json.loads(item.pop("parse_errors_json"))
            items.append(item)
        return {"items": items, "count": len(items), "total": total, "page": page, "page_size": page_size}

    def delete_imported_session(self, workspace_id: str, session_id: str) -> dict[str, Any]:
        workspace_id = _require_id(workspace_id, "workspace_id")
        session_id = _require_id(session_id, "session_id")
        with self._write_lock, self._connection(write=True) as conn:
            session = conn.execute(
                "SELECT conversation_id FROM imported_sessions WHERE workspace_id=? AND session_id=?",
                (workspace_id, session_id),
            ).fetchone()
            if session is None:
                return {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "affected_count": 0,
                    "deleted_session_count": 0,
                    "deleted_conversation_count": 0,
                    "deleted_message_count": 0,
                    "deleted_context_count": 0,
                }
            conversation_id = session["conversation_id"]
            message_count = 0
            context_count = 0
            conversation_count = 0
            if isinstance(conversation_id, str) and conversation_id:
                message_count = int(conn.execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE workspace_id=? AND conversation_id=?",
                    (workspace_id, conversation_id),
                ).fetchone()[0])
                context_count = int(conn.execute(
                    "SELECT COUNT(*) FROM chat_context_entries WHERE workspace_id=? AND conversation_id=?",
                    (workspace_id, conversation_id),
                ).fetchone()[0])
                conversation_count = int(conn.execute(
                    "SELECT COUNT(*) FROM chat_conversations WHERE workspace_id=? AND conversation_id=?",
                    (workspace_id, conversation_id),
                ).fetchone()[0])
                conn.execute(
                    "DELETE FROM chat_conversations WHERE workspace_id=? AND conversation_id=?",
                    (workspace_id, conversation_id),
                )
            cursor = conn.execute(
                "DELETE FROM imported_sessions WHERE workspace_id=? AND session_id=?",
                (workspace_id, session_id),
            )
        session_count = int(cursor.rowcount)
        return {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "affected_count": session_count + conversation_count + message_count + context_count,
            "deleted_session_count": session_count,
            "deleted_conversation_count": conversation_count,
            "deleted_message_count": message_count,
            "deleted_context_count": context_count,
        }

    def _delete_by_id(self, table: str, field: str, workspace_id: str, identifier: str) -> dict[str, Any]:
        if table not in {"chat_messages", "chat_context_entries", "imported_sessions"}:
            raise TranscriptStoreError("Unsupported delete target.")
        workspace_id = _require_id(workspace_id, "workspace_id")
        identifier = _require_id(identifier, field)
        with self._write_lock, self._connection(write=True) as conn:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE workspace_id=? AND {field}=?",
                (workspace_id, identifier),
            )
        return {"workspace_id": workspace_id, field: identifier, "affected_count": int(cursor.rowcount)}


class WorkspaceTranscriptService:
    """Non-admin facade fixed to one immutable Workspace scope."""

    def __init__(self, store: TranscriptStore, scope: WorkspaceScope) -> None:
        self.store = store
        self.scope = scope

    def record_messages(self, conversation_id: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return self.store.record_messages(self.scope.workspace_id, conversation_id, messages, **kwargs)

    def record_context(self, conversation_id: str, entries: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return self.store.record_context(self.scope.workspace_id, conversation_id, entries, **kwargs)

    def list_conversations(self, **kwargs: Any) -> dict[str, Any]:
        return self.store.list_conversations(self.scope.workspace_id, **kwargs)

    def conversation_detail(self, conversation_id: str, **kwargs: Any) -> dict[str, Any] | None:
        return self.store.conversation_detail(self.scope.workspace_id, conversation_id, **kwargs)


def _decode_metadata(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.pop("metadata_json", "{}")
    try:
        item["metadata"] = json.loads(raw)
    except json.JSONDecodeError:
        item["metadata"] = {"parse_error": True}
    return item


__all__ = [
    "MAX_PAGE_SIZE",
    "TranscriptStore",
    "TranscriptStoreError",
    "WorkspaceScope",
    "WorkspaceTranscriptService",
]
