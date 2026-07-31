# Dogfood

Dogfood verifies that the MCP server can act as a coding-agent backend through MCP tool calls only.

## Current Artifact

- Report: [../reports/dogfood/coding-tools-dogfood.md](../reports/dogfood/coding-tools-dogfood.md)
- JSON: [../reports/dogfood/coding-tools-dogfood.json](../reports/dogfood/coding-tools-dogfood.json)
- Transcript: [dogfood/coding-tools-dogfood-transcript.json](dogfood/coding-tools-dogfood-transcript.json)
- Current conclusion in the checked-in report: `PASS`
- Verified server entrypoint: `python3 -m coding_tools_mcp --workspace {workspace} --host 127.0.0.1 --port 18772`
- Direct filesystem/shell bypass during task execution: `False`

The deterministic runner exercises `server_info`, repo search/read, two
patch-and-test loops, `git_diff`, a real PTY stdin session, `kill_session`, and
workspace escape denial. The broader compliance suite separately covers every
catalog tool, timeouts, output paging, `view_image`, binary rejection, HTTP sessions,
OAuth, and transport edge cases.

The report records completion rate, total elapsed time, tool-call and byte
counts, first-attempt patch success rate, poll count, all-case pass state, and
tool latency p50/p95. These are deterministic runtime regression metrics, not a
cross-agent leaderboard.

## Run It

```bash
make dogfood-runner
make dogfood-smoke
```

## MCP-Only Rule

After fixture setup and server startup, task execution must use only:

- `initialize`
- `tools/list`
- `tools/call`

The dogfood report flags any direct file, shell, or git bypass during task execution.
