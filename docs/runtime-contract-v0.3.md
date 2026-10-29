# Coding Tools MCP Runtime Contract v0.3

Status: implemented contract for `coding-tools-mcp` 0.3.x. The frozen contract
for 0.2.x is [runtime-contract-v0.2.md](runtime-contract-v0.2.md); what changed
between them, and what a client has to do about it, is
[migration-0.3.md](migration-0.3.md).

Protocol targets: MCP `2026-07-28`, which serves every request on its own, and
the handshake era `2025-11-25` with explicit compatibility for `2025-06-18`.

This contract describes one stable, model-neutral coding tool set. There are no
tool profiles and the server does not add or remove process tools dynamically.
`apply_patch` and `apply_changes` are the direct file-mutation primitives;
`edit_file` is not provided. Permission modes alter command policy and gate
`request_permissions` in the advertised catalog: only `dangerous` mode
advertises it, though its handler remains callable by name in every mode.

One switch, `--dangerously-fake-readonly-annotations`, rewrites the exposure hints
in `tools/list` for clients that refuse mutating tools by annotation. It is not a
tool profile: within the selected permission mode, the catalog, the schemas, and
what every tool actually does are unchanged, and the switch hides no tool. It
requires `dangerous` permission mode, requires authentication over HTTP, and is
reported by `server_info.annotation_override` and the server card, both of which
continue to publish the real annotations recorded below. Unless that switch is
set, the annotations in this document are what `tools/list` returns.

## Two protocol eras, one server

Both eras are served by the one runtime that owns the workspace, and neither
leaves state behind. Which era a request belongs to is decided by the request
alone: a `params._meta` carrying `io.modelcontextprotocol/protocolVersion` is a
`2026-07-28` request, and everything else is a handshake-era one. A legacy
`_meta` such as `progressToken` does not make a request modern, and `initialize`
is always the handshake, whatever `_meta` it carries.

The only advertised server capability is stable tools with `listChanged: false`,
in both eras. Logging, resources, prompts, sampling, and elicitation are not
advertised.

### The handshake era

- `initialize` negotiates a version and answers with it. A version this server
  does not speak — including `2026-07-28`, which is not negotiated at all — is
  answered with an `InitializeResult` naming the newest version that it does
  (`2025-11-25`), as the handshake spec requires.
- `initialize` is idempotent and is not an admission gate. Each one negotiates
  on its own, and every other implemented method is served whether or not one
  was sent first. `notifications/initialized` is accepted and answered with
  nothing.
- No session is created. HTTP responses carry no `Mcp-Session-Id`, and a header
  returned by a client that spoke to an older server is ignored rather than
  refused.
- The results of this era are exactly what they were: no field of the modern
  era is added to them, and every error is HTTP `200` with the JSON-RPC error.

### The `2026-07-28` era

A request states its own protocol version, so it needs no handshake and may
call `server/discover`, `ping`, `tools/list`, and `tools/call` immediately.
`notifications/cancelled` is accepted here as well. Its `params._meta` carries:

| `_meta` key | Required | Value |
| --- | --- | --- |
| `io.modelcontextprotocol/protocolVersion` | yes | `"2026-07-28"` |
| `io.modelcontextprotocol/clientCapabilities` | yes | object, may be empty |
| `io.modelcontextprotocol/clientInfo` | no | object naming the client |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {}
    }
  }
}
```

Over Streamable HTTP such a request also mirrors its body in headers, as
SEP-2243 requires, so a gateway can route it without reading the body:

- `MCP-Protocol-Version` repeats the `_meta` protocol version.
- `Mcp-Method` repeats the JSON-RPC method, on notifications too.
- `Mcp-Name` repeats the subject of the methods that name one: `params.name`
  for `tools/call` and `prompts/get`, `params.uri` for `resources/read`. A
  value that cannot travel as an HTTP field is wrapped as
  `=?base64?<base64 of the UTF-8 value>?=`, whose payload may not exceed 8192
  characters. No other method takes this header, `server/discover` included.

Each of the three headers may appear exactly once. A gateway routes on them
alone, and which of two values it would read is its own business, so a request
that states its version, method, or subject twice is refused with `-32020`
rather than resolved.

Handshake-era requests are asked for none of these headers. A
`MCP-Protocol-Version: 2026-07-28` header over a body that carries no modern
`_meta` is a mirror violation like any other.

### `server/discover`

The probe a `2026-07-28` client sends in place of a handshake. It reports the
versions this server speaks per request, its capabilities, and the workspace
instructions:

```json
{
  "supportedVersions": ["2026-07-28"],
  "capabilities": {"tools": {"listChanged": false}},
  "instructions": "...",
  "resultType": "complete",
  "ttlMs": 0,
  "cacheScope": "private",
  "_meta": {
    "io.modelcontextprotocol/serverInfo": {
      "name": "coding-tools-mcp",
      "title": "Coding Tools MCP",
      "version": "0.3.0"
    }
  }
}
```

`supportedVersions` lists the modern versions only. The handshake versions are
negotiated by `initialize` and are not accepted in `_meta`, so naming one here
would invite a client to retry a version that cannot work.

A `server/discover` that carries no modern `_meta` is a handshake-era request
for a method this server does not implement in that era, and is answered with
`-32601`. That is what sends a client which probes before it handshakes to
`initialize`, which works.

### Result encoding

A `2026-07-28` result carries `resultType: "complete"` and an
`_meta.io.modelcontextprotocol/serverInfo` naming this server. The results
whose content a client might be tempted to keep — `tools/list` and
`server/discover` — also carry `ttlMs: 0` and `cacheScope: "private"` on the
result root. Both are shaped by the workspace and the permission mode they were
served under, and the discover instructions quote the workspace's own
instruction files, so the conservative defaults are the correct ones: never
shared, never reused. A tool result that failed still reports
`resultType: "complete"` with `isError: true`; the envelope was complete, the
tool was not.

### Errors and HTTP statuses

| Code | Meaning | HTTP status of a `2026-07-28` request |
| --- | --- | --- |
| `-32600` | invalid request envelope | `200`, in either era |
| `-32601` | unknown method | `404` |
| `-32602` | invalid params, including a missing or mistyped required `_meta` field | `400` |
| `-32020` | headers do not mirror the body: missing, duplicated, or contradicting it | `400` |
| `-32022` | `_meta` names a protocol version this server does not speak; `data.supported` lists the modern versions only | `400` |
| `-32603` | unexpected server failure | `200` |
| `-32700` | parse error | `400` |

The one `-32600` that is not `200` is a transport refusal rather than a verdict
on a request that was dispatched: a **handshake-era** request whose
`MCP-Protocol-Version` header names a version from neither era is refused with
`400` before dispatch, and `data.supported` lists every version this server
speaks. A `2026-07-28` request is never refused that way, because its body
settles what the header could not: an unknown version stated by both is
`-32022`, and a header that contradicts the body is `-32020`.

That order holds generally. What the body says about itself is checked before
the headers that mirror it, so a request that is wrong in both ways is answered
with the protocol's verdict — an invalid `_meta` field is `-32602` and an
unsupported `_meta` version is `-32022` on either transport, never the `-32020`
the mirror would also have produced.

Every handshake-era error a dispatched request produced stays HTTP `200` with
the JSON-RPC error in the body, which is the only thing a client of that era
reads. What the transport rejects before dispatch, in either era, carries its
own status: `415` for a content type that is not JSON, `411` for a missing
`Content-Length`, `413` for a body over the maximum size, and `400` for a parse
error, a JSON-RPC batch, or the unknown version header above. `-32002 Server
not initialized` and `-32001 Unknown MCP session` are never returned by any
era.

A notification — a message with no `id` — is answered with nothing whatever
goes wrong inside it: over HTTP with an empty `202`, over stdio with silence.
The mirror headers are the exception, because they are a transport contract
rather than a verdict on the message: a notification whose headers do not
mirror its body is still refused with `400` and `-32020`.

### Transports

- Streamable HTTP uses `POST /mcp`. There are no sessions, so `DELETE /mcp`
  returns `405` with `Allow: POST`. Because this server does not provide an SSE
  stream, `GET /mcp` and `HEAD /mcp` return `405` as well.
- A request without an `MCP-Protocol-Version` header is treated as
  `2025-11-25`, this server's newest handshake version. The older spec suggests
  assuming `2025-03-26`, a version this server does not speak. The header value
  travels with the request as context and is available to the runtime; nothing
  echoes it, records it, or acts on it, and no method behaves differently for
  it.
- JSON-RPC batches are rejected.
- `notifications/cancelled` is accepted in both eras and answered with nothing,
  but it does not terminate the command the cancelled request started. A
  command outlives its request and is shared by every client of the workspace;
  terminate one with `kill_command`.
- stdio is newline-delimited JSON-RPC. stdout contains protocol messages only;
  diagnostics and logs go to stderr. An explicit operator setting can also
  retain [local tool events](troubleshooting.md#durable-local-tool-events) on
  disk; this does not advertise the MCP logging capability.
- The server card at `/.well-known/mcp.json` and
  `/.well-known/mcp/server-card.json` reports `supportedProtocolVersions`,
  every version this server speaks, newest first.

## Automatic project context

The server loads bounded root project instructions from `AGENTS.md`,
`AGENTS.MD`, `CLAUDE.md`, and `CLAUDE.MD` when present. The content is returned
in the `instructions` field of `initialize` and of `server/discover`, so an
agent of either era does not need an `open_workspace` call. Nested instruction
files are indexed by path but are not eagerly injected. Loading is UTF-8 safe
and bounded by file-count, scan-count, depth, per-file, and total-byte limits.

## Workspace and patch guarantees

- One server runtime owns one canonical workspace root and serves every client
  of it. Concurrent clients share the command pool, the retained output, and
  the patch baselines; this is a single trust domain by design.
- Direct path inputs are workspace-relative and always resolve against the
  workspace root. Absolute paths, `..` traversal, NUL bytes, and symlink
  escapes are rejected.
- `apply_patch` parses and validates every operation before committing, under a
  lock that spans every client of **one server process**, so two clients of that
  process patching one file cannot lose an update: the later one is answered
  with a conflict rather than silently overwriting.
- That lock is an in-process mutex, not a file lock. Two server processes
  pointed at one workspace do not exclude each other, and neither does an
  external editor or a command started through `exec_command`. Across
  processes the only protection is the pre-commit baseline recheck below: a
  concurrent writer is detected and reported as `PATCH_CONFLICT`, but the
  detection window is the interval between the recheck and `os.replace`, not
  zero. Run one server per workspace if you need mutual exclusion.
- Every replacement is prepared and fsynced in the target directory, then
  installed with `os.replace`.
- Existing mode bits, UTF-8 BOMs, and CRLF/LF style are preserved. Moves inherit
  the source mode.
- Baseline hashes and modes are checked before commit and again immediately
  before replacement. Conflicts are retryable and never silently overwrite a
  newly-created target.
- A failed multi-file commit restores all backups. Portable filesystems do not
  offer a true transaction across directories, so a rollback failure is
  reported explicitly as `PATCH_ROLLBACK_FAILED` with recovery details.

## Result contract

Every valid `tools/call` response contains:

```json
{
  "content": [{"type": "text", "text": "Short agent-readable result"}],
  "structuredContent": {"ok": true},
  "isError": false
}
```

`content` is concise model-facing text and is never a JSON serialization of the
whole payload. Its normal size is governed by each tool's own per-call limits
(`max_bytes`, `max_output_bytes`, `max_results`, ...), without the former
16 KiB renderer preview cap. A 2,162,688-byte emergency safety ceiling protects
clients from pathological individual entries that count-based limits cannot
bound. Command results always begin with a status line (status, exit code,
signal, timeout). Stable pageable truncation names an executable continuation
call (`read_output(output_ref=..., offset=...)`,
`read_file(path=..., start_line=...)`, ...); non-pageable results explicitly
say which limit or scope to change. `structuredContent` is the complete,
stable machine-readable interface. Large diffs and command output are not
copied into `_meta`; `_meta` is optional UI extension space only.

Tool failures keep the same envelope with `isError: true`, a readable error in
`content`, and this machine shape:

```json
{
  "ok": false,
  "error": {
    "code": "PATCH_CONTEXT_AMBIGUOUS",
    "message": "Patch context matched more than one location.",
    "category": "validation",
    "retryable": true,
    "details": {"path": "src/app.py", "hunk_index": 0, "match_count": 2}
  }
}
```

A client that forwards only `content` to the model must still be able to tell
a transient failure from a permanent one, so the error text restates the
category and retryability under the code and message, and a terminal failure
says so outright:

```text
COMMAND_NOT_FOUND: Command not found; stdin access denied.
Category: not_found. Retryable: no. Do not repeat this call unchanged.
Retry: This command_id has expired or never existed; …
```

Known tool error codes include:

```json
["ABSOLUTE_PATH_DENIED", "BINARY_FILE", "COMMAND_CLOSED", "COMMAND_LIMIT_REACHED", "COMMAND_NOT_FOUND", "COMMAND_SPAWN_FAILED", "ELICITATION_UNSUPPORTED", "EXTERNAL_GIT_STORAGE_DENIED", "GIT_ERROR", "HARDLINK_DENIED", "IDEMPOTENCY_KEY_REUSED", "INTERNAL_ERROR", "INVALID_ARGUMENT", "IS_DIRECTORY", "NOT_A_DIRECTORY", "NOT_FOUND", "OUTPUT_TOO_LARGE", "PATCH_CONFLICT", "PATCH_CONTEXT_AMBIGUOUS", "PATCH_CONTEXT_NOT_FOUND", "PATCH_FAILED", "PATCH_HUNKS_OVERLAP", "PATCH_ROLLBACK_FAILED", "PATH_DENIED", "PATH_OUTSIDE_WORKSPACE", "PERMISSION_REQUIRED", "REPEATED_CALL_BLOCKED", "REVISION_MISMATCH", "REVISION_REQUIRED", "RUNTIME_DIR_UNWRITABLE", "SANDBOX_HELPER_UNTRUSTED", "SANDBOX_INITIALIZATION_FAILED", "SANDBOX_NETWORK_UNSUPPORTED", "SANDBOX_POLICY_INVALID", "SANDBOX_UNAVAILABLE", "SHELL_CONFIGURATION_ERROR", "SHELL_NOT_FOUND", "SHELL_UNTRUSTED", "SHELL_VERSION_UNSUPPORTED", "SYMLINK_ESCAPE", "TTY_UNSUPPORTED", "UNSUPPORTED_ENCODING", "UNSUPPORTED_FILE_TYPE", "UNTRUSTED_EXECUTABLE", "WINDOWS_JOB_UNAVAILABLE", "WRITE_DENIED"]
```

Error categories are `validation`, `security`, `permission`, `runtime`,
`not_found`, `conflict`, and `internal`.

### Repeated-failure advice

Repeated failures never prevent a tool handler from running. Every request
still passes the ordinary schema, permission, path, revision, and resource
checks. The original handler result or error is returned, including on the
third and subsequent identical attempts. External state changes can therefore
recover immediately without changed arguments or a cooldown.

After two matching countable failures, `error.details` includes
`recent_identical_failures` and `repeat_warning`; the warning is also rendered
in the model-visible text. The count is a recent server observation for a
(tool, argument fingerprint, error code), not a client's consecutive streak.
One runtime can serve multiple clients; the advisory does not claim a unique
client or task identity. `idempotency_key` is excluded from the fingerprint.
There is no new force-retry parameter or permission bypass.

History is bounded to 256 fingerprints and expires after more than 60 seconds
without a counted failure. Success with those arguments clears that entry.
Existing workspace mutation and potentially writable command lifecycle resets
clear stale history; failures begun before a reset do not seed the new
history. Non-retryable errors count except `IDEMPOTENCY_KEY_REUSED` and
`INTERNAL_ERROR`. `PATCH_CONTEXT_NOT_FOUND`, `PATCH_CONTEXT_AMBIGUOUS`,
`REVISION_MISMATCH`, and `REVISION_REQUIRED` also contribute advice; other
retryable errors such as `PATCH_CONFLICT` and `COMMAND_LIMIT_REACHED` do not.
These classifications guide advice only, never execution permission.

The former `consecutive_identical_failures`/`breaker` details and pre-handler
`REPEATED_CALL_BLOCKED` response are no longer produced by Runtime. The legacy
Python helper name and its threshold-query method remain available for
compatibility; querying the threshold does not block a runtime call. The
legacy error code remains documented for historical results and telemetry.
Agent hosts should enforce their own turn/time/cost budgets. Advice does not
guarantee that a model stops looping, and every actual failed execution is
still counted as a tool failure.

Malformed JSON-RPC uses standard protocol errors: parse `-32700`, invalid
request `-32600`, unknown method `-32601`, invalid params/tool `-32602`, and
unexpected server failure `-32603`. The two codes the modern era adds are
`-32020` and `-32022`, described above.

## Command lifecycle

`exec_command`, `write_stdin`, `read_output`, and `kill_command` are always in
the catalog. `exec_command` and `write_stdin` default to a 10-second yield. A
short command normally finishes in one call. A running command returns:

```json
{
  "status": "running",
  "command_id": "...",
  "next_action": {
    "tool": "write_stdin",
    "arguments": {"command_id": "...", "chars": "", "yield_time_ms": 10000}
  }
}
```

Call `write_stdin` with empty `chars` to poll. Every `exec_command` and
`write_stdin` result carries `output_refs` (`command:<id>:stdout` and
`command:<id>:stderr`); `read_output` pages either stream and also reports the
command's `status` and `exit_code`, but it is needed only when output is
truncated or a caller explicitly requested compact retained output.
Its offsets are absolute and independent for stdout and stderr. A single
truncated stream is selected by `next_action`; when both streams are truncated,
`next_actions` contains one executable `read_output` call for each stream.
Command results carry `operation_outcome`: `exited_0`, `exited_nonzero`,
`timeout`, `signal`, `killed` (stopped by `kill_command`), `running`, or
`spawn_error`. A `Popen` failure returns
`COMMAND_SPAWN_FAILED` with the last outcome instead of advertising a state the
runtime can never produce.

A command belongs to the workspace, not to the client or the request that
started it. Any authenticated client of the same workspace can continue, read,
or terminate one with its `command_id`, and no transport event — a closed HTTP
response, a cancelled request, a reconnect — ends it. Active processes,
completed-output commands, per-command bytes, and total runtime bytes are
bounded, all of them per workspace rather than per client. A completed
command's TTL starts when a response first reports its terminal state; a
command that finished unobserved is kept until the retained-command count or
byte budget evicts it. POSIX `tty=true` uses a real pseudo-terminal; Windows
reports `TTY_UNSUPPORTED` in this build instead of pretending pipes are a TTY.
Without a TTY, stdin is closed after the optional `stdin` text so programs that
read it see EOF; `keep_stdin_open=true` keeps the pipe open for `write_stdin`
on every platform.

## HTTP authentication

Non-loopback deployment requires bearer or OAuth authentication unless the
operator explicitly selects no-auth. OAuth implements Authorization Code +
PKCE S256, protected-resource metadata, authorization-server metadata, exact
redirect URI matching, one-time five-minute codes, 24-hour access tokens, and
RFC 7591 dynamic client registration at `POST /oauth/register`. Public and
confidential clients are bound to their registered authentication method.

Authentication admits a client to the workspace; it does not partition it.
Every admitted client of one workspace shares that workspace's commands,
retained output, and patch state.

Dynamic registrations and authorization codes are process-local; restarting
the server requires clients to register again. Configure a stable
`CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET` and public server URL only when tokens must
survive tunnel churn. Forwarded headers are ignored unless
`CODING_TOOLS_MCP_TRUST_PROXY_HEADERS=1` is explicitly set.

## Stable tool inventory

The default catalog has 18 tools, including `view_image`. Setting
`CODING_TOOLS_MCP_ENABLE_VIEW_IMAGE=0` is the sole installation capability gate
and removes only that optional binary-content tool. It is not a tool profile.

Each definition below lists the live input property names and annotations. The
authoritative JSON Schemas are returned by `tools/list` and checked for drift in
CI. The annotations recorded here are the truthful ones and are what `server_info`
and the server card always report, including while
`--dangerously-fake-readonly-annotations` is rewriting the hints in `tools/list`.

### server_info

Inputs: none.

Annotations: `{"title":"Server info","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns server version, `supported_protocol_versions`, workspace, fixed tool
count, auth state, permission mode, runtime directories, project-context
metadata, exec policy, and the static retained-output budget. It reports no
per-session value and no runtime counter: there is no session, and how often a
budget was hit is a property of the process rather than an answer to whichever
client asked. Those counters travel with telemetry.

`workspace_mutation_policy` reports who may write to the workspace: `mode` is
`unrestricted` or `structured-only`, `write_paths` lists the directories that
stay writable for commands under `structured-only`, `structured_write_tools`
names the tools that write regardless, and `enforced` is `false` whenever
`structured-only` lacks enabled Landlock ABI 3 or newer. ABIs 1–2 cannot deny
truncate and therefore cannot provide the promised read-only workspace.
Reporting validates configured in-workspace write directories without creating
them. Missing directories are created only immediately before an
`exec_command` installs its Landlock ruleset, so Landlock never silently skips
a nonexistent allowlist root.
`output_retention` reports the per-stream buffer budget alongside
`completed_command_ttl_seconds` and `max_retained_completed_commands`, which
together decide when a finished `command_id` stops answering.

### check_exec_environment

Inputs: none.

Annotations: `{"title":"Check exec environment","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns lightweight policy and Landlock status without running active probes,
including the same `workspace_mutation_policy` object `server_info` reports.
Warnings cover an unavailable Landlock, a non-Linux host (where nothing
confines a command's filesystem access), a `structured-only` policy that cannot
be enforced, `permission_mode=dangerous`, and faked read-only annotations.

### read_file

Inputs: `"path"`, `"start_line"`, `"end_line"`, `"max_lines"`, `"max_bytes"`, `"encoding"`, `"line_numbers"`.

Annotations: `{"title":"Read file","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Reads UTF-8 ranges as a stream, reports full file line/byte metadata, rejects
binary content, and returns continuation metadata when bounded. The
continuation repeats the workspace-relative path it was given.

Every response carries `revision` and `revision_algorithm` (`"sha256"`).
`revision` is the SHA-256 of the whole file's UTF-8 bytes, computed inside the
same streaming pass that produced `content`, so it names the bytes this call
decoded rather than a second, later read. It covers the entire file even when
the response is a line range or is truncated: it identifies the file version,
not the excerpt. It is also the value `apply_changes` requires, and it equals
the `revision` `apply_patch` reports for the same bytes. The model-facing text
opens with a `[Showing lines a-b of n revision=<hash>]` banner so a client that
forwards only text can still supply it.

`"line_numbers"` (default `false`) prefixes each line of the model-facing text
with its 1-based number and a tab (`12\tsource line`), which is the numbering
`apply_changes` edits use. It changes only the text: `structuredContent.content`
stays the file's bytes, the payload gains `"line_numbers": true`, and a
truncation continuation carries the flag forward. The default text is unchanged.

### list_dir

Inputs: `"path"`, `"recursive"`, `"max_depth"`, `"max_entries"`, `"include_hidden"`, `"include_ignored"`, `"sort"`.

Annotations: `{"title":"List directory","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### list_files

Inputs: `"path"`, `"patterns"`, `"glob"`, `"exclude_patterns"`, `"include_hidden"`, `"include_ignored"`, `"max_results"`, `"sort"`.

Annotations: `{"title":"List files","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Traversal is iterative and git-ignore checks are batched.

### search_text

Inputs: `"query"`, `"path"`, `"regex"`, `"case_sensitive"`, `"include_globs"`, `"glob"`, `"exclude_globs"`, `"context_lines"`, `"max_results"`, `"max_preview_bytes"`.

Annotations: `{"title":"Search text","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Ripgrep output is consumed incrementally and the process stops once the result
cap is known to be exceeded. `context_lines=0` does not reread matching files.

### apply_patch

Inputs: `"patch"`, `"dry_run"`, `"idempotency_key"`.

Annotations: `{"title":"Apply patch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Supports `*** Add File`, `*** Update File`, `*** Delete File`, and
`*** Move to` inside a `*** Begin Patch` / `*** End Patch` envelope.

```text
*** Begin Patch
*** Update File: app.py
@@
-old
+new
*** End Patch
```

Hunk location, grading, idempotency, and the success/failure fields are
specified once in [tools-and-schemas.md](tools-and-schemas.md#apply_patch).
An explicit `@@ <context>` is a forward-only text anchor, as in Codex. It must
be found at or after the current hunk cursor, and the hunk takes its first
match after the anchor; consecutive `@@` lines are found in turn. An
unanchored hunk must match exactly once after the cursor. An anchor does not
create a language-aware function/class/block boundary.
Pure-addition hunks validate their anchor, if any, and append at EOF.
Each operation's resolved primary path may appear only once per envelope.
`Add File` may overwrite an existing file and `Move to` may overwrite an
existing destination. Move destinations are not primary paths, so distinct
sources may move to one destination in order and the later write wins. A move
that changes paths reports both the destination's final evidence and a source
`delete` record in `affected_files`.
`apply_patch` carries no `revision` argument — its context lines are its
optimistic check.

### apply_changes

Inputs: `"changes"`, `"dry_run"`, `"idempotency_key"`.

Annotations: `{"title":"Apply changes","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Line-addressed sibling of `apply_patch`, committed through the same staging,
baseline-recheck, and rollback machinery, and returning the same result shape.
Nothing has to match: edits name line numbers instead of context.

Each entry of `changes` carries an `action`, a `path`, and, for every action
except `create`, the `revision` of that path: the value `read_file` published,
or the `revision` the latest `apply_changes` or `apply_patch` result reported
for it (the result text prints `path: revision=<hash>`). Line numbers must come
from the same version of the file as the revision: after a write, either use
the new revision with the new numbering or re-read. A dry run's text labels its
revision `would_be_revision=`, because those bytes were never written.

| action | requires | notes |
| --- | --- | --- |
| `create` | `content` | the path must not exist, except that identical bytes are an `unchanged` no-op; `revision` is rejected |
| `write` | `content`, `revision` when the path exists | upsert of the whole file |
| `edit` | `edits`, `revision` | line operations, below |
| `delete` | `revision` | file only, never a directory |
| `move` | `destination`, `revision` | destination must not exist |
| `copy` | `destination`, `revision` | source is verified, not written |

`revision` is the SHA-256 of the file's UTF-8 bytes, identical to the value
`read_file` and `apply_patch` report. A `sha256:` prefix, upper-case hex, and
surrounding whitespace are normalized away; anything else that is not 64 hex
characters (a placeholder, an abbreviated hash) is a non-retryable
`INVALID_ARGUMENT`, not a mismatch. A missing revision is `REVISION_REQUIRED`,
which carries the current revision. A stale one is `REVISION_MISMATCH`, which
deliberately does **not**: pasted back with line numbers from the old version
it would pass the check and edit the wrong lines, so the error points at
`read_file`, which returns the revision together with the numbering that
belongs to it. Re-read the file before correcting either revision error. Repeating the
same request returns its current result, with advice after repeated failures. `apply_patch`
deliberately has no equivalent check.

A `create` whose path exists with byte-identical content (a retry after a lost
response) succeeds as `unchanged` and writes nothing; different content is
`PATCH_FAILED` and points at `write` with the file's revision. `create` or
`write` naming a directory is `PATCH_FAILED` saying so.

Each entry of `edits` is one of:

- `{"op": "replace", "start_line": n, "end_line": m, "content": "…"}`
- `{"op": "delete", "start_line": n, "end_line": m}`
- `{"op": "insert_after", "line": n, "content": "…"}` where `n` is in
  `[0, total_lines]` and `0` inserts at the beginning
- `{"op": "insert_before", "line": n, "content": "…"}` where `n` is in
  `[1, total_lines + 1]` and `total_lines + 1` appends

`end_line` is inclusive, must be at least `start_line`, and defaults to it.
For `replace` and `delete`, `line` without `start_line` selects one line;
`end_line` must be omitted or equal to `line`. When both `line` and
`start_line` are supplied, they must agree, and the explicit `start_line`
allows `end_line` to extend the range. For example, `line: 2, start_line: 2,
end_line: 3` selects lines 2–3, while `line: 2, end_line: 3` is
`INVALID_ARGUMENT`.

For `insert_after` and `insert_before`, `start_line` can stand in for `line`.
Any supplied `line`, `start_line`, and `end_line` must equal the insertion
anchor; insertions do not accept a range. Conflicting aliases are
`INVALID_ARGUMENT`. Every line number refers to the file as `read_file`
reported it, never to the result of an earlier edit
in the same call, so the caller does not track its own shifts. Two edits that
address the same lines — including two insertions at one point — are
`PATCH_HUNKS_OVERLAP`. A line number past the end is `INVALID_ARGUMENT` with
the file's `total_lines` in `details`.

`content` is whole lines. `""` is **zero** lines, which is what makes `replace`
with empty content a deletion; a trailing newline adds a blank line, so
`"a\n"` is the two lines `a` and the empty line after it. There are no
intra-line spans. LF, CRLF, and CR separators supplied in replacement content
are normalized, and line endings are then tracked per line: every untouched
line keeps its own terminator byte-for-byte (so mixed and bare-CR files stay as
they were), a replacement line takes the terminator of the line it replaces,
an inserted line that of the line it is inserted before (or of the last line
when appending), and the file's dominant terminator fills any gap. A file
without a final newline keeps that shape.

A path may appear once per call, as `path` or as `destination`; a duplicate is
`INVALID_ARGUMENT` whose message names both changes and the path. Paths are
compared after they resolve, so `a.txt` and `./a.txt` are one path and naming
both is the same error. Put multiple line edits for one file in that file's
single `edit` change. An empty `changes`
array fails exactly as an empty patch does, with `PATCH_FAILED` and "No files
were modified."

The binding size limit is the 1 MiB HTTP request cap, not a change count. At
most 100 changes and 200 edits per change are accepted as a guard against
pathological input; roughly 20 files per call is the practical advice. A change
whose result equals the file's current bytes is reported as `unchanged`, is
committed as a baseline assertion rather than a rewrite, and sets
`already_applied` when every change in the call is unchanged. `additions` and
`removals` count only lines that differ, so a no-op replace reports `+0 -0`.

### exec_command

Inputs: `"cmd"`, `"workdir"`, `"cwd"`, `"timeout_ms"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`, `"stdin"`, `"tty"`, `"keep_stdin_open"`, `"env"`.

Annotations: `{"title":"Execute command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Statuses are `exited`, `running`, `timeout`, `terminated`, or `failed`.
Launch/policy failures use the error envelope with `status: "failed"`; signal
exits use `terminated`. Ordinary non-zero exit codes still use `exited`.
`"workdir"` is workspace-relative and defaults to the workspace root.

`"yield_time_ms"` and `"timeout_ms"` are two separate budgets.
`"yield_time_ms"` (default `10000`, maximum `30000`) is how long the call
waits before returning; a command still running then keeps running under its
`command_id`. `"timeout_ms"` (default `300000`, maximum `600000`) is the total
process lifetime, after which the runtime kills the process group whether or
not the call has already returned. Before v0.5.0 the lifetime defaulted to
`30000`, so a command that outlived its first return was killed shortly after.

Example: `{"cmd":"pytest -q","workdir":".","yield_time_ms":30000}`.

### write_stdin

Inputs: `"command_id"`, `"chars"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Annotations: `{"title":"Write stdin","readOnlyHint":false,"destructiveHint":false,"idempotentHint":false,"openWorldHint":false}`.

Poll or interact with a command. Pass empty `chars` to wait for output.
Non-empty `chars` requires a command started with `keep_stdin_open=true` or
`tty=true`. Otherwise the call fails with `COMMAND_CLOSED` and
`details.reason` `stdin_closed` (still running) or `exited` (finished, with
its exit status).

Poll example: `{"command_id":"abc","chars":"","yield_time_ms":10000}`.
Input example: `{"command_id":"abc","chars":"yes\n"}`.

### kill_command

Inputs: `"command_id"`, `"signal"`, `"wait_ms"`, `"kill_wait_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Annotations: `{"title":"Kill command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Statuses are `["terminated", "killed", "exited", "terminating", "not_found"]`.

If the process is still alive `"wait_ms"` after a non-KILL signal, the runtime
escalates to a hard kill and waits up to `"kill_wait_ms"` for the exit. This is
how a client stops a command: `notifications/cancelled` does not. A command it
stops reports `operation_outcome` `killed`. On an already-finished command it
sends nothing: `status` is `exited`, `signal_sent` is `null`, and `exit_code`/
`signal` report the real exit. On Windows the whole process tree is killed.

Example: `{"command_id":"abc","signal":"KILL"}`.

### read_output

Inputs: `"output_ref"`, `"stream"`, `"offset"`, `"limit"`.

Annotations: `{"title":"Read output","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Retention is head+tail per stream: the earliest bytes (head) and the most
recent bytes (rolling tail) are kept; the range between them may be evicted
once the per-stream buffer overflows. Responses report `head_retained_bytes`,
`evicted_gap_bytes`, and `omitted_bytes`; reads inside the evicted range clamp
forward to the tail. Offsets remain absolute and stable.

`"output_ref"` is a full ref, or a bare command id or `command:<id>` whose
stream is `"stream"` (default `stdout`); a full ref with a contradicting
`"stream"` is `INVALID_ARGUMENT`. Results include `status` and `exit_code`
(`null` while running) so polling with `read_output` sees completion.

Example: `{"output_ref":"command:abc:stdout","offset":0,"limit":4096}`.

### git_status

Inputs: `"path"`, `"include_untracked"`, `"max_entries"`.

Annotations: `{"title":"Git status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_diff

Inputs: `"path"`, `"paths"`, `"staged"`, `"unstaged"`, `"include_untracked"`, `"context_lines"`, `"max_bytes"`.

Annotations: `{"title":"Git diff","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

`"include_untracked"` defaults to `true`. Each untracked path reported by
`git ls-files --others --exclude-standard` is diffed against an empty file and
appended to the unstaged pass under the same truncation budget, so a file
created by `*** Add File` is verifiable from `git_diff` alone. At most 100
untracked files are diffed per call; the overflow is reported in `warnings`.
Responses echo `include_untracked`. Setting `"unstaged": false` also disables
the untracked pass.

### git_log

Inputs: `"path"`, `"ref"`, `"max_count"`, `"skip"`.

Annotations: `{"title":"Git log","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_show

Inputs: `"rev"`, `"path"`, `"paths"`, `"include_diff"`, `"context_lines"`, `"max_bytes"`.

Annotations: `{"title":"Git show","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_blame

Inputs: `"path"`, `"rev"`, `"start_line"`, `"end_line"`, `"max_lines"`.

Annotations: `{"title":"Git blame","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### request_permissions

Inputs: `"tool_name"`, `"permission"`, `"reason"`, `"arguments"`, `"scope"`, `"ttl_seconds"`.

Annotations: `{"title":"Request permissions","readOnlyHint":true,"destructiveHint":false,"idempotentHint":false,"openWorldHint":false}`.

The current server does not advertise MCP elicitation. This tool therefore
returns `ELICITATION_UNSUPPORTED`, except that dangerous mode reports the
operator's explicit auto-grant policy. It never silently escalates safe mode.

Because that answer is fixed outside dangerous mode, `tools/list` advertises
`request_permissions` only when `permission_mode=dangerous`. The handler stays
reachable: a client that calls it by name still gets the same answer it always
got, in every mode. See [migration-0.5.md](migration-0.5.md).

### view_image

Inputs: `"path"`, `"max_bytes"`, `"max_width"`, `"max_height"`, `"auto_resize"`.

Annotations: `{"title":"View image","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

The base64 data appears exactly once, in one MCP image content block. Stable
`structuredContent` contains metadata only; it has no duplicate base64 or data
URL. Pillow is optional and used only for requested auto-resize.

## Forbidden product-layer tools

The runtime does not expose external-agent login/accounts, agent memory, cloud
tasks, web search/fetch, image generation, model routing, plugin installation,
subagent orchestration, or high-level prompt wrappers.

## Known limitation: cancellation responsiveness

A cancelled request is answered as this contract says it is, in both eras, but
the work it started is not stopped any sooner. On stdio the loop is serial, so
the response is already written by the time a cancellation could be read; over
HTTP the modern cancellation signal is a closed response stream, and this
server does not detect a disconnect. No client-observable rule is broken —
nothing is sent for a cancelled request that would not have been sent anyway —
but the SHOULD to stop working promptly is not met. The mitigations are the
30-second foreground window of `exec_command` and terminating a command with
`kill_command`. Tracked in issue
[#48](https://github.com/xyTom/coding-tools-mcp/issues/48).

## Compatibility note for 0.3

0.2 clients keep working: the handshake era is unchanged on the wire. What
changed for them is the tool catalog and the transport, not the envelope —
`get_default_cwd` and `set_default_cwd` are gone, relative paths resolve
against the workspace root, and HTTP no longer has sessions. Every removal, and
what to do instead, is in [migration-0.3.md](migration-0.3.md).

The additive execution-isolation configuration and native helper errors are described in [Cross-platform sandbox](cross-platform-sandbox.md). Unsupported strict capabilities fail before a command is accepted; compatibility permission modes retain their previous semantics.
