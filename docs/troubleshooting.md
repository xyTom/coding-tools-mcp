# Troubleshooting

## Protocol Version Errors

Send `MCP-Protocol-Version` on every HTTP request. A `2026-07-28` client sends
`MCP-Protocol-Version: 2026-07-28`, repeating the version in its
`params._meta`; a header that disagrees with the body, or is missing from such
a request, returns `400` with `-32020`. A handshake client sends the version it
negotiated at initialization, normally `MCP-Protocol-Version: 2025-11-25`, or
`2025-06-18` for compatibility. A header naming a version this server does not
know returns `400` with `-32600` and lists the ones it does; a request with no
header at all is read as `2025-11-25`.

Asking to handshake with a version this server does not speak is no longer an
error: `initialize` answers with the newest version it does speak. If a client
seems to be on an older protocol than expected, read the `protocolVersion` in
the `InitializeResult` rather than assuming the one that was requested.

## SANDBOX_UNAVAILABLE

If `exec_command` returns a warning about Linux Landlock being unavailable, the command still ran under server-side policy checks, but without kernel filesystem confinement. This is expected on Windows, macOS, and Linux hosts without Landlock support. Put the server inside an external sandbox before running untrusted commands or untrusted project code.

If an older client or server reports `SANDBOX_UNAVAILABLE` as an error, upgrade to the current behavior or run on a Landlock-capable Linux kernel.

## Windows Command Shell

Windows string commands prefer PowerShell 7 (`pwsh`). If it is not installed or
an unpinned copy cannot be verified, the server automatically uses the trusted
Windows `cmd.exe` compatibility fallback. The selected interpreter appears as
`command_shell` in `server_info`, `check_exec_environment`, and
`exec_command`; fallback results also include a warning so the agent can switch
to cmd syntax.

The selection is resolved once and pinned for the server process, so concurrent
commands always agree on one interpreter. `check_exec_environment` re-resolves
the pin: after installing PowerShell 7, run that tool (or restart the server) to
leave the `cmd.exe` fallback.

To require a specific PowerShell installation, set
`CODING_TOOLS_MCP_PWSH_PATH` to its absolute `pwsh.exe` path. A bad explicit pin
returns `SHELL_NOT_FOUND` or `SHELL_VERSION_UNSUPPORTED` instead of falling back.
If neither interpreter can be resolved, restore `cmd.exe` or install PowerShell
7.

## Command Hangs Or Times Out

If the result returns `status: "running"`, poll with `write_stdin` using empty `chars`, or terminate with `kill_command`. Command deadlines still apply when the client stops polling.

## Permission Elicitation Is Unsupported

If `request_permissions` returns `ELICITATION_UNSUPPORTED`, the MCP client cannot show approval prompts. For dependency downloads and local development, prefer `--permission-mode trusted`; it allows network-looking commands, shell expansion, and inline scripts while keeping secret filtering and destructive-command checks. For isolated containers or VMs, use `--permission-mode dangerous` to disable `exec_command` permission gates.

## Missing Toolchain Environment

`exec_command` defaults to a core shell environment. If tools such as MSVC, CUDA, oneAPI, or Nix depend on variables from the parent terminal, start the server with:

```bash
CODING_TOOLS_MCP_SHELL_ENV_INHERIT=all coding-tools-mcp --workspace /path/to/repo
```

This still filters secret-looking and loader/startup variables unless `--permission-mode dangerous` is also enabled.

## Exec Diagnostics

`exec_command` may include `diagnostics` with codes such as `DEV_NULL_DENIED`, `DNS_RESOLUTION_FAILED`, `NETWORK_PERMISSION_REQUIRED`, `TMPDIR_NOT_WRITABLE`, `HOME_NOT_WRITABLE`, `COMMAND_TIMED_OUT`, and `OUTPUT_TRUNCATED`. See [troubleshooting-exec.md](troubleshooting-exec.md).

## Trace Tool Calls

For local debugging:

```bash
CODING_TOOLS_MCP_TRACE=1 coding-tools-mcp --workspace /path/to/repo
```

Trace events are JSON lines on stderr. Arguments are redacted for secret-looking keys and values; stdout remains reserved for stdio JSON-RPC frames.

## SWE-bench

If Docker or the `swebench` package is missing, the default scaffold should report `PREFLIGHT_ONLY`; an explicit evaluation attempt should report `BLOCKED`, not pass. See [swe-bench.md](swe-bench.md).
