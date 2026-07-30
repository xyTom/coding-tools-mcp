# Security Policy

This project exposes local coding-runtime primitives over MCP. The intended boundary is one configured workspace root plus server-side policy, best-effort Linux Landlock filesystem confinement for `exec_command` when available, and external deployment sandboxing.

## Current Implementation Caution

The current compliance suite covers workspace traversal, symlink escape, direct and interpreter-mediated outside reads, direct syscall outside reads and writes on Landlock-capable Linux hosts, risky environment variables, network-looking commands, destructive commands, shell-expansion gating, output caps, and command deadlines. Even so, `exec_command` must not be treated as a complete OS/container sandbox. It launches host processes and still relies on platform support plus command classification for non-filesystem risks.

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
- Configurable shell environment inheritance with controlled external `HOME`, `TMPDIR`, and `cache_dir`.
- Process group isolation for timeout and kill.
- Best-effort Linux Landlock rules, when available, that allow workspace access, exact runtime-directory writes, and read/execute access to interpreter/runtime roots.
- Optional operator-supplied read/execute roots from `CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS` for toolchains installed outside standard system prefixes.
- Policy denial for network-looking commands, destructive commands, shell expansion, inline interpreter/shell snippets, setuid/setgid executables, and outside-workspace path arguments.

On hosts with Landlock support, commands must not read or write outside-workspace files indirectly through interpreters, nested shells, or direct syscalls. On Windows, macOS, or Linux hosts without Landlock, `exec_command` still runs after policy checks but returns a warning; use an external sandbox before running untrusted commands or untrusted project code. String checks are not a substitute for OS network isolation, so inline code forms such as `python -c`, `python -`, `node -e`, and `sh -c` require explicit permission by default.

## Environment Scrubbing

The runtime denies or drops secret-looking variables and values:

- API keys and tokens.
- Cloud credentials.
- Shell startup injection variables.
- Dynamic loader and interpreter path injection variables such as `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`, `BASH_ENV`, `ENV`, `PYTHONPATH`, `RUBYLIB`, and `NODE_OPTIONS`.

`CODING_TOOLS_MCP_SHELL_ENV_INHERIT=all` broadens compatibility for local toolchains, but still applies the default secret and startup-loader filters. Only combine `--permission-mode dangerous` with `inherit=all` when the workspace, client, and child processes are trusted enough to receive the full parent environment.

Secret redaction is defense in depth and must not be treated as the primary protection.

## Permission Model

Risky capabilities return structured permission-required or unsupported responses; the server never silently grants:

- `network`
- `destructive_command`
- `long_timeout`
- `sensitive_env`
- `shell_expansion`
- `inline_script`
- `privileged_executable`
- `write_generated_or_ignored`

`request_permissions` currently returns `ELICITATION_UNSUPPORTED` unless a future MCP client elicitation flow is implemented and tested.

Operators should choose one of three permission modes:

- `safe`: default mode. Workspace writes are allowed, system toolchain roots are read-only, `HOME`, `TMPDIR`, and `cache_dir` point under an external server-owned runtime directory, network-looking commands are denied, shell expansion and inline scripts are denied, secrets and loader/startup env are filtered, and Landlock is enabled when available.
- `trusted`: local development mode. It allows network-looking commands, shell expansion, and inline scripts while still filtering secrets and blocking destructive commands and host-root writes. Runtime writes are scoped to the exact external runtime directory, not global `/tmp`.
- `dangerous`: disables `exec_command` permission gates and Landlock. Use only inside an isolated container or VM. Workspace path boundaries for direct file and patch tools still apply.

`--allow-network` remains a compatibility flag to open only the network-looking command gate. `--dangerously-skip-all-permissions` remains a compatibility alias for dangerous mode.

## Command Lifecycle

Persistent commands use opaque server-owned IDs. `write_stdin` requires a live command. `kill_command` terminates only server-managed process groups. Deadlines continue to apply even if the client stops polling, and output buffers are bounded with dropped-byte metadata.

## HTTP Exposure

HTTP is intended for local MCP clients:

- Default bind remains `127.0.0.1`.
- Non-loopback deployment requires external authentication and sandboxing.
- Browser `Origin` is validated as defense in depth.
- Logs and optional `CODING_TOOLS_MCP_TRACE=1` JSON traces go to stderr, not stdout.

## Reporting Security Issues

Report security issues privately to repository maintainers. Include the affected tool, minimal reproduction, expected and actual behavior, and whether the issue escapes the workspace, exposes credentials, permits network access, bypasses approval, or survives timeout/cancellation.

## Residual Risks

- Shell commands and test runners execute arbitrary project code.
- Network denial is policy-based unless the operator supplies external egress controls.
- Landlock is Linux-specific and best-effort; non-Linux platforms and Linux hosts without Landlock run `exec_command` with policy checks only and need an external sandbox for untrusted clients or workspaces.
- Symlink race resistance still depends on platform support for anchored/no-follow file operations.
- Secret redaction can miss transformed or fragmented secrets.
