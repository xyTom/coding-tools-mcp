# Security Policy

This project exposes local coding-runtime primitives over MCP. The intended boundary is one configured workspace root plus server-side policy, Linux Landlock filesystem confinement for `exec_command`, and external deployment sandboxing.

## Current Implementation Caution

The current compliance suite covers workspace traversal, symlink escape, direct and interpreter-mediated outside reads, direct syscall outside reads and writes, risky environment variables, network-looking commands, destructive commands, shell-expansion gating, Linux Landlock confinement, output caps, and session deadlines. Even so, `exec_command` must not be treated as a complete OS/container sandbox. It launches host processes and still relies on platform support plus command classification for non-filesystem risks.

For production, expose the server only to trusted local clients, bind HTTP to loopback, and run it inside an external container or sandbox with no host secrets, no broad filesystem mounts, and network egress disabled by policy.

## Workspace Boundary

- The workspace root is canonicalized once at startup.
- Tool path inputs are workspace-relative.
- Absolute paths, NUL bytes, `..`, and symlink escapes are rejected.
- Write paths validate the nearest existing parent before creating new files.
- `apply_patch` refuses symlink writes and stages changes before committing them.

## Command Execution

Commands run with:

- Workspace-bound cwd.
- Minimal environment with controlled `HOME` and `TMPDIR`.
- Process group isolation for timeout and kill.
- Linux Landlock rules that allow workspace access and read/execute access to interpreter/runtime roots.
- Optional operator-supplied read/execute roots from `CODEX_TOOL_RUNTIME_EXEC_ALLOW_ROOTS` for toolchains installed outside standard system prefixes.
- Policy denial for network-looking commands, destructive commands, shell expansion, setuid/setgid executables, and outside-workspace path arguments.

Commands must not read or write outside-workspace files indirectly through interpreters, nested shells, or direct syscalls. The Landlock tests cover both normal Python file APIs and `syscall(SYS_openat, ...)`.

## Environment Scrubbing

The runtime denies or drops secret-looking variables and values:

- API keys and tokens.
- Cloud credentials.
- Shell startup injection variables.
- Dynamic loader and interpreter path injection variables such as `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`, `BASH_ENV`, `ENV`, `PYTHONPATH`, `RUBYLIB`, and `NODE_OPTIONS`.

Secret redaction is defense in depth and must not be treated as the primary protection.

## Permission Model

Risky capabilities return structured permission-required or unsupported responses; the server never silently grants:

- `network`
- `destructive_command`
- `long_timeout`
- `sensitive_env`
- `shell_expansion`
- `privileged_executable`
- `write_generated_or_ignored`

`request_permissions` currently returns `ELICITATION_UNSUPPORTED` unless a future MCP client elicitation flow is implemented and tested.

## Session Lifecycle

Persistent command sessions use opaque server-owned IDs. `write_stdin` requires a live session. `kill_session` terminates only server-managed process groups. Deadlines continue to apply even if the client stops polling, and output buffers are bounded with dropped-byte metadata.

## HTTP Exposure

HTTP is intended for local MCP clients:

- Default bind remains `127.0.0.1`.
- Non-loopback deployment requires external authentication and sandboxing.
- Browser `Origin` is validated as defense in depth.
- Logs and optional `CODEX_TOOL_RUNTIME_TRACE=1` JSON traces go to stderr, not stdout.

## Reporting Security Issues

Report security issues privately to repository maintainers. Include the affected tool, minimal reproduction, expected and actual behavior, and whether the issue escapes the workspace, exposes credentials, permits network access, bypasses approval, or survives timeout/cancellation.

## Residual Risks

- Shell commands and test runners execute arbitrary project code.
- Network denial is policy-based unless the operator supplies external egress controls.
- Landlock is Linux-specific; non-Linux platforms need an external sandbox before enabling `exec_command` for untrusted clients.
- Symlink race resistance still depends on platform support for anchored/no-follow file operations.
- Secret redaction can miss transformed or fragmented secrets.
