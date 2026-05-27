# Coding Tools MCP Profile v0.1

Status: draft contract for implementation and compliance tests.

Protocol target: MCP `2025-06-18`.

This profile defines a coding-agent runtime MCP server. It exposes local coding primitives only: file inspection, search, patch editing, command execution, interactive process stdin, git status/diff, permission requests, and optional image viewing. It is not a product-agent wrapper and must not expose account, memory, web search, model routing, plugin marketplace, cloud task, or subagent orchestration tools.

## Normative References

- MCP tools specification: https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- MCP lifecycle: https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
- MCP transports: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- MCP schema reference: https://modelcontextprotocol.io/specification/2025-06-18/schema
- MCP elicitation: https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation

## Transport

P0 transport is Streamable HTTP.

- Endpoint: one MCP endpoint, default `/mcp`.
- Bind address: local deployments must default to `127.0.0.1`, not `0.0.0.0`.
- Methods: `POST` is required for JSON-RPC. `GET /mcp`, `HEAD /mcp`, `/.well-known/mcp.json`, and `/.well-known/mcp/server-card.json` return server-card metadata for remote-client discovery. `OPTIONS` supports browser preflight.
- Headers: clients must send `Accept: application/json, text/event-stream` on `POST` requests. After initialization, HTTP clients must include `MCP-Protocol-Version: 2025-06-18` or the negotiated version.
- Sessions: if the server emits `Mcp-Session-Id` during initialization, clients must send it on later requests. Session IDs must be unguessable visible ASCII values.
- Security: validate `Origin` for HTTP requests, require loopback by default, and require bearer authentication before non-loopback binding is allowed. When `--auth-token` or `CODING_TOOLS_MCP_AUTH_TOKEN` is configured, `/mcp` requires `Authorization: Bearer <token>`.
- Logging: logs go to stderr or structured MCP logging, never to stdout or HTTP response bodies outside JSON-RPC/SSE frames.

P1 transport is stdio.

- The client launches the server as a subprocess.
- stdin/stdout carry newline-delimited JSON-RPC messages.
- stdout must contain only valid MCP messages.
- stderr may contain UTF-8 logs.

## Lifecycle

The server must support `initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call`.

If a client requests a newer date-based protocol revision, the server negotiates down by returning its supported
`2025-06-18` protocol version in the `initialize` result. Older protocol revisions are rejected.

`initialize` response:

```json
{
  "protocolVersion": "2025-06-18",
  "capabilities": {
    "tools": {
      "listChanged": false
    },
    "logging": {}
  },
  "serverInfo": {
    "name": "coding-tools-mcp",
    "title": "Coding Tools MCP",
    "version": "0.1.6"
  },
  "instructions": "Use these tools only for local coding operations inside the configured workspace."
}
```

The server must not advertise `prompts`, `resources`, `sampling`, or product-level capabilities unless separately implemented and covered by compliance tests. `tools/list` is stable for a server process; if a future implementation changes the tool set dynamically it must set `listChanged: true` and send `notifications/tools/list_changed`.

## Workspace Rules

- The server is started with exactly one workspace root, by `--workspace <path>` or `CODING_TOOLS_MCP_WORKSPACE`.
- Tool path inputs are workspace-relative POSIX-style paths.
- Absolute paths are rejected by default.
- `..` traversal is rejected before and after canonicalization.
- Symlinks may be listed, but tools must not follow symlinks that resolve outside the workspace.
- Recursive listing and search exclude `.git`, `.reference`, `node_modules`, `target`, `dist`, `build`, `.venv`, `venv`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, and equivalent large/generated directories unless the caller explicitly opts in.
- The server must not expose sensitive environment variables to child processes unless explicitly configured.

## Tool Result Shape

Every successful `tools/call` response must include MCP `content` and `structuredContent`.

```json
{
  "content": [
    {
      "type": "text",
      "text": "<JSON serialization of structuredContent>"
    }
  ],
  "structuredContent": {
    "ok": true
  },
  "isError": false
}
```

Tool execution failures that occur after the `tools/call` request is valid return `isError: true` with structured error content:

```json
{
  "content": [
    {
      "type": "text",
      "text": "<JSON serialization of structuredContent>"
    }
  ],
  "structuredContent": {
    "ok": false,
    "error": {
      "code": "PATH_OUTSIDE_WORKSPACE",
      "message": "Path escapes the configured workspace.",
      "category": "security",
      "retryable": false,
      "details": {}
    }
  },
  "isError": true
}
```

Shared output fields:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "warnings": {
      "type": "array",
      "items": { "type": "string" },
      "default": []
    },
    "truncated": { "type": "boolean", "default": false },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

Shared error object:

```json
{
  "$defs": {
    "tool_error": {
      "type": "object",
      "properties": {
        "code": {
          "type": "string",
          "enum": [
            "INVALID_ARGUMENT",
            "PATH_OUTSIDE_WORKSPACE",
            "ABSOLUTE_PATH_DENIED",
            "SYMLINK_ESCAPE",
            "NOT_FOUND",
            "NOT_A_DIRECTORY",
            "IS_DIRECTORY",
            "BINARY_FILE",
            "UNSUPPORTED_ENCODING",
            "OUTPUT_TOO_LARGE",
            "TIMEOUT",
            "SESSION_NOT_FOUND",
            "SESSION_CLOSED",
            "COMMAND_REJECTED",
            "PERMISSION_REQUIRED",
            "PERMISSION_DENIED",
            "SANDBOX_UNAVAILABLE",
            "ELICITATION_UNSUPPORTED",
            "PATCH_FAILED",
            "GIT_ERROR",
            "INTERNAL_ERROR"
          ]
        },
        "message": { "type": "string" },
        "category": {
          "type": "string",
          "enum": ["validation", "security", "permission", "runtime", "not_found", "internal"]
        },
        "retryable": { "type": "boolean" },
        "details": { "type": "object", "additionalProperties": true },
        "permission_request": {
          "type": "object",
          "additionalProperties": true
        }
      },
      "required": ["code", "message", "category", "retryable"],
      "additionalProperties": false
    }
  }
}
```

Every `outputSchema` returned from `tools/list` must inline this error object or include a same-document `$defs.tool_error`. The per-tool snippets below use `$ref` to the shared definition for readability.

Protocol-level errors:

- Unknown JSON-RPC method: JSON-RPC `-32601`.
- Invalid JSON-RPC params, invalid tool name, invalid schema, or invalid cursor: JSON-RPC `-32602`.
- Server crash or unexpected internal failure before a tool result can be formed: JSON-RPC `-32603`.
- Unknown tool in `tools/call`: JSON-RPC `-32602` with `error.data.reason = "unknown_tool"`.

## Tool Inventory

P0 tools:

- `server_info`
- `get_default_cwd`
- `set_default_cwd`
- `read_file`
- `list_dir`
- `list_files`
- `search_text`
- `apply_patch`
- `exec_command`
- `write_stdin`
- `kill_session`
- `git_status`
- `git_diff`
- `git_log`
- `git_show`
- `git_blame`
- `request_permissions`

P1 tool:

- `view_image`

Tool profiles:

- `full`: expose all tools with truthful annotations.
- `read-only`: expose only `server_info`, `get_default_cwd`, `set_default_cwd`, file read/list/search tools, git inspection tools, and `view_image`.
- `compat-readonly-all`: expose all tools, but advertise `readOnlyHint: true`, `destructiveHint: false`, and `openWorldHint: false` for every tool. This profile is a compatibility escape hatch only; mutation-capable tools still mutate local state.

Forbidden tools and equivalent aliases:

- External agent memory or user personalization
- External provider login/account/token/keyring management
- External agent cloud tasks or remote queues
- web search or arbitrary network fetch as a direct tool
- image generation
- subagent orchestration
- model selection or paid account routing
- plugin marketplace or connector installation
- high-level prompt wrapper tools

Compliance tests must assert these forbidden capabilities are absent from `tools/list`.

## Tool Definitions

### read_file

Description: Read a text file slice inside the workspace.

Annotations:

```json
{
  "title": "Read file",
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "minLength": 1,
      "description": "Workspace-relative file path."
    },
    "start_line": {
      "type": "integer",
      "minimum": 1,
      "default": 1,
      "description": "1-based inclusive starting line."
    },
    "end_line": {
      "type": "integer",
      "minimum": 1,
      "description": "1-based inclusive ending line. Omit to read to max_bytes."
    },
    "max_bytes": {
      "type": "integer",
      "minimum": 1,
      "maximum": 1048576,
      "default": 131072
    },
    "encoding": {
      "type": "string",
      "enum": ["utf-8"],
      "default": "utf-8"
    }
  },
  "required": ["path"],
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "path": { "type": "string" },
    "content": { "type": "string" },
    "encoding": { "type": "string" },
    "start_line": { "type": "integer" },
    "end_line": { "type": "integer" },
    "total_lines": { "type": "integer" },
    "total_bytes": { "type": "integer" },
    "bytes_read": { "type": "integer" },
    "truncated": { "type": "boolean" },
    "truncated_by": { "type": ["string", "null"], "enum": ["lines", "bytes", null] },
    "output_lines": { "type": "integer" },
    "output_bytes": { "type": "integer" },
    "next_start_line": { "type": ["integer", "null"] },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

Failure cases include `NOT_FOUND`, `IS_DIRECTORY`, `BINARY_FILE`, `ABSOLUTE_PATH_DENIED`, `PATH_OUTSIDE_WORKSPACE`, `SYMLINK_ESCAPE`, `UNSUPPORTED_ENCODING`, and `OUTPUT_TOO_LARGE`.

### list_dir

Description: List immediate or bounded-recursive directory entries.

Annotations:

```json
{
  "title": "List directory",
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "default": ".",
      "description": "Workspace-relative directory path."
    },
    "recursive": { "type": "boolean", "default": false },
    "max_depth": { "type": "integer", "minimum": 1, "maximum": 20, "default": 1 },
    "max_entries": { "type": "integer", "minimum": 1, "maximum": 10000, "default": 1000 },
    "include_hidden": { "type": "boolean", "default": false },
    "include_ignored": { "type": "boolean", "default": false },
    "sort": {
      "type": "string",
      "enum": ["name", "type", "modified"],
      "default": "name"
    }
  },
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "path": { "type": "string" },
    "entries": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "name": { "type": "string" },
          "path": { "type": "string" },
          "type": { "type": "string", "enum": ["file", "directory", "symlink", "other"] },
          "size_bytes": { "type": "integer" },
          "modified": { "type": "string", "format": "date-time" },
          "is_hidden": { "type": "boolean" },
          "is_ignored": { "type": "boolean" },
          "symlink_target": { "type": "string" }
        },
        "required": ["name", "path", "type"],
        "additionalProperties": false
      }
    },
    "truncated": { "type": "boolean" },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

`list_dir` must not follow symlinked directories outside the workspace.

### list_files

Description: List files using glob and ignore filters.

Annotations:

```json
{
  "title": "List files",
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string", "default": "." },
    "patterns": {
      "type": "array",
      "items": { "type": "string" },
      "default": ["**/*"]
    },
    "glob": { "type": "string" },
    "exclude_patterns": {
      "type": "array",
      "items": { "type": "string" },
      "default": []
    },
    "include_hidden": { "type": "boolean", "default": false },
    "include_ignored": { "type": "boolean", "default": false },
    "max_results": { "type": "integer", "minimum": 1, "maximum": 50000, "default": 5000 },
    "sort": { "type": "string", "enum": ["path", "modified"], "default": "path" }
  },
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "path": { "type": "string" },
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "path": { "type": "string" },
          "type": { "type": "string", "enum": ["file", "symlink"] },
          "size_bytes": { "type": "integer" },
          "modified": { "type": "string", "format": "date-time" }
        },
        "required": ["path", "type"],
        "additionalProperties": false
      }
    },
    "truncated": { "type": "boolean" },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

### search_text

Description: Search workspace text, preferably using `rg` semantics.

Annotations:

```json
{
  "title": "Search text",
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "query": { "type": "string", "minLength": 1 },
    "path": { "type": "string", "default": "." },
    "regex": { "type": "boolean", "default": false },
    "case_sensitive": { "type": "boolean", "default": false },
    "include_globs": { "type": "array", "items": { "type": "string" }, "default": [] },
    "glob": { "type": "string" },
    "exclude_globs": { "type": "array", "items": { "type": "string" }, "default": [] },
    "context_lines": { "type": "integer", "minimum": 0, "maximum": 5, "default": 0 },
    "max_results": { "type": "integer", "minimum": 1, "maximum": 10000, "default": 1000 },
    "max_preview_bytes": { "type": "integer", "minimum": 80, "maximum": 4096, "default": 512 }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "query": { "type": "string" },
    "matches": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "path": { "type": "string" },
          "line": { "type": "integer" },
          "column": { "type": "integer" },
          "preview": { "type": "string" },
          "before": { "type": "array", "items": { "type": "string" } },
          "after": { "type": "array", "items": { "type": "string" } }
        },
        "required": ["path", "line", "preview"],
        "additionalProperties": false
      }
    },
    "total_matches": { "type": "integer" },
    "truncated": { "type": "boolean" },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

Search must skip binary files and default excluded directories.

### apply_patch

Description: Apply a patch envelope inside the workspace.

Annotations:

```json
{
  "title": "Apply patch",
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "patch": {
      "type": "string",
      "minLength": 1,
      "description": "Patch envelope beginning with *** Begin Patch and ending with *** End Patch."
    },
    "dry_run": { "type": "boolean", "default": false }
  },
  "required": ["patch"],
  "additionalProperties": false
}
```

Patch envelope operations:

```text
*** Begin Patch
*** Add File: path
+new line
*** Update File: path
@@
 old context
-removed line
+added line
*** Delete File: path
*** Update File: old/path
*** Move to: new/path
@@
...
*** End Patch
```

Paths in patch headers must be workspace-relative. Absolute paths, `..`, and symlink escapes are rejected. Patch application must be transactionally safe: either all file changes are committed, or no workspace files are changed. If full transactionality is not technically possible in the first implementation, the tool must reject the patch rather than leave partial edits.

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "dry_run": { "type": "boolean" },
    "clean": { "type": "boolean" },
    "summary": { "type": "string" },
    "affected_files": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "path": { "type": "string" },
          "old_path": { "type": "string" },
          "operation": { "type": "string", "enum": ["add", "update", "delete", "move"] }
        },
        "required": ["path", "operation"],
        "additionalProperties": false
      }
    },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

### exec_command

Description: Run a command in the workspace under permission policy. Linux Landlock filesystem confinement is applied when available; otherwise the result includes a warning and the command runs with policy checks only.

Annotations:

```json
{
  "title": "Execute command",
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": true
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "cmd": {
      "type": "string",
      "minLength": 1,
      "description": "Command line to execute."
    },
    "workdir": {
      "type": "string",
      "default": ".",
      "description": "Workspace-relative working directory."
    },
    "timeout_ms": {
      "type": "integer",
      "minimum": 1,
      "maximum": 600000,
      "default": 30000
    },
    "yield_time_ms": {
      "type": "integer",
      "minimum": 0,
      "maximum": 30000,
      "default": 1000,
      "description": "Return early with a session_id if the command is still running after this time."
    },
    "max_output_bytes": {
      "type": "integer",
      "minimum": 1024,
      "maximum": 1048576,
      "default": 65536
    },
    "stdin": { "type": "string", "default": "" },
    "tty": { "type": "boolean", "default": false },
    "env": {
      "type": "object",
      "additionalProperties": { "type": "string" },
      "default": {}
    }
  },
  "required": ["cmd"],
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "status": {
      "type": "string",
      "enum": ["exited", "running", "timeout", "rejected", "permission_required"]
    },
    "session_id": { "type": "string" },
    "exit_code": { "type": "integer" },
    "signal": { "type": "string" },
    "stdout": { "type": "string" },
    "stderr": { "type": "string" },
    "stdout_truncated": { "type": "boolean" },
    "stderr_truncated": { "type": "boolean" },
    "elapsed_ms": { "type": "integer" },
    "permission_request": { "type": "object", "additionalProperties": true },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok", "status"]
}
```

Policy requirements:

- `workdir` must remain inside the workspace.
- Commands with network access, broad filesystem destruction, privilege changes, or sensitive environment access must be rejected or return `PERMISSION_REQUIRED`.
- Inline interpreter and shell snippets such as `python -c`, `python -`, `node -e`, `ruby -e`, `perl -e`, and `sh -c` must return `PERMISSION_REQUIRED` by default because network and filesystem effects cannot be verified statically.
- `rm -rf /`, `git reset --hard`, broad `chmod`/`chown`, and similar destructive commands must not execute without explicit permission.
- Linux Landlock confinement must be applied when available. If it is unavailable, `exec_command` must continue to run under policy checks and include a warning that an external sandbox is required for untrusted commands.
- Long-running commands return `ok: true`, `status: "running"`, and `session_id`.
- Timed-out commands must clean up their process group.

### write_stdin

Description: Write characters to a server-managed running command session.

Annotations:

```json
{
  "title": "Write stdin",
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "session_id": { "type": "string", "minLength": 1 },
    "chars": { "type": "string", "default": "" },
    "yield_time_ms": { "type": "integer", "minimum": 0, "maximum": 30000, "default": 1000 },
    "max_output_bytes": { "type": "integer", "minimum": 1024, "maximum": 1048576, "default": 65536 }
  },
  "required": ["session_id"],
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "session_id": { "type": "string" },
    "status": { "type": "string", "enum": ["running", "exited", "closed"] },
    "exit_code": { "type": "integer" },
    "signal": { "type": "string" },
    "stdout": { "type": "string" },
    "stderr": { "type": "string" },
    "stdout_truncated": { "type": "boolean" },
    "stderr_truncated": { "type": "boolean" },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

Writing to an unknown or closed session returns `SESSION_NOT_FOUND` or `SESSION_CLOSED`.

### kill_session

Description: Terminate a server-managed running command session.

Annotations:

```json
{
  "title": "Kill session",
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "session_id": { "type": "string", "minLength": 1 },
    "signal": { "type": "string", "enum": ["TERM", "KILL", "INT"], "default": "TERM" },
    "wait_ms": { "type": "integer", "minimum": 0, "maximum": 30000, "default": 5000 },
    "max_output_bytes": { "type": "integer", "minimum": 1024, "maximum": 1048576, "default": 65536 }
  },
  "required": ["session_id"],
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "session_id": { "type": "string" },
    "killed": { "type": "boolean" },
    "status": { "type": "string", "enum": ["terminated", "exited", "not_found"] },
    "exit_code": { "type": "integer" },
    "signal": { "type": "string" },
    "stdout": { "type": "string" },
    "stderr": { "type": "string" },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

The tool may terminate only sessions created by this MCP server.

### git_status

Description: Return git working tree status for the workspace.

Annotations:

```json
{
  "title": "Git status",
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string", "default": "." },
    "include_untracked": { "type": "boolean", "default": true },
    "max_entries": { "type": "integer", "minimum": 1, "maximum": 10000, "default": 1000 }
  },
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "is_repo": { "type": "boolean" },
    "branch": { "type": "string" },
    "head": { "type": "string" },
    "upstream": { "type": "string" },
    "ahead": { "type": "integer" },
    "behind": { "type": "integer" },
    "clean": { "type": "boolean" },
    "entries": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "path": { "type": "string" },
          "original_path": { "type": "string" },
          "index_status": { "type": "string" },
          "worktree_status": { "type": "string" }
        },
        "required": ["path", "index_status", "worktree_status"],
        "additionalProperties": false
      }
    },
    "truncated": { "type": "boolean" },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

### git_diff

Description: Return unified git diff for workspace changes.

Annotations:

```json
{
  "title": "Git diff",
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "paths": { "type": "array", "items": { "type": "string" }, "default": [] },
    "staged": { "type": "boolean", "default": false },
    "unstaged": { "type": "boolean", "default": true },
    "context_lines": { "type": "integer", "minimum": 0, "maximum": 20, "default": 3 },
    "max_bytes": { "type": "integer", "minimum": 1024, "maximum": 1048576, "default": 262144 }
  },
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "diff": { "type": "string" },
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "path": { "type": "string" },
          "old_path": { "type": "string" },
          "status": { "type": "string" },
          "binary": { "type": "boolean" }
        },
        "required": ["path", "status"],
        "additionalProperties": false
      }
    },
    "truncated": { "type": "boolean" },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

Path filters must use workspace-relative paths and must not escape the workspace.

### request_permissions

Description: Request a scoped permission grant for a dangerous operation.

Annotations:

```json
{
  "title": "Request permissions",
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "tool_name": {
      "type": "string",
      "enum": ["exec_command", "apply_patch"]
    },
    "permission": {
      "type": "string",
      "enum": [
        "network",
        "destructive_command",
        "long_timeout",
        "sensitive_env",
        "shell_expansion",
        "inline_script",
        "privileged_executable",
        "write_generated_or_ignored"
      ]
    },
    "reason": { "type": "string", "minLength": 1 },
    "arguments": { "type": "object", "additionalProperties": true },
    "scope": { "type": "string", "enum": ["once", "session"], "default": "once" },
    "ttl_seconds": { "type": "integer", "minimum": 1, "maximum": 3600, "default": 300 }
  },
  "required": ["tool_name", "permission", "reason", "arguments"],
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "status": { "type": "string", "enum": ["granted", "denied", "unsupported", "not_required"] },
    "grant_id": { "type": ["string", "null"] },
    "expires_at": { "type": ["string", "null"], "format": "date-time" },
    "constraints": { "type": "object", "additionalProperties": true },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok", "status"]
}
```

Behavior:

- If the MCP client declared `elicitation`, the server may send an `elicitation/create` request to obtain a user decision.
- If elicitation is unavailable, the tool returns `ok: false`, `status: "unsupported"`, and `ELICITATION_UNSUPPORTED`, unless the server was explicitly started with a documented non-default permission mode such as `--dangerously-skip-all-permissions`.
- In `--dangerously-skip-all-permissions` mode, permission-gated operations are auto-granted. Workspace path boundaries still apply.
- v0.1 does not expose a grant registry consumed by `exec_command` or `apply_patch`; `request_permissions` is an unsupported/not-required diagnostic unless dangerous mode is explicitly enabled.
- Workspace escape is not grantable in v0.1.
- Dangerous permissions must not be silently granted by default.

### view_image (P1)

Description: Return a local workspace image as MCP image content for UI/frontend work.

Annotations:

```json
{
  "title": "View image",
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Input schema:

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string", "minLength": 1 },
    "max_bytes": { "type": "integer", "minimum": 1024, "maximum": 10485760, "default": 5242880 },
    "max_width": { "type": "integer", "minimum": 1, "maximum": 10000, "default": 2000 },
    "max_height": { "type": "integer", "minimum": 1, "maximum": 10000, "default": 2000 },
    "auto_resize": { "type": "boolean", "default": true },
    "output": { "type": "string", "enum": ["mcp_image", "data_url"], "default": "mcp_image" }
  },
  "required": ["path"],
  "additionalProperties": false
}
```

Output schema:

```json
{
  "type": "object",
  "properties": {
    "ok": { "type": "boolean" },
    "path": { "type": "string" },
    "mime_type": { "type": "string" },
    "bytes": { "type": "integer" },
    "width": { "type": "integer" },
    "height": { "type": "integer" },
    "resized": { "type": "boolean" },
    "original": { "type": "object", "additionalProperties": true },
    "data_url": { "type": "string" },
    "warnings": { "type": "array", "items": { "type": "string" } },
    "error": { "$ref": "#/$defs/tool_error" }
  },
  "required": ["ok"]
}
```

For `output: "mcp_image"`, `content` must include an MCP image content item:

```json
{
  "type": "image",
  "data": "<base64>",
  "mimeType": "image/png"
}
```

The server must verify image type by content, not only extension. Non-image files return `INVALID_ARGUMENT` or `BINARY_FILE` with a clear message. Oversized images return `OUTPUT_TOO_LARGE`. `auto_resize` requires the optional `image` extra (`Pillow`); if Pillow is unavailable or resizing fails, the tool must include a warning and either return the original image when still within limits or return `OUTPUT_TOO_LARGE` with warning details.

## Compliance Expectations

Contract tests must verify:

- `initialize` succeeds and advertises `tools`.
- `tools/list` includes all P0 tools and excludes forbidden product-layer tools.
- `view_image` appears only when P1 image support is enabled.
- Every tool has `name`, `description`, `inputSchema`, `outputSchema`, and annotations.
- Every schema is valid JSON Schema.
- Successful `tools/call` results include `content`, `structuredContent`, and `isError: false`.
- Failed tool executions include `content`, `structuredContent.ok: false`, a structured `error`, and `isError: true`.
- Unknown tools and malformed arguments return JSON-RPC errors.
- Debug logs never pollute stdout or JSON-RPC response bodies.
- Path traversal, absolute paths, symlink escape, unsafe command execution, network-by-default, and session abuse are rejected or permission-gated.
