# -*- coding: utf-8 -*-
"""Parser, filters, checkpoints and exports for telegram-parser v2.4."""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from telethon import utils as tl_utils
from telethon.errors import FloodWaitError, RPCError
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import (
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from logging_setup import log_info, log_ok, log_pause, log_warn
DB_PATH = "invite_ledger.db"


from storage import (
    checkpoint_clear,
    checkpoint_get,
    checkpoint_put,
    connect_db,
    export_users_rows,
    upsert_user,
)



# -------------------- PARSER FILTERS --------------------

@dataclass(frozen=True)
class ParserFilterConfig:
    exclude_bots: bool = True
    exclude_deleted: bool = True
    exclude_scam_fake: bool = True
    require_username: bool = False
    require_photo: bool = False
    active_days: int = 0  # 0 = activity is ignored


DEFAULT_PARSER_FILTERS = ParserFilterConfig()


PARSER_FLOOD_SLEEP_THRESHOLD = 60


def _configure_parser_client(client: TelegramClient) -> None:
    """Use Telethon's normal short FloodWait auto-sleep for parser workloads."""
    try:
        client.flood_sleep_threshold = PARSER_FLOOD_SLEEP_THRESHOLD
    except Exception:
        pass


def _resolve_source_entity(client: TelegramClient, source: Any) -> Any:
    """Normalize manual refs and dialog entities to the same Telegram entity."""
    if isinstance(source, (str, int)):
        try:
            return client.get_entity(source)
        except Exception:
            return source
    return source


def _resume_cursor(cp: Any, resume: bool) -> int:
    """Resume only interrupted/running/error checkpoints, never completed ones."""
    if not resume or cp is None or getattr(cp, "cursor_int", None) is None:
        return 0
    status = str(getattr(cp, "status", "") or "")
    if status == "running" or status == "interrupted" or status.startswith("error:"):
        return int(cp.cursor_int)
    return 0



def _is_active_within(status: Any, days: int) -> bool:
    """Best-effort activity check for Telegram's coarse presence statuses."""
    if days <= 0:
        return True
    if status is None:
        return False
    if isinstance(status, UserStatusOnline):
        return True
    if isinstance(status, UserStatusRecently):
        return days >= 3
    if isinstance(status, UserStatusLastWeek):
        return days >= 7
    if isinstance(status, UserStatusLastMonth):
        return days >= 30
    if isinstance(status, UserStatusOffline):
        try:
            was = status.was_online
            if was is None:
                return False
            now = datetime.now(timezone.utc)
            if was.tzinfo is None:
                was = was.replace(tzinfo=timezone.utc)
            return (now - was) <= timedelta(days=days)
        except Exception:
            return False
    return False


def quality_user(
    user: Any,
    filters: Optional[ParserFilterConfig] = None,
) -> Tuple[bool, str]:
    filters = filters or DEFAULT_PARSER_FILTERS
    if filters.exclude_bots and getattr(user, "bot", False):
        return False, "бот"
    if filters.exclude_deleted and getattr(user, "deleted", False):
        return False, "удалён"
    if filters.exclude_scam_fake and getattr(user, "scam", False):
        return False, "scam"
    if filters.exclude_scam_fake and getattr(user, "fake", False):
        return False, "fake"
    if filters.require_username and not getattr(user, "username", None):
        return False, "нет username"
    if filters.require_photo:
        photo = getattr(user, "photo", None)
        if photo is None or type(photo).__name__ == "UserProfilePhotoEmpty":
            return False, "нет фото"
    if filters.active_days > 0 and not _is_active_within(
        getattr(user, "status", None), filters.active_days
    ):
        return False, "не активен"
    return True, "ok"


def quality_hard(user: Any) -> Tuple[bool, str]:
    """Legacy strict filter kept for compatibility."""
    return quality_user(
        user,
        ParserFilterConfig(
            exclude_bots=True,
            exclude_deleted=True,
            exclude_scam_fake=True,
            require_username=True,
            require_photo=True,
            active_days=7,
        ),
    )

# -------------------- DEDUP HELPERS --------------------

def _read_set(path: str, strip_at: bool = False) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        items = set()
        for line in f:
            s = line.strip()
            if not s:
                continue
            if strip_at and s.startswith("@"):
                s = s[1:]
            items.add(s)
        return items

def _append_unique(path: str, values: Iterable[str], prefix_at: bool = False) -> int:
    existing = _read_set(path, strip_at=prefix_at)
    new_vals = []
    for v in values:
        if not v:
            continue
        vv = v.strip()
        if not vv:
            continue
        # нормализация
        if prefix_at and vv.startswith("@"):
            vv = vv[1:]
        if vv in existing:
            continue
        existing.add(vv)
        new_vals.append(("@" + vv) if prefix_at else vv)

    if not new_vals:
        return 0

    with open(path, "a", encoding="utf-8") as f:
        for value in new_vals:
            f.write(value + "\n")
    return len(new_vals)


def _source_metadata(
    chat_entity: Any,
    source_type: str,
) -> Tuple[Optional[str], Optional[str], str]:
    # Primitive references must be handled before getattr(), because strings
    # expose methods like .title() which are not Telegram metadata.
    if isinstance(chat_entity, int):
        raw_id = str(chat_entity)
        return raw_id, raw_id, source_type

    if isinstance(chat_entity, str):
        raw = chat_entity.strip()
        return (
            raw or None,
            raw or None,
            source_type,
        )

    source_id = getattr(chat_entity, "id", None)
    source_title = getattr(chat_entity, "title", None)
    if not isinstance(source_title, str):
        source_title = None
    if source_title is None:
        username = getattr(chat_entity, "username", None)
        if isinstance(username, str) and username:
            source_title = "@" + username.lstrip("@")

    try:
        if source_id is None:
            source_id = tl_utils.get_peer_id(chat_entity)
    except Exception:
        pass

    return (
        str(source_id) if source_id is not None else None,
        str(source_title) if source_title else None,
        source_type,
    )

def _parser_checkpoint_key(
    source_id: Optional[str],
    source_title: Optional[str],
    mode: str,
) -> str:
    identity = source_id or source_title or "unknown"
    return f"{mode}:{identity}"


def _finalize_legacy_exports(
    usernames: Iterable[str],
    user_ids: Iterable[str],
    *,
    parse_name: bool,
    parse_id: bool,
) -> Tuple[int, int]:
    added_u = (
        _append_unique("usernames.txt", usernames, prefix_at=True)
        if parse_name else 0
    )
    added_i = (
        _append_unique("userids.txt", user_ids, prefix_at=False)
        if parse_id else 0
    )
    return added_u, added_i


def parsing(
    client: TelegramClient,
    chat_entity: Union[str, int, Any],
    parse_id: bool,
    parse_name: bool,
    *,
    filters: Optional[ParserFilterConfig] = None,
    checkpoint_batch: int = 100,
) -> None:
    """Parse visible participants with durable SQLite checkpoints.

    Telegram does not expose a reliable generic offset for all participant
    listings through this high-level iterator. Resume therefore means safe
    re-scan with SQLite de-duplication; already stored users are not duplicated.
    """
    filters = filters or DEFAULT_PARSER_FILTERS
    _configure_parser_client(client)
    source_entity = _resolve_source_entity(client, chat_entity)
    batch = max(1, int(checkpoint_batch or 100))
    good_usernames: List[str] = []
    good_ids: List[str] = []
    total = 0
    kept = 0
    skipped: Dict[str, int] = {}
    conn = connect_db(DB_PATH)
    source_id, source_title, source_type = _source_metadata(
        source_entity, "participants"
    )
    cp_key = _parser_checkpoint_key(source_id, source_title, source_type)
    error_name: Optional[str] = None

    log_info(
        f"🔍 Парсинг участников: {source_title or source_id or chat_entity} | "
        f"checkpoint={cp_key}"
    )

    try:
        try:
            for user in client.iter_participants(source_entity):
                total += 1
                ok, reason = quality_user(user, filters)
                if not ok:
                    skipped[reason] = skipped.get(reason, 0) + 1
                else:
                    kept += 1
                    upsert_user(
                        conn,
                        user,
                        source_id=source_id,
                        source_title=source_title,
                        source_type=source_type,
                    )
                    if parse_name and getattr(user, "username", None):
                        good_usernames.append(str(user.username))
                    if parse_id and getattr(user, "id", None) is not None:
                        good_ids.append(str(int(user.id)))

                if total % batch == 0:
                    checkpoint_put(
                        conn,
                        checkpoint_key=cp_key,
                        mode=source_type,
                        source_id=source_id,
                        source_title=source_title,
                        cursor_int=None,
                        processed=total,
                        saved=kept,
                        status="running",
                    )
                    log_info(
                        f"💾 Checkpoint участников: processed={total}, saved={kept}"
                    )
        except FloodWaitError as exc:
            seconds = max(0, int(getattr(exc, "seconds", 0) or 0))
            error_name = f"FloodWait:{seconds}"
            log_pause(
                f"⏳ Парсинг участников остановлен на FloodWait {seconds}s; "
                "checkpoint сохранён, новые запросы не выполняются."
            )
        except KeyboardInterrupt:
            checkpoint_put(
                conn,
                checkpoint_key=cp_key,
                mode=source_type,
                source_id=source_id,
                source_title=source_title,
                cursor_int=None,
                processed=total,
                saved=kept,
                status="interrupted",
            )
            raise
        except Exception as exc:
            error_name = type(exc).__name__
            log_warn(
                f"⚠️ Парсинг участников прерван ({error_name}); "
                "уже сохранённые пользователи остаются в SQLite."
            )

        checkpoint_put(
            conn,
            checkpoint_key=cp_key,
            mode=source_type,
            source_id=source_id,
            source_title=source_title,
            cursor_int=None,
            processed=total,
            saved=kept,
            status=f"error:{error_name}" if error_name else "completed",
        )
    finally:
        conn.close()

    added_u, added_i = _finalize_legacy_exports(
        good_usernames, good_ids,
        parse_name=parse_name, parse_id=parse_id,
    )
    log_ok(
        f"✅ Участники: просмотрено={total}, сохранено={kept}, "
        f"TXT usernames+={added_u}, ids+={added_i}"
    )
    if skipped:
        parts = ", ".join(
            f"{key}={value}"
            for key, value in sorted(skipped.items(), key=lambda x: -x[1])
        )
        log_info(f"📉 Фильтр: {parts}")


def parsing_from_messages(
    client: TelegramClient,
    chat_entity: Union[str, int, Any],
    parse_id: bool,
    parse_name: bool,
    limit_messages: int = 5000,
    max_age_days: int = 7,
    *,
    filters: Optional[ParserFilterConfig] = None,
    checkpoint_batch: int = 100,
    resume: bool = True,
) -> None:
    """Collect active message authors with durable cursor-based resume."""
    filters = filters or DEFAULT_PARSER_FILTERS
    _configure_parser_client(client)
    source_entity = _resolve_source_entity(client, chat_entity)
    batch = max(1, int(checkpoint_batch or 100))
    good_usernames: List[str] = []
    good_ids: List[str] = []
    scanned = 0
    unique_found = 0
    kept = 0
    skipped: Dict[str, int] = {}
    seen_user_ids: set[int] = set()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if max_age_days > 0 else None
    )
    conn = connect_db(DB_PATH)
    source_id, source_title, source_type = _source_metadata(
        source_entity, "messages"
    )
    cp_key = _parser_checkpoint_key(source_id, source_title, source_type)
    cp = checkpoint_get(conn, cp_key) if resume else None
    offset_id = _resume_cursor(cp, resume)
    last_cursor = offset_id or None
    error_name: Optional[str] = None

    if not resume:
        checkpoint_clear(conn, cp_key)

    def save_checkpoint(status: str) -> None:
        checkpoint_put(
            conn,
            checkpoint_key=cp_key,
            mode=source_type,
            source_id=source_id,
            source_title=source_title,
            cursor_int=last_cursor,
            processed=scanned,
            saved=kept,
            status=status,
        )

    log_info(
        f"🔍 Авторы сообщений: {source_title or source_id or source_entity} | "
        f"limit={limit_messages}, age_days={max_age_days}, "
        f"resume_offset={offset_id or 'start'}"
    )

    try:
        try:
            for msg in client.iter_messages(
                source_entity,
                limit=max(1, int(limit_messages)),
                offset_id=offset_id,
            ):
                scanned += 1
                current_cursor = (
                    int(msg.id) if getattr(msg, "id", None) is not None else None
                )

                msg_date = getattr(msg, "date", None)
                if cutoff is not None and msg_date is not None and msg_date < cutoff:
                    break

                sid = getattr(msg, "sender_id", None)
                if sid:
                    sid = int(sid)
                    if sid not in seen_user_ids:
                        seen_user_ids.add(sid)
                        unique_found += 1

                        user = getattr(msg, "sender", None)
                        if user is None:
                            try:
                                user = msg.get_sender()
                            except Exception:
                                user = None
                        if user is None:
                            try:
                                user = client.get_entity(sid)
                            except Exception:
                                user = None

                        if user is None:
                            skipped["не удалось получить пользователя"] = (
                                skipped.get("не удалось получить пользователя", 0) + 1
                            )
                        else:
                            ok, reason = quality_user(user, filters)
                            if not ok:
                                skipped[reason] = skipped.get(reason, 0) + 1
                            else:
                                kept += 1
                                upsert_user(
                                    conn,
                                    user,
                                    source_id=source_id,
                                    source_title=source_title,
                                    source_type=source_type,
                                    seen_at=(
                                        msg_date.isoformat()
                                        if msg_date else None
                                    ),
                                )
                                if parse_name and getattr(user, "username", None):
                                    good_usernames.append(str(user.username))
                                if parse_id and getattr(user, "id", None) is not None:
                                    good_ids.append(str(int(user.id)))

                # Current message is fully handled (including deliberate skips).
                if current_cursor is not None:
                    last_cursor = current_cursor

                if scanned % batch == 0:
                    save_checkpoint("running")
                    log_info(
                        f"💾 Checkpoint сообщений: id={last_cursor}, "
                        f"processed={scanned}, saved={kept}"
                    )

        except FloodWaitError as exc:
            seconds = max(0, int(getattr(exc, "seconds", 0) or 0))
            error_name = f"FloodWait:{seconds}"
            log_pause(
                f"⏳ Парсинг сообщений остановлен на FloodWait {seconds}s; "
                "checkpoint сохранён, новые запросы не выполняются."
            )
        except KeyboardInterrupt:
            save_checkpoint("interrupted")
            raise
        except Exception as exc:
            error_name = type(exc).__name__
            log_warn(
                f"⚠️ Парсинг сообщений прерван ({error_name}); "
                "следующий запуск может продолжить с checkpoint."
            )

        save_checkpoint(
            f"error:{error_name}" if error_name else "completed"
        )
    finally:
        conn.close()

    added_u, added_i = _finalize_legacy_exports(
        good_usernames, good_ids,
        parse_name=parse_name, parse_id=parse_id,
    )
    log_ok(
        f"✅ Сообщения: просмотрено={scanned}, уникальных={unique_found}, "
        f"сохранено={kept}, TXT usernames+={added_u}, ids+={added_i}"
    )
    if skipped:
        parts = ", ".join(
            f"{key}={value}"
            for key, value in sorted(skipped.items(), key=lambda x: -x[1])
        )
        log_info(f"📉 Фильтр сообщений: {parts}")

def parsing_channel_comments(
    client: TelegramClient,
    channel_entity: Union[str, int, Any],
    parse_id: bool,
    parse_name: bool,
    *,
    limit_posts: int = 200,
    comments_per_post: int = 0,
    max_age_days: int = 30,
    filters: Optional[ParserFilterConfig] = None,
    checkpoint_batch: int = 10,
    resume: bool = True,
) -> None:
    """Collect authors of comments under broadcast-channel posts.

    This is active audience discovery, not a subscriber-list parser.
    """
    filters = filters or DEFAULT_PARSER_FILTERS
    _configure_parser_client(client)
    batch = max(1, int(checkpoint_batch or 10))
    channel = client.get_entity(channel_entity)

    if not getattr(channel, "broadcast", False):
        raise ValueError(
            "Режим комментариев предназначен для broadcast-канала."
        )

    full = client(GetFullChannelRequest(channel))
    linked_chat_id = getattr(full.full_chat, "linked_chat_id", None)
    if not linked_chat_id:
        raise ValueError("У канала нет связанной discussion-группы/комментариев.")

    linked_group = next(
        (
            chat for chat in getattr(full, "chats", [])
            if getattr(chat, "id", None) == linked_chat_id
        ),
        None,
    )
    linked_title = getattr(linked_group, "title", None) or str(linked_chat_id)

    source_id, source_title, source_type = _source_metadata(
        channel, "channel_comments"
    )
    cp_key = _parser_checkpoint_key(source_id, source_title, source_type)
    conn = connect_db(DB_PATH)
    cp = checkpoint_get(conn, cp_key) if resume else None
    offset_id = _resume_cursor(cp, resume)
    if not resume:
        checkpoint_clear(conn, cp_key)

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if max_age_days > 0 else None
    )
    posts_scanned = 0
    comments_scanned = 0
    kept = 0
    last_post_id = offset_id or None
    seen_user_ids: set[int] = set()
    good_usernames: List[str] = []
    good_ids: List[str] = []
    skipped: Dict[str, int] = {}
    error_name: Optional[str] = None

    def save_checkpoint(status: str) -> None:
        checkpoint_put(
            conn,
            checkpoint_key=cp_key,
            mode=source_type,
            source_id=source_id,
            source_title=source_title,
            cursor_int=last_post_id,
            processed=posts_scanned,
            saved=kept,
            status=status,
        )

    log_info(
        f"💬 Комментарии канала: {source_title or source_id} | "
        f"discussion={linked_title} | posts={limit_posts} | "
        f"resume_offset={offset_id or 'start'}"
    )

    try:
        try:
            for post in client.iter_messages(
                channel,
                limit=max(1, int(limit_posts)),
                offset_id=offset_id,
            ):
                if not getattr(post, "id", None):
                    continue

                current_post_id = int(post.id)
                posts_scanned += 1
                post_date = getattr(post, "date", None)
                if cutoff is not None and post_date is not None and post_date < cutoff:
                    break

                reply_limit = (
                    None if int(comments_per_post or 0) <= 0
                    else int(comments_per_post)
                )
                try:
                    replies = client.iter_messages(
                        channel,
                        reply_to=current_post_id,
                        limit=reply_limit,
                    )
                    for reply in replies:
                        comments_scanned += 1
                        sid = getattr(reply, "sender_id", None)
                        if sid:
                            sid = int(sid)
                            if sid not in seen_user_ids:
                                seen_user_ids.add(sid)
                                user = getattr(reply, "sender", None)
                                if user is None:
                                    try:
                                        user = reply.get_sender()
                                    except Exception:
                                        user = None
                                if user is None:
                                    try:
                                        user = client.get_entity(sid)
                                    except Exception:
                                        user = None

                                if user is None:
                                    skipped["не удалось получить пользователя"] = (
                                        skipped.get(
                                            "не удалось получить пользователя", 0
                                        ) + 1
                                    )
                                else:
                                    ok, reason = quality_user(user, filters)
                                    if not ok:
                                        skipped[reason] = skipped.get(reason, 0) + 1
                                    else:
                                        kept += 1
                                        reply_date = getattr(reply, "date", None)
                                        upsert_user(
                                            conn,
                                            user,
                                            source_id=source_id,
                                            source_title=source_title,
                                            source_type=source_type,
                                            seen_at=(
                                                reply_date.isoformat()
                                                if reply_date else None
                                            ),
                                        )
                                        if parse_name and getattr(user, "username", None):
                                            good_usernames.append(str(user.username))
                                        if parse_id and getattr(user, "id", None) is not None:
                                            good_ids.append(str(int(user.id)))

                        # Persist long comment threads even before the post ends.
                        if comments_scanned and comments_scanned % 100 == 0:
                            conn.commit()

                except FloodWaitError:
                    # Do not let the generic RPC handler continue hammering posts.
                    raise
                except RPCError as exc:
                    skipped[f"comments:{type(exc).__name__}"] = (
                        skipped.get(f"comments:{type(exc).__name__}", 0) + 1
                    )
                    log_warn(
                        f"⚠️ Пост {current_post_id}: комментарии недоступны "
                        f"({type(exc).__name__})"
                    )

                # Only advance cursor after the entire post is handled.
                last_post_id = current_post_id
                if posts_scanned % batch == 0:
                    save_checkpoint("running")
                    log_info(
                        f"💾 Checkpoint комментариев: post={last_post_id}, "
                        f"posts={posts_scanned}, comments={comments_scanned}, "
                        f"saved={kept}"
                    )

        except FloodWaitError as exc:
            seconds = max(0, int(getattr(exc, "seconds", 0) or 0))
            error_name = f"FloodWait:{seconds}"
            log_pause(
                f"⏳ Комментарии остановлены на FloodWait {seconds}s; "
                "текущий пост будет повторён при resume."
            )
        except KeyboardInterrupt:
            save_checkpoint("interrupted")
            raise
        except Exception as exc:
            error_name = type(exc).__name__
            log_warn(
                f"⚠️ Парсинг комментариев прерван ({error_name}); "
                "результат и checkpoint сохранены."
            )

        save_checkpoint(
            f"error:{error_name}" if error_name else "completed"
        )
    finally:
        conn.close()

    added_u, added_i = _finalize_legacy_exports(
        good_usernames, good_ids,
        parse_name=parse_name, parse_id=parse_id,
    )
    log_ok(
        f"✅ Комментарии: posts={posts_scanned}, comments={comments_scanned}, "
        f"уникальных сохранено={kept}, TXT usernames+={added_u}, ids+={added_i}"
    )
    if skipped:
        parts = ", ".join(
            f"{key}={value}"
            for key, value in sorted(skipped.items(), key=lambda x: -x[1])
        )
        log_info(f"📉 Комментарии/фильтр: {parts}")

def export_users(
    output_dir: str = "exports",
    formats: Tuple[str, ...] = ("csv", "json", "txt"),
) -> Dict[str, str]:
    """Export the canonical SQLite user database."""
    conn = connect_db(DB_PATH)
    try:
        rows = export_users_rows(conn)
    finally:
        conn.close()

    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result: Dict[str, str] = {}
    normalized = {fmt.lower().strip() for fmt in formats}

    if "csv" in normalized:
        path = os.path.join(output_dir, f"users_{stamp}.csv")
        fields = [
            "user_id", "username", "first_name", "last_name",
            "source_id", "source_title", "source_type",
            "parsed_at", "last_seen_at",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        result["csv"] = path

    if "json" in normalized:
        path = os.path.join(output_dir, f"users_{stamp}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        result["json"] = path

    if "txt" in normalized:
        path = os.path.join(output_dir, f"users_{stamp}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                username = row.get("username") or ""
                if username:
                    username = "@" + str(username).lstrip("@")
                handle.write(
                    "\t".join(
                        [
                            str(row.get("user_id") or ""),
                            username,
                            str(row.get("first_name") or ""),
                            str(row.get("last_name") or ""),
                            str(row.get("source_title") or ""),
                            str(row.get("source_type") or ""),
                        ]
                    )
                    + "\n"
                )
        result["txt"] = path

    log_ok(
        f"📦 Экспорт пользователей: rows={len(rows)} | "
        + ", ".join(f"{key}={value}" for key, value in result.items())
    )
    return result


