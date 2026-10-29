# Known Limitations

The Landlock and network-policy limitations below describe the default
`--execution-isolation compatibility` mode. See
[cross-platform execution isolation](cross-platform-sandbox.md) for strict-mode
capabilities and remaining native-platform acceptance requirements.

- `exec_command` is policy-constrained and uses Linux Landlock filesystem confinement where available, but it is not a complete OS/container sandbox.
- Command classification uses string/path checks for non-filesystem risk classes and can miss behavior hidden inside interpreters, package scripts, static binaries, or generated files.
- Network denial is policy-based unless the operator runs the server in an external sandbox with egress controls.
- Non-Linux platforms or Linux kernels without Landlock are not production targets for `exec_command` without an external sandbox.
- This build uses real POSIX PTYs but does not implement Windows ConPTY;
  `tty=true` returns `TTY_UNSUPPORTED` on Windows.
- Portable filesystems do not provide a transaction across unrelated
  directories. `apply_patch` keeps same-directory backups and rolls back the
  full staged set, but a storage failure that also prevents rollback is surfaced
  as `PATCH_ROLLBACK_FAILED` and may require operator recovery.
- `apply_patch` locates a hunk by matching its context lines, so that context
  must be unique in the file. Matching is graded — exact, then ignoring
  trailing whitespace, then ignoring indentation width — and the grade used is
  reported as `match_quality`; there is no similarity-scored fuzzy matching and
  no line-number fallback. Context that appears twice fails with
  `PATCH_CONTEXT_AMBIGUOUS` unless a forward `@@ <context>` text anchor or
  `*** End of File` picks one occurrence, and context that appears nowhere fails with
  `PATCH_CONTEXT_NOT_FOUND`. Both failures carry the hunk index, numbered
  nearby text, and candidate positions to repair from.
- The `patch_lock` that serializes `apply_patch` is an in-process mutex. Two
  server processes on one workspace, an external editor, or a command run
  through `exec_command` are not excluded by it; the pre-commit baseline
  recheck detects such a writer and reports `PATCH_CONFLICT`, but the window
  between recheck and replace is not zero.
- Retained command output expires. A finished command keeps its output for
  `COMPLETED_COMMAND_TTL_SECONDS` (300s, reported as `output_retention` in
  `server_info`) and only the most recent 32 completed commands are kept, so a
  `command_id` or `output_ref` read late enough answers `COMMAND_NOT_FOUND`.
  Per-stream retention is head plus rolling tail, so the middle of a very large
  stream can be evicted while the command is still running; redirect large
  output to a file and page it with `read_file`.
- OAuth dynamic client registrations and pending authorization codes are held in
  process memory. Restarting the server requires dynamic clients to register
  again.
- Cancelling a request does not stop the work it started any sooner. The
  response is answered as the protocol requires — on stdio the loop is serial,
  so the answer is already written before a cancellation could be read, and over
  HTTP the modern cancellation signal is a closed response stream this server
  does not detect — but the SHOULD to stop working promptly is not met. Bound
  long work with `exec_command`'s timeout and terminate it with `kill_command`.
  Tracked in [issue #48](https://github.com/xyTom/coding-tools-mcp/issues/48).
- A workspace is a single trust domain shared by every client authenticated to
  it. Commands, retained output, and the resource quotas that bound them are one
  pool per workspace rather than per client, so one client can consume what
  another was going to use, and any client can read or kill any command with its
  `command_id`. Per-client identity and quotas are tracked in
  [issue #46](https://github.com/xyTom/coding-tools-mcp/issues/46).
- Current SWE-bench scaffold is preflight-only by default; an explicit official Docker harness attempt is blocked in this environment when Docker or the harness is unavailable.
- Checked-in SWE-bench predictions are placeholders until replaced by real native baseline and MCP-candidate patches.
