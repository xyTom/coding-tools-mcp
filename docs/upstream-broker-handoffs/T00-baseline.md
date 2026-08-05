# T00 Handoff — Upstream Tool Broker v6 Baseline

Status: **blocked**

Baseline HEAD: `ac739a74e36c6505f7862fa8cd026feeb907d8b8`

Branch: `feat/upstream-tool-broker-v6`

Worktree at task start: clean except for the newly supplied, untracked taskbook.

## Why T00 is blocked

The v6 taskbook assumes a live `tool_profile=full/read-only/compat-readonly-all`
runtime control and requires Broker exposure to vary by that profile. The current
v0.2.2 `main` contract intentionally removed that control:

- `README.md` describes stable tools instead of profiles.
- `tests/compliance/test_upstream_gateway.py` explicitly asserts that
  `tool_profile` is absent from `coding_tools_mcp.upstream`,
  `UpstreamManager.tool_names()`, and `Runtime`.
- The same test asserts that `UpstreamManager` has no dynamic `start_server()` or
  `stop_server()` API and that the upstream tool snapshot remains fixed with
  `listChanged=false`.
- `docs/integration-contract-v0.2.2.md` states that legacy `tool_profile` values
  are migration input only and do not control the catalog.

Implementing T01 onward literally would therefore reintroduce a control path
that the current release contract and compliance tests deliberately prohibit.
Per the taskbook execution protocol, this design conflict must be resolved
before production behavior is changed.

## Baseline inventory

### Repository target

- Source branch: `main`
- Source commit: `ac739a74e36c6505f7862fa8cd026feeb907d8b8`
- Local fixed tool registry: 20 tools (`TOOL_REGISTRY`)
- Repository-local `mcp-servers.json`: none
- Target runtime profile control: none
- Upstream discovery model: initialize once, then fixed per-`Runtime` snapshot
- MCP capability: `tools.listChanged=false`

### Currently running host service (diagnostic only)

The Codex service used to perform this work is an older installed runtime, not
the target checkout:

- package version: `0.1.7`
- profile: `full`
- exposed tools: 54
- upstream alias: `zotero-mcp`
- upstream initialized: yes
- upstream tools: 23

This old service proves that a profile-based deployment exists in the local
environment, but it must not be treated as the current v0.2.2 source contract.
A read-only count was not collected because changing the host service profile
would mutate the tool environment used for this task.

## Migration touchpoints

### `UpstreamTool`

- Definition: `coding_tools_mcp/upstream.py:118`
- Manager storage: `coding_tools_mcp/upstream.py:564`
- Registration: `coding_tools_mcp/upstream.py:696-720`
- Public definitions currently preserve the upstream definition nearly
  verbatim through `namespaced_tool_definition()`.

### Current tool/client state

- `UpstreamManager.clients`: mutable dict created at initialization.
- `UpstreamManager._tools`: mutable dict created at initialization.
- `UpstreamManager._tool_order`: tuple published after initialization.
- Reads:
  - `tool_definitions()`
  - `tool_names()`
  - `has_tool()`
  - `call_tool()`
- Writes:
  - `_initialize_configs()`
  - `close()` clears clients only; the exposed runtime snapshot is otherwise
    fixed for the lifetime of `Runtime`.

### `BaseUpstreamClient.call_tool()`

- Definition: `coding_tools_mcp/upstream.py:190`
- Runtime manager call site: `coding_tools_mcp/upstream.py:634`
- Test double override: `tests/compliance/test_upstream_gateway.py:92`
- Normalization is currently owned by the client method, not the manager.

### Session identity paths

- Outbound upstream HTTP session:
  `HttpUpstreamClient.session_id`, populated from `Mcp-Session-Id` response
  headers and sent on subsequent upstream requests.
- Inbound HTTP session lookup:
  `coding_tools_mcp/server.py:4996-4997` and `5156-5181`.
- Per-runtime HTTP identity:
  `Runtime.http_session_id` at `coding_tools_mcp/server.py:1355`.
- Stdio has no Broker result owner today; the taskbook's process-random stdio
  owner would be new behavior.

### Existing schema validation

- Entry point: `validate_arguments()` at `coding_tools_mcp/server.py:4433`.
- Recursive helper: `validate_schema_value()`.
- Already supports object/array/string/integer/number/boolean/null type checks,
  type arrays, properties, required, additionalProperties, enum for strings,
  integer minimum/maximum, and array items.
- It does not yet cover all v6 requirements (`const`, numeric enum,
  string maxLength, array min/max items, or oneOf/anyOf/allOf).
- Direct upstream dispatch currently bypasses `validate_arguments()` and relies
  on the upstream server.

## Existing behavior to preserve

- Public upstream names are `alias__remote_name` and nested remote names remain
  stable.
- All local `TOOL_REGISTRY` names are permanently reserved.
- Enable/include/exclude filtering happens before upstream initialization is
  published.
- Two runtimes do not share upstream client session state.
- Upstream annotations are not rewritten by the local fake-readonly override.
- Unknown, timeout, disconnect, RPC, and protocol errors remain structured.
- Legacy configurations have no live `tool_profile` control.

## Test baseline

- Command:
  `python -m pytest tests/compliance/test_upstream_gateway.py tests/compliance/test_mcp_admin.py -q`
  - Result: **PASS**
  - `24 passed, 20 subtests passed in 7.62s`

- Command:
  `uv run --frozen python -m unittest discover -s tests -p "test_*.py"`
  with explicit Windows `HOME`, `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA`
  - Result: **PASS**
  - `Ran 315 tests in 84.666s`
  - `OK (skipped=84)`

- A bare `pytest -q` is not the repository's authoritative full-suite entry:
  it collects the nested tiny-project fixture and, under this constrained
  runner, initially lacked a resolvable Windows home directory.

## Proposed resolution before T01

Choose and document one of these contracts before code implementation:

1. **Stable-catalog adaptation (recommended):** keep v0.2.2's fixed tool
   catalog and remove all live `tool_profile` branches from the v6 taskbook.
   Always expose the four read-only Broker tools plus the mutating Broker call;
   use existing tool annotations and permission policy rather than profile-based
   omission. Treat restart/stop safety as manager construction/close and Admin
   runtime replacement unless a separate dynamic lifecycle feature is approved.
2. **Profile restoration:** explicitly supersede the v0.2.2 integration
   contract, update migration docs and compliance tests, and reintroduce a live
   profile control. This is a product-level compatibility change, not an
   implementation detail.

## Task file map after resolution

- T01: `coding_tools_mcp/upstream.py`, upstream gateway tests
- T02: new `coding_tools_mcp/upstream_sanitize.py`, `upstream.py`, sanitizer tests
- T03: `upstream.py`, result-budget tests
- T04: new `coding_tools_mcp/upstream_search.py`, search tests
- T05: `upstream.py`, Admin/config parser and tests
- T06: `coding_tools_mcp/server.py`, `upstream.py`, Broker discovery tests
- T07: `server.py`, `upstream.py`, argument validation and passthrough tests
- T08: `server.py`, `upstream.py`, HTTP/stdio owner plumbing and ResultStore tests
- T09: `upstream.py`, concurrency/lifecycle tests
- T10: config/Admin/docs/status tests
- T11: validation fixtures, performance records, release handoff

## Next task prerequisites

- Resolve whether v6 is adapted to the v0.2.2 stable-catalog contract or
  intentionally supersedes it.
- Update the taskbook's T01/T05/T06/T07/T08/T10 acceptance text accordingly.
- Do not start T01 while both contracts remain simultaneously normative.

## Do not redo

- Do not rerun the baseline solely to diagnose the initial HOME failure; the
  explicit-environment full suite passed.
- Do not reintroduce `tool_profile` implicitly while implementing registry
  snapshots.
- Do not treat the currently running 0.1.7 host server as the target source
  baseline.
