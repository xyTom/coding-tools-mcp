from __future__ import annotations

import unittest

from coding_tools_mcp.admin_sessions import (
    ADMIN_SESSION_ABSOLUTE_LIFETIME_SECONDS,
    ADMIN_SESSION_IDLE_TIMEOUT_SECONDS,
    AdminSessionStore,
)


class AdminSessionStoreTests(unittest.TestCase):
    def test_defaults_match_security_policy(self) -> None:
        self.assertEqual(ADMIN_SESSION_IDLE_TIMEOUT_SECONDS, 45 * 60)
        self.assertEqual(ADMIN_SESSION_ABSOLUTE_LIFETIME_SECONDS, 8 * 60 * 60)

    def test_session_id_and_csrf_are_stored_only_as_digests(self) -> None:
        values = iter(["raw-session-id", "raw-csrf-token"])
        store = AdminSessionStore(token_factory=lambda: next(values))

        session_id, csrf_token = store.create()

        self.assertEqual(session_id, "raw-session-id")
        self.assertEqual(csrf_token, "raw-csrf-token")
        stored = repr(store._sessions)  # noqa: SLF001 - security invariant test
        self.assertNotIn(session_id, stored)
        self.assertNotIn(csrf_token, stored)
        self.assertTrue(store.authorize(session_id))
        self.assertTrue(
            store.authorize(
                session_id,
                csrf_token=csrf_token,
                require_csrf=True,
            )
        )
        self.assertFalse(
            store.authorize(
                session_id,
                csrf_token="wrong-csrf",
                require_csrf=True,
            )
        )

    def test_idle_timeout_and_absolute_lifetime_fail_closed(self) -> None:
        now = [100.0]
        values = iter(
            [
                "idle-session",
                "idle-csrf",
                "absolute-session",
                "absolute-csrf",
            ]
        )
        store = AdminSessionStore(
            idle_timeout_seconds=10,
            absolute_lifetime_seconds=30,
            clock=lambda: now[0],
            token_factory=lambda: next(values),
        )

        idle_session, _csrf = store.create()
        now[0] = 109.0
        self.assertTrue(store.authorize(idle_session))
        now[0] = 119.0
        self.assertFalse(store.authorize(idle_session))

        now[0] = 200.0
        absolute_session, _csrf = store.create()
        now[0] = 209.0
        self.assertTrue(store.authorize(absolute_session))
        now[0] = 218.0
        self.assertTrue(store.authorize(absolute_session))
        now[0] = 230.0
        self.assertFalse(store.authorize(absolute_session))

    def test_additional_csrf_tokens_support_reload_and_multiple_tabs(self) -> None:
        values = iter(["session", "csrf-a", "csrf-b"])
        store = AdminSessionStore(token_factory=lambda: next(values))
        session_id, first = store.create()

        second = store.issue_csrf(session_id)

        self.assertEqual(second, "csrf-b")
        self.assertTrue(store.authorize(session_id, csrf_token=first, require_csrf=True))
        self.assertTrue(store.authorize(session_id, csrf_token=second, require_csrf=True))

    def test_is_active_does_not_extend_idle_timeout(self) -> None:
        now = [100.0]
        values = iter(["session", "csrf"])
        store = AdminSessionStore(
            idle_timeout_seconds=10,
            absolute_lifetime_seconds=30,
            clock=lambda: now[0],
            token_factory=lambda: next(values),
        )
        session_id, _csrf = store.create()

        now[0] = 109.0
        self.assertTrue(store.is_active(session_id))
        now[0] = 110.0
        self.assertFalse(store.is_active(session_id))

    def test_logout_revokes_session_immediately(self) -> None:
        values = iter(["session", "csrf"])
        store = AdminSessionStore(token_factory=lambda: next(values))
        session_id, _csrf = store.create()

        self.assertTrue(store.revoke(session_id))
        self.assertFalse(store.authorize(session_id))
        self.assertEqual(store.active_count(), 0)


if __name__ == "__main__":
    unittest.main()
