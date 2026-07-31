# Stable-Catalog Upstream Broker

The upstream Broker extends the fixed local MCP catalog without introducing live
catalog mutation. The five local Broker tools are always registered:

- `upstream_tool_search`
- `upstream_tool_describe`
- `upstream_tool_call`
- `upstream_tool_call_mutating`
- `upstream_result_fetch`

`tools.listChanged` remains `false`. A Runtime discovers upstream tools once,
builds one sanitized catalog and search index, freezes direct routing, and keeps
that snapshot until the Runtime closes.

## Exposure configuration

Each server accepts:

```json
{
  "expose_mode": "broker",
  "pinned_tools": ["search"],
  "include_tools": ["search", "create_issue"],
  "exclude_tools": [],
  "tags": ["code", "remote"],
  "tool_policy": {
    "search": "readonly",
    "create_issue": "mutating"
  }
}
```

Missing `expose_mode` means `direct` for legacy compatibility. In `direct` mode,
every tool remaining after include/exclude filtering is included in both direct
`tools/list` exposure and the Broker catalog. In `broker` mode, the complete
filtered set enters the Broker catalog, while only remote names listed in
`pinned_tools` are directly exposed.

New server templates in the Admin WebUI default to `broker`. Admin writes only
validate and persist configuration. They do not alter an existing Runtime. The
change becomes active for a newly constructed MCP Session/Runtime or after a
service restart; no `tools/list_changed` notification is sent.

Legacy `tool_profile` values remain ignored migration inputs. They do not control
Broker visibility, routing, search, risk classification, or calls.

## Workflow

1. Call `upstream_tool_search` with a natural-language query and optional server,
   read-only, tags, or name-prefix filters.
2. Call `upstream_tool_describe` for the selected public name. Describe returns
   the sanitized public definition and its 32-character public schema digest;
   raw definitions are not returned.
3. Use `upstream_tool_call` only for targets classified `readonly`.
4. Use `upstream_tool_call_mutating` for targets classified `mutating` or
   unknown. The mutating route requires the current public schema digest.
5. The Gateway validates arguments against the public `inputSchema` before
   forwarding. A stale digest returns `UPSTREAM_SCHEMA_CHANGED`; invalid
   arguments return `UPSTREAM_ARGUMENTS_INVALID`.

`upstream_tool_call_mutating` is always visible and truthfully annotated with
`readOnlyHint=false`, `destructiveHint=true`, and `openWorldHint=true`. The
fake-readonly compatibility override changes displayed annotations only and is
not a security boundary. Remote capabilities remain governed by the upstream
server's own authorization and side-effect model.

## Oversized results

All upstream results are normalized and constrained to the final inline MCP
budget. For oversized Broker calls with a concrete owner, the original normalized
UTF-8 JSON envelope is stored before truncation and the inline result includes a
short-lived handle for `upstream_result_fetch`.

Default limits are:

- one stored result: 8 MiB;
- one owner: 16 handles and 16 MiB;
- all owners in one manager: 64 MiB;
- TTL: five minutes;
- eviction: FIFO;
- fetch page: 1–32000 Unicode codepoints.

HTTP ownership uses the MCP Session ID. stdio uses a random Runtime-local owner.
Unknown, expired, and cross-session handles return the same opaque not-found
response. Direct upstream calls do not create handles. Stores are in-memory and
per `UpstreamManager`; they are not persistent.

OAuth-principal-plus-session composite ownership is not implemented.

## Operational report

`server_info` and the authenticated Admin Gateway payload include an
upstream-only exposure report:

- direct tool count and public-definition bytes;
- catalog total and broker-only count;
- the largest sanitized public definitions;
- per-server direct and broker-only classification.

The report excludes built-in local and Admin definitions. It contains public tool
metadata only and does not include credentials or raw definitions.

## Runtime and lifecycle boundary

Each Runtime owns independent upstream clients, upstream HTTP session state,
catalog, search index, and ResultStore. Calls that already hold a client lease may
finish while close waits. New calls after close fail with a retryable disconnect.
There is no manager start, stop, live reload, or profile activation API.

## Explicitly not implemented

The current stable-catalog phases do not implement:

- dynamic `tools/listChanged`;
- Runtime-local start/stop/reload;
- live profile filtering or profile activation;
- vector search;
- a complete `$ref` resolver;
- persistent ResultStore data;
- OAuth-principal-plus-session composite result ownership;
- `expose_mode=auto`.
