import os
import tempfile
import time
import unittest
from unittest.mock import patch

import defunc
from storage import (
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
        a = defunc.SessionState(
            session_file="a.session",
            blocked_until=now + 120,
            status="flood_wait",
        )
        b = defunc.SessionState(session_file="b.session")
        chosen = defunc._pick_best_session([a, b])
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.session_file, "b.session")

    def test_picker_respects_run_exclusions(self):
        a = defunc.SessionState(session_file="a.session")
        b = defunc.SessionState(session_file="b.session")
        chosen = defunc._pick_best_session(
            [a, b], excluded={"a.session"}
        )
        self.assertEqual(chosen.session_file, "b.session")

    def test_hour_limit_returns_next_due(self):
        now = time.time()
        st = defunc.SessionState(
            session_file="a.session",
            hour_window_start=now - 10,
            hour_count=5,
        )
        due = defunc.session_next_time_due_to_limits(st, 5, 0)
        self.assertGreater(due, now)

    def test_session_status_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.db")
            with patch.object(defunc, "LEDGER_DB", db):
                conn = defunc._db()
                states = defunc.session_stats_load(conn, ["a.session"])
                st = states["a.session"]
                st.status = "flood_wait"
                st.status_reason = "45s"
                st.blocked_until = time.time() + 45
                defunc.session_stats_save(conn, st)
                loaded = defunc.session_stats_load(
                    conn, ["a.session"]
                )["a.session"]
                self.assertEqual(loaded.status, "flood_wait")
                self.assertEqual(loaded.status_reason, "45s")
                conn.close()


if __name__ == "__main__":
    unittest.main()
