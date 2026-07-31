from __future__ import annotations

from typing import Any

from .errors import JsonRpcError


PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION, "2025-06-18")


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
    requested = params.get("protocolVersion")
    if requested is None:
        return PROTOCOL_VERSION
    if not protocol_version_is_supported(requested):
        raise JsonRpcError(
            -32602,
            "Unsupported MCP protocol version",
            {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "received": requested},
        )
    return str(requested)


def validate_initialize_request(request: dict[str, Any]) -> None:
    if "id" not in request or request.get("id") is None:
        raise JsonRpcError(-32600, "initialize must be a JSON-RPC request with a non-null id")


def protocol_version_is_supported(version: Any) -> bool:
    return isinstance(version, str) and version in SUPPORTED_PROTOCOL_VERSIONS


def dispatch_rpc(runtime: Any, request: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one MCP JSON-RPC request against a runtime, shared by all transports.

    Handshake state lives on ``runtime.initialized``; transports add only their
    transport-specific framing (session headers, stream handling) around this.
    Returns None for notifications and requests without an id.
    """

    request_id = request.get("id")
    try:
        validate_rpc_envelope(request)
        method = request["method"]
        params = rpc_params(request)
        if not runtime.initialized and method not in {"initialize", "ping"}:
            raise JsonRpcError(-32002, "Server not initialized")
        if method == "initialize":
            if runtime.initialized:
                raise JsonRpcError(-32600, "Server is already initialized")
            validate_initialize_request(request)
            runtime.protocol_version = validate_initialize_params(params)
            client_info = params.get("clientInfo")
            result = runtime.initialize(client_info if isinstance(client_info, dict) else None)
            runtime.initialized = True
        elif method == "notifications/initialized":
            return None
        elif method == "notifications/cancelled":
            cancelled_request_id = params.get("requestId")
            if isinstance(cancelled_request_id, (str, int)) and not isinstance(cancelled_request_id, bool):
                runtime.cancel_request(cancelled_request_id)
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = runtime.list_tools()
        elif method == "tools/call":
            if not isinstance(params.get("name"), str):
                raise JsonRpcError(-32602, "tools/call requires a tool name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise JsonRpcError(-32602, "tools/call arguments must be an object")
            result = runtime.call_tool(params["name"], arguments, request_id=request_id)
        else:
            raise JsonRpcError(-32601, f"Unknown method: {method}")
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except JsonRpcError as exc:
        return jsonrpc_error(response_id(request), exc.code, exc.message, exc.data)
