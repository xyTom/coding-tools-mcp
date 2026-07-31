# Known Limitations

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
- OAuth dynamic client registrations and pending authorization codes are held in
  process memory. Restarting the server requires dynamic clients to register
  again.
- Current SWE-bench scaffold is preflight-only by default; an explicit official Docker harness attempt is blocked in this environment when Docker or the harness is unavailable.
- Checked-in SWE-bench predictions are placeholders until replaced by real native baseline and MCP-candidate patches.
