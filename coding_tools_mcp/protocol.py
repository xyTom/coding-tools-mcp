from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import JsonRpcError


# The two eras negotiate their version through different channels and must not
# borrow each other's values: legacy versions are agreed once by ``initialize``,
# modern versions travel in the ``_meta`` of every request.
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
LATEST_LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]
MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)
KNOWN_PROTOCOL_VERSIONS = (*MODERN_PROTOCOL_VERSIONS, *LEGACY_PROTOCOL_VERSIONS)
LEGACY_ERA = "legacy"
MODERN_ERA = "modern"

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

UNSUPPORTED_PROTOCOL_VERSION = -32022
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
HEADER_MISMATCH = -32020

# SEP-2243 lets a gateway route a modern request on its headers alone, so the
# headers must mirror the body they travel with. These methods name their
# subject in the body, and the name is mirrored in ``Mcp-Name``; the two this
# server does not implement are listed as well, because the mirror is a
# property of the request, not of what we can answer.
MIRRORED_NAME_METHODS = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}
BASE64_SENTINEL_PREFIX = "=?base64?"
BASE64_SENTINEL_SUFFIX = "?="
# A mirrored subject is a tool name, a URI, or a prompt name. Nothing this
# server answers needs more than a few hundred characters of it, and the
# decode happens before the body is compared, so the payload is capped.
BASE64_SENTINEL_MAX_PAYLOAD = 8192

# The method a dual-era client probes with before it decides to handshake.
DISCOVER_METHOD = "server/discover"
MODERN_METHODS = frozenset(
    {
        DISCOVER_METHOD,
        "notifications/cancelled",
        "ping",
        "tools/list",
        "tools/call",
    }
)
MODERN_CACHEABLE_METHODS = frozenset({DISCOVER_METHOD, "tools/list"})
MODERN_RESULT_TYPE = "complete"

# The only two ``clientInfo`` fields anything reads, and the length a label
# built from one is worth carrying.
CLIENT_INFO_KEYS = ("name", "version")
CLIENT_INFO_VALUE_LIMIT = 200


@dataclass(frozen=True)
class RequestContext:
    """Per-request facts that transports hand to the runtime.

    One runtime serves concurrent clients, so a request carries its own
    context instead of parking it on runtime state. ``frozen`` freezes only
    the top level, so ``client_info`` is not the object the request carried:
    it is a small dict rebuilt from the two string fields anything reads,
    which is both bounded and immune to a later mutation of the request body.
    """

    era: str = LEGACY_ERA
    protocol_version: str = LATEST_LEGACY_PROTOCOL_VERSION
    client_info: Mapping[str, Any] | None = None


def jsonrpc_error(
    request_id: str | int | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """Build a JSON-RPC error response envelope."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def invalid_request_response() -> dict[str, Any]:
    return jsonrpc_error(None, -32600, "Invalid Request")


def response_id(request: dict[str, Any]) -> str | int | None:
    """Return a response-safe JSON-RPC id, using null for invalid or absent ids."""

    value = request.get("id")
    if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    return None


def validate_rpc_envelope(request: dict[str, Any]) -> None:
    if request.get("jsonrpc") != "2.0":
        raise JsonRpcError(-32600, "Invalid Request: jsonrpc must be 2.0", {"reason": "jsonrpc_version"})
    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(-32600, "Invalid Request: method must be a string", {"reason": "method"})
    if "id" in request and not (
        request["id"] is None
        or isinstance(request["id"], str)
        or (isinstance(request["id"], int) and not isinstance(request["id"], bool))
    ):
        raise JsonRpcError(-32600, "Invalid Request: id must be string, integer, or null", {"reason": "id"})


def rpc_params(request: dict[str, Any]) -> dict[str, Any]:
    params = request.get("params", {})
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise JsonRpcError(-32602, "MCP method params must be an object")
    return params


def validate_initialize_params(params: dict[str, Any]) -> str:
    """Negotiate the legacy handshake version, downgrading what we cannot speak.

    The handshake spec requires the server to answer a version it does not
    support with one it does, so every unsupported value — including the modern
    ``2026-07-28``, which is carried per request instead of negotiated — comes
    back as the newest legacy version rather than as an error.
    """

    requested = params.get("protocolVersion")
    if legacy_protocol_version_is_supported(requested):
        return str(requested)
    return LATEST_LEGACY_PROTOCOL_VERSION


def validate_initialize_request(request: dict[str, Any]) -> None:
    if "id" not in request or request.get("id") is None:
        raise JsonRpcError(-32600, "initialize must be a JSON-RPC request with a non-null id")


def legacy_protocol_version_is_supported(version: Any) -> bool:
    return isinstance(version, str) and version in LEGACY_PROTOCOL_VERSIONS


def protocol_version_is_known(version: Any) -> bool:
    return isinstance(version, str) and version in KNOWN_PROTOCOL_VERSIONS


def decode_mirror_header(value: str) -> str:
    """Read one mirror header value, unwrapping a base64 sentinel if present.

    A value that cannot travel as an HTTP field is wrapped as
    ``=?base64?<payload>?=``. The affixes are matched exactly, so a value that
    merely resembles one is compared as the literal it is.
    """

    if not (value.startswith(BASE64_SENTINEL_PREFIX) and value.endswith(BASE64_SENTINEL_SUFFIX)):
        return value
    payload = value[len(BASE64_SENTINEL_PREFIX) : -len(BASE64_SENTINEL_SUFFIX)]
    if len(payload) > BASE64_SENTINEL_MAX_PAYLOAD:
        raise JsonRpcError(
            HEADER_MISMATCH,
            f"Mirror header carries a base64 sentinel longer than {BASE64_SENTINEL_MAX_PAYLOAD} characters",
            {"reason": "oversized", "max_length": BASE64_SENTINEL_MAX_PAYLOAD},
        )
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except ValueError as exc:  # binascii.Error and UnicodeDecodeError both subclass it
        raise JsonRpcError(
            HEADER_MISMATCH,
            "Mirror header carries a base64 sentinel that does not decode to UTF-8",
            {"reason": "invalid_base64"},
        ) from exc


def validate_mirror_headers(
    era: str,
    method: str,
    params: Mapping[str, Any],
    *,
    version_header: str | None,
    method_header: str | None,
    name_header: str | None,
) -> None:
    """Check that a request's headers mirror the body they travel with.

    SEP-2243 asks a modern request to restate its version, method, and subject
    in headers so a gateway can route on them alone. We read the body first
    and enforce the mirror against it, which gives up part of that intent but
    is the only way to tell the two eras apart: a legacy request carries no
    such headers and is left alone, except that a modern version header over a
    legacy body is a mismatch like any other.
    """

    if era != MODERN_ERA:
        if version_header in MODERN_PROTOCOL_VERSIONS:
            raise _mirror_error(
                "MCP-Protocol-Version",
                f"MCP-Protocol-Version {version_header} needs a request that states the same "
                f"version in params._meta.{META_PROTOCOL_VERSION}",
                "body_is_not_modern",
            )
        return

    meta = params.get("_meta")
    meta_version = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
    if version_header is None:
        raise _mirror_error(
            "MCP-Protocol-Version",
            "MCP-Protocol-Version is required and must repeat the version in params._meta",
            "missing",
        )
    if version_header != meta_version:
        raise _mirror_error(
            "MCP-Protocol-Version",
            "MCP-Protocol-Version does not match the version in params._meta",
            "mismatch",
        )
    if method_header is None:
        raise _mirror_error("Mcp-Method", "Mcp-Method is required and must repeat the request method", "missing")
    if method_header != method:
        raise _mirror_error("Mcp-Method", "Mcp-Method does not match the request method", "mismatch")
    subject = MIRRORED_NAME_METHODS.get(method)
    if subject is None:
        return
    if name_header is None:
        raise _mirror_error(
            "Mcp-Name",
            f"Mcp-Name is required for {method} and must repeat params.{subject}",
            "missing",
        )
    if decode_mirror_header(name_header) != params.get(subject):
        raise _mirror_error("Mcp-Name", f"Mcp-Name does not match params.{subject}", "mismatch")


def _mirror_error(header: str, message: str, reason: str) -> JsonRpcError:
    return JsonRpcError(HEADER_MISMATCH, message, {"header": header, "reason": reason})


def request_era(method: str, params: Mapping[str, Any]) -> str:
    """Decide which protocol era a request belongs to.

    The only signal is a ``_meta`` carrying the modern protocol version key.
    ``initialize`` is the one exception and is always legacy: a client that
    sends a handshake is asking for one, whatever its ``_meta`` says. Legacy
    requests carry unrelated ``_meta`` entries such as ``progressToken``, so the
    key must be present, not merely the ``_meta`` object.
    """

    if method == "initialize":
        return LEGACY_ERA
    meta = params.get("_meta")
    if isinstance(meta, dict) and META_PROTOCOL_VERSION in meta:
        return MODERN_ERA
    return LEGACY_ERA


def validate_modern_meta(params: Mapping[str, Any]) -> str:
    """Check every ``_meta`` field a modern request owes, and return its version.

    Only called once :func:`request_era` has found the protocol version key, so
    ``_meta`` is known to be an object that carries it. A transport may run
    this before its own checks so that a request which is wrong in both ways
    is answered with the protocol's verdict rather than the transport's.
    """

    meta = params["_meta"]
    version = meta[META_PROTOCOL_VERSION]
    if not isinstance(version, str):
        raise JsonRpcError(
            -32602,
            f"{META_PROTOCOL_VERSION} must be a string",
            {"reason": "protocol_version"},
        )
    if version not in MODERN_PROTOCOL_VERSIONS:
        raise JsonRpcError(
            UNSUPPORTED_PROTOCOL_VERSION,
            f"Unsupported MCP protocol version in _meta: {version}",
            {"supported": list(MODERN_PROTOCOL_VERSIONS), "received": version},
        )
    if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
        raise JsonRpcError(
            -32602,
            f"{META_CLIENT_CAPABILITIES} is required and must be an object",
            {"reason": "client_capabilities"},
        )
    if META_CLIENT_INFO in meta and not isinstance(meta[META_CLIENT_INFO], dict):
        raise JsonRpcError(
            -32602,
            f"{META_CLIENT_INFO} must be an object when present",
            {"reason": "client_info"},
        )
    return version


def bounded_client_info(declared: Mapping[str, Any]) -> dict[str, str]:
    """Rebuild a client's self-description as a small, flat dict.

    A client controls both the shape and the size of what it declares, so
    nothing it sent is carried onwards: only ``name`` and ``version`` are
    read, only when they are strings, and each is truncated. Copying the
    object instead would let an arbitrarily nested one travel with every
    request that carried it.
    """

    bounded: dict[str, str] = {}
    for key in CLIENT_INFO_KEYS:
        value = declared.get(key)
        if isinstance(value, str):
            bounded[key] = value[:CLIENT_INFO_VALUE_LIMIT]
    return bounded


def modern_request_context(params: Mapping[str, Any]) -> RequestContext:
    """Validate a modern request's ``_meta`` and turn it into a context."""

    version = validate_modern_meta(params)
    declared = params["_meta"].get(META_CLIENT_INFO)
    return RequestContext(
        era=MODERN_ERA,
        protocol_version=version,
        client_info=bounded_client_info(declared) if isinstance(declared, dict) else None,
    )


def shape_result(
    context: RequestContext,
    method: str,
    result: dict[str, Any],
    server_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Encode one successful result for the era that asked for it.

    Handlers return business fields only; every era-specific field is added
    here so no handler can add one twice. A legacy result is returned as it
    was built — a client that never spoke the modern protocol must not receive
    fields its schema does not know. A modern result is decorated on a shallow
    copy so the runtime's own dict is left alone.
    """

    if context.era != MODERN_ERA:
        return result
    shaped = dict(result)
    shaped["resultType"] = MODERN_RESULT_TYPE
    carried = shaped.get("_meta")
    meta = dict(carried) if isinstance(carried, dict) else {}
    meta[META_SERVER_INFO] = dict(server_identity)
    shaped["_meta"] = meta
    if method in MODERN_CACHEABLE_METHODS:
        # A catalog is shaped by the workspace and the permission mode it was
        # served under, and a discover result quotes the workspace's own
        # instruction files, so the conservative defaults apply to both:
        # never shared, never reused.
        shaped["ttlMs"] = 0
        shaped["cacheScope"] = "private"
    return shaped


def dispatch_rpc(
    runtime: Any,
    request: dict[str, Any],
    *,
    transport_protocol_version: str | None = None,
) -> dict[str, Any] | None:
    """Dispatch one MCP JSON-RPC request against a runtime, shared by all transports.

    The era is decided first, from the request itself: a modern request states
    its protocol version per request, a legacy one negotiated it through a
    handshake this runtime keeps no record of. Neither era leaves state behind,
    so one runtime answers every client of the workspace. Transports add only
    their transport-specific framing (stream handling, status codes) around
    this, and may report the legacy version their framing negotiated through
    ``transport_protocol_version``; it reaches the runtime in the request
    context and is not acted on, echoed, or recorded today.

    Returns None for notifications and requests without an id. A notification
    that fails is answered with nothing at all, as JSON-RPC requires: only a
    message whose envelope is too malformed to tell a notification from a
    request is answered with an error carrying a null id.
    """

    try:
        validate_rpc_envelope(request)
    except JsonRpcError as exc:
        return jsonrpc_error(response_id(request), exc.code, exc.message, exc.data)
    is_notification = "id" not in request
    try:
        method = request["method"]
        params = rpc_params(request)
        era = request_era(method, params)
        # Before the method runs: a first request that fails must still be
        # able to report the failure it caused.
        runtime.telemetry.record_request(era, method)
        if era == MODERN_ERA:
            context = modern_request_context(params)
            result = _dispatch_modern(runtime, method, params, context)
        else:
            context = RequestContext(
                era=LEGACY_ERA,
                protocol_version=transport_protocol_version or LATEST_LEGACY_PROTOCOL_VERSION,
            )
            result = _dispatch_legacy(runtime, request, method, params, context)
        request_id = request.get("id")
        if result is None or request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": shape_result(context, method, result, runtime.server_identity()),
        }
    except JsonRpcError as exc:
        if is_notification:
            return None
        return jsonrpc_error(response_id(request), exc.code, exc.message, exc.data)


def _dispatch_modern(
    runtime: Any,
    method: str,
    params: dict[str, Any],
    context: RequestContext,
) -> dict[str, Any] | None:
    """Handle a request that carries its own protocol version.

    There is no handshake to be missing, so the initialized guard never applies
    here. Returns None for a notification.
    """

    if method not in MODERN_METHODS:
        raise JsonRpcError(-32601, f"Unknown method: {method}")
    if method == "notifications/cancelled":
        # Accepted and acknowledged by staying silent, as in the legacy era.
        return None
    if method == "ping":
        return {}
    if method == DISCOVER_METHOD:
        return runtime.discover_payload()
    if method == "tools/list":
        return runtime.list_tools()
    return _call_tool(runtime, params, context)


def _dispatch_legacy(
    runtime: Any,
    request: dict[str, Any],
    method: str,
    params: dict[str, Any],
    context: RequestContext,
) -> dict[str, Any] | None:
    """Handle a request that negotiated its version through ``initialize``.

    No handshake state is kept, so nothing here depends on what a client sent
    before: a method this server does not implement is unknown, and every other
    method is served whether or not the client handshook first. ``initialize``
    is therefore idempotent — it negotiates a version and answers with it as
    often as it is asked, which is what a connector that probes, falls back,
    and handshakes again needs. ``server/discover`` is deliberately absent: it
    answers for the modern era only, so a probe that states no protocol version
    is unknown here and the client falls back to the handshake, which is a
    working path rather than a failure. Returns None for a notification.
    """

    if method == "initialize":
        validate_initialize_request(request)
        negotiated_version = validate_initialize_params(params)
        client_info = params.get("clientInfo")
        return runtime.initialize(
            client_info if isinstance(client_info, dict) else None,
            negotiated_version,
        )
    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        # Accepted and acknowledged by staying silent. A command outlives
        # the request that started it and is shared with every other
        # client of this workspace, so cancelling a request must not kill
        # it; clients terminate a command with kill_command.
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return runtime.list_tools()
    if method == "tools/call":
        return _call_tool(runtime, params, context)
    raise JsonRpcError(-32601, f"Unknown method: {method}")


def _call_tool(runtime: Any, params: dict[str, Any], context: RequestContext) -> dict[str, Any]:
    """Validate tools/call parameters, record rejected arguments, and dispatch the call."""
    if not isinstance(params.get("name"), str):
        raise JsonRpcError(-32602, "tools/call requires a tool name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        # Rejected before the runtime sees the call, so tell its telemetry.
        record_rejected = getattr(runtime, "record_rejected_tool_call", None)
        if callable(record_rejected):
            record_rejected(params["name"], context=context)
        raise JsonRpcError(-32602, "tools/call arguments must be an object")
    return runtime.call_tool(params["name"], arguments, context=context)
