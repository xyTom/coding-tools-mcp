from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRANSCRIPT_SCHEMA_VERSION = 3
SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


class TranscriptStore:
    def __init__(self, db_path: Path, *, markdown_dir: Path | None = None) -> None:
        self.db_path = db_path
        self.markdown_dir = markdown_dir or db_path.parent / "transcripts-md"
        self._lock = threading.Lock()
        self._initialized = False

    def status(self) -> dict[str, Any]:
        self._ensure_schema()
        with closing(self._connect()) as conn, conn:
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            chat_conversation_count = conn.execute("SELECT COUNT(DISTINCT conversation_id) FROM chat_messages").fetchone()[0]
            chat_message_count = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
            chat_context_entry_count = conn.execute("SELECT COUNT(*) FROM chat_context_entries").fetchone()[0]
        return {
            "ok": True,
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "db_path": str(self.db_path),
            "markdown_dir": str(self.markdown_dir),
            "session_count": int(session_count),
            "event_count": int(event_count),
            "chat_conversation_count": int(chat_conversation_count),
            "chat_message_count": int(chat_message_count),
            "chat_context_entry_count": int(chat_context_entry_count),
        }

    def record_http_request(self, event: dict[str, Any]) -> None:
        self._record_event(event, request_increment=1)

    def record_tool_call(self, event: dict[str, Any]) -> None:
        self._record_event(event, request_increment=0)

    def record_chat_messages(
        self,
        *,
        conversation_id: str,
        messages: list[dict[str, Any]],
        source: str | None = None,
        conversation_title: str | None = None,
        conversation_uid: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        conversation_id = normalized_conversation_id(conversation_id)
        now = utc_now()
        rows: list[tuple[str, str | None, str, str, str, str | None, str | None]] = []
        skipped = 0
        for message in messages:
            if not isinstance(message, dict):
                skipped += 1
                continue
            content = message.get("content")
            if content is None:
                skipped += 1
                continue
            metadata = message.get("metadata")
            metadata_json = None
            if metadata is not None:
                metadata_json = safe_json_dumps(metadata)
            rows.append(
                (
                    conversation_id,
                    normalized_optional_text(message.get("message_id") or message.get("id"), limit=192),
                    normalized_role(message.get("role")),
                    sanitized_text(message.get("timestamp") or now),
                    sanitized_text(content),
                    normalized_optional_text(message.get("source") or source, limit=128),
                    metadata_json,
                )
            )
        inserted = 0
        if rows:
            with self._lock:
                with closing(self._connect()) as conn, conn:
                    self._upsert_chat_conversation(
                        conn,
                        conversation_id=conversation_id,
                        title=conversation_title,
                        unique_id=conversation_uid,
                        source=source,
                        timestamp=now,
                    )
                    for row in rows:
                        cursor = conn.execute(
                            """
                            INSERT OR IGNORE INTO chat_messages (
                                conversation_id, message_id, role, timestamp, content, source, metadata_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            row,
                        )
                        inserted += max(0, int(cursor.rowcount or 0))
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "message_count": len(rows),
            "inserted_count": inserted,
            "duplicate_count": max(0, len(rows) - inserted),
            "skipped_count": skipped,
        }

    def record_context_entries(
        self,
        *,
        conversation_id: str,
        entries: list[dict[str, Any]],
        source: str | None = None,
        conversation_title: str | None = None,
        conversation_uid: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        conversation_id = normalized_conversation_id(conversation_id)
        now = utc_now()
        rows: list[tuple[str, str | None, str, str, str, str | None, str | None]] = []
        skipped = 0
        for entry in entries:
            if not isinstance(entry, dict):
                skipped += 1
                continue
            content = entry.get("content")
            if content is None:
                skipped += 1
                continue
            metadata = entry.get("metadata")
            metadata_json = None
            if metadata is not None:
                metadata_json = safe_json_dumps(metadata)
            rows.append(
                (
                    conversation_id,
                    normalized_optional_text(entry.get("entry_id") or entry.get("id"), limit=192),
                    normalized_context_kind(entry.get("kind")),
                    sanitized_text(entry.get("timestamp") or now),
                    sanitized_text(content),
                    normalized_optional_text(entry.get("source") or source, limit=128),
                    metadata_json,
                )
            )
        inserted = 0
        if rows:
            with self._lock:
                with closing(self._connect()) as conn, conn:
                    self._upsert_chat_conversation(
                        conn,
                        conversation_id=conversation_id,
                        title=conversation_title,
                        unique_id=conversation_uid,
                        source=source,
                        timestamp=now,
                    )
                    for row in rows:
                        cursor = conn.execute(
                            """
                            INSERT OR IGNORE INTO chat_context_entries (
                                conversation_id, entry_id, kind, timestamp, content, source, metadata_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            row,
                        )
                        inserted += max(0, int(cursor.rowcount or 0))
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "entry_count": len(rows),
            "inserted_count": inserted,
            "duplicate_count": max(0, len(rows) - inserted),
            "skipped_count": skipped,
        }

    def list_sessions(self, *, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        self._ensure_schema()
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT s.session_id, s.first_seen, s.last_seen, s.request_count,
                       s.workspace, s.default_cwd, s.default_cwd_display,
                       s.remote_addr, s.user_agent, s.protocol_version,
                       COUNT(e.id) AS event_count
                FROM sessions s
                LEFT JOIN events e ON e.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {"ok": True, "sessions": [dict(row) for row in rows], "session_count": len(rows)}

    def list_chat_conversations(self, *, limit: int = 100, query: str | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        self._ensure_schema()
        rows = self._load_chat_conversations(limit=limit, query=query)
        return {"ok": True, "conversations": rows, "conversation_count": len(rows)}

    def list_chat_messages(self, *, conversation_id: str, limit: int = 500) -> dict[str, Any]:
        limit = max(1, min(int(limit), 5000))
        conversation_id = normalized_conversation_id(conversation_id)
        messages = self._load_chat_messages(conversation_id=conversation_id, max_messages=limit)
        return {"ok": True, "conversation_id": conversation_id, "messages": messages, "message_count": len(messages)}

    def list_context_entries(self, *, conversation_id: str, limit: int = 200) -> dict[str, Any]:
        limit = max(1, min(int(limit), 5000))
        conversation_id = normalized_conversation_id(conversation_id)
        entries = self._load_context_entries(conversation_id=conversation_id, max_entries=limit)
        return {"ok": True, "conversation_id": conversation_id, "entries": entries, "entry_count": len(entries)}

    def update_chat_message(self, *, row_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        self._ensure_schema()
        assignments: list[str] = []
        values: list[Any] = []
        if "role" in updates:
            assignments.append("role = ?")
            values.append(normalized_role(updates.get("role")))
        if "timestamp" in updates:
            assignments.append("timestamp = ?")
            values.append(sanitized_text(updates.get("timestamp") or utc_now()))
        if "content" in updates:
            assignments.append("content = ?")
            values.append(sanitized_text(updates.get("content") or ""))
        if "source" in updates:
            assignments.append("source = ?")
            values.append(normalized_optional_text(updates.get("source"), limit=128))
        if "metadata" in updates:
            assignments.append("metadata_json = ?")
            metadata = updates.get("metadata")
            values.append(None if metadata is None else safe_json_dumps(metadata))
        if not assignments:
            raise ValueError("No chat message fields were provided to update.")
        values.append(int(row_id))
        with self._lock:
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute(f"UPDATE chat_messages SET {', '.join(assignments)} WHERE id = ?", values)
                if cursor.rowcount == 0:
                    raise ValueError(f"Chat message not found: {row_id}")
                row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (int(row_id),)).fetchone()
        return {"ok": True, "updated": True, "message": dict(row) if row else None}

    def delete_chat_message(self, *, row_id: int) -> dict[str, Any]:
        self._ensure_schema()
        with self._lock:
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute("DELETE FROM chat_messages WHERE id = ?", (int(row_id),))
        return {"ok": True, "deleted_count": int(cursor.rowcount or 0), "message_id": int(row_id)}

    def update_context_entry(self, *, row_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        self._ensure_schema()
        assignments: list[str] = []
        values: list[Any] = []
        if "entry_id" in updates:
            assignments.append("entry_id = ?")
            values.append(normalized_optional_text(updates.get("entry_id"), limit=192))
        if "kind" in updates:
            assignments.append("kind = ?")
            values.append(normalized_context_kind(updates.get("kind")))
        if "timestamp" in updates:
            assignments.append("timestamp = ?")
            values.append(sanitized_text(updates.get("timestamp") or utc_now()))
        if "content" in updates:
            assignments.append("content = ?")
            values.append(sanitized_text(updates.get("content") or ""))
        if "source" in updates:
            assignments.append("source = ?")
            values.append(normalized_optional_text(updates.get("source"), limit=128))
        if "metadata" in updates:
            assignments.append("metadata_json = ?")
            metadata = updates.get("metadata")
            values.append(None if metadata is None else safe_json_dumps(metadata))
        if not assignments:
            raise ValueError("No context entry fields were provided to update.")
        values.append(int(row_id))
        with self._lock:
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute(f"UPDATE chat_context_entries SET {', '.join(assignments)} WHERE id = ?", values)
                if cursor.rowcount == 0:
                    raise ValueError(f"Context entry not found: {row_id}")
                row = conn.execute("SELECT * FROM chat_context_entries WHERE id = ?", (int(row_id),)).fetchone()
        return {"ok": True, "updated": True, "entry": dict(row) if row else None}

    def delete_context_entry(self, *, row_id: int) -> dict[str, Any]:
        self._ensure_schema()
        with self._lock:
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute("DELETE FROM chat_context_entries WHERE id = ?", (int(row_id),))
        return {"ok": True, "deleted_count": int(cursor.rowcount or 0), "entry_id": int(row_id)}

    def delete_chat_conversation(self, *, conversation_id: str) -> dict[str, Any]:
        self._ensure_schema()
        conversation_id = normalized_conversation_id(conversation_id)
        with self._lock:
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute("DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,))
                context_cursor = conn.execute("DELETE FROM chat_context_entries WHERE conversation_id = ?", (conversation_id,))
                conn.execute("DELETE FROM chat_conversations WHERE conversation_id = ?", (conversation_id,))
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "deleted_count": int(cursor.rowcount or 0),
            "context_deleted_count": int(context_cursor.rowcount or 0),
        }

    def clear_chat_messages(self) -> dict[str, Any]:
        self._ensure_schema()
        with self._lock:
            with closing(self._connect()) as conn, conn:
                count = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
                context_count = conn.execute("SELECT COUNT(*) FROM chat_context_entries").fetchone()[0]
                conn.execute("DELETE FROM chat_messages")
                conn.execute("DELETE FROM chat_context_entries")
                conn.execute("DELETE FROM chat_conversations")
        return {"ok": True, "deleted_count": int(count), "context_deleted_count": int(context_count)}

    def merge_chat_conversations(self, *, target_conversation_id: str, source_conversation_ids: list[str]) -> dict[str, Any]:
        self._ensure_schema()
        target = normalized_conversation_id(target_conversation_id)
        sources = [normalized_conversation_id(item) for item in source_conversation_ids if normalized_conversation_id(item) != target]
        moved = 0
        duplicate_deleted = 0
        with self._lock:
            with closing(self._connect()) as conn, conn:
                for source in sources:
                    rows = conn.execute("SELECT id FROM chat_messages WHERE conversation_id = ? ORDER BY id ASC", (source,)).fetchall()
                    for row in rows:
                        cursor = conn.execute("UPDATE OR IGNORE chat_messages SET conversation_id = ? WHERE id = ?", (target, row["id"]))
                        if cursor.rowcount:
                            moved += 1
                        else:
                            conn.execute("DELETE FROM chat_messages WHERE id = ?", (row["id"],))
                            duplicate_deleted += 1
                    context_rows = conn.execute(
                        "SELECT id FROM chat_context_entries WHERE conversation_id = ? ORDER BY id ASC",
                        (source,),
                    ).fetchall()
                    for row in context_rows:
                        cursor = conn.execute("UPDATE OR IGNORE chat_context_entries SET conversation_id = ? WHERE id = ?", (target, row["id"]))
                        if cursor.rowcount:
                            moved += 1
                        else:
                            conn.execute("DELETE FROM chat_context_entries WHERE id = ?", (row["id"],))
                            duplicate_deleted += 1
                    conn.execute("DELETE FROM chat_conversations WHERE conversation_id = ?", (source,))
        return {
            "ok": True,
            "target_conversation_id": target,
            "source_conversation_ids": sources,
            "moved_count": moved,
            "duplicate_deleted_count": duplicate_deleted,
        }

    def export_markdown(self, *, session_id: str | None = None, max_events: int = 1000, write_file: bool = True) -> dict[str, Any]:
        max_events = max(1, min(int(max_events), 5000))
        sessions = self._load_sessions(session_id=session_id)
        if session_id and not sessions:
            raise ValueError(f"Transcript session not found: {session_id}")
        events = self._load_events(session_id=session_id, max_events=max_events)
        markdown = self._render_markdown(sessions, events, session_id=session_id, max_events=max_events)
        output_path: Path | None = None
        if write_file:
            self.markdown_dir.mkdir(parents=True, exist_ok=True)
            safe_name = safe_filename(session_id or "all-sessions")
            output_path = self.markdown_dir / f"{safe_name}.md"
            output_path.write_text(markdown, encoding="utf-8")
        return {
            "ok": True,
            "session_id": session_id,
            "event_count": len(events),
            "sessions": sessions,
            "path": str(output_path) if output_path else None,
            "markdown": markdown,
        }

    def export_chat_markdown(
        self,
        *,
        conversation_id: str | None = None,
        max_messages: int = 5000,
        write_file: bool = True,
    ) -> dict[str, Any]:
        max_messages = max(1, min(int(max_messages), 20000))
        normalized_id = normalized_conversation_id(conversation_id) if conversation_id else None
        messages = self._load_chat_messages(conversation_id=normalized_id, max_messages=max_messages)
        if normalized_id and not messages:
            raise ValueError(f"Chat conversation not found: {normalized_id}")
        conversations = self._load_chat_conversations(limit=500)
        if normalized_id:
            conversations = [item for item in conversations if item.get("conversation_id") == normalized_id]
        markdown = self._render_chat_markdown(
            conversations,
            messages,
            conversation_id=normalized_id,
            max_messages=max_messages,
        )
        output_path: Path | None = None
        if write_file:
            self.markdown_dir.mkdir(parents=True, exist_ok=True)
            safe_name = safe_filename(normalized_id or "all-chat-conversations")
            output_path = self.markdown_dir / f"chat-{safe_name}.md"
            output_path.write_text(markdown, encoding="utf-8")
        return {
            "ok": True,
            "conversation_id": normalized_id,
            "message_count": len(messages),
            "conversations": conversations,
            "path": str(output_path) if output_path else None,
            "markdown": markdown,
        }

    def export_context_markdown(
        self,
        *,
        conversation_id: str | None = None,
        max_entries: int = 200,
        write_file: bool = True,
    ) -> dict[str, Any]:
        max_entries = max(1, min(int(max_entries), 5000))
        normalized_id = normalized_conversation_id(conversation_id) if conversation_id else None
        entries = self._load_context_entries(conversation_id=normalized_id, max_entries=max_entries)
        conversations = self._load_chat_conversations(limit=500)
        if normalized_id:
            conversations = [item for item in conversations if item.get("conversation_id") == normalized_id]
        markdown = self._render_context_markdown(
            conversations,
            entries,
            conversation_id=normalized_id,
            max_entries=max_entries,
        )
        output_path: Path | None = None
        if write_file:
            self.markdown_dir.mkdir(parents=True, exist_ok=True)
            safe_name = safe_filename(normalized_id or "all-chat-context")
            output_path = self.markdown_dir / f"context-{safe_name}.md"
            output_path.write_text(markdown, encoding="utf-8")
        return {
            "ok": True,
            "conversation_id": normalized_id,
            "entry_count": len(entries),
            "conversations": conversations,
            "path": str(output_path) if output_path else None,
            "markdown": markdown,
        }

    def _record_event(self, event: dict[str, Any], *, request_increment: int) -> None:
        self._ensure_schema()
        session_id = normalized_session_id(event.get("session_id"))
        timestamp = sanitized_text(event.get("timestamp") or utc_now())
        payload_json = safe_json_dumps(event)
        with self._lock:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, first_seen, last_seen, request_count, workspace,
                        default_cwd, default_cwd_display, remote_addr, user_agent, protocol_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        request_count=sessions.request_count + excluded.request_count,
                        workspace=COALESCE(excluded.workspace, sessions.workspace),
                        default_cwd=COALESCE(excluded.default_cwd, sessions.default_cwd),
                        default_cwd_display=COALESCE(excluded.default_cwd_display, sessions.default_cwd_display),
                        remote_addr=COALESCE(excluded.remote_addr, sessions.remote_addr),
                        user_agent=COALESCE(excluded.user_agent, sessions.user_agent),
                        protocol_version=COALESCE(excluded.protocol_version, sessions.protocol_version)
                    """,
                    (
                        session_id,
                        timestamp,
                        timestamp,
                        request_increment,
                        event.get("workspace"),
                        event.get("default_cwd"),
                        event.get("default_cwd_display"),
                        event.get("remote_addr"),
                        event.get("user_agent"),
                        event.get("protocol_version"),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO events (session_id, timestamp, event_type, tool, rpc_method, status, ok, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        timestamp,
                        sanitized_text(event.get("event") or "event"),
                        event.get("tool"),
                        event.get("rpc_method"),
                        sanitized_text(event.get("status")) if event.get("status") is not None else None,
                        bool_to_int(event.get("ok")),
                        payload_json,
                    ),
                )

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(f"PRAGMA user_version = {TRANSCRIPT_SCHEMA_VERSION}")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        request_count INTEGER NOT NULL DEFAULT 0,
                        workspace TEXT,
                        default_cwd TEXT,
                        default_cwd_display TEXT,
                        remote_addr TEXT,
                        user_agent TEXT,
                        protocol_version TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        tool TEXT,
                        rpc_method TEXT,
                        status TEXT,
                        ok INTEGER,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_conversations (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        unique_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        source TEXT,
                        metadata_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        message_id TEXT,
                        role TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        content TEXT NOT NULL,
                        source TEXT,
                        metadata_json TEXT,
                        UNIQUE(conversation_id, message_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chat_context_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        entry_id TEXT,
                        kind TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        content TEXT NOT NULL,
                        source TEXT,
                        metadata_json TEXT,
                        UNIQUE(conversation_id, entry_id)
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_transcript_events_session ON events(session_id, id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_transcript_events_time ON events(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_time ON chat_messages(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_context_conversation ON chat_context_entries(conversation_id, id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_context_time ON chat_context_entries(timestamp)")
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_sessions(self, *, session_id: str | None) -> list[dict[str, Any]]:
        self._ensure_schema()
        with closing(self._connect()) as conn, conn:
            if session_id:
                rows = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM sessions ORDER BY last_seen DESC").fetchall()
        return [dict(row) for row in rows]

    def _load_events(self, *, session_id: str | None, max_events: int) -> list[dict[str, Any]]:
        self._ensure_schema()
        with closing(self._connect()) as conn, conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM events WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                    (session_id, max_events),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY id ASC LIMIT ?", (max_events,)).fetchall()
        return [dict(row) for row in rows]

    def _load_chat_conversations(self, *, limit: int, query: str | None = None) -> list[dict[str, Any]]:
        self._ensure_schema()
        normalized_query = sanitized_text(query or "").strip().lower()
        where_clause = ""
        params: list[Any] = []
        if normalized_query:
            like_query = f"%{normalized_query}%"
            where_clause = """
                WHERE LOWER(all_ids.conversation_id) LIKE ?
                   OR LOWER(COALESCE(c.title, '')) LIKE ?
                   OR LOWER(COALESCE(c.unique_id, '')) LIKE ?
                """
            params.extend([like_query, like_query, like_query])
        params.append(limit)
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                f"""
                SELECT all_ids.conversation_id,
                       c.title,
                       c.unique_id,
                       COALESCE(MIN(m.timestamp), MIN(ctx.timestamp), c.created_at) AS first_seen,
                       COALESCE(MAX(m.timestamp), MAX(ctx.timestamp), c.updated_at) AS last_seen,
                       COUNT(DISTINCT m.id) AS message_count,
                       COUNT(DISTINCT ctx.id) AS context_entry_count,
                       COALESCE(MAX(m.source), MAX(ctx.source), c.source) AS source
                FROM (
                    SELECT conversation_id FROM chat_messages
                    UNION
                    SELECT conversation_id FROM chat_context_entries
                    UNION
                    SELECT conversation_id FROM chat_conversations
                ) AS all_ids
                LEFT JOIN chat_messages m ON m.conversation_id = all_ids.conversation_id
                LEFT JOIN chat_context_entries ctx ON ctx.conversation_id = all_ids.conversation_id
                LEFT JOIN chat_conversations c ON c.conversation_id = all_ids.conversation_id
                {where_clause}
                GROUP BY all_ids.conversation_id
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        conversations = [dict(row) for row in rows]
        for conversation in conversations:
            conversation["date"] = str(conversation.get("last_seen") or "")[:10]
        return conversations

    def _load_chat_messages(self, *, conversation_id: str | None, max_messages: int) -> list[dict[str, Any]]:
        self._ensure_schema()
        with closing(self._connect()) as conn, conn:
            if conversation_id:
                rows = conn.execute(
                    "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id ASC LIMIT ?",
                    (conversation_id, max_messages),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM chat_messages ORDER BY conversation_id ASC, id ASC LIMIT ?", (max_messages,)).fetchall()
        return [dict(row) for row in rows]

    def _load_context_entries(self, *, conversation_id: str | None, max_entries: int) -> list[dict[str, Any]]:
        self._ensure_schema()
        with closing(self._connect()) as conn, conn:
            if conversation_id:
                rows = conn.execute(
                    "SELECT * FROM chat_context_entries WHERE conversation_id = ? ORDER BY id ASC LIMIT ?",
                    (conversation_id, max_entries),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM chat_context_entries ORDER BY conversation_id ASC, id ASC LIMIT ?",
                    (max_entries,),
                ).fetchall()
        return [dict(row) for row in rows]

    def _upsert_chat_conversation(
        self,
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        title: str | None,
        unique_id: str | None,
        source: str | None,
        timestamp: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        metadata_json = None if metadata is None else json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
        conn.execute(
            """
            INSERT INTO chat_conversations (
                conversation_id, title, unique_id, created_at, updated_at, source, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                title=COALESCE(excluded.title, chat_conversations.title),
                unique_id=COALESCE(excluded.unique_id, chat_conversations.unique_id),
                updated_at=excluded.updated_at,
                source=COALESCE(excluded.source, chat_conversations.source),
                metadata_json=COALESCE(excluded.metadata_json, chat_conversations.metadata_json)
            """,
            (
                conversation_id,
                normalized_optional_text(title, limit=192),
                normalized_optional_text(unique_id, limit=96),
                timestamp,
                timestamp,
                normalized_optional_text(source, limit=128),
                metadata_json,
            ),
        )

    def _render_markdown(
        self,
        sessions: list[dict[str, Any]],
        events: list[dict[str, Any]],
        *,
        session_id: str | None,
        max_events: int,
    ) -> str:
        title = f"MCP Session Transcript: {session_id}" if session_id else "MCP Session Transcripts"
        lines = [f"# {title}", ""]
        lines.append(f"Generated: {utc_now()}")
        lines.append(f"Database: `{self.db_path}`")
        lines.append("")
        lines.append("## Sessions")
        if sessions:
            for session in sessions:
                lines.extend(render_session_summary(session))
        else:
            lines.append("No sessions recorded.")
        lines.append("")
        lines.append(f"## Events ({len(events)} shown, max {max_events})")
        if not events:
            lines.append("No events recorded.")
            lines.append("")
            return "\n".join(lines)
        for row in events:
            payload = load_payload(row.get("payload_json"))
            lines.extend(render_event(row, payload))
        return "\n".join(lines).rstrip() + "\n"

    def _render_chat_markdown(
        self,
        conversations: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        *,
        conversation_id: str | None,
        max_messages: int,
    ) -> str:
        title = f"聊天备份记录：{conversation_id}" if conversation_id else "聊天备份记录"
        lines = [f"# {title}", ""]
        lines.append(f"生成时间：{utc_now()}")
        lines.append(f"数据库：`{self.db_path}`")
        lines.append("")
        lines.append("## 会话")
        if conversations:
            for conversation in conversations:
                lines.extend(render_chat_conversation_summary(conversation))
        else:
            lines.append("暂无聊天会话记录。")
        lines.append("")
        lines.append(f"## 消息（显示 {len(messages)} 条，最多 {max_messages} 条）")
        if not messages:
            lines.append("暂无聊天消息记录。")
            lines.append("")
            return "\n".join(lines)
        current_conversation = None
        for message in messages:
            if not conversation_id and message.get("conversation_id") != current_conversation:
                current_conversation = str(message.get("conversation_id") or "")
                lines.extend(["", f"## 会话 `{current_conversation}`"])
            lines.extend(render_chat_message(message))
        return "\n".join(lines).rstrip() + "\n"

    def _render_context_markdown(
        self,
        conversations: list[dict[str, Any]],
        entries: list[dict[str, Any]],
        *,
        conversation_id: str | None,
        max_entries: int,
    ) -> str:
        title = f"恢复上下文：{conversation_id}" if conversation_id else "恢复上下文记录"
        lines = [f"# {title}", ""]
        lines.append(f"生成时间：{utc_now()}")
        lines.append(f"数据库：`{self.db_path}`")
        lines.append("")
        lines.append("## 会话")
        if conversations:
            for conversation in conversations:
                lines.extend(render_chat_conversation_summary(conversation))
        else:
            lines.append("暂无聊天会话记录。")
        lines.append("")
        lines.append(f"## 上下文条目（显示 {len(entries)} 条，最多 {max_entries} 条）")
        if not entries:
            lines.append("暂无恢复上下文条目。")
            lines.append("")
            return "\n".join(lines)
        current_conversation = None
        for entry in entries:
            if not conversation_id and entry.get("conversation_id") != current_conversation:
                current_conversation = str(entry.get("conversation_id") or "")
                lines.extend(["", f"## 会话 `{current_conversation}`"])
            lines.extend(render_context_entry(entry))
        return "\n".join(lines).rstrip() + "\n"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalized_session_id(value: Any) -> str:
    if isinstance(value, str) and value:
        return sanitized_text(value)[:128]
    return "server"


def normalized_conversation_id(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return sanitized_text(value).strip()[:192]
    return "default"


def normalized_role(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return sanitized_text(value).strip()[:64]
    return "message"


def normalized_context_kind(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return sanitized_text(value).strip()[:64]
    return "checkpoint"


def normalized_optional_text(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    text = sanitized_text(value).strip()
    return text[:limit] if text else None


def sanitized_text(value: Any) -> str:
    return SURROGATE_RE.sub("\ufffd", str(value))


def safe_json_dumps(value: Any) -> str:
    return sanitized_text(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def bool_to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return 1 if value else 0
    return None


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return (cleaned or "transcript")[:96]


def load_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def render_session_summary(session: dict[str, Any]) -> list[str]:
    return [
        f"- Session `{session.get('session_id')}`",
        f"  - First seen: {session.get('first_seen')}",
        f"  - Last seen: {session.get('last_seen')}",
        f"  - Requests: {session.get('request_count')}",
        f"  - Workspace: `{session.get('workspace') or ''}`",
        f"  - Default cwd: `{session.get('default_cwd_display') or session.get('default_cwd') or ''}`",
        f"  - Remote: `{session.get('remote_addr') or ''}`",
    ]


def render_chat_conversation_summary(conversation: dict[str, Any]) -> list[str]:
    lines = [
        f"- 会话 `{conversation.get('conversation_id')}`",
    ]
    if conversation.get("title"):
        lines.append(f"  - 标题：{conversation.get('title')}")
    if conversation.get("unique_id"):
        lines.append(f"  - UID：`{conversation.get('unique_id')}`")
    lines.extend([
        f"  - 开始时间：{conversation.get('first_seen')}",
        f"  - 最近时间：{conversation.get('last_seen')}",
        f"  - 聊天消息：{conversation.get('message_count')}",
        f"  - 恢复上下文：{conversation.get('context_entry_count', 0)}",
        f"  - 来源：`{conversation.get('source') or ''}`",
    ])
    return lines


def localized_role(value: Any) -> str:
    role = str(value or "message").strip()
    return {
        "user": "用户",
        "assistant": "助手",
        "system": "系统",
        "tool": "工具",
        "message": "消息",
    }.get(role.lower(), role)


def localized_context_kind(value: Any) -> str:
    kind = str(value or "checkpoint").strip()
    return {
        "checkpoint": "检查点",
        "summary": "摘要",
        "note": "备注",
        "context": "上下文",
    }.get(kind.lower(), kind)


def render_event(row: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    event_type = str(row.get("event_type") or payload.get("event") or "event")
    timestamp = str(row.get("timestamp") or payload.get("timestamp") or "")
    if event_type == "tool_call":
        heading = f"### {timestamp} - Tool `{payload.get('tool') or row.get('tool')}`"
    elif event_type == "mcp_http_request":
        heading = f"### {timestamp} - HTTP `{payload.get('method')} {payload.get('path')}`"
    else:
        heading = f"### {timestamp} - {event_type}"
    lines = ["", heading]
    if payload.get("session_id"):
        lines.append(f"- Session: `{payload.get('session_id')}`")
    if payload.get("rpc_method"):
        lines.append(f"- RPC: `{payload.get('rpc_method')}`")
    if payload.get("status") is not None:
        lines.append(f"- Status: `{payload.get('status')}`")
    if payload.get("ok") is not None:
        lines.append(f"- OK: `{payload.get('ok')}`")
    if payload.get("duration_ms") is not None:
        lines.append(f"- Duration: `{payload.get('duration_ms')} ms`")
    if "args" in payload:
        lines.append("- Arguments:")
        lines.append(json_block(payload.get("args")))
    if "result" in payload:
        lines.append("- Result:")
        lines.append(json_block(payload.get("result")))
    elif event_type != "tool_call":
        lines.append("- Payload:")
        lines.append(json_block(payload))
    return lines


def render_chat_message(message: dict[str, Any]) -> list[str]:
    timestamp = str(message.get("timestamp") or "")
    role = localized_role(message.get("role"))
    lines = ["", f"### {timestamp} - {role}"]
    if message.get("message_id"):
        lines.append(f"- 消息 ID：`{message.get('message_id')}`")
    if message.get("source"):
        lines.append(f"- 来源：`{message.get('source')}`")
    metadata = load_payload(message.get("metadata_json"))
    if metadata:
        lines.append("- 元数据：")
        lines.append(json_block(metadata))
    content = str(message.get("content") or "")
    lines.append("")
    lines.append(content.rstrip() if content else "（空）")
    lines.append("")
    lines.append("---")
    return lines


def render_context_entry(entry: dict[str, Any]) -> list[str]:
    timestamp = str(entry.get("timestamp") or "")
    kind = localized_context_kind(entry.get("kind"))
    lines = ["", f"### {timestamp} - {kind}"]
    if entry.get("entry_id"):
        lines.append(f"- 条目 ID：`{entry.get('entry_id')}`")
    if entry.get("source"):
        lines.append(f"- 来源：`{entry.get('source')}`")
    metadata = load_payload(entry.get("metadata_json"))
    if metadata:
        lines.append("- 元数据：")
        lines.append(json_block(metadata))
    content = str(entry.get("content") or "")
    lines.append("")
    lines.append(content.rstrip() if content else "（空）")
    lines.append("")
    lines.append("---")
    return lines


def json_block(value: Any) -> str:
    text = sanitized_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    text = text.replace("```", "` ` `")
    return f"```json\n{text}\n```"
