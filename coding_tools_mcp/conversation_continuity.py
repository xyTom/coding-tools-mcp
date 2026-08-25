"""Privacy-preserving client-window identity and durable Conversation bindings."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .conversation_recorder import ConversationEvidenceRecorder

SCHEMA_VERSION = 3
WINDOW_DIGEST_DOMAIN = b"coding-tools.client-window.v1"
BINDING_KEY_DOMAIN = b"coding-tools.conversation-binding.v1"
OWNERSHIP_DIGEST_DOMAIN = b"coding-tools.conversation-ownership.v1"
AMBIGUOUS_OWNER_DIGEST = "ambiguous-owner"


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


OWNERSHIP_MCP = "mcp_principal"
OWNERSHIP_AGENT = "agent_principal"
OWNERSHIP_ADMIN = "admin"
OWNERSHIP_AMBIGUOUS = "ambiguous"
OWNERSHIP_UNOWNED = "unowned"


def _digest(*parts: str) -> str:
    encoded = BINDING_KEY_DOMAIN + b"\0" + "\0".join(parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def principal_scope_digest(authorization_key: tuple[str, str | None, str | None, str]) -> str:
    method, client_id, grant_id, workspace_id = authorization_key
    return _digest(method, client_id or "", grant_id or "", workspace_id)


def conversation_owner_digest(principal_id: str) -> str:
    """Hash a stable principal identity without retaining credential material."""

    if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 256:
        raise ConversationBindingStoreError("Principal identity is invalid.")
    encoded = OWNERSHIP_DIGEST_DOMAIN + b"\0" + principal_id.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
            )
            version = self._read_version(connection)
            if version > SCHEMA_VERSION:
                raise ConversationBindingStoreError(
                    "Conversation continuity database was written by a newer version."
                )
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
                "CREATE INDEX IF NOT EXISTS idx_bindings_conversation "
                "ON conversation_bindings(workspace_id, conversation_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_bindings_window "
                "ON conversation_bindings(principal_scope_digest, transport_kind)"
            )
            if version < 2:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_ownership (
                        workspace_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        owner_scope_digest TEXT NOT NULL,
                        source TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(workspace_id, conversation_id)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ownership_owner "
                    "ON conversation_ownership(workspace_id, owner_scope_digest)"
                )
            if version < 3:
                self._migrate_ownership_v3(connection)
            self._write_version(connection, SCHEMA_VERSION)

    @staticmethod
    def _read_version(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT version FROM schema_version WHERE version<=? "
            "ORDER BY version DESC LIMIT 1",
            (SCHEMA_VERSION,),
        ).fetchone()
        newer = connection.execute(
            "SELECT version FROM schema_version WHERE version>? LIMIT 1",
            (SCHEMA_VERSION,),
        ).fetchone()
        if newer is not None:
            return int(newer["version"])
        return int(row["version"]) if row is not None else 0

    @staticmethod
    def _write_version(connection: sqlite3.Connection, version: int) -> None:
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))

    @classmethod
    def _migrate_ownership_v3(cls, connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(conversation_ownership)")
        }
        required = {"owner_kind", "repo_scope_digest", "mcp_claimable"}
        if not columns:
            return
        if not required.issubset(columns):
            connection.execute("ALTER TABLE conversation_ownership RENAME TO conversation_ownership_v2")
            connection.execute(
                """
                CREATE TABLE conversation_ownership (
                    workspace_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    owner_kind TEXT NOT NULL,
                    owner_scope_digest TEXT NOT NULL,
                    repo_scope_digest TEXT NOT NULL DEFAULT '',
                    mcp_claimable INTEGER NOT NULL DEFAULT 0 CHECK(mcp_claimable IN (0, 1)),
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id, conversation_id)
                )
                """
            )
            legacy_rows = connection.execute(
                "SELECT * FROM conversation_ownership_v2"
            ).fetchall()
            for row in legacy_rows:
                source = str(row["source"])
                digest = str(row["owner_scope_digest"])
                if digest == AMBIGUOUS_OWNER_DIGEST:
                    kind = OWNERSHIP_AMBIGUOUS
                elif source in {"mcp", "mcp-conversation"}:
                    kind = OWNERSHIP_MCP
                else:
                    kind = OWNERSHIP_AGENT
                claimable = 0
                repo_scope = ""
                if kind == OWNERSHIP_MCP:
                    candidates = connection.execute(
                        """
                        SELECT DISTINCT principal_scope_digest, repo_scope_digest
                        FROM conversation_bindings
                        WHERE workspace_id=? AND conversation_id=?
                          AND principal_scope_digest=?
                        """,
                        (row["workspace_id"], row["conversation_id"], digest),
                    ).fetchall()
                    if len(candidates) == 1:
                        repo_scope = str(candidates[0]["repo_scope_digest"])
                        claimable = 1 if repo_scope else 0
                    elif len(candidates) > 1:
                        kind = OWNERSHIP_AMBIGUOUS
                connection.execute(
                    """
                    INSERT INTO conversation_ownership(
                        workspace_id, conversation_id, owner_kind, owner_scope_digest,
                        repo_scope_digest, mcp_claimable, source, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["workspace_id"], row["conversation_id"], kind, digest,
                        repo_scope, claimable, source, row["created_at"], row["updated_at"],
                    ),
                )
            connection.execute("DROP TABLE conversation_ownership_v2")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ownership_owner "
            "ON conversation_ownership(workspace_id, owner_kind, mcp_claimable)"
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

    def find_exact(
        self,
        *,
        principal_scope: str,
        identity: ClientWindowIdentity,
        workspace_id: str,
        repo_scope: str,
    ) -> ConversationBinding | None:
        return self.resolve(
            principal_scope=principal_scope,
            identity=identity,
            workspace_id=workspace_id,
            repo_scope=repo_scope,
        )

    def set_ownership(
        self,
        *,
        workspace_id: str,
        conversation_id: str,
        owner_scope_digest: str,
        source: str,
        owner_kind: str = OWNERSHIP_UNOWNED,
        repo_scope_digest: str = "",
        mcp_claimable: bool = False,
    ) -> None:
        allowed = {OWNERSHIP_MCP, OWNERSHIP_AGENT, OWNERSHIP_ADMIN, OWNERSHIP_AMBIGUOUS, OWNERSHIP_UNOWNED}
        if owner_kind not in allowed:
            raise ConversationBindingStoreError("Ownership namespace is invalid.")
        if mcp_claimable and (owner_kind != OWNERSHIP_MCP or not repo_scope_digest):
            raise ConversationBindingStoreError("MCP ownership must have an exact repository scope.")
        now = time.time()
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO conversation_ownership(
                    workspace_id, conversation_id, owner_kind, owner_scope_digest,
                    repo_scope_digest, mcp_claimable, source, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace_id, conversation_id) DO UPDATE SET
                    owner_kind=excluded.owner_kind,
                    owner_scope_digest=excluded.owner_scope_digest,
                    repo_scope_digest=excluded.repo_scope_digest,
                    mcp_claimable=excluded.mcp_claimable,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (
                    workspace_id, conversation_id, owner_kind, owner_scope_digest,
                    repo_scope_digest, int(mcp_claimable), source, now, now,
                ),
            )

    def ownership(self, workspace_id: str, conversation_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT workspace_id, conversation_id, owner_kind, owner_scope_digest,
                       repo_scope_digest, mcp_claimable, source, created_at, updated_at
                FROM conversation_ownership
                WHERE workspace_id=? AND conversation_id=?
                """,
                (workspace_id, conversation_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["owner_kind"] = str(row["owner_kind"])
        result["repo_scope_digest"] = str(row["repo_scope_digest"])
        result["mcp_claimable"] = bool(row["mcp_claimable"])
        return result

    def owned_conversation_ids(
        self,
        *,
        workspace_id: str,
        owner_scope_digest: str,
        repo_scope_digest: str,
    ) -> list[str]:
        """Return exact MCP-authorizable Conversation IDs before pagination."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT conversation_id FROM conversation_ownership
                WHERE workspace_id=? AND owner_scope_digest=? AND repo_scope_digest=?
                  AND owner_kind=? AND mcp_claimable=1
                ORDER BY conversation_id
                """,
                (
                    workspace_id,
                    owner_scope_digest,
                    repo_scope_digest,
                    OWNERSHIP_MCP,
                ),
            ).fetchall()
        return [str(row["conversation_id"]) for row in rows]

    def backfill_ownership(self, records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
        """Backfill only unambiguous historical Agent Session ownership."""

        grouped: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for raw in records:
            workspace_id = raw.get("workspace_id")
            conversation_id = raw.get("conversation_id")
            owner = raw.get("owner_principal_id")
            if not all(isinstance(value, str) and value for value in (workspace_id, conversation_id, owner)):
                continue
            grouped.setdefault((workspace_id, conversation_id), set()).add((owner, conversation_owner_digest(owner)))
        migrated = 0
        ambiguous = 0
        unowned = 0
        now = time.time()
        with self._write_lock, closing(self._connect()) as connection, connection:
            for (workspace_id, conversation_id), owners in grouped.items():
                existing = connection.execute(
                    "SELECT owner_scope_digest FROM conversation_ownership "
                    "WHERE workspace_id=? AND conversation_id=?",
                    (workspace_id, conversation_id),
                ).fetchone()
                if existing is not None:
                    continue
                is_unambiguous = len(owners) == 1
                owner_digest = (
                    next(iter(owners))[1]
                    if is_unambiguous
                    else AMBIGUOUS_OWNER_DIGEST
                )
                owner_kind = OWNERSHIP_AGENT if is_unambiguous else OWNERSHIP_AMBIGUOUS
                connection.execute(
                    """
                    INSERT INTO conversation_ownership(
                        workspace_id, conversation_id, owner_kind, owner_scope_digest,
                        repo_scope_digest, mcp_claimable, source, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(workspace_id, conversation_id) DO NOTHING
                    """,
                    (
                        workspace_id,
                        conversation_id,
                        owner_kind,
                        owner_digest,
                        "",
                        0,
                        "legacy-agent-sessions",
                        now,
                        now,
                    ),
                )
                if is_unambiguous:
                    migrated += 1
                else:
                    ambiguous += 1
        return {"migrated": migrated, "ambiguous": ambiguous, "unowned": unowned}

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
        self.recorder = (
            ConversationEvidenceRecorder(transcripts)
            if hasattr(transcripts, "record_context") and hasattr(transcripts, "prune_context")
            else None
        )

    def start_or_resume_exact(
        self,
        *,
        runtime: Any,
        title: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(runtime)
        identity = ClientWindowIdentity.from_mcp_session(str(runtime.http_session_id))
        binding = self.bindings.resolve(**scope, identity=identity)
        resumed = binding is not None
        if binding is None:
            conversation_id = f"conversation-{uuid.uuid4().hex}"
            self.transcripts.record_messages(
                scope["workspace_id"],
                conversation_id,
                [],
                title=title or "MCP Conversation",
                source="mcp",
            )
            binding = self.bindings.bind(conversation_id=conversation_id, **scope, identity=identity)
            self.bindings.set_ownership(
                workspace_id=scope["workspace_id"],
                conversation_id=conversation_id,
                owner_scope_digest=scope["principal_scope"],
                owner_kind=OWNERSHIP_MCP,
                repo_scope_digest=scope["repo_scope"],
                mcp_claimable=True,
                source="mcp",
            )
        else:
            conversation_id = binding.conversation_id
            self._require_ownership(
                scope["workspace_id"], conversation_id, scope["principal_scope"],
                scope["repo_scope"],
            )
        runtime.current_conversation_id = conversation_id
        if instruction:
            self.record_instruction(runtime, instruction)
        return {
            "conversation_id": conversation_id,
            "resumed": resumed,
            "transport": {"kind": binding.transport_kind, "window_digest": identity.window_digest},
        }

    def record_instruction(self, runtime: Any, instruction: str) -> None:
        scope = self._scope(runtime)
        conversation_id = getattr(runtime, "current_conversation_id", None)
        if not conversation_id:
            return
        if self.recorder is not None:
            self.recorder.record(
                scope["workspace_id"],
                str(conversation_id),
                "task_instruction",
                instruction,
                source="mcp-conversation",
            )

    def list_for_runtime(self, runtime: Any, *, limit: int = 20) -> dict[str, Any]:
        scope = self._scope(runtime)
        authorized_ids = self.bindings.owned_conversation_ids(
            workspace_id=scope["workspace_id"],
            owner_scope_digest=scope["principal_scope"],
            repo_scope_digest=scope["repo_scope"],
        )
        payload = self.transcripts.list_conversations(
            scope["workspace_id"],
            page=1,
            page_size=max(1, min(int(limit), 100)),
            conversation_ids=authorized_ids,
        )
        items = payload.get("items", [])
        total = int(payload.get("total") or 0)
        return {
            "items": items,
            "total": total,
            "page": payload.get("page", 1),
            "page_size": payload.get("page_size"),
            "truncated": total > len(items),
        }

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
        self._require_ownership(
            scope["workspace_id"], conversation_id, scope["principal_scope"],
            scope["repo_scope"],
        )
        identity = ClientWindowIdentity.from_mcp_session(str(runtime.http_session_id))
        binding = self.bindings.bind(conversation_id=conversation_id, **scope, identity=identity)
        runtime.current_conversation_id = conversation_id
        return {
            "conversation_id": binding.conversation_id,
            "resumed": True,
            "transport": {"kind": binding.transport_kind, "window_digest": identity.window_digest},
        }

    def recover_retained(self, runtime: Any) -> dict[str, Any] | None:
        """Recover only an exact durable binding for a retained transport header."""

        scope = self._scope(runtime)
        identity = ClientWindowIdentity.from_mcp_session(str(runtime.http_session_id))
        binding = self.bindings.find_exact(**scope, identity=identity)
        if binding is None or not self._is_owned(
            scope["workspace_id"], binding.conversation_id, scope["principal_scope"],
            scope["repo_scope"],
        ):
            return None
        runtime.current_conversation_id = binding.conversation_id
        return {
            "conversation_id": binding.conversation_id,
            "resumed": True,
            "transport": {"kind": binding.transport_kind, "window_digest": identity.window_digest},
        }

    def _is_owned(
        self,
        workspace_id: str,
        conversation_id: str,
        owner_scope: str,
        repo_scope: str | None = None,
    ) -> bool:
        record = self.bindings.ownership(workspace_id, conversation_id)
        return (
            record is not None
            and str(record.get("owner_kind")) == OWNERSHIP_MCP
            and bool(record.get("mcp_claimable"))
            and str(record.get("owner_scope_digest")) == owner_scope
            and (repo_scope is None or str(record.get("repo_scope_digest")) == repo_scope)
        )

    def _require_ownership(
        self,
        workspace_id: str,
        conversation_id: str,
        owner_scope: str,
        repo_scope: str | None = None,
    ) -> None:
        if not self._is_owned(workspace_id, conversation_id, owner_scope, repo_scope):
            raise ConversationBindingStoreError("Conversation is unavailable.")

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
    "AMBIGUOUS_OWNER_DIGEST",
    "OWNERSHIP_DIGEST_DOMAIN",
    "conversation_owner_digest",
    "principal_scope_digest",
    "repo_scope_digest",
]
