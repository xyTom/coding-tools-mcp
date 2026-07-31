# Changelog

## Unreleased

### Added

- Persistent OAuth authorization state in `oauth.sqlite3`: Clients, Grants,
  access-token metadata, refresh-token families, signing-key metadata, and
  redacted audit events survive restart. Authorization Code + PKCE now returns
  rotating refresh tokens; replay of an already-rotated token revokes its family.
- Immutable OAuth Agent-to-Workspace binding. `client_id`, `grant_id`,
  `workspace_id`, and `jti` are produced by bearer validation and frozen into
  each HTTP MCP Session. cwd, processes, retained output, paths, and project
  instructions remain isolated by Session.
- Runtime-initialized upstream MCP Gateway snapshots with stable namespaces,
  collision fail-closed behavior, preserved schemas/annotations/
  `structuredContent`, and restart-only administration.
- Dedicated authenticated Admin API and WebUI for revision-aware Settings,
  Workspace Catalog, Gateway, OAuth revocation/key lifecycle, Secret Vault,
  and Workspace-partitioned chat/Codex session persistence.
- Packaged self-contained Admin WebUI, Desktop package data, privacy-safe Admin
  telemetry mode, and Windows-portable npm launcher contract tests.
- `docs/migration-v0.1-to-v0.2.2.md` with upgrade, backup, reauthorization,
  and rollback guidance.

### Changed

- The stable user configuration directory now owns `server-settings.json`,
  `mcp-servers.json`, `oauth.sqlite3`, `oauth-secrets.json`, the server Secret
  Vault, and `transcripts.sqlite3`; managed Workspace roots no longer own server
  identity state.
- Legacy `tool_profile` is accepted only as migration input, removed on write,
  and reported as `legacy_tool_profile_ignored`. It never controls `tools/list`.
- Settings and Gateway writes use optimistic revisions. Gateway changes require
  restart and never mutate existing Runtime tool snapshots.

### Security

- Ordinary MCP bearer and OAuth credentials never grant Admin authority.
- OAuth, Gateway, Vault, and chat management responses are redacted; raw bearer
  tokens, refresh tokens, signing material, client secrets, hashes/digests, and
  credential references are not displayed.
- OAuth Store/Vault failures, missing Workspace mappings, disabled Workspaces,
  upstream namespace collisions, and invalid Gateway credential references fail
  closed.

### Fixed

- Refresh-token rotation, replacement-token insertion, access-token metadata,
  family timestamps, and both issuance audits now commit in one SQLite
  transaction. If access-token persistence or auditing fails, the whole exchange
  rolls back, the original refresh token remains usable, and no replacement or
  access-token metadata is left behind.

## 0.2.2 - 2026-07-28

### Fixed

- Dynamic client registration no longer rejects clients that request grant or
  response types the server does not implement. Requested values are now
  narrowed to the supported set and reported back in the registration
  response, per RFC 7591, instead of failing the registration with
  `invalid_client_metadata`. Clients that register with
  `grant_types: ["authorization_code", "refresh_token"]` — the common case —
  could not connect at all.

### Changed

- Raised the default OAuth access token lifetime from one hour to 24 hours
  (`CODING_TOOLS_MCP_OAUTH_TOKEN_TTL`, still capped at 604800). The original
  upstream 0.2.2 release did not issue refresh tokens; the integrated
  Unreleased state above now supports rotating refresh tokens.
- Advertised grant and response types now derive from single constants shared
  by authorization-server metadata and the registration endpoint.

## 0.2.1 - 2026-07-26

### Changed

- Replaced the manual multi-dispatch release choreography with a single
  `release.yml` pipeline triggered by pushing a `v<version>` tag. The evidence
  workflows (`compliance`, `real-workloads`, `swebench-lite`) run as called
  jobs of the same workflow run as the registry publishes, so the
  same-release-commit audit property holds by construction with no inputs, run
  ids, or ref choices; the npm launcher publishes only when its version is not
  already on the registry; the GitHub Release is created automatically with
  notes from the CHANGELOG section. The dispatch-only `publish-pypi` and
  `publish-npm` workflows were removed, the npm tarball is published by local
  path (the previous form was parsed as a GitHub shorthand and failed), and
  `final-audit` remains as a manual audit tool outside the release path. PyPI
  and npm trusted publishing must point at `release.yml`.

### Added

- Anonymous usage telemetry, on by default, sent to PostHog over HTTPS with
  the standard library only. Events are a closed schema of counters, enums,
  durations, and version/platform strings — session starts, per-tool
  success/error/latency summaries, and individual tool failures capped at 20
  per session — and never contain paths, arguments, command lines, or file
  contents. `CODING_TOOLS_MCP_TELEMETRY=off` or `DO_NOT_TRACK=1` disables it,
  `CODING_TOOLS_MCP_TELEMETRY=debug` prints events to stderr instead of
  sending, CI environments are excluded automatically, and the anonymous
  install id in `~/.coding-tools-mcp/id` can be deleted to reset identity.
  Tool calls never block on delivery: events queue in memory to a daemon
  thread and failures are dropped silently. See `docs/telemetry.md`.

## 0.2.0 - 2026-07-24

### Changed

- Replaced selectable tool profiles with one stable, truthfully annotated
  coding catalog. Permission modes still control command policy but never alter
  `tools/list`.
- Made `apply_patch` the sole direct file-mutation tool and added staged,
  baseline-checked, same-directory atomic replacement with multi-file rollback,
  mode/BOM/newline preservation, and structured retry errors.
- Changed model-facing `content` from a JSON mirror to per-tool summaries and
  previews sized by each tool's own per-call limits, without the former 16 KiB
  renderer preview cap. A generous emergency ceiling still protects clients
  from unbounded individual entries. Command results always lead with
  status/exit code; pageable truncation names executable continuation calls,
  while non-pageable results state how to narrow or raise their limits. Clients
  that parsed text as JSON must read `structuredContent`.
- Changed `exec_command` and `write_stdin` default yield to 10 seconds. Running
  or truncated results now provide explicit machine-readable `next_action`.
- Split active and retained process sessions and added concurrency, count, byte,
  and TTL limits. POSIX TTY requests now use a real PTY; Windows reports an
  explicit unsupported error in this build and uses portable graceful/forced
  process termination without assuming `SIGKILL` exists.
- Upgraded the primary protocol to MCP `2025-11-25` while retaining explicit
  `2025-06-18` compatibility.

### Added

- Independent per-`Mcp-Session-Id` HTTP runtimes, session termination, standard
  cancellation mapping, batch rejection, and strict protocol-header checks.
- OAuth protected-resource metadata, Authorization Code + PKCE S256, exact
  redirect binding, one-hour client-bound tokens, and RFC 7591 dynamic client
  registration.
- Automatic bounded root project-instruction loading during initialization.
- Streaming/bounded file reads, early-stopping ripgrep, batched Git ignore
  checks, and iterator-based traversal.
- Dedicated patch, process, result, project-context, OAuth, error, and HTTP
  session modules plus regression coverage for their boundary conditions.
- Reproducible dogfood efficiency metrics and a five-run 0.1.7/0.2.0 comparison;
  serialized tool-result bytes fell 37.279% with unchanged completion and call
  counts on the deterministic workload.
- Desktop client MVP, shipped in the same distribution as the server and
  exposed as the `coding-tools-mcp-desktop` entry point behind the optional
  `[desktop]` extra: per-workspace profiles, local server start/stop, FRP and
  Cloudflare tunnel modes (quick URL or named fixed domain), OAuth and bearer
  credential setup with clipboard helpers, and concurrent health checks across
  local and public `.well-known/mcp.json` plus OAuth authorization-server and
  protected-resource metadata.
- Desktop profile storage under `~/.coding-tools-mcp-desktop` that keeps
  secrets in a separate file from profiles, writes both through `fsync` and
  atomic replacement with `0600` files and `0700` directories, validates
  profile IDs before deriving state paths, and drops unknown keys so records
  written by other releases keep loading.
- Desktop English and Simplified Chinese catalogs that follow the system
  language on first launch and switch at runtime, a
  `scripts/check_desktop_i18n.py` coverage and placeholder gate wired into
  `make lint`, and `make desktop-i18n-update`, `-release`, and `-check`.
- npm launcher package for `npx coding-tools-mcp`. It starts the PyPI server
  through `uvx` or `pipx run`, forwards arguments and stdio, mirrors fatal
  signals instead of remapping them to exit codes, and prints actionable
  install steps when neither runner is on `PATH`. Its own version is
  independent of the server version, which `CODING_TOOLS_MCP_VERSION` pins.
- Cloudflare sandbox control Worker in `cloudflare/sandbox-control`: an
  authenticated control plane that dispatches
  `.github/workflows/start-sandbox.yml` through `POST /start` and through an
  MCP JSON-RPC endpoint at `/mcp` exposing `start_coding_tools_sandbox` and
  `get_coding_tools_sandbox_status`. It answers `initialize`, `ping`,
  `resources/list`, and `prompts/list`, acknowledges notifications with `202`,
  and reports GitHub dispatch failures with the ref and workflow that failed.
  The Worker never runs code and never proxies MCP traffic.
- `start-sandbox` workflow inputs for `tunnel_type`, `tunnel_hostname`,
  `auth_token`, and `hide_auth_token`, so a sandbox can publish one reusable
  Cloudflare named hostname with a secret-managed bearer token kept out of
  workflow logs and run summaries.
- `--dangerously-fake-readonly-annotations` and
  `CODING_TOOLS_MCP_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS`, which report every tool
  in `tools/list` as read-only and non-destructive. This restores the one capability
  lost when the `compat-readonly-all` profile was removed: a client that gates on
  annotations is unreachable from server-side permission modes, so `dangerous` mode
  cannot quiet it. Unlike the old profile the catalog is untouched and the claim is
  fenced in — it requires `dangerous` permission mode, requires authentication over
  HTTP because a tunnel forwards to a loopback bind, and is confined to `tools/list`.
  `server_info.annotation_override`, the server card's `tools.annotationOverride`,
  and `check_exec_environment` all report it, and both `server_info` and the server
  card keep publishing the real per-tool annotations.
- A `deploy-sandbox-control` workflow that deploys the Cloudflare control-plane
  Worker on every push touching `cloudflare/sandbox-control/**` or
  `start-sandbox.yml`. Nothing deployed the Worker before, so its half of the
  `workflow_dispatch` contract could sit unshipped indefinitely while the workflow
  half moved on, which GitHub rejects with `422 Unexpected inputs provided` before
  creating a run.
- `make check-dispatch-inputs`, which compares the Worker's dispatch body against
  `start-sandbox.yml`'s declared `workflow_dispatch` inputs and cross-checks
  `workflow_call`. It gates the deploy and runs in `make ci`.

### Removed

- `--tool-profile`, `CODING_TOOLS_MCP_TOOL_PROFILE`, and all launcher/UI/control
  plane profile selectors.
- Duplicate image base64/data URLs and the `view_image.output` selector. Image
  bytes now appear once in one MCP image block.
- JSON-RPC batch handling and the unimplemented logging capability declaration.

### Fixed

- Lowercase the owner segment when composing the GHCR sandbox image tag, so
  builds from mixed-case repository owners resolve to a valid image reference.

### Security

- Public tunnel documentation no longer recommends anonymous read-only mode:
  the fixed catalog includes mutation and execution, so remote access must be
  authenticated.
- Forwarded headers are trusted only when explicitly enabled; browser origins,
  OAuth resources, clients, redirect URIs, and token auth methods are bound
  exactly.
