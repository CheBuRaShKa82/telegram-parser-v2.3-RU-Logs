import csv
import json
import os
import tempfile
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl.types import PeerChannel, PeerChat

import parser as parser_mod
from storage import checkpoint_get, checkpoint_put, connect_db, export_users_rows


class FakeMessageClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.offsets = []

    def iter_messages(self, entity, limit=None, offset_id=0, **kwargs):
        self.offsets.append(offset_id)
        items = [
            msg for msg in self.messages
            if not offset_id or int(msg.id) < int(offset_id)
        ]
        return iter(items[:limit] if limit else items)

    def get_entity(self, value):
        return value


class FakeCommentsClient:
    def __init__(self, channel, posts, replies, discussion):
        self.channel = channel
        self.posts = posts
        self.replies = replies
        self.discussion = discussion

    def get_entity(self, value):
        return self.channel

    def __call__(self, request):
        return SimpleNamespace(
            full_chat=SimpleNamespace(linked_chat_id=self.discussion.id),
            chats=[self.discussion],
        )

    def iter_messages(
        self, entity, limit=None, offset_id=0, reply_to=None, **kwargs
    ):
        if reply_to is None:
            items = [
                post for post in self.posts
                if not offset_id or int(post.id) < int(offset_id)
            ]
        else:
            items = list(self.replies.get(int(reply_to), []))
        return iter(items[:limit] if limit else items)

    def get_entity(self, value):
        if isinstance(value, int):
            for messages in self.replies.values():
                for msg in messages:
                    if getattr(msg.sender, "id", None) == value:
                        return msg.sender
        return self.channel


class ParserFilterTests(unittest.TestCase):
    def test_default_filter_allows_id_only_user(self):
        user = SimpleNamespace(
            id=1,
            username=None,
            photo=None,
            bot=False,
            deleted=False,
            scam=False,
            fake=False,
            status=None,
        )
        self.assertEqual(parser_mod.quality_user(user)[0], True)

    def test_optional_username_filter_is_enforced(self):
        user = SimpleNamespace(
            id=1,
            username=None,
            photo=None,
            bot=False,
            deleted=False,
            scam=False,
            fake=False,
            status=None,
        )
        cfg = parser_mod.ParserFilterConfig(require_username=True)
        ok, reason = parser_mod.quality_user(user, cfg)
        self.assertFalse(ok)
        self.assertEqual(reason, "нет username")


    def test_empty_photo_object_is_rejected_when_required(self):
        EmptyPhoto = type("UserProfilePhotoEmpty", (), {})
        user = SimpleNamespace(
            id=2,
            username="has_name",
            photo=EmptyPhoto(),
            bot=False,
            deleted=False,
            scam=False,
            fake=False,
            status=None,
        )
        cfg = parser_mod.ParserFilterConfig(require_photo=True)
        ok, reason = parser_mod.quality_user(user, cfg)
        self.assertFalse(ok)
        self.assertEqual(reason, "нет фото")


class SourceMetadataTests(unittest.TestCase):
    def test_manual_source_gets_stable_checkpoint_identity(self):
        source_id, source_title, source_type = parser_mod._source_metadata(
            "@manual_group", "messages"
        )
        self.assertEqual(source_id, "@manual_group")
        self.assertEqual(source_title, "@manual_group")
        self.assertEqual(source_type, "messages")
        self.assertEqual(
            parser_mod._parser_checkpoint_key(
                source_id, source_title, source_type
            ),
            "messages:@manual_group",
        )

    def test_chat_and_channel_same_raw_id_have_distinct_source_ids(self):
        chat_id, _, _ = parser_mod._source_metadata(
            PeerChat(123),
            "participants",
        )
        channel_id, _, _ = parser_mod._source_metadata(
            PeerChannel(123),
            "participants",
        )
        self.assertNotEqual(chat_id, channel_id)
        self.assertEqual(chat_id, "-123")
        self.assertEqual(channel_id, "-1000000000123")


class ParserCheckpointTests(unittest.TestCase):
    def make_user(self, uid, username=None):
        return SimpleNamespace(
            id=uid,
            username=username,
            first_name="Test",
            last_name="User",
            photo=None,
            bot=False,
            deleted=False,
            scam=False,
            fake=False,
            status=None,
        )

    def test_completed_message_checkpoint_starts_fresh(self):
        user1 = self.make_user(101, "one")
        user2 = self.make_user(102, "two")
        messages = [
            SimpleNamespace(id=30, sender_id=101, sender=user1, date=None),
            SimpleNamespace(id=20, sender_id=102, sender=user2, date=None),
        ]
        client = FakeMessageClient(messages)

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "parser.db")
            with patch.object(parser_mod, "DB_PATH", db):
                parser_mod.parsing_from_messages(
                    client,
                    SimpleNamespace(id=777, title="Chat"),
                    parse_id=False,
                    parse_name=False,
                    limit_messages=1,
                    max_age_days=0,
                    checkpoint_batch=1,
                    resume=True,
                )
                parser_mod.parsing_from_messages(
                    client,
                    SimpleNamespace(id=777, title="Chat"),
                    parse_id=False,
                    parse_name=False,
                    limit_messages=10,
                    max_age_days=0,
                    checkpoint_batch=1,
                    resume=True,
                )

            self.assertEqual(client.offsets, [0, 0])
            conn = connect_db(db)
            rows = export_users_rows(conn)
            self.assertEqual({row["user_id"] for row in rows}, {101, 102})
            cp = checkpoint_get(conn, "messages:777")
            self.assertIsNotNone(cp)
            self.assertEqual(cp.status, "completed")
            conn.close()


    def test_error_checkpoint_resumes_from_cursor(self):
        user2 = self.make_user(102, "two")
        client = FakeMessageClient(
            [SimpleNamespace(id=20, sender_id=102, sender=user2, date=None)]
        )

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "resume.db")
            conn = connect_db(db)
            checkpoint_put(
                conn,
                checkpoint_key="messages:777",
                mode="messages",
                source_id="777",
                source_title="Chat",
                cursor_int=30,
                processed=1,
                saved=1,
                status="error:ConnectionError",
            )
            conn.close()

            with patch.object(parser_mod, "DB_PATH", db):
                parser_mod.parsing_from_messages(
                    client,
                    SimpleNamespace(id=777, title="Chat"),
                    parse_id=False,
                    parse_name=False,
                    limit_messages=10,
                    max_age_days=0,
                    checkpoint_batch=1,
                    resume=True,
                )

            self.assertEqual(client.offsets, [30])
            conn = connect_db(db)
            rows = export_users_rows(conn)
            self.assertEqual({row["user_id"] for row in rows}, {102})
            conn.close()

    def test_keyboard_interrupt_commits_current_progress(self):
        user = self.make_user(103, "interrupt")

        class InterruptingClient(FakeMessageClient):
            def iter_messages(self, entity, limit=None, offset_id=0, **kwargs):
                self.offsets.append(offset_id)

                def generator():
                    yield SimpleNamespace(
                        id=30,
                        sender_id=103,
                        sender=user,
                        date=None,
                    )
                    raise KeyboardInterrupt()

                return generator()

        client = InterruptingClient([])
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "interrupt.db")
            with patch.object(parser_mod, "DB_PATH", db):
                with self.assertRaises(KeyboardInterrupt):
                    parser_mod.parsing_from_messages(
                        client,
                        SimpleNamespace(id=777, title="Chat"),
                        parse_id=False,
                        parse_name=False,
                        limit_messages=10,
                        max_age_days=0,
                        checkpoint_batch=100,
                        resume=True,
                    )

            conn = connect_db(db)
            rows = export_users_rows(conn)
            self.assertEqual({row["user_id"] for row in rows}, {103})
            cp = checkpoint_get(conn, "messages:777")
            self.assertEqual(cp.status, "interrupted")
            self.assertEqual(cp.cursor_int, 30)
            conn.close()


class ParserFloodWaitTests(unittest.TestCase):
    class FakeFloodWait(Exception):
        def __init__(self, seconds=120):
            super().__init__(f"FloodWait {seconds}")
            self.seconds = seconds

    def _user(self, uid):
        return SimpleNamespace(
            id=uid,
            username=f"user{uid}",
            first_name="Flood",
            last_name="Test",
            photo=None,
            bot=False,
            deleted=False,
            scam=False,
            fake=False,
            status=None,
        )

    def test_message_sender_floodwait_stops_and_checkpoints(self):
        class Message:
            id = 50
            sender_id = 500
            sender = None
            date = None

            def get_sender(self):
                raise ParserFloodWaitTests.FakeFloodWait(120)

        client = FakeMessageClient([Message()])

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "flood.db")
            with (
                patch.object(parser_mod, "DB_PATH", db),
                patch.object(
                    parser_mod,
                    "FloodWaitError",
                    self.FakeFloodWait,
                ),
            ):
                parser_mod.parsing_from_messages(
                    client,
                    SimpleNamespace(id=777, title="Chat"),
                    parse_id=False,
                    parse_name=False,
                    limit_messages=10,
                    max_age_days=0,
                    checkpoint_batch=100,
                    resume=True,
                )

            conn = connect_db(db)
            cp = checkpoint_get(conn, "messages:777")
            self.assertTrue(cp.status.startswith("error:FloodWait:120"))
            self.assertEqual(cp.cursor_int, None)
            self.assertEqual(export_users_rows(conn), [])
            conn.close()

    def test_failed_first_resolution_does_not_lose_same_sender(self):
        user = self._user(501)

        class FirstMessage:
            id = 50
            sender_id = 501
            sender = None
            date = None

            def get_sender(self):
                raise ValueError("not cached")

        second = SimpleNamespace(
            id=40,
            sender_id=501,
            sender=user,
            date=None,
        )

        class Client(FakeMessageClient):
            def __init__(self):
                super().__init__([FirstMessage(), second])
                self.entity_calls = 0

            def get_entity(self, value):
                if isinstance(value, int):
                    self.entity_calls += 1
                    if self.entity_calls == 1:
                        raise ValueError("temporary resolve miss")
                return value

        client = Client()

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "retry-user.db")
            with patch.object(parser_mod, "DB_PATH", db):
                parser_mod.parsing_from_messages(
                    client,
                    SimpleNamespace(id=777, title="Chat"),
                    parse_id=False,
                    parse_name=False,
                    limit_messages=10,
                    max_age_days=0,
                    checkpoint_batch=100,
                    resume=True,
                )

            conn = connect_db(db)
            rows = export_users_rows(conn)
            self.assertEqual([row["user_id"] for row in rows], [501])
            conn.close()

    def test_manual_source_floodwait_is_not_hidden(self):
        class Client:
            def get_entity(self, value):
                raise ParserFloodWaitTests.FakeFloodWait(60)

        with patch.object(
            parser_mod,
            "FloodWaitError",
            self.FakeFloodWait,
        ):
            with self.assertRaises(self.FakeFloodWait):
                parser_mod._resolve_source_entity(Client(), "@source")


class ParticipantCheckpointTests(unittest.TestCase):
    def test_partial_participant_failure_keeps_saved_users(self):
        user = SimpleNamespace(
            id=777,
            username=None,
            first_name="Partial",
            last_name="User",
            photo=None,
            bot=False,
            deleted=False,
            scam=False,
            fake=False,
            status=None,
        )

        class BrokenParticipantsClient:
            def iter_participants(self, entity):
                yield user
                raise ConnectionError("simulated participant failure")

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "participants.db")
            with patch.object(parser_mod, "DB_PATH", db):
                parser_mod.parsing(
                    BrokenParticipantsClient(),
                    SimpleNamespace(id=321, title="Group"),
                    parse_id=False,
                    parse_name=False,
                    checkpoint_batch=1,
                )

            conn = connect_db(db)
            rows = export_users_rows(conn)
            self.assertEqual([row["user_id"] for row in rows], [777])
            cp = checkpoint_get(conn, "participants:321")
            self.assertIsNotNone(cp)
            self.assertTrue(cp.status.startswith("error:"))
            self.assertEqual(cp.saved, 1)
            conn.close()


class ChannelCommentsTests(unittest.TestCase):
    def test_channel_comments_save_comment_authors(self):
        channel = SimpleNamespace(
            id=900,
            title="News",
            username="news",
            broadcast=True,
        )
        discussion = SimpleNamespace(id=901, title="News discussion")
        user = SimpleNamespace(
            id=555,
            username="commenter",
            first_name="Comment",
            last_name="User",
            photo=None,
            bot=False,
            deleted=False,
            scam=False,
            fake=False,
            status=None,
        )
        post = SimpleNamespace(id=100, date=None)
        reply = SimpleNamespace(
            id=200,
            sender_id=555,
            sender=user,
            date=None,
        )
        client = FakeCommentsClient(
            channel, [post], {100: [reply]}, discussion
        )

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "comments.db")
            with patch.object(parser_mod, "DB_PATH", db):
                parser_mod.parsing_channel_comments(
                    client,
                    channel,
                    parse_id=False,
                    parse_name=False,
                    limit_posts=10,
                    max_age_days=0,
                    checkpoint_batch=1,
                    resume=True,
                )

            conn = connect_db(db)
            rows = export_users_rows(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["user_id"], 555)
            self.assertEqual(rows[0]["source_type"], "channel_comments")
            cp = checkpoint_get(conn, "channel_comments:900")
            self.assertEqual(cp.cursor_int, 100)
            conn.close()


class ExportTests(unittest.TestCase):
    def test_export_writes_csv_json_txt(self):
        user = SimpleNamespace(
            id=42,
            username="exported",
            first_name="=2+2",
            last_name="@formula",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "export.db")
            out = os.path.join(tmp, "out")
            conn = connect_db(db)
            from storage import upsert_user
            upsert_user(
                conn,
                user,
                source_id="1",
                source_title="Source",
                source_type="participants",
            )
            conn.commit()
            conn.close()

            with patch.object(parser_mod, "DB_PATH", db):
                paths = parser_mod.export_users(out)

            self.assertEqual(set(paths), {"csv", "json", "txt"})
            for path in paths.values():
                self.assertTrue(os.path.exists(path))
            with open(paths["json"], "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload[0]["user_id"], 42)
            self.assertEqual(payload[0]["first_name"], "=2+2")

            with open(
                paths["csv"],
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                csv_row = next(csv.DictReader(handle))
            self.assertEqual(csv_row["first_name"], "'=2+2")
            self.assertEqual(csv_row["last_name"], "'@formula")

            if os.name != "nt":
                for path in paths.values():
                    self.assertEqual(
                        stat.S_IMODE(os.stat(path).st_mode),
                        0o600,
                    )
                self.assertEqual(
                    stat.S_IMODE(os.stat(out).st_mode),
                    0o700,
                )


if __name__ == "__main__":
    unittest.main()
