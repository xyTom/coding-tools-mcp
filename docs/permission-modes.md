# Permission Modes

These settings are independent of `--execution-isolation`. The descriptions of
optional Landlock enforcement below apply to the default `compatibility`
isolation mode. See [cross-platform execution isolation](cross-platform-sandbox.md)
for the additive strict-mode contract, supported capabilities, and migration.

`exec_command` has three permission modes.

## safe

Default mode. Commands run with:

- workspace read/write
- system toolchain and DNS resolver paths read-only
- `HOME`, `TMPDIR`, and `cache_dir` under an external server-owned runtime directory
- network-looking commands blocked
- shell expansion and inline interpreter snippets blocked
- secret-looking and loader/startup env filtered
- Landlock enabled when available

Start explicitly:

```bash
coding-tools-mcp --permission-mode safe --workspace /path/to/repo
```

## trusted

Local development mode. It allows dependency downloads, shell expansion, and inline interpreter snippets while keeping secret filtering and destructive-command checks.

`HOME`, `TMPDIR`, and `cache_dir` use the same external runtime directory layout as safe mode. Only that exact runtime directory is added as an extra writable Landlock root.

```bash
coding-tools-mcp --permission-mode trusted --workspace /path/to/repo
```

## dangerous

Dangerous mode disables `exec_command` permission gates and Landlock. Use it only inside an isolated container or VM.

```bash
coding-tools-mcp --permission-mode dangerous --workspace /path/to/repo
```

Compatibility aliases:

- `--allow-network`: opens only the network-looking command gate.
- `--dangerously-skip-all-permissions`: alias for `--permission-mode dangerous`.

## Workspace Mutation Policy

Permission modes govern what `exec_command` may *do*. `--workspace-mutation`
governs who may write to the workspace at all, and it is orthogonal to them.

```bash
coding-tools-mcp --workspace-mutation structured-only \
  --write-path build --write-path .pytest_cache --workspace /path/to/repo
```

| Mode | Meaning |
| --- | --- |
| `unrestricted` (default) | `exec_command` may write anywhere inside the workspace. |
| `structured-only` | The workspace is read-only for commands; `apply_patch` and `apply_changes` are the only way to change files. |

`structured-only` is enforced by Linux Landlock, the same mechanism that
confines `exec_command` to the workspace. It is **experimental and off by
default** because it breaks every command that writes into the tree —
`pytest`'s caches, `__pycache__`, `npm`, `cargo`, `gradle`, and `git` itself —
unless each of those directories is listed with `--write-path`. `--write-path`
is repeatable, is workspace-relative, only applies in `structured-only`, and
silently drops any entry that escapes the workspace root. A valid in-workspace
directory is created if missing before Landlock rules are installed; a path
that cannot be created is an explicit error rather than a silently skipped
allowlist entry.
`CODING_TOOLS_MCP_WORKSPACE_MUTATION` and an `os.pathsep`-separated
`CODING_TOOLS_MCP_WRITE_PATHS` are equivalent.

Full enforcement requires Landlock ABI 3 or newer: ABIs 1–2 cannot deny file
truncation. Where that support is unavailable — a non-Linux host, an old
kernel, or `--permission-mode dangerous` — the mode is reported with
`"enforced": false` and a warning in `server_info.workspace_mutation_policy`
and in `check_exec_environment`; the server also prints a startup warning,
rather than claiming a restriction that is not in force.

## Client-Side Annotation Gates

Permission modes govern this server's own gates. They cannot affect a client that
gates on MCP annotations — one that refuses to call, or prompts on every call to, a
tool advertised as mutating. That friction lives entirely in the client, so
`--permission-mode dangerous` does nothing about it.

`--dangerously-fake-readonly-annotations` addresses that one case. It makes
`tools/list` report every tool with `readOnlyHint: true`, `destructiveHint: false`,
and `openWorldHint: false`:

```bash
coding-tools-mcp --permission-mode dangerous \
  --dangerously-fake-readonly-annotations --workspace /path/to/repo
```

The annotations are false. `apply_patch` still rewrites files and `exec_command`
still runs commands; only the advertised hints change. Because the claim is false,
it is fenced in:

- It requires `--permission-mode dangerous`, so it can only be set alongside an
  explicit assertion that the workspace is disposable.
- Over HTTP it requires bearer auth or OAuth. A tunnel forwards to a loopback bind,
  so the bind address cannot distinguish a private sandbox from a publicly reachable
  one; authentication can. Use stdio for an unauthenticated local sandbox.
- `server_info.annotation_override` and the server card's
  `tools.annotationOverride` report `fake_readonly`, and both keep listing the real
  per-tool annotations. `check_exec_environment` adds a warning. The lie is confined
  to `tools/list`, so ground truth is always one call away.

`CODING_TOOLS_MCP_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS=1` is equivalent. This is
not a tool profile: the catalog is unchanged and every tool remains callable.

## Runtime Directory

Safe and trusted modes keep command runtime state outside the Git worktree:

```text
/tmp/coding-tools-mcp/<workspace-hash>/<instance-id>/
  home/
  tmp/
  cache/
```

On Windows, the parent is the platform temp directory instead of `/tmp`. The server creates these directories lazily when `exec_command` first needs an environment. `server_info` and `check_exec_environment` report `runtime_dir`, `home`, `tmpdir`, and `cache_dir`.

The server does not create workspace-local `.coding-tools/` directories by default. Runtime directories are per server instance; after stopping the server, operators may remove an instance directory or the whole external runtime tree. Normal OS temp cleanup may also remove stale directories.

Set `CODING_TOOLS_MCP_RUNTIME_ROOT` to choose an explicit external runtime parent. The server reports `RUNTIME_DIR_UNWRITABLE` instead of falling back into the workspace for runtime state.
