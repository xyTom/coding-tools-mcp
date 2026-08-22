"""Privacy-preserving client-window identity and durable Conversation bindings."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
WINDOW_DIGEST_DOMAIN = b"coding-tools.client-window.v1"
BINDING_KEY_DOMAIN = b"coding-tools.conversation-binding.v1"


class ConversationBindingStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClientWindowIdentity:
    """A domain-separated digest of one opaque transport window identifier."""

    transport_kind: str
    window_digest: str

    @classmethod
    def from_transport(cls, transport_kind: str, raw_identifier: str) -> "ClientWindowIdentity":
        if transport_kind not in {"mcp", "webui", "actions"} or not transport_kind:
            raise ConversationBindingStoreError("Unsupported client-window transport.")
        if not raw_identifier or len(raw_identifier) > 4096:
            raise ConversationBindingStoreError("Client-window identifier is invalid.")
        digest = hashlib.sha256(
            WINDOW_DIGEST_DOMAIN
            + b"\0"
            + transport_kind.encode("ascii")
            + b"\0"
            + raw_identifier.encode("utf-8")
        ).hexdigest()
        return cls(transport_kind, digest)

    @classmethod
    def from_mcp_session(cls, session_id: str) -> "ClientWindowIdentity":
        return cls.from_transport("mcp", session_id)


@dataclass(frozen=True)
class ConversationBinding:
    binding_digest: str
    conversation_id: str
    workspace_id: str
    transport_kind: str
    repo_scope_digest: str
    created_at: float
    updated_at: float


def _digest(*parts: str) -> str:
    encoded = BINDING_KEY_DOMAIN + b"\0" + "\0".join(parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def principal_scope_digest(authorization_key: tuple[str, str | None, str | None, str]) -> str:
    method, client_id, grant_id, workspace_id = authorization_key
    return _digest(method, client_id or "", grant_id or "", workspace_id)


def repo_scope_digest(workspace_root: Path) -> str:
    try:
        canonical = str(workspace_root.expanduser().resolve(strict=True))
    except (OSError, RuntimeError):
        canonical = str(workspace_root)
    return hashlib.sha256(canonical.casefold().encode("utf-8")).hexdigest()


class ConversationBindingStore:
    """Durable exact-match bindings without retaining raw transport identifiers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _migrate(self) -> None:
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_bindings (
                    binding_digest TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    principal_scope_digest TEXT NOT NULL,
                    transport_kind TEXT NOT NULL,
                    repo_scope_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """,
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_bindings_conversation "
                "ON conversation_bindings(workspace_id, conversation_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_bindings_window "
                "ON conversation_bindings(principal_scope_digest, transport_kind)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    @staticmethod
    def make_binding_digest(
        *,
        principal_scope: str,
        identity: ClientWindowIdentity,
        workspace_id: str,
        repo_scope: str,
    ) -> str:
        return _digest(principal_scope, identity.transport_kind, identity.window_digest, workspace_id, repo_scope)

    def resolve(
        self,
        *,
        principal_scope: str,
        identity: ClientWindowIdentity,
        workspace_id: str,
        repo_scope: str,
    ) -> ConversationBinding | None:
        binding_digest = self.make_binding_digest(
            principal_scope=principal_scope,
            identity=identity,
            workspace_id=workspace_id,
            repo_scope=repo_scope,
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM conversation_bindings WHERE binding_digest=?", (binding_digest,)
            ).fetchone()
        return self._payload(row) if row is not None else None

    def bind(
        self,
        *,
        conversation_id: str,
        principal_scope: str,
        identity: ClientWindowIdentity,
        workspace_id: str,
        repo_scope: str,
    ) -> ConversationBinding:
        if not conversation_id or len(conversation_id) > 256:
            raise ConversationBindingStoreError("Conversation ID is invalid.")
        now = time.time()
        binding_digest = self.make_binding_digest(
            principal_scope=principal_scope,
            identity=identity,
            workspace_id=workspace_id,
            repo_scope=repo_scope,
        )
        with self._write_lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT workspace_id, repo_scope_digest FROM conversation_bindings WHERE binding_digest=?",
                (binding_digest,),
            ).fetchone()
            if row is not None and (
                row["workspace_id"] != workspace_id or row["repo_scope_digest"] != repo_scope
            ):
                raise ConversationBindingStoreError("Binding scope collision.")
            connection.execute(
                """
                INSERT INTO conversation_bindings (
                    binding_digest, conversation_id, workspace_id, principal_scope_digest,
                    transport_kind, repo_scope_digest, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(binding_digest) DO UPDATE SET
                    conversation_id=excluded.conversation_id,
                    updated_at=excluded.updated_at
                """
                ,
                (
                    binding_digest,
                    conversation_id,
                    workspace_id,
                    principal_scope,
                    identity.transport_kind,
                    repo_scope,
                    now,
                    now,
                ),
            )
        return ConversationBinding(
            binding_digest=binding_digest,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            transport_kind=identity.transport_kind,
            repo_scope_digest=repo_scope,
            created_at=now,
            updated_at=now,
        )

    def list_for_conversation(self, workspace_id: str, conversation_id: str) -> list[ConversationBinding]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM conversation_bindings "
                "WHERE workspace_id=? AND conversation_id=? ORDER BY updated_at DESC LIMIT 100",
                (workspace_id, conversation_id),
            ).fetchall()
        return [item for row in rows if (item := self._payload(row)) is not None]

    def delete_conversation(self, workspace_id: str, conversation_id: str) -> int:
        with self._write_lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM conversation_bindings WHERE workspace_id=? AND conversation_id=?",
                (workspace_id, conversation_id),
            )
            return int(cursor.rowcount)

    @staticmethod
    def _payload(row: sqlite3.Row) -> ConversationBinding:
        return ConversationBinding(
            binding_digest=str(row["binding_digest"]),
            conversation_id=str(row["conversation_id"]),
            workspace_id=str(row["workspace_id"]),
            transport_kind=str(row["transport_kind"]),
            repo_scope_digest=str(row["repo_scope_digest"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


class ConversationContinuityService:
    """MCP-facing facade over exact bindings and the existing transcript store."""

    def __init__(
        self,
        bindings: ConversationBindingStore,
        transcripts: Any,
    ) -> None:
        self.bindings = bindings
        self.transcripts = transcripts

    def start_or_resume_exact(
        self,
        *,
        runtime: Any,
        title: str | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(runtime)
        identity = ClientWindowIdentity.from_mcp_session(str(runtime.http_session_id))
        binding = self.bindings.resolve(**scope, identity=identity)
        resumed = binding is not None
        if binding is None:
            result = self.transcripts.record_messages(
                scope["workspace_id"],
                f"conversation-{hashlib.sha256(scope['principal_scope'].encode()).hexdigest()[:12]}-{int(time.time()*1000)}",
                [],
                title=title or "MCP Conversation",
                source="mcp",
            )
            conversation_id = str(result["conversation_id"])
            binding = self.bindings.bind(conversation_id=conversation_id, **scope, identity=identity)
        else:
            conversation_id = binding.conversation_id
        return {
            "conversation_id": conversation_id,
            "resumed": resumed,
            "transport": {"kind": binding.transport_kind, "window_digest": identity.window_digest},
        }

    def list_for_runtime(self, runtime: Any, *, limit: int = 20) -> dict[str, Any]:
        scope = self._scope(runtime)
        payload = self.transcripts.list_conversations(
            scope["workspace_id"],
            page=1,
            page_size=max(1, min(int(limit), 100)),
        )
        return {"items": payload.get("items", [])}

    def resume_explicit(self, runtime: Any, conversation_id: str) -> dict[str, Any]:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ConversationBindingStoreError("conversation_id must be a non-empty string.")
        conversation_id = conversation_id.strip()
        scope = self._scope(runtime)
        detail = self.transcripts.conversation_detail(
            scope["workspace_id"],
            conversation_id,
            message_page_size=1,
            context_page_size=1,
        )
        if detail is None:
            # Avoid an ownership oracle across principals/workspaces.
            raise ConversationBindingStoreError("Conversation is unavailable.")
        identity = ClientWindowIdentity.from_mcp_session(str(runtime.http_session_id))
        binding = self.bindings.bind(conversation_id=conversation_id, **scope, identity=identity)
        return {
            "conversation_id": binding.conversation_id,
            "resumed": True,
            "transport": {"kind": binding.transport_kind, "window_digest": identity.window_digest},
        }

    @staticmethod
    def _scope(runtime: Any) -> dict[str, str]:
        context = getattr(runtime, "authorization_context", None)
        binding = getattr(runtime, "workspace_binding", None)
        root = getattr(binding, "root", None)
        if context is None or binding is None or root is None:
            raise ConversationBindingStoreError("Runtime scope is unavailable.")
        return {
            "principal_scope": principal_scope_digest(context.authorization_key(binding.workspace_id)),
            "workspace_id": str(binding.workspace_id),
            "repo_scope": repo_scope_digest(root),
        }


__all__ = [
    "BINDING_KEY_DOMAIN",
    "ClientWindowIdentity",
    "ConversationBinding",
    "ConversationBindingStore",
    "ConversationBindingStoreError",
    "ConversationContinuityService",
    "principal_scope_digest",
    "repo_scope_digest",
]
