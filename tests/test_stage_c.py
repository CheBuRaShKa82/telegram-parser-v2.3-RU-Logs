import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import defunc
from storage import checkpoint_get, connect_db, export_users_rows


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
        self.assertEqual(defunc.quality_user(user)[0], True)

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
        cfg = defunc.ParserFilterConfig(require_username=True)
        ok, reason = defunc.quality_user(user, cfg)
        self.assertFalse(ok)
        self.assertEqual(reason, "нет username")


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

    def test_message_parser_resumes_from_last_cursor(self):
        user1 = self.make_user(101, "one")
        user2 = self.make_user(102, "two")
        messages = [
            SimpleNamespace(id=30, sender_id=101, sender=user1, date=None),
            SimpleNamespace(id=20, sender_id=102, sender=user2, date=None),
        ]
        client = FakeMessageClient(messages)

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "parser.db")
            with patch.object(defunc, "LEDGER_DB", db):
                defunc.parsing_from_messages(
                    client,
                    SimpleNamespace(id=777, title="Chat"),
                    parse_id=False,
                    parse_name=False,
                    limit_messages=1,
                    max_age_days=0,
                    checkpoint_batch=1,
                    resume=True,
                )
                defunc.parsing_from_messages(
                    client,
                    SimpleNamespace(id=777, title="Chat"),
                    parse_id=False,
                    parse_name=False,
                    limit_messages=10,
                    max_age_days=0,
                    checkpoint_batch=1,
                    resume=True,
                )

            self.assertEqual(client.offsets, [0, 30])
            conn = connect_db(db)
            rows = export_users_rows(conn)
            self.assertEqual({row["user_id"] for row in rows}, {101, 102})
            cp = checkpoint_get(conn, "messages:777")
            self.assertIsNotNone(cp)
            self.assertEqual(cp.cursor_int, 20)
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
            with patch.object(defunc, "LEDGER_DB", db):
                defunc.parsing_channel_comments(
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
            first_name="Ex",
            last_name="Port",
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

            with patch.object(defunc, "LEDGER_DB", db):
                paths = defunc.export_users(out)

            self.assertEqual(set(paths), {"csv", "json", "txt"})
            for path in paths.values():
                self.assertTrue(os.path.exists(path))
            with open(paths["json"], "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload[0]["user_id"], 42)


if __name__ == "__main__":
    unittest.main()
