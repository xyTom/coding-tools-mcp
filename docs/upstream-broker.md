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

Schema containment is deliberately widen-only. If an `enum`, `anyOf`, or
`oneOf` exceeds its public budget, the restrictive keyword is removed rather
than truncated. `allOf` may drop excess branches because that only removes
constraints. When declared properties are omitted, restrictive
`additionalProperties` rules are also removed. Numeric `const`, `enum`, and
`uniqueItems` comparisons remain exact beyond the Decimal context precision.

The two passthrough call tools intentionally omit `outputSchema`: a valid upstream
result may contain only `content` and `isError`, without `structuredContent`.

`upstream_tool_call_mutating` is always visible and truthfully annotated with
`readOnlyHint=false`, `destructiveHint=true`, and `openWorldHint=true`. The
fake-readonly compatibility override does not rewrite this mutating route; other
local compatibility annotations may still be displayed as read-only. It is not a
security boundary. Remote capabilities remain governed by the upstream
server's own authorization and side-effect model.

## Oversized results

All upstream results are normalized and constrained to the final inline MCP
budget. For oversized Broker calls with a concrete owner, the original normalized
UTF-8 JSON envelope is stored before truncation and the inline result includes a
short-lived handle for `upstream_result_fetch`. Handle metadata is reserved during
budgeting, retained by minimal fallbacks, and included in the final serialized
128,000-byte limit check.

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

## Strict transport JSON

MCP stdio and HTTP, Admin JSON bodies, OAuth dynamic client registration, and
upstream stdio/HTTP/SSE use the same strict decoder. Byte input is decoded
explicitly as UTF-8, so UTF-16 and UTF-32 payloads are rejected. A JSON integer
may contain at most 4,300 digits, and that limit remains project-owned even when
the host lowers Python's process-global integer-string limit. Floating-point
literals that overflow to a non-finite Python value, including `1e309`, are
rejected. Decoder recursion failures are normalized to parse/protocol errors,
so deeply nested input cannot terminate stdio loops or HTTP request handlers.
Schema assertion comparison is separately bounded by the sanitizer containment depth, so deeply nested but parseable `oneOf`/`not` branches cannot escape as `RecursionError`.
Upstream result sizing preserves escaped unpaired surrogates through an ASCII JSON fallback, while ordinary Chinese and emoji remain real UTF-8. Upstream stdio reads the binary pipe with a 1 MiB per-frame limit; oversized frames become `UPSTREAM_RESPONSE_TOO_LARGE`, are drained in bounded chunks, and the next LF-delimited response remains readable. Upstream `structuredContent`, when present, must be a JSON object. Calls made after an upstream client or Manager has closed return retryable `UPSTREAM_NOT_AVAILABLE`; transport disconnects remain `UPSTREAM_DISCONNECTED`.
Upstream discovery sanitizes untrusted Schema metadata before any deep snapshot operation, and raw/public definitions are frozen and thawed for mutable `deepcopy()` export with iterative traversal. JSON container depth is defined uniformly: the root dict/list is level 1, every child dict/list adds one level, scalars add no level, and the 65th container is rejected. Upstream results, `structuredContent`, JSON-RPC error trees, and error details use the same 64-container boundary. Response IDs and JSON-RPC error `code` values require exact integers; booleans, floats, strings, and missing values are protocol errors. Error details are bounded independently for each untrusted top-level value without charging Gateway or status wrappers. Status export preserves safe code/message/category/retryable fields and bounds only details. Excessive nesting, NaN/Infinity, or integers beyond the 4,300-digit limit become bounded omissions. Cycles or shared containers inside one detail value are omitted; cross-top-level Python identity sharing is normalized independently by value.
Production MCP stdio serializes ordinary Unicode as real UTF-8. If a response
contains an unpaired surrogate code unit, serialization falls back to
`ensure_ascii=True`, preserving it as a JSON `\ud800`/`\udc00` escape and
keeping the stdio request loop alive.
The production MCP stdio server reads `sys.stdin.buffer` and writes
`sys.stdout.buffer`, framing raw UTF-8 JSON-RPC bytes with a single LF. It does
not depend on the host console code page or Python's text-wrapper encoding.

## Operational report

`server_info` and the authenticated Admin Gateway payload include an
upstream-only exposure report:

- direct tool count and public-definition bytes;
- catalog total and broker-only count;
- the largest sanitized public definitions;
- per-server direct and broker-only classification.

The WebUI consumes only these per-server aggregate counts; it does not expect or render a `servers[].tools` array. Draft include/exclude/pin settings are shown separately and take effect only in a new Runtime.

The report excludes built-in local and Admin definitions. It contains public tool
metadata only and does not include credentials or raw definitions.

## Runtime and lifecycle boundary

Each Runtime owns independent upstream clients, upstream HTTP session state,
catalog, search index, and ResultStore. Calls that already hold a client lease may
finish while close waits. New calls after close fail with retryable `UPSTREAM_NOT_AVAILABLE`; actual pipe/socket disconnects remain retryable `UPSTREAM_DISCONNECTED`.
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
