import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main as main_mod

import inviter
from storage import (
    connect_db,
    get_or_create_log_hmac_key,
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
            # Same user also appears in a different source/type combination.
            upsert_user(
                conn,
                self._user(10, "a"),
                source_id="B",
                source_title="Source B",
                source_type="messages",
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

            impossible_pair = load_user_candidates(
                conn,
                source_id="A",
                source_type="messages",
            )
            self.assertEqual(impossible_pair, [])

            sources = list_user_sources(conn)
            self.assertEqual(
                {source["source_id"] for source in sources},
                {"A", "B"},
            )
            conn.close()


class LimitRegressionTests(unittest.TestCase):
    def test_negative_limits_are_treated_as_disabled(self):
        st = inviter.SessionState(
            session_file="a.session",
            hour_window_start=1,
            hour_count=99,
            day_window_start=1,
            day_count=99,
        )
        self.assertEqual(
            inviter.session_next_time_due_to_limits(st, -1, -5),
            0,
        )
        before_hour = st.hour_count
        before_day = st.day_count
        inviter.session_consume_invite_token(st, -1, -5)
        self.assertEqual(st.hour_count, before_hour)
        self.assertEqual(st.day_count, before_day)


class SchemaMigrationTests(unittest.TestCase):
    def test_v6_positive_numeric_checkpoint_is_reset_on_v7(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "migration.db")
            conn = connect_db(db)
            conn.execute(
                """
                INSERT OR REPLACE INTO parser_checkpoints(
                    checkpoint_key, source_id, source_title, mode,
                    cursor_int, processed, saved, status, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    "messages:123",
                    "123",
                    "Legacy",
                    "messages",
                    99,
                    10,
                    5,
                    "running",
                    "2026-10-01T00:00:00+00:00",
                ),
            )
            conn.execute(
                "UPDATE schema_meta SET value='6' "
                "WHERE key='schema_version'"
            )
            conn.commit()
            conn.close()

            conn = connect_db(db)
            row = conn.execute(
                "SELECT 1 FROM parser_checkpoints "
                "WHERE checkpoint_key='messages:123'"
            ).fetchone()
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertIsNone(row)
            self.assertEqual(version, "7")
            conn.close()


class PrivacyLabelTests(unittest.TestCase):
    def test_log_hmac_key_is_stable_per_database_and_not_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            db1 = os.path.join(tmp, "one.db")
            db2 = os.path.join(tmp, "two.db")

            conn1 = connect_db(db1)
            key1a = get_or_create_log_hmac_key(conn1)
            key1b = get_or_create_log_hmac_key(conn1)
            label1 = inviter._privacy_user_label("id:42", key1a)
            conn1.close()

            conn2 = connect_db(db2)
            key2 = get_or_create_log_hmac_key(conn2)
            label2 = inviter._privacy_user_label("id:42", key2)
            conn2.close()

            self.assertEqual(key1a, key1b)
            self.assertNotEqual(key1a, key2)
            self.assertNotEqual(label1, label2)
            self.assertTrue(label1.startswith("user#"))


class MenuIsolationTests(unittest.TestCase):
    def test_menu_action_catches_recoverable_exception(self):
        def broken_action():
            raise RuntimeError("private source")

        with (
            patch.object(main_mod.time, "sleep", return_value=None),
            patch("builtins.print"),
        ):
            main_mod._run_menu_action(broken_action)


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
