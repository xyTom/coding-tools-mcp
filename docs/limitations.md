# Known Limitations

- `exec_command` is policy-constrained and uses Linux Landlock filesystem confinement where available, but it is not a complete OS/container sandbox.
- Command classification uses string/path checks for non-filesystem risk classes and can miss behavior hidden inside interpreters, package scripts, static binaries, or generated files.
- Network denial is policy-based unless the operator runs the server in an external sandbox with egress controls.
- Non-Linux platforms or Linux kernels without Landlock are not production targets for `exec_command` without an external sandbox.
- This build uses real POSIX PTYs but does not implement Windows ConPTY;
  `tty=true` returns `TTY_UNSUPPORTED` on Windows.
- Windows string commands prefer PowerShell 7 (`pwsh`) and run it with
  `-NoLogo -NoProfile -NonInteractive`. Operators may pin an absolute trusted
  executable with `CODING_TOOLS_MCP_PWSH_PATH`; otherwise the server searches
  absolute entries on its own process `PATH` while excluding the current
  directory tree. If no usable unpinned `pwsh` is found, the server
  automatically falls back to a trusted `cmd.exe` resolved from the server
  environment or Windows system directory. An invalid explicit PowerShell pin
  remains an error.
- Windows `safe` mode cannot statically decide what PowerShell dynamic syntax
  resolves to, so variables (`$`), splatting (`@`), the call and dot-source
  operators, .NET member access (`::`), and alias or expression evaluation
  cmdlets require the `shell_expansion` permission even when the command would
  turn out to be harmless. Nested shells (`pwsh -Command`, `pwsh
  -EncodedCommand`, `cmd /c`) require `inline_script`. Use
  `request_permissions` or `trusted` mode for commands that need them. Command
  scanning is not a sandbox: this build has no OS-level confinement on Windows,
  so `safe` mode there is a best-effort gate rather than a boundary.
- The `cmd.exe` compatibility fallback disables AutoRun and delayed expansion.
  In `safe` mode, percent expansion, caret escaping, and `CALL`/`FOR` evaluation
  require `shell_expansion`; recursive `del`/`rmdir`, `format`, and `diskpart`
  remain permission-gated. These checks are also best-effort rather than an
  OS-level boundary.
- Portable filesystems do not provide a transaction across unrelated
  directories. `apply_patch` keeps same-directory backups and rolls back the
  full staged set, but a storage failure that also prevents rollback is surfaced
  as `PATCH_ROLLBACK_FAILED` and may require operator recovery.
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
