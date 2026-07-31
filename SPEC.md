# Coding Tools MCP Spec

This repository implements the `coding-tools-mcp-v0.2` runtime contract defined
in [docs/runtime-contract-v0.2.md](docs/runtime-contract-v0.2.md).

## Product boundary

The server exposes low-level coding primitives over MCP: inspect a workspace,
apply structured patches, run and interact with commands, and inspect Git. It is
not an agent wrapper and does not expose accounts, memory, cloud tasks, web
search, model routing, plugins, image generation, or subagent orchestration.

## Fixed tool model

There is one stable catalog. The runtime has no tool profiles, no `edit_file`,
no dynamic `tools/list_changed`, and no required `open_workspace` call.
`apply_patch` is the only direct file-write tool. `safe`, `trusted`, and
`dangerous` are command permission policies and never alter `tools/list`.

The default catalog contains 20 tools:

- runtime/context: `server_info`, `check_exec_environment`, `get_default_cwd`,
  `set_default_cwd`
- workspace inspection: `read_file`, `list_dir`, `list_files`, `search_text`
- mutation: `apply_patch`
- processes: `exec_command`, `write_stdin`, `read_output`, `kill_session`
- Git: `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame`
- policy/image: `request_permissions`, `view_image`

`view_image` can be disabled as an installation capability. All other tools are
fixed.

## Protocol

- MCP `2025-11-25` is current; `2025-06-18` is explicitly supported.
- Streamable HTTP uses `/mcp`; stdio uses newline-delimited JSON-RPC.
- Every HTTP `Mcp-Session-Id` owns an independent `Runtime`.
- JSON-RPC batches are rejected, cancellation follows `requestId`, and
  unimplemented logging is not advertised.
- `content` is agent-readable text normally sized by each tool's per-call
  limits, with a documented emergency safety ceiling for pathological entries.
  `structuredContent` is the complete stable machine result. `_meta` is
  optional UI space only.
- Root project instructions enter the initialization context automatically.

## Correctness guarantees

Patch operations are staged before writing, use same-directory fsynced temporary
files and atomic replacement, preserve mode/BOM/newlines, detect stale
baselines, and roll back multi-file failures. Filesystem rollback failure is
reported explicitly rather than hidden.

Command sessions use a 10-second default yield, real POSIX PTYs, bounded active
and retained-session stores, per-session and runtime output budgets, TTL cleanup,
and explicit `next_action` objects for polling or truncated output.

## Security boundary

Direct tools reject absolute paths, traversal, NULs, and symlink escapes.
`exec_command` also applies permission policy and Linux Landlock when available,
but remains a coding runtime rather than a complete container sandbox. Remote
deployment must use bearer or OAuth authentication. OAuth supports protected
resource metadata, PKCE S256, exact redirect binding, and RFC 7591 dynamic client
registration.

## Compatibility

Version 0.2 changes model-facing result text from a JSON mirror to summaries.
Clients that parsed `content[0].text` as JSON must read `structuredContent`.
Image base64 now appears once, in the MCP image block. Tool profiles and the
`view_image.output` selector are removed.
