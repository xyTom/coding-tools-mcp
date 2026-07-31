# Permission Modes

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
