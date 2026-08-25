from __future__ import annotations

import unittest

from coding_tools_mcp.admin import ADMIN_API_PREFIX, AdminService


class Router:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _require_conversation_service(self) -> object:
        return object()

    def _workspace_entry(self, workspace_id: str) -> str:
        self.calls.append(("workspace", workspace_id))
        return workspace_id

    def conversation_detail(self, workspace_id: str, conversation_id: str, query: dict[str, str]) -> dict[str, object]:
        self.calls.append(("detail", conversation_id, query))
        return {"ok": True}

    def conversation_continuation(self, workspace_id: str, conversation_id: str) -> dict[str, object]:
        self.calls.append(("continuation", conversation_id))
        return {"ok": True}

    def conversation_handoff(self, workspace_id: str, conversation_id: str) -> dict[str, object]:
        self.calls.append(("handoff", conversation_id))
        return {"ok": True}

    def conversation_validation(
        self,
        workspace_id: str,
        conversation_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(("validation", conversation_id, body["recipe"]))
        return {"ok": True}


class AdminConversationRouteTests(unittest.TestCase):
    def test_detail_and_projection_routes_dispatch_by_workspace_and_id(self) -> None:
        router = Router()

        detail = AdminService.dispatch(
            router,
            "GET",
            f"{ADMIN_API_PREFIX}/conversations/ws-a/conv-a",
            {},
            {"message_page": "2", "context_page": "3"},
        )
        continuation = AdminService.dispatch(
            router,
            "GET",
            f"{ADMIN_API_PREFIX}/conversations/ws-a/conv-a/continuation",
            {},
            {},
        )
        handoff = AdminService.dispatch(
            router,
            "GET",
            f"{ADMIN_API_PREFIX}/conversations/ws-a/conv-a/handoff",
            {},
            {},
        )
        validation = AdminService.dispatch(
            router,
            "POST",
            f"{ADMIN_API_PREFIX}/conversations/ws-a/conv-a/validation",
            {"recipe": "pytest:focus"},
            {},
        )

        self.assertEqual(detail, {"ok": True})
        self.assertEqual(continuation, {"ok": True})
        self.assertEqual(handoff, {"ok": True})
        self.assertEqual(validation, {"ok": True})
        self.assertEqual(
            router.calls,
            [
                ("detail", "conv-a", {"message_page": "2", "context_page": "3"}),
                ("continuation", "conv-a"),
                ("handoff", "conv-a"),
                ("validation", "conv-a", "pytest:focus"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
