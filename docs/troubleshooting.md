# Troubleshooting

## Protocol Version Errors

HTTP clients should send the version negotiated at initialization, normally
`MCP-Protocol-Version: 2025-11-25`. Compatibility clients may negotiate
`2025-06-18`; unsupported versions return a JSON-RPC error.

## SANDBOX_UNAVAILABLE

If `exec_command` returns a warning about Linux Landlock being unavailable, the command still ran under server-side policy checks, but without kernel filesystem confinement. This is expected on Windows, macOS, and Linux hosts without Landlock support. Put the server inside an external sandbox before running untrusted commands or untrusted project code.

If an older client or server reports `SANDBOX_UNAVAILABLE` as an error, upgrade to the current behavior or run on a Landlock-capable Linux kernel.

## Command Hangs Or Times Out

If the result returns `status: "running"`, poll with `write_stdin` using empty `chars`, or terminate with `kill_session`. Session deadlines still apply when the client stops polling.

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
