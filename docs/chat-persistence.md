# Chat and Codex Session Persistence

Phase 09 stores chat messages, durable context, and imported Codex session metadata in `transcripts.sqlite3` under the stable server configuration directory.

Every primary key and query is partitioned by `workspace_id`. Normal callers receive a `WorkspaceTranscriptService` fixed to one validated Workspace root. Admin aggregation and destructive operations remain behind the dedicated Admin authentication boundary.

Session discovery scans only caller-selected relative roots below a registered Workspace. It does not follow symlinks and enforces configurable depth, file count, per-file bytes, total bytes, and message count ceilings. UTF-8, UTF-16 BOM, GB18030, CRLF, locked files, malformed lines, and partial final JSONL records have focused tests.

List endpoints are summary-only and paginated. Full message/context content is returned only by explicit paginated detail requests. Stable-ID message, context, conversation, and imported-session deletion requires the dedicated Admin credential, is idempotent, and reports actual affected counts. Stored chat content, paths, commands, model output, and summaries are not sent to telemetry.
