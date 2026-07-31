# Telemetry

coding-tools-mcp collects anonymous usage telemetry to answer two product
questions: which tool paths fail most often, and which releases become slower.
The upstream v0.2.2 default is enabled. Telemetry never changes MCP responses and
delivery failures are dropped silently.

## How to disable

Any of the following turns telemetry off completely:

```bash
export CODING_TOOLS_MCP_TELEMETRY=off   # also accepts 0, false, or no
export DO_NOT_TRACK=1
```

Telemetry is disabled automatically when `CI` is set. Debug mode prints the same
closed-schema events to stderr instead of sending them:

```bash
export CODING_TOOLS_MCP_TELEMETRY=debug
```

The authenticated Admin status response exposes only:

```json
{"telemetry": {"mode": "on", "docs": "docs/telemetry.md"}}
```

The mode is `on`, `off`, or `debug`. Admin does not expose event payloads.

## What is collected

Events use a closed schema. Every event carries package version, OS platform
and architecture, Python `major.minor`, transport (`stdio` or `http`), permission
mode, MCP protocol version, the connecting MCP `clientInfo` name/version
(truncated to 64 characters), a random telemetry-session identifier, and the
anonymous install identifier.

| Event | When | Additional properties |
| --- | --- | --- |
| `session_start` | MCP `initialize` completes | none |
| `tool_error` | a tool call fails (max 20 per session) | tool name, error code, duration ms, consecutive-failure count |
| `tool_summary` | the Session ends, one per tool used | calls, successes, errors, per-error-code counts, duration buckets, truncation count |
| `session_end` | the Session ends | Session duration, total calls, distinct tool count, dropped error-event count |

The random telemetry-session identifier is not an MCP `Mcp-Session-Id`, OAuth
identity, Workspace identity, or persistent user identifier. Tool calls never
block on delivery. Events are queued in memory to a daemon thread and error
events are capped as shown above.

## What is never collected

Telemetry does not contain:

- file or directory paths;
- repository/branch names, Workspace IDs, OAuth Agent/Client/Grant/token IDs, or MCP `Mcp-Session-Id` values;
- command lines, arguments, environment values, patch bodies, diffs, or file
  contents;
- OAuth bearer/refresh tokens, Admin tokens, client secrets, signing material,
  Vault references, hashes, or digests;
- upstream MCP inputs/results;
- chat/transcript content, model responses, context entries, transcript paths,
  or conversation summaries.

The install identifier is random and is not derived from a Workspace or user
property. Delete `~/.coding-tools-mcp/id` to reset it.

## Delivery guarantees

- stdout is never used for telemetry, so stdio JSON-RPC cannot be polluted;
- debug events go to stderr;
- network errors do not fail tool calls;
- the telemetry endpoint is not reachable through MCP tools;
- tests enforce the closed schema and privacy boundary.
