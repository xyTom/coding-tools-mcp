from __future__ import annotations

import json
import sys
from typing import Any


THREAD_ID = "thread-1"
TURN_ID = "turn-1"
APPROVAL_ID = "approval-1"
APPROVAL_REQUEST_ID = 900


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    if not raw_line.strip():
        continue
    request = json.loads(raw_line)
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialized":
        continue
    if method is None and request_id == APPROVAL_REQUEST_ID and "result" in request:
        emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": THREAD_ID,
                    "turn": {"id": TURN_ID, "status": "completed"},
                },
            }
        )
        continue
    if method == "initialize":
        emit(
            {
                "id": request_id,
                "result": {"serverInfo": {"name": "fake-codex", "version": "test"}},
            }
        )
    elif method in {"thread/start", "thread/resume"}:
        emit({"id": request_id, "result": {"thread": {"id": THREAD_ID}}})
    elif method == "turn/start":
        emit(
            {
                "id": request_id,
                "result": {"turn": {"id": TURN_ID, "status": "inProgress"}},
            }
        )
        emit(
            {
                "method": "turn/started",
                "params": {
                    "threadId": THREAD_ID,
                    "turn": {"id": TURN_ID, "status": "inProgress"},
                },
            }
        )
        emit(
            {
                "id": APPROVAL_REQUEST_ID,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "approvalId": APPROVAL_ID,
                    "threadId": THREAD_ID,
                    "turnId": TURN_ID,
                    "command": "echo safe",
                },
            }
        )
    elif method == "thread/list":
        emit({"id": request_id, "result": {"data": [{"id": THREAD_ID}]}})
    elif method in {"turn/interrupt", "thread/unsubscribe"}:
        emit({"id": request_id, "result": {}})
    elif request_id is not None:
        emit(
            {
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported fake method: {method}"},
            }
        )
