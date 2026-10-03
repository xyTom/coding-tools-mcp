# Telemetry

coding-tools-mcp collects anonymous usage telemetry to answer two product
questions: how many installs are active, and which tools succeed, fail, or run
slowly in the wild. Telemetry is enabled by default and can be disabled at any
time; disabling it changes nothing else about the server.

## How to disable

Any one of the following turns telemetry off completely:

```bash
export CODING_TOOLS_MCP_TELEMETRY=off   # also accepts 0 / false / no
export DO_NOT_TRACK=1                    # the cross-tool convention
```

Telemetry is also disabled automatically whenever `CI` is set, and the test
suite forces it off in `tests/__init__.py`, so CI and test runs never pollute
usage data. The `Makefile` exports `CODING_TOOLS_MCP_TELEMETRY=off` unless it
is already set, and the benchmark, dogfood, and agent-eval harnesses start
their servers with it defaulted to `off` the same way. Deleting `~/.coding-tools-mcp/id` resets the anonymous install
identity.

To see exactly what would be sent without sending it:

```bash
export CODING_TOOLS_MCP_TELEMETRY=debug  # prints events to stderr instead
```

## What is collected

Events are sent to PostHog (`us.i.posthog.com`) over HTTPS using the standard
library only. The payload is a closed schema — counters, enums, durations, and
version strings assembled by one function (`coding_tools_mcp/telemetry.py`).
It is structurally incapable of carrying paths, arguments, or file contents.

Every event carries: package version, OS platform and architecture, Python
`major.minor`, transport (`stdio`/`http`), permission mode, a build
fingerprint (`install` and `build`, below), a random per-session id, and the
anonymous install id. No client identity and no
protocol version is carried by every event: one server process answers every
client of its workspace, so a value recorded once would only ever describe
whichever client connected first.

The build fingerprint exists because a version string alone proved
untrustworthy: forks and modified copies report whatever version they were
copied from, including versions this repository never published. `install` is
one of `index` (installed from a package index), `editable`, `local` (a
non-editable install from a local path or archive), `vcs` (installed from a
version-control URL), `source` (run from a checkout with no installed
distribution, or with one that is not the code running), or `unknown`. `build`
is the first 12 hex characters of a SHA-256 over the package's own `.py`
source files, so the official wheel of a release has exactly one value and
any modified copy has another; it is `unknown` if the sources cannot be read.
Only these two labels are sent — never the install URL, path, or file names
they were derived from.

| Event | When | Additional properties |
| --- | --- | --- |
| `session_start` | the first request or notification of the session, `ping` excepted | — |
| `handshake` | every MCP `initialize` | negotiated protocol version, the client's `clientInfo` name and version |
| `tool_error` | a tool call fails (max 20 per session) | tool name, error code, duration ms, consecutive-failure count, and for a 2026-07-28 request the `clientInfo` name and version it carried |
| `tool_summary` | session ends, one per tool used | `calls`, `ok`, `errors`, `operation_failures`, `breaker_blocks`, `already_applied`, per-error-code `err_*` counts, per-outcome `outcome_*` counts, duration buckets, truncation count |
| `session_end` | session ends | session duration, total calls, distinct tools, dropped error-event count, breaker-block count, handshake-era and 2026-07-28 request counts, `server/discover` probe count, `unknown_tool_calls`, retained-output eviction and omitted-read counters |

A typical session produces 5–15 events totalling a few kilobytes.

In `tool_summary`, `ok` means successful operations, not merely calls that
dispatched successfully: it is `calls - errors - operation_failures`.
`operation_failures` on `exec_command` counts successfully dispatched commands
whose terminal outcome was `exited_nonzero`, `timeout`, or `signal`; a
`spawn_error` remains a tool error and is not counted twice. A background
command's terminal outcome stays attributed to the `exec_command` that launched
it even when `write_stdin`, `read_output`, or `kill_command` is the first call
to observe that outcome. Those observer calls remain successful unless the
calls themselves fail. Each `outcome_*` property counts the named operation
outcome. A terminal command outcome is counted only on its first observation,
however many later polls report it again; the non-terminal `running` is not
counted. A command stopped by `kill_command` reports `killed`, which is not an
operation failure.

`breaker_blocks` is retained for legacy `REPEATED_CALL_BLOCKED` results:
those pre-handler refusals are excluded from `calls`, `errors`, `ok`, duration
buckets, and failure streaks, and emit no `tool_error`. Current Runtime no
longer produces these refusals, so ordinary current sessions report zero
blocks. A repeated call now really executes: if it fails, it increments
`calls`, `errors`, and its original `err_*` counter even when accompanied by
nonblocking advice. Do not count advice as success or subtract these real
failures from the error rate.

Summaries from v0.5.0 counted refusals as calls and as
`err_REPEATED_CALL_BLOCKED` errors. When comparing executed-operation error
rates, remove those historical refusals from both numerator and denominator.
A drop in block counts after upgrading is a policy change, not proof that
client loops or underlying tool failures decreased.

`already_applied` counts successful calls whose result reported
`already_applied: true` — a write tool that found its change already in place
and wrote nothing. Those calls are still counted in `ok`.

A `tools/call` rejected with JSON-RPC `-32602` before any handler runs —
arguments that violate the tool's input schema, or arguments that are not an
object — is still a failed call: for a tool this server has, it counts toward
`calls` and `errors` as `err_INVALID_PARAMS` and may emit a `tool_error` like
any other failure. A call naming a tool the server does not have is counted
only in `session_end`'s `unknown_tool_calls`; the name itself is never
recorded, because it is whatever the client sent.

## What a session is

A session is one server process, not one client: every client of a workspace
is served by the same runtime, and neither protocol era leaves a session
behind on the server.

- The session is activated by the first request or notification that passes
  envelope validation, whichever era it belongs to, and before the method
  runs — so a first call that fails still reports its `tool_error`. A client
  that never sends `initialize` is measured like any other.
- `ping` never activates a session. An HTTP health probe against an idle
  server produces no events at all.
- `consecutive_failures` on `tool_error` is keyed by `(tool, error_code)` and
  counts that pair's runtime-wide streak across every client of the process.
  A successful operation by the same tool clears its error-code streaks;
  another tool's success and a failed operation clear nothing. It is not a
  single client's failure streak, and must not be read as one.
- The 20-error budget per session is likewise a whole-process budget, shared
  by every client; `session_end` reports how many error events were dropped
  once it ran out.
- A long-running HTTP server emits `session_start` once when it first serves
  a client and `tool_summary`/`session_end` once when it shuts down, however
  many clients it served in between.

`client_name` and `client_version` are sanitized self-reported labels, not
identity. `clientInfo` is whatever a client says it is, so each field is
narrowed to letters, digits, spaces, and `. _ -` — dropping the characters that
make up an address or a path, so that neither can travel verbatim — and then
truncated to 40 characters. Only `name` and `version` are read; a handshake-era
`tool_error` carries no identity at all, because the request that failed did
not name one.

## First-appearance server log

Independently of telemetry — including when telemetry is off — the server
writes one line to stderr the first time a process sees each protocol choice,
so an operator can tell from the log which era their clients actually speak:

```text
coding-tools-mcp: legacy client handshake (2025-11-25)
coding-tools-mcp: modern client request (tools/list)
coding-tools-mcp: server/discover probe
```

Each line appears at most once per process and only ever on stderr; over
stdio, stdout is the MCP wire.

## What is never collected

File paths, file contents, tool arguments, command lines, environment
variables, patch bodies, diffs, repository or branch names, workspace
locations, hostnames, usernames, and IP-derived identity. The install id is a
random UUID generated locally — never derived from hardware, hostname, or any
workspace property — so it cannot be reversed into an identity.

`tests/test_telemetry.py` enforces the boundary: a probe session touches files
with distinctive path substrings and the test asserts none of them appear
anywhere in the serialized payload, and that a disabled session never reaches
the sender at all.

## Delivery guarantees

Events queue in memory on a bounded queue serviced by a daemon thread with a
3-second send timeout; failures are swallowed and overflow is dropped. Nothing
is written to stdout (over stdio that is the MCP wire), nothing telemetry-
related is stored on disk beyond the install id file, and a dead or slow
telemetry endpoint is invisible to tool calls.
