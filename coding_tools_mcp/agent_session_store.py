"""Durable, workspace- and principal-partitioned Agent Session persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List, Mapping


SCHEMA_VERSION = 1
MAX_INSTRUCTIONS_CHARS = 131_072
MAX_JSON_CHARS = 262_144
MAX_LIST_LIMIT = 200

SESSION_STATUSES = frozenset(
    {
        "creating",
        "ready",
        "running",
        "waiting_approval",
        "unavailable",
        "failed",
        "closed",
    }
)


class AgentSessionStoreError(RuntimeError):
    pass


class AgentSessionNotFoundError(AgentSessionStoreError):
    """Generic not-found response used to avoid ownership-oracle leaks."""


def _now() -> float:
    return time.time()


def _require_id(value: Any, field: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise AgentSessionStoreError(f"{field} must be a non-empty string up to {limit} characters.")
    if "\x00" in value or value in {".", ".."}:
        raise AgentSessionStoreError(f"{field} contains unsupported characters.")
    return value


def _optional_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_id(value, field)


def _instructions(value: Any) -> str:
    if value is None:
        return ""
    result = str(value).replace("\x00", "\ufffd")
    if len(result) > MAX_INSTRUCTIONS_CHARS:
        raise AgentSessionStoreError(
            f"explicit_instructions exceeds the {MAX_INSTRUCTIONS_CHARS}-character limit."
        )
    return result


def _json_object(value: Any, field: str) -> tuple[dict[str, Any], str]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise AgentSessionStoreError(f"{field} must be a JSON object.")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AgentSessionStoreError(f"{field} must contain standard JSON values: {exc}") from exc
    if len(encoded) > MAX_JSON_CHARS:
        raise AgentSessionStoreError(f"{field} exceeds the {MAX_JSON_CHARS}-character limit.")
    return dict(value), encoded


def _status(value: Any) -> str:
    if not isinstance(value, str) or value not in SESSION_STATUSES:
        raise AgentSessionStoreError(
            "status must be one of: " + ", ".join(sorted(SESSION_STATUSES))
        )
    return value


@dataclass(frozen=True)
class AgentSessionRecord:
    session_id: str
    workspace_id: str
    owner_principal_id: str
    backend_kind: str
    backend_thread_id: str | None
    conversation_id: str | None
    status: str
    explicit_instructions: str
    instruction_digest: str | None
    repo_fingerprint: dict[str, Any]
    last_turn_id: str | None
    created_at: float
    updated_at: float

    def summary_payload(self) -> dict[str, Any]:
        """Bounded external projection that intentionally hides the backend thread id."""
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "backend_kind": self.backend_kind,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "last_turn_id": self.last_turn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AgentSessionStore:
    """Independent SQLite store for durable execution state, not chat message bodies."""

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
                raise AgentSessionStoreError(
                    "Agent Session database was written by a newer version."
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions(
                    session_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    owner_principal_id TEXT NOT NULL,
                    backend_kind TEXT NOT NULL,
                    backend_thread_id TEXT,
                    conversation_id TEXT,
                    status TEXT NOT NULL,
                    explicit_instructions TEXT NOT NULL DEFAULT '',
                    instruction_digest TEXT,
                    repo_fingerprint_json TEXT NOT NULL DEFAULT '{}',
                    last_turn_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_sessions_owner_workspace "
                "ON agent_sessions(owner_principal_id, workspace_id, updated_at DESC)"
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def create(
        self,
        *,
        workspace_id: str,
        owner_principal_id: str,
        backend_kind: str,
        backend_thread_id: str | None = None,
        conversation_id: str | None = None,
        status: str = "creating",
        explicit_instructions: str | None = None,
        instruction_digest: str | None = None,
        repo_fingerprint: Mapping[str, Any] | None = None,
        session_id: str | None = None,
    ) -> AgentSessionRecord:
        workspace_id = _require_id(workspace_id, "workspace_id")
        owner_principal_id = _require_id(owner_principal_id, "owner_principal_id")
        backend_kind = _require_id(backend_kind, "backend_kind", limit=128)
        backend_thread_id = _optional_id(backend_thread_id, "backend_thread_id")
        conversation_id = _optional_id(conversation_id, "conversation_id")
        session_id = _require_id(session_id or f"agent-{uuid.uuid4().hex}", "session_id")
        normalized_status = _status(status)
        normalized_instructions = _instructions(explicit_instructions)
        instruction_digest = _optional_id(instruction_digest, "instruction_digest")
        _, fingerprint_json = _json_object(repo_fingerprint, "repo_fingerprint")
        now = _now()
        try:
            with self._write_lock, self._connection(write=True) as conn:
                conn.execute(
                    """
                    INSERT INTO agent_sessions(
                        session_id, workspace_id, owner_principal_id, backend_kind,
                        backend_thread_id, conversation_id, status, explicit_instructions,
                        instruction_digest, repo_fingerprint_json, last_turn_id,
                        created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?)
                    """,
                    (
                        session_id,
                        workspace_id,
                        owner_principal_id,
                        backend_kind,
                        backend_thread_id,
                        conversation_id,
                        normalized_status,
                        normalized_instructions,
                        instruction_digest,
                        fingerprint_json,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AgentSessionStoreError("Agent Session id already exists.") from exc
        return self.get(session_id, workspace_id, owner_principal_id)

    def get(
        self,
        session_id: str,
        workspace_id: str,
        owner_principal_id: str,
    ) -> AgentSessionRecord:
        session_id = _require_id(session_id, "session_id")
        workspace_id = _require_id(workspace_id, "workspace_id")
        owner_principal_id = _require_id(owner_principal_id, "owner_principal_id")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE session_id=? AND workspace_id=? AND owner_principal_id=?",
                (session_id, workspace_id, owner_principal_id),
            ).fetchone()
        if row is None:
            raise AgentSessionNotFoundError("Agent Session was not found.")
        return self._record(row)

    def get_for_owner(self, session_id: str, owner_principal_id: str) -> AgentSessionRecord:
        session_id = _require_id(session_id, "session_id")
        owner_principal_id = _require_id(owner_principal_id, "owner_principal_id")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE session_id=? AND owner_principal_id=?",
                (session_id, owner_principal_id),
            ).fetchone()
        if row is None:
            raise AgentSessionNotFoundError("Agent Session was not found.")
        return self._record(row)

    def list(
        self,
        workspace_id: str,
        owner_principal_id: str,
        *,
        limit: int = 100,
    ) -> list[AgentSessionRecord]:
        workspace_id = _require_id(workspace_id, "workspace_id")
        owner_principal_id = _require_id(owner_principal_id, "owner_principal_id")
        if type(limit) is not int or limit < 1:
            raise AgentSessionStoreError("limit must be a positive integer.")
        bounded_limit = min(limit, MAX_LIST_LIMIT)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_sessions
                WHERE workspace_id=? AND owner_principal_id=?
                ORDER BY updated_at DESC, session_id
                LIMIT ?
                """,
                (workspace_id, owner_principal_id, bounded_limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_workspace(self, workspace_id: str, *, limit: int = 100) -> List[AgentSessionRecord]:
        """Privileged workspace-wide listing; callers must authorize admin access."""

        workspace_id = _require_id(workspace_id, "workspace_id")
        if type(limit) is not int or limit < 1:
            raise AgentSessionStoreError("limit must be a positive integer.")
        bounded_limit = min(limit, MAX_LIST_LIMIT)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_sessions
                WHERE workspace_id=?
                ORDER BY updated_at DESC, session_id
                LIMIT ?
                """,
                (workspace_id, bounded_limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_all(self, *, limit: int = 1000) -> List[AgentSessionRecord]:
        """Privileged inventory used only for explicit ownership migration."""

        if type(limit) is not int or limit < 1:
            raise AgentSessionStoreError("limit must be a positive integer.")
        bounded_limit = min(limit, MAX_LIST_LIMIT)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_sessions ORDER BY updated_at DESC, session_id LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def iter_all(self, *, batch_size: int = MAX_LIST_LIMIT) -> Iterator[AgentSessionRecord]:
        """Iterate every historical session without a fixed total limit.

        The result is materialized by the caller-facing generator so ownership
        migration sees all candidates before making any uniqueness decision.
        """

        if type(batch_size) is not int or batch_size < 1:
            raise AgentSessionStoreError("batch_size must be a positive integer.")
        bounded_batch = min(batch_size, MAX_LIST_LIMIT)
        last_key: tuple[float, str] | None = None
        while True:
            with self._connection() as conn:
                if last_key is None:
                    rows = conn.execute(
                        """
                        SELECT * FROM agent_sessions
                        ORDER BY updated_at DESC, session_id LIMIT ?
                        """,
                        (bounded_batch,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM agent_sessions
                        WHERE (updated_at < ?) OR (updated_at=? AND session_id>?)
                        ORDER BY updated_at DESC, session_id LIMIT ?
                        """,
                        (last_key[0], last_key[0], last_key[1], bounded_batch),
                    ).fetchall()
            if not rows:
                return
            for row in rows:
                yield self._record(row)
            final = rows[-1]
            last_key = (float(final["updated_at"]), str(final["session_id"]))

    def update_state(
        self,
        session_id: str,
        workspace_id: str,
        owner_principal_id: str,
        *,
        status: str | None = None,
        backend_thread_id: str | None = None,
        conversation_id: str | None = None,
        last_turn_id: str | None = None,
        instruction_digest: str | None = None,
        repo_fingerprint: Mapping[str, Any] | None = None,
    ) -> AgentSessionRecord:
        current = self.get(session_id, workspace_id, owner_principal_id)
        next_status = current.status if status is None else _status(status)
        next_thread = current.backend_thread_id if backend_thread_id is None else _require_id(
            backend_thread_id, "backend_thread_id"
        )
        next_conversation = (
            current.conversation_id
            if conversation_id is None
            else _require_id(conversation_id, "conversation_id")
        )
        next_turn = current.last_turn_id if last_turn_id is None else _require_id(
            last_turn_id, "last_turn_id"
        )
        next_digest = (
            current.instruction_digest
            if instruction_digest is None
            else _require_id(instruction_digest, "instruction_digest")
        )
        if repo_fingerprint is None:
            fingerprint_json = json.dumps(
                current.repo_fingerprint, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
        else:
            _, fingerprint_json = _json_object(repo_fingerprint, "repo_fingerprint")
        now = _now()
        with self._write_lock, self._connection(write=True) as conn:
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET status=?, backend_thread_id=?, conversation_id=?, last_turn_id=?, instruction_digest=?,
                    repo_fingerprint_json=?, updated_at=?
                WHERE session_id=? AND workspace_id=? AND owner_principal_id=?
                """,
                (
                    next_status,
                    next_thread,
                    next_conversation,
                    next_turn,
                    next_digest,
                    fingerprint_json,
                    now,
                    session_id,
                    workspace_id,
                    owner_principal_id,
                ),
            )
            if cursor.rowcount != 1:
                raise AgentSessionNotFoundError("Agent Session was not found.")
        return self.get(session_id, workspace_id, owner_principal_id)

    def close(
        self,
        session_id: str,
        workspace_id: str,
        owner_principal_id: str,
    ) -> AgentSessionRecord:
        current = self.get(session_id, workspace_id, owner_principal_id)
        if current.status == "closed":
            return current
        return self.update_state(
            session_id,
            workspace_id,
            owner_principal_id,
            status="closed",
        )

    def admin_get(self, session_id: str, workspace_id: str) -> AgentSessionRecord:
        """Fetch any owner after the caller has proven privileged workspace access."""

        session_id = _require_id(session_id, "session_id")
        workspace_id = _require_id(workspace_id, "workspace_id")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE session_id=? AND workspace_id=?",
                (session_id, workspace_id),
            ).fetchone()
        if row is None:
            raise AgentSessionNotFoundError("Agent Session was not found.")
        return self._record(row)

    def admin_update_state(
        self,
        session_id: str,
        workspace_id: str,
        *,
        status: str | None = None,
        last_turn_id: str | None = None,
    ) -> AgentSessionRecord:
        """Privileged state update that deliberately preserves historical owner."""

        current = self.admin_get(session_id, workspace_id)
        next_status = current.status if status is None else _status(status)
        next_turn = current.last_turn_id if last_turn_id is None else _require_id(last_turn_id, "last_turn_id")
        now = _now()
        with self._write_lock, self._connection(write=True) as conn:
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET status=?, last_turn_id=?, updated_at=?
                WHERE session_id=? AND workspace_id=?
                """,
                (next_status, next_turn, now, session_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise AgentSessionNotFoundError("Agent Session was not found.")
        return self.admin_get(session_id, workspace_id)

    def admin_close(self, session_id: str, workspace_id: str) -> AgentSessionRecord:
        current = self.admin_get(session_id, workspace_id)
        if current.status == "closed":
            return current
        return self.admin_update_state(session_id, workspace_id, status="closed")

    @staticmethod
    def _record(row: sqlite3.Row) -> AgentSessionRecord:
        try:
            fingerprint = json.loads(str(row["repo_fingerprint_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentSessionStoreError("Stored repo fingerprint JSON is corrupt.") from exc
        if not isinstance(fingerprint, dict):
            raise AgentSessionStoreError("Stored repo fingerprint must be a JSON object.")
        status = str(row["status"])
        _status(status)
        return AgentSessionRecord(
            session_id=str(row["session_id"]),
            workspace_id=str(row["workspace_id"]),
            owner_principal_id=str(row["owner_principal_id"]),
            backend_kind=str(row["backend_kind"]),
            backend_thread_id=(
                str(row["backend_thread_id"]) if row["backend_thread_id"] is not None else None
            ),
            conversation_id=(
                str(row["conversation_id"]) if row["conversation_id"] is not None else None
            ),
            status=status,
            explicit_instructions=str(row["explicit_instructions"]),
            instruction_digest=(
                str(row["instruction_digest"]) if row["instruction_digest"] is not None else None
            ),
            repo_fingerprint=dict(fingerprint),
            last_turn_id=(str(row["last_turn_id"]) if row["last_turn_id"] is not None else None),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


__all__ = [
    "AgentSessionNotFoundError",
    "AgentSessionRecord",
    "AgentSessionStore",
    "AgentSessionStoreError",
    "SCHEMA_VERSION",
    "SESSION_STATUSES",
]
