<a id="migrating-to-coding-tools-mcp-050"></a>

# Migration guide for 0.5 moved

The maintained release tutorial is published in the [0.5 migration guide](https://coding-tools-mcp.github.io/docs/migrations/0.5/).
Its source is maintained at:

https://github.com/coding-tools-mcp/docs/blob/main/content/docs/migrations/0.5.mdx

The public guide describes the 0.5 release. The source-coupled references below
describe this checkout, including subsequent reliability fixes, and take
precedence when that tutorial differs. Existing section links remain usable.

## Breaking changes

See the release tutorial for the original migration steps and the current
contracts linked below for the behavior of this checkout.

### `request_permissions` is advertised only in `dangerous` mode

See the [request_permissions contract](runtime-contract-v0.3.md#request_permissions).

### `read_file` model text now starts with a banner

The current [read_file contract](runtime-contract-v0.3.md#read_file) also covers
opt-in `line_numbers: true` and the revision accompanying those line numbers.

### `exec_command` default process lifetime is 300s

See [command/output behavior](tools-and-schemas.md#command-and-output-behavior)
for the separate process-lifetime and yield budgets.

## `apply_patch` behavior changes since 0.3

Use the authoritative [patch behavior reference](tools-and-schemas.md#patch-behavior)
for overwrite, forward-anchor, EOF, ambiguity, whitespace-matching, and
already-applied rules, including intentional differences from Codex.

## New behavior you may want to adopt

### `apply_changes`

The current [apply_changes contract](runtime-contract-v0.3.md#apply_changes)
covers complete revisions paired with the same file version's line numbers,
re-reading after `REVISION_MISMATCH`, byte-identical create retries, and
preserving untouched mixed or bare-CR line endings.
It also defines the compatible `line` / `start_line` / `end_line` aliases.

### `apply_patch` recovery

Follow the current [locating rules](tools-and-schemas.md#locating-a-hunk) and
[already-applied/idempotency rules](tools-and-schemas.md#primary-paths-overwrites-and-idempotency).
Blank or punctuation-only text is not evidence of an already-applied hunk;
spelling an argument's schema default explicitly does not change a replay key.

### `git_diff` includes untracked files

See the [git_diff contract](runtime-contract-v0.3.md#git_diff).

## New startup arguments

### `--workspace-mutation` and `--write-path`

See the authoritative [workspace mutation policy](permission-modes.md#workspace-mutation-policy).
Its write-path environment variable uses the platform's `os.pathsep`.

## Behavior changes that need no action

Repeated failures now produce advice, not a hard block: calls still execute
and return their actual result, with `repeat_warning` when applicable.
The runtime no longer produces `REPEATED_CALL_BLOCKED`.
See [repeated-failure advice](runtime-contract-v0.3.md#repeated-failure-advice)
and [telemetry](telemetry.md) for current counters and historical comparisons.

For per-tool result schemas, use the [runtime result contract](runtime-contract-v0.3.md#result-contract).
For enforcement limits, use the [security boundary](security-boundary.md).
For optional restart-persistent diagnostics, use the [local tool event journal](local-tool-events.md).
