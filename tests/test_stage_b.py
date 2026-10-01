import os
import tempfile
import time
import unittest
from unittest.mock import patch

import inviter
from storage import (
    UserCandidate,
    connect_db,
    invite_event_count,
    invite_record,
    invite_state_get,
)


class InviteStorageTests(unittest.TestCase):
    def test_events_are_append_only_and_state_is_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_db(os.path.join(tmp, "test.db"))
            invite_record(
                conn,
                target="@target",
                user_key="id:42",
                user_id=42,
                username="tester",
                status="floodwait",
                reason="30",
                session_file="a.session",
                flood_seconds=30,
            )
            invite_record(
                conn,
                target="@target",
                user_key="id:42",
                user_id=42,
                username="tester",
                status="ok",
                reason="invited",
                session_file="b.session",
            )
            self.assertEqual(invite_event_count(conn, "@target", "id:42"), 2)
            self.assertEqual(
                invite_state_get(conn, "@target", "id:42"),
                ("ok", "invited"),
            )
            attempts = conn.execute(
                "SELECT attempt_count FROM invite_state "
                "WHERE target=? AND user_key=?",
                ("@target", "id:42"),
            ).fetchone()[0]
            self.assertEqual(attempts, 2)
            conn.close()


class SessionSchedulerTests(unittest.TestCase):
    def test_picker_prefers_ready_session(self):
        now = time.time()
        a = inviter.SessionState(
            session_file="a.session",
            blocked_until=now + 120,
            status="flood_wait",
        )
        b = inviter.SessionState(session_file="b.session")
        chosen = inviter._pick_best_session([a, b])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.session_file, "b.session")

    def test_picker_respects_run_exclusions(self):
        a = inviter.SessionState(session_file="a.session")
        b = inviter.SessionState(session_file="b.session")
        chosen = inviter._pick_best_session(
            [a, b], excluded={"a.session"}
        )
        self.assertEqual(chosen.session_file, "b.session")

    def test_picker_handles_equal_fresh_sessions(self):
        a = inviter.SessionState(session_file="a.session")
        b = inviter.SessionState(session_file="b.session")
        chosen = inviter._pick_best_session([b, a])
        self.assertEqual(chosen.session_file, "a.session")

    def test_hour_limit_returns_next_due(self):
        now = time.time()
        st = inviter.SessionState(
            session_file="a.session",
            hour_window_start=now - 10,
            hour_count=5,
        )
        due = inviter.session_next_time_due_to_limits(st, 5, 0)
        self.assertGreater(due, now)

    def test_session_status_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.db")
            with patch.object(defunc, "LEDGER_DB", db):
                conn = inviter._db()
                states = inviter.session_stats_load(conn, ["a.session"])
                st = states["a.session"]
                st.status = "flood_wait"
                st.status_reason = "45s"
                st.blocked_until = time.time() + 45
                inviter.session_stats_save(conn, st)
                loaded = inviter.session_stats_load(
                    conn, ["a.session"]
                )["a.session"]
                self.assertEqual(loaded.status, "flood_wait")
                self.assertEqual(loaded.status_reason, "45s")
                conn.close()


class RetryIntegrationTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, fail_network=False):
            self.fail_network = fail_network
            self.calls = 0
            self.disconnected = False

        def __call__(self, request):
            self.calls += 1
            if self.fail_network:
                raise ConnectionError("simulated")
            return object()

        def disconnect(self):
            self.disconnected = True

    def test_same_user_retries_on_second_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "retry.db")
            clients = {
                "a.session": self.FakeClient(fail_network=True),
                "b.session": self.FakeClient(fail_network=False),
            }

            def make_client(session_file, api_id, api_hash):
                return clients[session_file]

            with (
                patch.object(defunc, "LEDGER_DB", db),
                patch.object(defunc, "_make_client", side_effect=make_client),
                patch.object(
                    defunc, "resolve_target_for_client", return_value=object()
                ),
                patch.object(
                    defunc, "resolve_user_for_client", return_value=object()
                ),
                patch.object(inviter.time, "sleep", return_value=None),
            ):
                inviter.inviting_rotate_sessions(
                    api_id=1,
                    api_hash="hash",
                    session_files=["a.session", "b.session"],
                    target="@target",
                    users=[UserCandidate(42, "tester")],
                    base_delay=1.0,
                    jitter_min=0.0,
                    jitter_max=0.0,
                    max_user_attempts=2,
                    max_attempts_per_session=1,
                )

            conn = connect_db(db)
            self.assertEqual(
                invite_state_get(conn, "@target", "id:42"),
                ("ok", "invited"),
            )
            self.assertEqual(
                invite_event_count(conn, "@target", "id:42"), 2
            )
            event_sessions = [
                row[0]
                for row in conn.execute(
                    "SELECT session_file FROM invite_events "
                    "WHERE target=? AND user_key=? ORDER BY id",
                    ("@target", "id:42"),
                ).fetchall()
            ]
            self.assertEqual(event_sessions, ["a.session", "b.session"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
