# Troubleshooting

## Protocol Version Errors

HTTP clients should send `MCP-Protocol-Version: 2025-06-18` after initialization. Unsupported versions return a JSON-RPC error.

## SANDBOX_UNAVAILABLE

If `exec_command` reports `SANDBOX_UNAVAILABLE`, the host does not provide the required Linux Landlock confinement. Run on a supported Linux kernel or put the server inside an external sandbox and keep `exec_command` disabled for untrusted clients.

## Command Hangs Or Times Out

If the result returns `status: "running"`, poll with `write_stdin` using empty `chars`, or terminate with `kill_session`. Session deadlines still apply when the client stops polling.

## Trace Tool Calls

For local debugging:

```bash
CODEX_TOOL_RUNTIME_TRACE=1 codex-tool-runtime-mcp --workspace /path/to/repo
```

Trace events are JSON lines on stderr. Arguments are redacted for secret-looking keys and values; stdout remains reserved for stdio JSON-RPC frames.

## SWE-bench

If Docker or the `swebench` package is missing, the default scaffold should report `PREFLIGHT_ONLY`; an explicit evaluation attempt should report `BLOCKED`, not pass. See [swe-bench.md](swe-bench.md).
