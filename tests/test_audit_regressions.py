import os
import tempfile
import unittest
from types import SimpleNamespace

import inviter
from storage import (
    connect_db,
    list_user_sources,
    load_user_candidates,
    upsert_user,
)


class StorageRegressionTests(unittest.TestCase):
    def _user(self, uid, username=None):
        return SimpleNamespace(
            id=uid,
            username=username,
            first_name="Test",
            last_name="User",
        )

    def test_inviter_tables_are_part_of_canonical_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_db(os.path.join(tmp, "schema.db"))
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("session_stats", tables)
            self.assertIn("invites", tables)
            self.assertIn("invite_state", tables)
            self.assertIn("invite_events", tables)
            conn.close()

    def test_last_seen_never_moves_backwards_and_parsed_at_is_parse_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_db(os.path.join(tmp, "seen.db"))
            user = self._user(1, "one")
            upsert_user(
                conn,
                user,
                source_id="source",
                source_title="Source",
                source_type="messages",
                seen_at="2026-10-01T12:00:00+00:00",
            )
            conn.commit()
            upsert_user(
                conn,
                user,
                source_id="source",
                source_title="Source",
                source_type="messages",
                seen_at="2025-01-01T12:00:00+00:00",
            )
            conn.commit()

            parsed_at, last_seen = conn.execute(
                "SELECT parsed_at, last_seen_at FROM users WHERE user_id=1"
            ).fetchone()
            self.assertEqual(last_seen, "2026-10-01T12:00:00+00:00")
            self.assertNotEqual(parsed_at, "2026-10-01T12:00:00+00:00")
            conn.close()

    def test_candidate_prefers_session_that_observed_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_db(os.path.join(tmp, "session.db"))
            user = self._user(2, None)
            upsert_user(
                conn,
                user,
                source_id="source",
                source_title="Source",
                source_type="participants",
                session_file="/tmp/sessoins/account_a.session",
            )
            conn.commit()
            candidate = load_user_candidates(conn)[0]
            self.assertEqual(candidate.preferred_session, "account_a.session")
            conn.close()

    def test_queue_can_be_filtered_by_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_db(os.path.join(tmp, "source.db"))
            upsert_user(
                conn,
                self._user(10, "a"),
                source_id="A",
                source_title="Source A",
                source_type="participants",
            )
            upsert_user(
                conn,
                self._user(20, "b"),
                source_id="B",
                source_title="Source B",
                source_type="messages",
            )
            conn.commit()

            only_a = load_user_candidates(conn, source_id="A")
            self.assertEqual([candidate.user_id for candidate in only_a], [10])

            sources = list_user_sources(conn)
            self.assertEqual(
                {source["source_id"] for source in sources},
                {"A", "B"},
            )
            conn.close()


class RpcClassificationTests(unittest.TestCase):
    def _named_error(self, name):
        return type(name, (Exception,), {})()

    def test_terminal_rpc_classification(self):
        self.assertEqual(
            inviter._classify_rpc_error(
                self._named_error("UsersTooMuchError")
            ),
            "target_full",
        )
        self.assertEqual(
            inviter._classify_rpc_error(
                self._named_error("AuthKeyUnregisteredError")
            ),
            "session_dead",
        )
        self.assertEqual(
            inviter._classify_rpc_error(
                self._named_error("InputUserDeactivatedError")
            ),
            "user_deactivated",
        )


if __name__ == "__main__":
    unittest.main()
