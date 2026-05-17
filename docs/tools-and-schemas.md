# Tools And Schemas

The normative schema source is [profile-v0.1.md](profile-v0.1.md). Live schemas are returned by `tools/list` and compared against the profile by `make test-schema-drift`.

## Tool Inventory

- `read_file`: read UTF-8 text slices inside the workspace.
- `list_dir`: list directory entries under the workspace.
- `list_files`: glob workspace files.
- `search_text`: search text or regex matches.
- `apply_patch`: apply a Codex-style patch envelope.
- `exec_command`: run a bounded command under policy and Landlock confinement.
- `write_stdin`: write to a live server-managed command session.
- `kill_session`: terminate a server-managed command session.
- `git_status`: inspect git status.
- `git_diff`: inspect unified diff.
- `request_permissions`: return structured permission-request status.
- `view_image`: return a workspace image as MCP image content.

Every tool returns `content`, `structuredContent`, and `isError`. Tool execution failures use `isError: true` with structured error details.
