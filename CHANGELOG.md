# Changelog

## Unreleased

- Moved the source-checkout tunnel launchers to `integrations/tunnels/` so user-facing runtime integrations no longer live under repository-maintenance scripts. The previously documented `scripts/tunnel.sh` entry point remains as a compatibility wrapper.
- Organized repository-owned components by responsibility: the npm launcher now lives in `packages/npm-launcher/`, the Cloudflare sandbox control plane in `infra/cloudflare/sandbox-control/`, and promo-video sources in `media/promo-video/`.

## 0.3.0 - 2026-08-13

### Changed

- **Breaking:** command handles are now named `command_id`; `kill_session` is
  now `kill_command`; retained output references use
  `command:<command_id>:stdout|stderr`. The former command `session_id`,
  `kill_session`, and `session:` output references are not accepted.
- Commands and retained output are now owned by the workspace server rather
  than by an individual `Mcp-Session-Id`. Multiple authenticated clients for
  the same workspace can continue, read, or terminate a command after
  reconnecting or creating a new MCP transport session using the `command_id`
  returned by `exec_command`.
- Closing or expiring an HTTP transport session no longer terminates workspace
  commands. Commands are still bounded by the existing active-count, retained
  output, byte, timeout, and TTL limits and are terminated when the workspace
  server shuts down.
- **Breaking:** `notifications/cancelled` no longer terminates the command the
  cancelled request started. The notification is still accepted and, as
  before, answered with no response. A command outlives the request that
  started it and is shared by every client of the workspace, and the mapping
  was keyed by the client's own JSON-RPC id, so two clients that both used
  `id: 1` could cancel each other's commands. Terminate a command with
  `kill_command`; the reduced cancellation responsiveness is tracked in issue
  #48.
- **Breaking:** `get_default_cwd` and `set_default_cwd` are removed and the
  default catalog is now 18 tools. A relative `path` always resolves against
  the workspace root, so there is no session-scoped working directory to set,
  read, or lose on reconnect. Pass a workspace-relative `path`, or
  `exec_command`'s `workdir`, to target a subdirectory. The `read_file`
  `next_action` continuation now repeats the workspace-relative path it was
  given rather than one relative to a session cwd, and `server_info` no longer
  reports `default_cwd`.
- Tool descriptions now direct remote clients to pass explicit `path`/`workdir`
  arguments and include concrete examples for patching and command
  continuation.
- Error text now names the error's category and whether it is retryable, and
  tells a model not to repeat a call that cannot succeed. `retryable` and
  `category` were only ever in `structuredContent`, which most clients do not
  forward to the model, so a permanent failure was indistinguishable from a
  transient one.
- `COMMAND_NOT_FOUND` from `write_stdin`, `kill_command`, and `read_output` now
  explains that the handle expired or never existed, states the retention
  window a finished command's output has, and names `exec_command` as the way
  to recover. Retrying a dead handle is the single largest source of failed
  `write_stdin` calls.
- `kill_command` now declares `kill_wait_ms` (hard-kill escalation wait,
  default 2000 ms) in its input schema; previously the runtime honored it but
  schema validation rejected any call that passed it.
- `read_output` no longer accepts the undocumented `command:<id>:full`
  reference form, which silently read stdout only. Use the per-stream
  `command:<id>:stdout` / `command:<id>:stderr` references.
- Retained command output now keeps the earliest bytes per stream (a frozen
  head segment, one eighth of the per-stream budget) in addition to the
  rolling tail, so the command echo and first errors survive large outputs.
  `read_output` reports `head_retained_bytes` and `evicted_gap_bytes`.
- `server_info` exposes an `output_retention` block naming the per-stream
  retention budget (`buffer_bytes_per_stream`, `head_bytes_per_stream`). How
  often that budget was actually hit is a runtime-wide measurement rather than
  an answer to one client, so the eviction counters (`evict_events`,
  `evicted_bytes_total`) and omitted-read counters
  (`read_output_omitted_hits`, `poll_omitted_hits`) are reported in the
  telemetry `session_end` event instead.
- `exec_command` and `read_output` tool descriptions now direct clients to
  redirect very large output to a file and page it with `read_file` /
  `search_text`.
- A method this server does not implement now returns `-32601` before the
  handshake as well as after it. Such a call previously returned `-32002 Server
  not initialized`, which tells a client to handshake and retry a method that
  will never exist.
- **Breaking:** HTTP is now stateless. The server no longer issues an
  `Mcp-Session-Id`, and a request that returns one from an older server is
  served normally instead of being refused with `-32001 Unknown MCP session`.
  Every request is answered by the one runtime that owns the workspace, so the
  128-session ceiling and its `503`, the session idle expiry, and the
  `MCP-Protocol-Version`-must-match-the-session check are all gone.
- **Breaking:** `DELETE /mcp` now returns `405` with `Allow: POST`; there is no
  session to terminate. `DELETE` is no longer advertised in the `Allow`,
  `Access-Control-Allow-Methods`, or server card `transport.methods` lists.
- **Breaking:** the handshake is no longer an admission gate. `tools/list`,
  `tools/call`, and the other implemented methods are served whether or not
  the caller sent `initialize` first, and `-32002 Server not initialized` is
  never returned. `initialize` is now idempotent: each one negotiates a
  version on its own and answers with it, so a repeat that names a different
  supported version is answered with that version instead of `-32600 Server is
  already initialized with a different protocol version`.
- **Breaking:** `server_info` reports `supported_protocol_versions` (every
  version this server speaks, newest first) in place of `protocol_version`
  (the one version a session had negotiated), and the server card at
  `/.well-known/mcp.json` reports `supportedProtocolVersions` in place of
  `protocolVersion`. Neither is a session-scoped value any more.
- Runtime state a shared server exposes to concurrent requests is hardened:
  the runtime directory (and the `HOME`, `TMPDIR`, and cache directories under
  it) is resolved to the primary or fallback location exactly once, so a later
  failure reports `RUNTIME_DIR_UNWRITABLE` instead of moving a running
  command's directories, and the non-git diff fallback snapshots its patch
  baselines under the patch lock.
- The `dev` extra now installs the official MCP python SDK (`mcp`). The
  compliance suite drives this server with it over both transports, which is
  the only check in the suite that does not use a client we wrote ourselves.
- Telemetry now measures the process rather than one client's handshake. A
  session is activated by the first request or notification that passes
  envelope validation — in either era, and before the method runs, so a first
  call that fails still reports it — while `ping` never activates one, leaving
  an HTTP health probe against an idle server silent. Client identity moves
  with the request that carried it: `initialize` emits its own `handshake`
  event with the negotiated version and the `clientInfo` it was given, a
  `2026-07-28` `tool_error` carries the sanitized `clientInfo` of that
  request, and no other event claims to know who is calling.
  `consecutive_failures` and the 20-error budget are runtime-wide across every
  client, `session_end` adds per-era request counts and a `server/discover`
  probe count, and the retained-output counters that `server_info` used to
  report travel with it. Each protocol choice a process first serves is also
  logged as one line on stderr, telemetry on or off. See
  [docs/telemetry.md](docs/telemetry.md).
- **Behavior change:** `initialize` no longer fails with `-32602` when a client
  asks for a `protocolVersion` this server does not speak. As the handshake
  spec requires, the server now answers with an `InitializeResult` naming the
  newest version it does speak (`2025-11-25`); a client that asks for a
  supported version still gets that version back. Asking to handshake with
  `2026-07-28` downgrades the same way, because that protocol states its
  version per request instead of negotiating one.
- **Behavior change:** an HTTP request without an `MCP-Protocol-Version` header
  is treated as `2025-11-25`, the newest handshake version this server speaks.
  The older spec suggests assuming `2025-03-26`, which this server has never
  spoken. The header value travels with the request as context and is available
  to the runtime; nothing echoes it, records it, or acts on it, and no method
  behaves differently for it.
- The runtime contract is now
  [docs/runtime-contract-v0.3.md](docs/runtime-contract-v0.3.md), and
  [docs/migration-0.3.md](docs/migration-0.3.md) collects every breaking change
  above with what to do about it. The v0.2 contract is kept, frozen, as the
  0.2.x record.

### Added

- Support for MCP `2026-07-28`, which serves a request without a handshake.
  Such a request states its own protocol version in `params._meta`
  (`io.modelcontextprotocol/protocolVersion` and
  `io.modelcontextprotocol/clientCapabilities` are required,
  `io.modelcontextprotocol/clientInfo` is optional) and may call
  `server/discover`, `ping`, `tools/list`, and `tools/call` immediately. A
  `_meta` version this server does not speak is answered with `-32022` and the
  versions it does
  (`data.supported`); a missing or mistyped required `_meta` field is answered
  with `-32602`. Requests without that `_meta` key — including legacy requests
  that carry `_meta.progressToken`, and every `initialize` — keep the
  handshake behavior they had.
- `server/discover` answers the probe a `2026-07-28` client sends instead of a
  handshake, so such a client never has to send one: it reports the versions
  this server speaks per request (`["2026-07-28"]` alone, since naming a
  handshake-era version here would invite the client to put one in its
  `_meta`, where it is unsupported), the `tools` capability, and the same
  workspace instructions `initialize` returns. Those instructions quote the
  workspace's own instruction files, so the result carries `ttlMs: 0` and
  `cacheScope: "private"` as `tools/list` does. A probe that states no
  protocol version in `_meta` is a handshake-era request and is still answered
  with `-32601`, which is what sends such a client to `initialize`.
- Streamable HTTP serves `2026-07-28` as well, with the mirror headers
  SEP-2243 requires. Such a request must repeat its `_meta` protocol version
  in `MCP-Protocol-Version` and its method in `Mcp-Method`; `tools/call`,
  `resources/read`, and `prompts/get` must also repeat their subject
  (`params.name`, or `params.uri` for `resources/read`) in `Mcp-Name`, either
  literally or wrapped as `=?base64?<payload>?=`. Any header that contradicts
  the body — including a `2026-07-28` version header on a request whose body
  is a handshake-era one — is answered with `400` and the new `-32020`.
  Handshake-era requests are not asked for these headers and are unaffected.
- A `2026-07-28` request that fails now reports it in the HTTP status as well:
  `-32601` is `404`, and `-32602`, `-32020`, and `-32022` are `400`. Any other
  code, `-32603` included, stays `200` with the JSON-RPC error, which is also
  what every handshake-era error keeps returning.
- `MCP-Protocol-Version` accepts any version this server speaks. A header
  naming an unknown version is still refused with `400` and `-32600`, and
  `data.supported` now lists both eras.
- CORS preflight allows `Mcp-Method` and `Mcp-Name`, and no longer allows
  `Mcp-Session-Id`.
- Results for `2026-07-28` requests carry `resultType: "complete"` and an
  `_meta.io.modelcontextprotocol/serverInfo`; `tools/list` and
  `server/discover` also carry the conservative cache hints `ttlMs: 0` and
  `cacheScope: "private"` on the result root. Responses to handshake clients
  are byte-for-byte what they were and never carry these fields.

### Fixed

- A repeated `initialize` on one persistent STDIO process is answered instead
  of failing with `-32600 Server is already initialized`. Connectors that probe
  for a newer protocol, fall back to the handshake, and then send `initialize`
  again on the same process could not finish a tool scan at all (issue #39).
  This shipped first in 0.2.3, which replayed the negotiated result; here there
  is no handshake state to replay, so each `initialize` simply negotiates and
  answers on its own, and a repeat naming a different supported version is
  answered with that version rather than rejected.
- Two clients patching the same file no longer lose an update. Each HTTP
  session used to own a runtime with its own patch lock while the files they
  wrote were shared, so a second patch could validate against a baseline it had
  read before the first one committed and overwrite it silently. One runtime
  now owns the workspace, so its lock covers every client: the later patch is
  answered with a retryable conflict.
- `apply_patch` no longer silently rewrites lines it was not asked to touch. It
  split both the file and the patch with `str.splitlines()`, which breaks on
  `\x0b`, `\x0c`, `\x1c`, `\x1d`, `\x1e`, `\x85`, `\u2028`, and `\u2029` as well
  as on `\n`, and then rejoined with `\n`. Any file containing one of those
  characters had it replaced by a newline by any patch, including a patch that
  changed an unrelated line, and a context line containing one could never
  match.
- The number of newlines at the end of a file is now whatever the hunk says it
  is. The trailing newline was captured from the file before applying and put
  back unconditionally, so a hunk that added a final blank line had it removed
  again and a hunk that removed the final newline had it restored. A file's
  last line is now addressable like any other.
- A context line that is empty is accepted as the empty context line it stands
  for, instead of failing the patch with `Invalid empty patch line`. V4A writes
  such a line as a single space, and model output and intermediate layers
  routinely strip that trailing space. Patch text ending in more than one
  newline is likewise accepted.

## 0.2.3 - 2026-08-12

### Fixed

- A repeated `initialize` on one persistent STDIO session now replays the
  negotiated handshake result instead of failing with `-32600 Server is
  already initialized`. The initializer does not run again, so no session
  state is reset and no second telemetry session is recorded. Connectors that
  send `initialize` twice on the same process — the OpenAI Secure MCP Tunnel
  probes with `server/discover` and then initializes twice — had their tool
  scan aborted, surfacing as HTTP 424, even though the session was healthy
  (issue #39).

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
  (`CODING_TOOLS_MCP_OAUTH_TOKEN_TTL`, still capped at 604800). The server
  does not issue refresh tokens, so a one-hour default forced re-authorization
  every hour.
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
