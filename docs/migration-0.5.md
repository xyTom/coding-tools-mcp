# Migrating to coding-tools-mcp 0.5.0

0.5.0 is a reliability release. Nothing about the transport or the handshake
changes, and no tool is renamed or removed from the runtime — but the default
advertised catalog still contains 18 tools. The registry gains
`apply_changes`, `request_permissions` becomes mode-gated rather than being
removed, and three existing tools return more than they used to. The contract
itself is [runtime-contract-v0.3.md](runtime-contract-v0.3.md); this page lists
only what a client or an operator has to notice.

The engineering rationale behind each item is in the
[v0.5.0 execution plan](plan-v0.5.md).

## Breaking changes

### `request_permissions` is advertised only in `dangerous` mode

`request_permissions` no longer appears in `tools/list` in `safe` or `trusted`
mode, because it cannot do anything there: it answers
`ELICITATION_UNSUPPORTED` in every case, which is where roughly 60% of its
observed calls went. The registry now holds 19 tools; `safe` and `trusted`
advertise 18 of them and `dangerous` advertises all 19.

The advertised count in `safe` mode is still 18, but it is a different 18:
`request_permissions` left and `apply_changes` arrived.

- A client that hardcodes the catalog, or asserts a fixed tool count, must
  read `tools/list` instead.
- A client that calls `request_permissions` directly still gets the same
  `ELICITATION_UNSUPPORTED` answer. The tool remains callable when hidden, so
  no existing call becomes an `Unknown tool` error.
- To keep it advertised, run with `--permission-mode dangerous`. Do that only
  inside an isolated container or VM.

The `write_generated_or_ignored` permission kind is gone from the
`request_permissions` schema. It was only ever an enum value; no code path
requested it and none granted it.

### `read_file` model text now starts with a banner

Every `read_file` result — not just a truncated one — now begins with a line
like:

```
[Showing lines 1-40 of 40 revision=9f2c…]
```

The revision is the token `apply_changes` requires, and most clients forward
only this text to the model, so a revision that lived solely in
`structuredContent` would be unreachable by the caller that needs it. The
truncation wording and the continuation hint are unchanged.

A client that compared `read_file` text to file bytes must strip the first line
or read `structuredContent.content` instead, which is unchanged.

An optional `line_numbers: true` argument additionally prefixes each text line
with `<n>\t`, the numbering `apply_changes` uses; the default text is as above.

### `exec_command` default process lifetime is 300s

`timeout_ms` defaults to 300000 instead of 30000. It always meant total process
lifetime, but the old default was shorter than an install or a build, so a
command backgrounded by the yield was killed shortly after the call returned.
The initial yield (`yield_time_ms`) is unchanged at 10000, and the schema
maximum is unchanged at 600000.

Set `timeout_ms` explicitly if you relied on the old default to bound runaway
commands.

## `apply_patch` behavior changes since 0.3

These changes shipped in 0.5.0 without being listed as breaking. When
migrating clients or prompts from 0.3:

- Check destination paths before additions or moves if overwriting would be
  a mistake; 0.3 refused those overwrites.
- Include context when an insertion belongs somewhere other than EOF;
  pure additions no longer go at the top of the file.
- Combine all hunks for a file into one update block and order them from top
  to bottom. Repeated primary paths are rejected rather than chained.
- Replace ignored or abbreviated `@@` labels with real whole-line anchors.
  Recheck patches that relied on unrestricted searches or ignored EOF markers.
- Inspect matching warnings, and use `idempotency_key` for safe retries after
  a lost response. A successful result no longer guarantees a write happened;
  check `already_applied`.

See the authoritative [patch behavior reference](tools-and-schemas.md#patch-behavior)
for locating, overwrite, whitespace-matching, and already-applied rules,
including the intentional differences from Codex.

## New behavior you may want to adopt

### `apply_changes`

A new tool for line-addressed editing: you name an action (`create`, `write`,
`edit`, `delete`, `move`, `copy`) and a path. Existing files also need a
`revision`: the one `read_file` reported, or the one the latest
`apply_changes`/`apply_patch` result printed for that path, with line numbers
from that same version. Nothing has to match textually, and a file that changed
since is refused with `REVISION_MISMATCH` rather than silently overwritten; the
error does not repeat the new revision, so re-read the file to get it with its
current line numbers. A malformed revision (a placeholder or abbreviated hash)
is `INVALID_ARGUMENT`.

`write` remains an upsert: it requires `revision` when its path exists and may
omit it when creating a missing path. `create` rejects `revision` and asserts
absence; `edit`, `delete`, `move`, and `copy` require it. A path may appear once
per call; put several line edits for one file in that file's single `edit`
change. Replacement content may use LF, CRLF, or CR separators; untouched
lines keep their own line endings byte-for-byte, including in mixed or bare-CR
files. A `create` that repeats a file's exact current content is an
`already_applied` no-op.
See the contract for the full semantics, including the line-content rules
(`""` is zero lines; a trailing newline adds a blank line) and the
`insert_after` / `insert_before` boundaries.

### `apply_patch` recovery

- `@@ <context>` is a text anchor, not a language scope. As in Codex, it
  moves a forward search cursor and the hunk takes the first match after it;
  consecutive `@@` lines are found in turn. A missing anchor is
  `PATCH_CONTEXT_NOT_FOUND`. An unanchored hunk that matches more than once
  after the cursor is `PATCH_CONTEXT_AMBIGUOUS`; add an `@@` line to pick the
  copy. The full rules are in
  [tools-and-schemas.md](tools-and-schemas.md#locating-a-hunk).
- A pure-addition update hunk validates any `@@ <context>` first and then
  appends at EOF, matching Codex's current placement semantics. Anchorless
  pure additions also append at EOF.
- `*** End of File` participates in locating non-empty old/context blocks
  instead of being ignored: the placement must reach the end of the file.
- Matching is graded: exact, then ignoring trailing whitespace, then ignoring
  indentation width. The grade actually used is reported in `match_quality`,
  so a downgrade is visible rather than silent.
- Success returns `changed_ranges`, a per-file `revision`, and `total_lines`.
  These are evidence; `apply_patch` still takes no `revision` argument, because
  its context lines are already its optimistic check.
- Failure returns the hunk index, nearby numbered text, and candidate match
  positions, so the next attempt can be aimed rather than guessed.
- A patch whose changes are already present reports `already_applied` instead
  of failing, provided the hunk's post-image is located uniquely, by the same
  rules as a normal placement, with non-trivial evidence (blank and
  punctuation-only lines never count). A context-free single line found
  somewhere in the file is a coincidence and still fails, and so does a patch
  in which any hunk is neither applicable nor provably applied. The exact
  evidence rules are in
  [tools-and-schemas.md](tools-and-schemas.md#primary-paths-overwrites-and-idempotency).
- `apply_patch` and `apply_changes` accept an optional `idempotency_key`. A
  replay of the same key with the same arguments returns the recorded result
  instead of doing the work twice; reusing the key for different arguments is
  `IDEMPOTENCY_KEY_REUSED`, and a `dry_run` result is never recorded. An
  argument spelled out at its schema default (`"dry_run": false`) is the same
  request as one that omits it.
  Concurrent duplicates under the same tool and key wait for the first call
  and replay its successful result.
- `apply_patch` now matches Codex path semantics: an operation's resolved
  primary path may appear only once per envelope; `Add File` may replace an
  existing file; `Move to` may replace an existing destination; and distinct
  source files may move to one destination in order, with the later write
  winning. Move destinations do not participate in the primary-path duplicate
  check. Moves that change paths also report an explicit source `delete` in
  `affected_files`, so machine-readable evidence describes the final workspace
  rather than only the destination bytes.

### `git_diff` includes untracked files

`git_diff` now reports untracked files by default so a file created by
`apply_patch` is visible in the diff. Pass `include_untracked: false` for the
old behavior.

## New startup arguments

### `--workspace-mutation` and `--write-path`

`--workspace-mutation=structured-only` makes the workspace read-only for
`exec_command` under Landlock, leaving `apply_patch` and `apply_changes` as the
only way to change files. It is **off by default** (`unrestricted`) and
experimental: it breaks any command that writes into the tree — pytest caches,
npm, cargo, gradle, git — unless every such directory is listed with a
repeatable `--write-path`.

Both are also settable as `CODING_TOOLS_MCP_WORKSPACE_MUTATION` and an
`os.pathsep`-separated `CODING_TOOLS_MCP_WRITE_PATHS`. The effective policy,
including whether it is actually enforced, is reported in `server_info` as
`workspace_mutation_policy`. See
[permission-modes.md](permission-modes.md).
Full enforcement requires Landlock ABI 3 or newer because ABIs 1–2 cannot deny
truncate. Reporting the policy does not create missing in-workspace
`--write-path` directories; they are created immediately before Landlock is
installed for `exec_command`.

## Behavior changes that need no action

- **Repeated failures now produce advice, not a hard block.** The third and
  later identical calls run normally and return their actual result. Clients
  should use the original error and the visible `repeat_warning`, rather than
  expecting `REPEATED_CALL_BLOCKED`. Task-level loop budgets belong in the
  agent host. See [repeated-failure advice](runtime-contract-v0.3.md#repeated-failure-advice)
  for diagnostic counters, expiration, and compatibility details.
- **Telemetry counts operations truthfully.** A command that exits nonzero,
  times out, or dies on a signal is no longer recorded as a successful tool
  call, and its terminal outcome is counted once however many times the command
  is polled afterwards. Consecutive failures are tracked per (tool, error code)
  rather than in one global slot any tool's success could reset. Anyone
  comparing a 0.5.0 dashboard to an earlier one is comparing different
  definitions.
- **Per-tool output schemas.** `tools/list` now carries a specific
  `outputSchema` per tool instead of one generic envelope.
- **`check_exec_environment` warns on non-Linux.** Landlock is Linux-only, so
  there is no filesystem confinement elsewhere; that is now stated rather than
  left to be inferred.
- **`patch_lock` is documented as in-process.** It serializes patches within
  one server process. Two servers on one workspace are protected only by the
  pre-commit baseline recheck.
