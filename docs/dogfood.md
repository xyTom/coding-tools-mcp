# Dogfood

Dogfood verifies that the MCP server can act as a coding-agent backend through MCP tool calls only.

## Current Artifact

- Report: [../reports/dogfood/codex-on-mcp.md](../reports/dogfood/codex-on-mcp.md)
- JSON: [../reports/dogfood/codex-on-mcp.json](../reports/dogfood/codex-on-mcp.json)
- Transcript: [dogfood/codex-on-mcp-transcript.json](dogfood/codex-on-mcp-transcript.json)
- Current conclusion in the checked-in report: `PASS`
- Direct filesystem/shell bypass during task execution: `False`

The deterministic runner exercises repo inspection, `search_text`, `apply_patch`, failing/passing JavaScript and Python tests, `git_status`, `git_diff`, timeout handling, stdin sessions, `kill_session`, `view_image`, binary rejection, and workspace escape denial.

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
