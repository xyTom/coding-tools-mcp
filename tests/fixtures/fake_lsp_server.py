from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\n", b"\r\n"}:
            break
        name, separator, value = line.decode("ascii").strip().partition(":")
        if separator:
            headers[name.lower()] = value.strip()
    length = int(headers["content-length"])
    payload = json.loads(sys.stdin.buffer.read(length).decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("request must be an object")
    return payload


def write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def location(uri: str, line: int) -> dict[str, Any]:
    return {
        "uri": uri,
        "range": {
            "start": {"line": line, "character": 0},
            "end": {"line": line, "character": 7},
        },
    }


def main() -> int:
    document_uri = ""
    while True:
        message = read_message()
        if message is None:
            return 0
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "capabilities": {
                            "definitionProvider": True,
                            "documentSymbolProvider": True,
                            "referencesProvider": True,
                            "diagnosticProvider": {
                                "interFileDependencies": False,
                                "workspaceDiagnostics": False,
                            },
                        }
                    },
                }
            )
        elif method == "textDocument/didOpen":
            params = message.get("params") or {}
            document_uri = params.get("textDocument", {}).get("uri", "")
        elif method == "textDocument/didChange":
            params = message.get("params") or {}
            document_uri = params.get("textDocument", {}).get("uri", document_uri)
        elif method == "textDocument/documentSymbol":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": [
                        {
                            "name": "Greeter",
                            "kind": 5,
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 4, "character": 20},
                            },
                            "selectionRange": {
                                "start": {"line": 0, "character": 6},
                                "end": {"line": 0, "character": 13},
                            },
                            "children": [
                                {
                                    "name": "greet",
                                    "kind": 6,
                                    "range": {
                                        "start": {"line": 1, "character": 4},
                                        "end": {"line": 2, "character": 22},
                                    },
                                    "selectionRange": {
                                        "start": {"line": 1, "character": 8},
                                        "end": {"line": 1, "character": 13},
                                    },
                                }
                            ],
                        }
                    ],
                }
            )
        elif method == "textDocument/definition":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": location(document_uri, 1),
                }
            )
        elif method == "textDocument/references":
            outside_uri = (Path.cwd().parent / "outside.py").resolve().as_uri()
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": [
                        location(document_uri, 1),
                        location(document_uri, 5),
                        location(document_uri, 6),
                        location(outside_uri, 0),
                    ],
                }
            )
        elif method == "textDocument/diagnostic":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "kind": "full",
                        "items": [
                            {
                                "range": {
                                    "start": {"line": 6, "character": 0},
                                    "end": {"line": 6, "character": 7},
                                },
                                "severity": 1,
                                "code": "fake-error",
                                "source": "fake-lsp",
                                "message": "diagnostic-message-that-is-intentionally-long",
                            }
                        ],
                    },
                }
            )
        elif method == "shutdown":
            write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
        elif method == "exit":
            return 0
        elif request_id is not None:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unsupported method {method}"},
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
