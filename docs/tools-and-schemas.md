# Tools And Schemas

The normative schema source is [profile-v0.1.md](profile-v0.1.md). Live schemas are returned by `tools/list` and compared against the profile by `make test-schema-drift`.

## Tool Inventory

- `read_file`: read UTF-8 text slices inside the workspace.
- `server_info`: inspect server/version/protocol/workspace/default cwd/profile/auth/runtime policy metadata.
- `workspace_identity`: confirm the active host, platform, workspace identity, and git state before remote edits.
- `check_exec_environment`: inspect lightweight `exec_command` environment and sandbox state known to the server.
- `get_default_cwd`: return the current default cwd inside the workspace.
- `set_default_cwd`: set the default cwd for relative tool paths.
- `file_stat`: inspect file existence and version metadata for optimistic concurrency checks.
- `list_dir`: list directory entries under the workspace.
- `list_files`: glob workspace files.
- `search_text`: search text or regex matches.
- `apply_patch`: apply a patch envelope.
- `restore_patch_checkpoint`: restore the before-image captured by a previous patch checkpoint.
- `exec_command`: run a bounded command under policy, with Landlock confinement when available.
- `write_stdin`: write to a live server-managed command session.
- `command_status`: read retained status and output from a command session without writing stdin.
- `kill_session`: terminate a server-managed command session.
- `git_status`: inspect git status.
- `git_diff`: inspect unified diff.
- `git_log`: inspect recent commits.
- `git_show`: inspect bounded `git show` output for a revision.
- `git_blame`: inspect bounded blame metadata for a file.
- `request_permissions`: return structured permission-request status.
- `view_image`: return a workspace image as MCP image content.

Every tool returns `content`, `structuredContent`, and `isError`. Tool execution failures use `isError: true` with structured error details.

`exec_command` results preserve raw `stdout`, `stderr`, and `exit_code`. They may also include `diagnostics`, a lightweight machine-readable list of common failure attributions such as `DEV_NULL_DENIED`, `DNS_RESOLUTION_FAILED`, `NETWORK_PERMISSION_REQUIRED`, `SHELL_EXPANSION_PERMISSION_REQUIRED`, `INLINE_SCRIPT_PERMISSION_REQUIRED`, `COMMAND_TIMED_OUT`, and `OUTPUT_TRUNCATED`.

## Tool Profiles

- `full`: exposes all tools with truthful annotations.
- `read-only`: exposes inspection-oriented tools and omits local mutation tools.
- `compat-readonly-all`: exposes all tools but advertises them as read-only for compatibility. This does not change behavior; mutation-capable tools can still mutate local state.

## Permission Modes

- `safe`: default mode. Commands run with workspace writes, system toolchain read roots, external server-owned `HOME`/`TMPDIR`/`cache_dir`, no network, blocked shell expansion, blocked inline scripts, filtered secrets, and Landlock when available.
- `trusted`: local development mode. It allows network-looking commands, shell expansion, and inline scripts while still filtering secrets and blocking destructive commands. Runtime writes remain scoped to the exact external runtime directory, not the whole Git worktree or global `/tmp`.
- `dangerous`: disables `exec_command` permission gates and Landlock. Use only in an isolated container or VM. Direct file tools still enforce workspace paths.
