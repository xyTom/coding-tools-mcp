# Tools And Schemas

The normative behavior is [runtime-contract-v0.2.md](runtime-contract-v0.2.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

## Fixed inventory

The default catalog contains exactly 20 tools:

- `server_info`: server, workspace, automatic project context, policy, runtime,
  auth, protocol, and fixed-catalog metadata.
- `check_exec_environment`: lightweight execution policy and Landlock status.
- `get_default_cwd`: inspect this MCP transport session's relative-path base.
- `set_default_cwd`: change this MCP transport session's relative-path base;
  prefer explicit `path`/`workdir` when reconnects are possible.
- `read_file`: stream a bounded UTF-8 range without loading the whole file.
- `list_dir`: list immediate or bounded-recursive directory entries.
- `list_files`: iterate files with glob, ignore, hidden-file, sort, and cap
  controls.
- `search_text`: literal or regex search; ripgrep stops after the result cap.
- `apply_patch`: stage and atomically commit add/update/delete/move envelopes.
- `exec_command`: run a bounded command and wait up to 10 seconds by default.
- `write_stdin`: poll or interact with a running command.
- `kill_command`: terminate one runtime-owned command.
- `read_output`: page retained stdout or stderr using absolute byte offsets.
- `git_status`: structured working-tree status.
- `git_diff`: bounded unified staged/unstaged diff.
- `git_log`: structured bounded commit history.
- `git_show`: bounded revision metadata/content/diff.
- `git_blame`: structured bounded line attribution.
- `request_permissions`: report elicitation status without silently granting.
- `view_image`: one MCP image content block plus structured metadata.

`view_image` may be disabled when an installation cannot accept binary image
content. That capability gate is not a tool profile. The other 19 tools are
always advertised, and `listChanged` is `false`.

## Result envelope

Every successful tool call has:

```json
{
  "content": [{"type": "text", "text": "Agent-readable summary or bounded preview"}],
  "structuredContent": {"ok": true},
  "isError": false
}
```

`content` is not a JSON mirror. `structuredContent` is the complete machine
interface and retains existing fields where possible. Model-facing text is
bounded at 16 KiB; if it is shortened, the full structured value is still
present. Errors use the same envelope with readable recovery guidance and
`isError: true`.

`view_image` is the exception to text-only content: its base64 appears exactly
once in one `image` block. `structuredContent` contains path, media type, byte
count, dimensions, resize metadata, and warnings, but no base64 or data URL.

## Patch behavior

`apply_patch` accepts the standard envelope:

```text
*** Begin Patch
*** Add File: path/to/new.py
+content
*** Update File: path/to/existing.py
@@
 old
-before
+after
*** Move to: path/to/moved.py
*** Delete File: path/to/old.py
*** End Patch
```

All operations are parsed and matched before writes. Context must be unique.
Files are prepared in their destination directories, fsynced, baseline-checked,
and installed with atomic replacement. Multi-file failure restores prior files.
Mode bits, BOM, and newline style are preserved; moves inherit source mode.

## Model-ready examples

Use explicit paths for multi-call workflows:

```json
{"cmd":"pytest -q","workdir":".","yield_time_ms":30000}
```

If the result is still running, copy its `command_id` exactly:

```json
{"command_id":"abc","chars":"","yield_time_ms":10000}
```

Terminate that command when needed:

```json
{"command_id":"abc","signal":"KILL"}
```

Page a truncated stream using the returned reference:

```json
{"output_ref":"command:abc:stdout","offset":0,"limit":4096}
```

`set_default_cwd` is only a session-local convenience:

```json
{"path":"src"}
```

It may reset after the client reconnects. `exec_command.workdir` and each
file/Git tool's `path` argument are the reliable source of truth.

## Command and output behavior

`exec_command` and `write_stdin` default `yield_time_ms` to `10000`. Short
commands ordinarily return `status: "exited"` in one call. A still-running
command returns a `command_id` and a machine-readable `next_action` for
`write_stdin` with empty `chars`.

Only truncated terminal output returns a `read_output` next action by default.
`output_ref` values are `command:<id>:stdout` or `command:<id>:stderr`; offsets
are stream-specific absolute byte positions. Runtime limits bound active
commands, retained completed commands, per-command output, total output, and
retention time.

Use `tty: true` only when a program requires a terminal. POSIX receives a real
PTY (`isatty()` is true). This build returns `TTY_UNSUPPORTED` on Windows rather
than labeling pipes as a TTY.

## Permission modes

- `safe`: blocks network-looking commands, shell expansion, inline scripts,
  destructive commands, outside-workspace arguments, and secret/loader env.
- `trusted`: enables normal local-development network, expansion, and inline
  snippets while retaining secret and destructive-command checks.
- `dangerous`: disables command permission gates and Landlock; use only inside
  an isolated container or VM.

These modes do not change the tool list. Direct path tools retain workspace
confinement in every mode.

`--dangerously-fake-readonly-annotations` advertises every tool as read-only in
`tools/list` for clients that gate on annotations. It does not change the tool list
either, and it does not stop mutation or execution. `server_info` and the server
card keep reporting the real annotations. See
[permission-modes.md](permission-modes.md).
