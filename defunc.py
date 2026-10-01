# -*- coding: utf-8 -*-
"""
telegram-parser-v2.2
Парсер участников + инвайтер (Telethon, sync)

Апгрейды:
- Жёсткий фильтр качества (без привязки к языку)
- Дедуп списков usernames/userids
- Invite ledger (SQLite) — пропускает уже обработанных, умеет продолжать
- FloodWait handling + "человеческие" RU-логи в app.log
"""

import os
import time
import random
import re
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Iterable, List, Optional, Tuple, Union, Dict, Any

from storage import (
    UserCandidate,
    candidate_from_raw,
    connect_db,
    exclusion_add,
    exclusion_has,
    exclusion_load_keys,
    exclusion_reason,
    upsert_user,
)

from telethon.sync import TelegramClient
from telethon import utils as tl_utils
from telethon.tl.functions.channels import InviteToChannelRequest, JoinChannelRequest, GetParticipantRequest
from telethon.tl.types import (
    UserStatusOnline,
    UserStatusRecently,
    UserStatusLastWeek,
    UserStatusLastMonth,
    UserStatusOffline,
)

from telethon.errors import (
    FloodWaitError,
    UserPrivacyRestrictedError,
    UserAlreadyParticipantError,
    ChatAdminRequiredError,
    PeerFloodError,
    UsernameInvalidError,
    UserIdInvalidError,
    UserNotParticipantError,
    RPCError,
)

# Extra RPC errors used to classify preflight issues more precisely
from telethon.errors.rpcerrorlist import (
    ChatWriteForbiddenError,
    ChannelPrivateError,
    UserBannedInChannelError,
    UserNotMutualContactError,
    UserChannelsTooMuchError,
    UserKickedError,
    UserBlockedError,
)


def _safe_str(x: Any) -> str:
    try:
        return str(x)
    except Exception:
        return repr(x)


def _target_brief(ent: Any) -> str:
    """Best-effort short description for logs."""
    try:
        # Channel/Chat/User objects
        uname = getattr(ent, "username", None)
        title = getattr(ent, "title", None)
        eid = getattr(ent, "id", None)
        mg = getattr(ent, "megagroup", None)
        bc = getattr(ent, "broadcast", None)
        bits = []
        if title:
            bits.append(_safe_str(title))
        if uname:
            bits.append("@" + _safe_str(uname))
        if eid is not None:
            bits.append(f"id={eid}")
        if mg is not None:
            bits.append(f"megagroup={bool(mg)}")
        if bc is not None:
            bits.append(f"broadcast={bool(bc)}")
        if bits:
            return " ".join(bits)
    except Exception:
        pass
    return _safe_str(ent)


def _diagnose_invite_context(client: TelegramClient, target_entity: Any) -> Dict[str, Any]:
    """Collects best-effort diagnostics why an invite action may be forbidden.

    Never raises; returns a dict safe for logging.
    """
    out: Dict[str, Any] = {}
    try:
        me = client.get_me()
        out["me_id"] = getattr(me, "id", None)
        out["me_username"] = getattr(me, "username", None)
    except Exception:
        pass

    try:
        out["target"] = _target_brief(target_entity)
    except Exception:
        pass

    # Permissions (Telethon helper)
    try:
        perms = client.get_permissions(target_entity, "me")
        out["perm_invite_users"] = getattr(perms, "invite_users", None)
        out["perm_send_messages"] = getattr(perms, "send_messages", None)
    except Exception as e:
        out["perm_error"] = type(e).__name__

    # Participant rights via GetParticipantRequest
    try:
        res = client(GetParticipantRequest(channel=target_entity, participant="me"))
        p = getattr(res, "participant", None)
        out["participant_type"] = type(p).__name__ if p is not None else None
        admin_rights = getattr(p, "admin_rights", None)
        banned_rights = getattr(p, "banned_rights", None)
        if admin_rights is not None:
            out["admin_rights_invite_users"] = getattr(admin_rights, "invite_users", None)
        if banned_rights is not None:
            out["banned_rights_invite_users"] = getattr(banned_rights, "invite_users", None)
            out["banned_rights_until"] = getattr(banned_rights, "until_date", None)
    except Exception as e:
        out["participant_error"] = type(e).__name__

    # Default banned rights on the chat/channel itself (if available)
    try:
        dbr = getattr(target_entity, "default_banned_rights", None)
        if dbr is not None:
            out["default_banned_invite_users"] = getattr(dbr, "invite_users", None)
    except Exception:
        pass

    return out


LOG_FILE = "app.log"
LEDGER_DB = "invite_ledger.db"

# -------------------- SESSIONS DIR --------------------

# Пользователь просил хранить все .session в отдельной папке.
# ВАЖНО: Telethon принимает "имя сессии" без расширения и сам добавляет .session.
# Поэтому мы используем путь вида: sessoins/<name>

SESSIONS_DIR = "sessoins"  # намеренно как в сообщении пользователя


def ensure_sessions_dir() -> str:
    """Создаёт папку для сессий и возвращает её путь."""
    Path(SESSIONS_DIR).mkdir(parents=True, exist_ok=True)
    # Мягкая миграция: если старые .session лежат рядом со скриптом — перенесём их в sessoins/
    try:
        for sf in Path(".").glob("*.session"):
            if not sf.is_file():
                continue
            dst = Path(SESSIONS_DIR) / sf.name
            if dst.exists():
                continue
            sf.rename(dst)
    except Exception:
        pass
    return SESSIONS_DIR


def session_name_from_file(session_file: str) -> str:
    """Преобразует '<name>.session' -> 'sessoins/<name>' (путь для Telethon)."""
    ensure_sessions_dir()
    base = os.path.basename(session_file)
    name = base[:-8] if base.endswith(".session") else base
    return os.path.join(SESSIONS_DIR, name)


def list_session_files() -> List[str]:
    """Возвращает список файлов .session из папки sessoins/."""
    ensure_sessions_dir()
    try:
        return sorted(
            p.name for p in Path(SESSIONS_DIR).glob("*.session") if p.is_file()
        )
    except OSError as exc:
        log_warn(f"Не удалось прочитать каталог сессий: {type(exc).__name__}")
        return []

# -------------------- ЛОГИ --------------------

def _setup_logging() -> None:
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

_setup_logging()

def log_info(msg: str) -> None:
    logging.info(f"ИНФО | {msg}")

def log_ok(msg: str) -> None:
    logging.info(f"УСПЕХ | {msg}")

def log_warn(msg: str) -> None:
    logging.info(f"ВНИМАНИЕ | {msg}")

def log_pause(msg: str) -> None:
    logging.info(f"ПАУЗА | {msg}")

def log_stop(msg: str) -> None:
    logging.info(f"СТОП | {msg}")

# -------------------- OPTIONS --------------------

DEFAULT_OPTIONS = [
    "NONEID\n",
    "NONEHASH\n",
    "True\n",   # parse user-id
    "True\n",   # parse user-name
]

def ensure_options() -> None:
    if not os.path.exists("options.txt"):
        with open("options.txt", "w", encoding="utf-8") as f:
            f.writelines(DEFAULT_OPTIONS)
        return

    # если файл пустой — тоже восстановим
    with open("options.txt", "r+", encoding="utf-8") as f:
        lines = f.readlines()
        if not lines:
            f.seek(0)
            f.writelines(DEFAULT_OPTIONS)

def getoptions() -> List[str]:
    ensure_options()
    with open("options.txt", "r", encoding="utf-8") as f:
        return f.readlines()

# -------------------- QUALITY FILTER (HARD) --------------------

def _is_active(status) -> bool:
    """Жёстко считаем активным: online/recently/last week или offline был в последние 7 дней."""
    if status is None:
        return False
    if isinstance(status, (UserStatusOnline, UserStatusRecently, UserStatusLastWeek)):
        return True
    if isinstance(status, UserStatusOffline):
        try:
            # was_online обычно tz-aware (UTC)
            was = status.was_online
            if was is None:
                return False
            now = datetime.now(timezone.utc)
            return (now - was) <= timedelta(days=7)
        except Exception:
            return False
    # LastMonth считаем уже слабым для жёсткого фильтра
    return False

def quality_hard(user) -> Tuple[bool, str]:
    """
    Жёсткий фильтр качества (язык НЕ учитываем):
    - не бот
    - не deleted
    - не scam/fake (если поле есть)
    - есть username
    - есть фото
    - активен (online/recently/last week или был онлайн <=7 дней)
    """
    if getattr(user, "bot", False):
        return False, "бот"
    if getattr(user, "deleted", False):
        return False, "удалён"
    if getattr(user, "scam", False):
        return False, "scam"
    if getattr(user, "fake", False):
        return False, "fake"
    if not getattr(user, "username", None):
        return False, "нет username"
    if not getattr(user, "photo", None):
        return False, "нет фото"
    if not _is_active(getattr(user, "status", None)):
        return False, "не активен"
    return True, "ok"

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
        for v in new_vals:
            f.write(v + "\n")
    return len(new_vals)

# -------------------- LEDGER (SQLite) --------------------

def _db() -> sqlite3.Connection:
    conn = connect_db(LEDGER_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            user_key TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            status TEXT NOT NULL,
            reason TEXT,
            ts TEXT NOT NULL
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_inv_unique ON invites(target, user_key)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_stats (
            session_file TEXT PRIMARY KEY,
            blocked_until REAL DEFAULT 0,
            frozen_until REAL DEFAULT 0,
            banned INTEGER DEFAULT 0,
            ok INTEGER DEFAULT 0,
            fail INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            last_invite_at REAL DEFAULT 0,
            next_invite_at REAL DEFAULT 0,
            hour_window_start REAL DEFAULT 0,
            hour_count INTEGER DEFAULT 0,
            day_window_start REAL DEFAULT 0,
            day_count INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.commit()
    return conn

def ledger_get(

def ledger_get(conn: sqlite3.Connection, target: str, user_key: str) -> Optional[Tuple[str, str]]:
    cur = conn.execute(
        "SELECT status, reason FROM invites WHERE target=? AND user_key=? LIMIT 1",
        (target, user_key),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None

def ledger_put(conn: sqlite3.Connection, target: str, user_key: str, user_id: Optional[int],
               username: Optional[str], status: str, reason: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO invites(target, user_key, user_id, username, status, reason, ts) VALUES (?,?,?,?,?,?,?)",
        (target, user_key, user_id, username, status, reason, ts),
    )
    conn.commit()



# -------------------- SESSION STATS (SQLite) --------------------



def excluded_load_all(
    conn: sqlite3.Connection,
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
) -> set:
    return exclusion_load_keys(
        conn, target_key=target_key, session_file=session_file
    )


def excluded_has(
    conn: sqlite3.Connection,
    user_key: str,
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
) -> bool:
    return exclusion_has(
        conn,
        user_key,
        target_key=target_key,
        session_file=session_file,
    )


def excluded_reason(
    conn: sqlite3.Connection,
    user_key: str,
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
) -> str:
    return exclusion_reason(
        conn,
        user_key,
        target_key=target_key,
        session_file=session_file,
    )


def excluded_add(
    conn: sqlite3.Connection,
    user_key: str,
    user_id: Optional[int],
    username: Optional[str],
    reason: str,
    *,
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
    global_scope: bool = False,
) -> None:
    exclusion_add(
        conn,
        user_key,
        user_id=user_id,
        username=username,
        reason=reason,
        target_key=target_key,
        session_file=session_file,
        global_scope=global_scope,
    )

def session_stats_load(

def session_stats_load(conn: sqlite3.Connection, session_files: List[str]) -> Dict[str, "SessionState"]:
    """Load persisted session states from DB (blocked/frozen/banned + rolling counters).

    Returns dict session_file -> SessionState. Missing sessions get defaults and are inserted.
    """
    out: Dict[str, SessionState] = {}
    now = _now()
    for sf in session_files:
        cur = conn.execute(
            "SELECT blocked_until,frozen_until,banned,ok,fail,attempts,last_invite_at,next_invite_at,"
            "hour_window_start,hour_count,day_window_start,day_count FROM session_stats WHERE session_file=?",
            (sf,),
        )
        row = cur.fetchone()
        if row:
            st = SessionState(session_file=sf)
            st.blocked_until = float(row[0] or 0)
            st.frozen_until = float(row[1] or 0)
            st.banned = bool(row[2] or 0)
            st.ok = int(row[3] or 0)
            st.fail = int(row[4] or 0)
            st.attempts = int(row[5] or 0)
            st.last_invite_at = float(row[6] or 0)
            st.next_invite_at = float(row[7] or 0)
            st.hour_window_start = float(row[8] or 0)
            st.hour_count = int(row[9] or 0)
            st.day_window_start = float(row[10] or 0)
            st.day_count = int(row[11] or 0)
        else:
            st = SessionState(session_file=sf)
            conn.execute(
                "INSERT OR IGNORE INTO session_stats(session_file, updated_at) VALUES (?,?)",
                (sf, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        # normalize windows if stale
        if st.hour_window_start <= 0 or now - st.hour_window_start >= 3600:
            st.hour_window_start = now
            st.hour_count = 0
        if st.day_window_start <= 0 or now - st.day_window_start >= 86400:
            st.day_window_start = now
            st.day_count = 0
        out[sf] = st
    return out


def session_stats_save(conn: sqlite3.Connection, st: "SessionState") -> None:
    conn.execute(
        "UPDATE session_stats SET blocked_until=?, frozen_until=?, banned=?, ok=?, fail=?, attempts=?, "
        "last_invite_at=?, next_invite_at=?, hour_window_start=?, hour_count=?, day_window_start=?, day_count=?, updated_at=? "
        "WHERE session_file=?",
        (
            float(st.blocked_until or 0),
            float(st.frozen_until or 0),
            1 if st.banned else 0,
            int(st.ok or 0),
            int(st.fail or 0),
            int(st.attempts or 0),
            float(st.last_invite_at or 0),
            float(st.next_invite_at or 0),
            float(getattr(st, "hour_window_start", 0) or 0),
            int(getattr(st, "hour_count", 0) or 0),
            float(getattr(st, "day_window_start", 0) or 0),
            int(getattr(st, "day_count", 0) or 0),
            datetime.now(timezone.utc).isoformat(),
            st.session_file,
        ),
    )
    conn.commit()


def session_next_time_due_to_limits(st: "SessionState", per_hour_limit: int, per_day_limit: int) -> float:
    """If limits are exceeded, returns the earliest timestamp when session can invite again (else 0)."""
    now = _now()
    next_due = 0.0
    if per_hour_limit and getattr(st, "hour_count", 0) >= int(per_hour_limit):
        next_due = max(next_due, float(getattr(st, "hour_window_start", now)) + 3600)
    if per_day_limit and getattr(st, "day_count", 0) >= int(per_day_limit):
        next_due = max(next_due, float(getattr(st, "day_window_start", now)) + 86400)
    return next_due


def session_consume_invite_token(st: "SessionState", per_hour_limit: int, per_day_limit: int) -> None:
    """Consumes one invite slot for rolling hour/day windows."""
    now = _now()
    if getattr(st, "hour_window_start", 0) <= 0 or now - st.hour_window_start >= 3600:
        st.hour_window_start = now
        st.hour_count = 0
    if getattr(st, "day_window_start", 0) <= 0 or now - st.day_window_start >= 86400:
        st.day_window_start = now
        st.day_count = 0
    if per_hour_limit:
        st.hour_count = int(getattr(st, "hour_count", 0) or 0) + 1
    if per_day_limit:
        st.day_count = int(getattr(st, "day_count", 0) or 0) + 1
# -------------------- CORE OPS --------------------

def _source_metadata(chat_entity: Any, source_type: str) -> Tuple[Optional[str], Optional[str], str]:
    source_id = getattr(chat_entity, "id", None)
    source_title = getattr(chat_entity, "title", None)
    if source_title is None:
        source_title = getattr(chat_entity, "username", None)
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


def parsing(client: TelegramClient, chat_entity: Union[str, int, Any], parse_id: bool, parse_name: bool) -> None:
    """Парсинг видимого списка участников с сохранением в SQLite."""
    log_info(f"🔍 Начат парсинг: {chat_entity}")
    good_usernames: List[str] = []
    good_ids: List[str] = []
    total = 0
    kept = 0
    skipped: Dict[str, int] = {}
    conn = _db()
    source_id, source_title, source_type = _source_metadata(chat_entity, "participants")

    try:
        for user in client.iter_participants(chat_entity):
            total += 1
            ok, reason = quality_hard(user)
            if not ok:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue

            kept += 1
            upsert_user(
                conn,
                user,
                source_id=source_id,
                source_title=source_title,
                source_type=source_type,
            )
            if parse_name and getattr(user, "username", None):
                good_usernames.append(user.username)
            if parse_id and getattr(user, "id", None) is not None:
                good_ids.append(str(int(user.id)))

            if kept % 200 == 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()

    added_u = _append_unique("usernames.txt", good_usernames, prefix_at=True) if parse_name else 0
    added_i = _append_unique("userids.txt", good_ids, prefix_at=False) if parse_id else 0
    log_ok(
        f"✅ Парсинг завершён. Всего: {total}, прошло фильтр: {kept}, "
        f"добавлено usernames: {added_u}, ids: {added_i}"
    )
    if skipped:
        parts = ", ".join(
            f"{k}={v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])
        )
        log_info(f"📉 Отфильтровано: {parts}")


def parsing_from_messages(
    client: TelegramClient,
    chat_entity: Union[str, int, Any],
    parse_id: bool,
    parse_name: bool,
    limit_messages: int = 5000,
    max_age_days: int = 7,
) -> None:
    """Собирает активных авторов сообщений и сохраняет их в SQLite."""
    log_info(
        f"🔍 Начат парсинг из сообщений: {chat_entity} | "
        f"лимит сообщений={limit_messages} | возраст≤{max_age_days}д"
    )
    print("Старт: парсинг из сообщений… Это может занять время. Прогресс будет обновляться.", flush=True)
    good_usernames: List[str] = []
    good_ids: List[str] = []
    scanned = 0
    unique_found = 0
    kept = 0
    skipped: Dict[str, int] = {}
    seen_user_ids: set = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    conn = _db()
    source_id, source_title, source_type = _source_metadata(chat_entity, "messages")

    try:
        for msg in client.iter_messages(chat_entity, limit=limit_messages):
            scanned += 1
            if scanned % 200 == 0:
                print(
                    f"Просмотрено: {scanned} | уникальных авторов: {unique_found} | прошло фильтр: {kept}",
                    end="\r",
                    flush=True,
                )
            try:
                if getattr(msg, "date", None) is not None and msg.date < cutoff:
                    break

                sid = getattr(msg, "sender_id", None)
                if not sid or sid in seen_user_ids:
                    continue
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
                    skipped["не удалось получить пользователя"] = skipped.get(
                        "не удалось получить пользователя", 0
                    ) + 1
                    continue

                ok, reason = quality_hard(user)
                if not ok:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue

                kept += 1
                upsert_user(
                    conn,
                    user,
                    source_id=source_id,
                    source_title=source_title,
                    source_type=source_type,
                    seen_at=getattr(msg, "date", None).isoformat()
                    if getattr(msg, "date", None)
                    else None,
                )
                if parse_name and getattr(user, "username", None):
                    good_usernames.append(user.username)
                if parse_id and getattr(user, "id", None) is not None:
                    good_ids.append(str(int(user.id)))

                if kept % 200 == 0:
                    conn.commit()
            except Exception as exc:
                skipped["ошибка сообщения"] = skipped.get("ошибка сообщения", 0) + 1
                log_warn(f"⚠️ Ошибка при обработке сообщения: {type(exc).__name__}")
        conn.commit()
    finally:
        conn.close()

    added_u = _append_unique("usernames.txt", good_usernames, prefix_at=True) if parse_name else 0
    added_i = _append_unique("userids.txt", good_ids, prefix_at=False) if parse_id else 0
    log_ok(
        f"✅ Парсинг из сообщений завершён. Сообщений просмотрено: {scanned}, "
        f"уникальных авторов: {unique_found}, прошло фильтр: {kept}, "
        f"добавлено usernames: {added_u}, ids: {added_i}"
    )
    print()
    print(
        "Готово: парсинг из сообщений завершён. Итоги — в app.log, SQLite и экспортных TXT",
        flush=True,
    )
    if skipped:
        parts = ", ".join(
            f"{k}={v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])
        )
        log_info(f"📉 Отфильтровано/пропущено: {parts}")

def _target_key(

def _target_key(target: Any) -> str:
    """Стабильный ключ для target в ledger."""
    try:
        uname = getattr(target, "username", None)
        if uname:
            return "@" + str(uname)
        tid = getattr(target, "id", None)
        if tid is not None:
            return f"id:{tid}"
    except Exception:
        pass
    return str(target)


def target_ref(target: Any) -> Union[str, int, Any]:
    """Удобная "ссылка" на target, которую можно резолвить в других сессиях.

    Приоритет:
    1) @username (самое стабильное)
    2) peer-id через telethon.utils.get_peer_id (для каналов/супергрупп даёт -100...)
    3) обычный .id
    4) как есть
    """
    try:
        uname = getattr(target, "username", None)
        if uname:
            return "@" + str(uname)
        # get_peer_id работает и для каналов/чатов/юзеров
        try:
            pid = tl_utils.get_peer_id(target)
            if isinstance(pid, int):
                return pid
        except Exception:
            pass
        tid = getattr(target, "id", None)
        if tid is not None:
            return int(tid)
    except Exception:
        pass
    return target


def resolve_target_for_client(client: TelegramClient, target: Any) -> Any:
    """Resolve target with the current session; never reuse another session's InputEntity."""
    ref = target_ref(target)
    try:
        return client.get_input_entity(ref)
    except Exception:
        return client.get_entity(ref)


def _make_client(session_file: str, api_id: int, api_hash: str) -> TelegramClient:
    """Создаёт sync TelethonClient по .session файлу."""
    # session_file хранится как '<name>.session' (basename), а Telethon ждёт имя БЕЗ расширения.
    session_name = session_name_from_file(session_file)
    client = TelegramClient(session_name, api_id, api_hash, flood_sleep_threshold=0)
    client.connect()
    if not client.is_user_authorized():
        raise RuntimeError(f"Сессия не авторизована: {session_file}")
    return client




# -------------------- INVITE ORCHESTRATION (PRO MODE) --------------------

from dataclasses import dataclass


@dataclass
class SessionState:
    session_file: str
    blocked_until: float = 0.0   # unix timestamp
    frozen_until: float = 0.0    # unix timestamp (PeerFlood etc)
    banned: bool = False
    last_invite_at: float = 0.0
    next_invite_at: float = 0.0
    hour_window_start: float = 0.0
    hour_count: int = 0
    day_window_start: float = 0.0
    day_count: int = 0
    ok: int = 0
    fail: int = 0
    attempts: int = 0


def _now() -> float:
    return time.time()


def _is_time_in_window(now_sec: float, start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    """Returns True if local time is inside [start, end] window. Supports window crossing midnight."""
    lt = time.localtime(now_sec)
    cur = lt.tm_hour * 60 + lt.tm_min
    start = int(start_h) * 60 + int(start_m)
    end = int(end_h) * 60 + int(end_m)
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _seconds_until_window_end(now_sec: float, start_h: int, start_m: int, end_h: int, end_m: int) -> int:
    """If we are inside a window, returns seconds until its end, else 0."""
    if not _is_time_in_window(now_sec, start_h, start_m, end_h, end_m):
        return 0
    lt = time.localtime(now_sec)
    cur_min = lt.tm_hour * 60 + lt.tm_min
    end_min = int(end_h) * 60 + int(end_m)
    start_min = int(start_h) * 60 + int(start_m)
    # window not crossing midnight
    if start_min <= end_min:
        minutes_left = max(0, end_min - cur_min)
        return minutes_left * 60
    # crossing midnight
    if cur_min <= end_min:
        return max(0, (end_min - cur_min) * 60)
    # cur >= start -> end is tomorrow
    minutes_left = (24*60 - cur_min) + end_min
    return max(0, minutes_left * 60)


def _pick_best_session(states: List[SessionState]) -> Optional[SessionState]:
    """Pick best available session: not banned, not frozen/blocked, earliest next_invite_at."""
    now = _now()
    candidates = []
    for st in states:
        if st.banned:
            continue
        ready_at = max(st.blocked_until, st.frozen_until, st.next_invite_at)
        candidates.append((ready_at, st.last_invite_at, st.attempts, st))
    if not candidates:
        return None
    # prefer already-ready, else earliest ready time
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    return candidates[0][3]


def _sleep_until_ready(states: List[SessionState], extra_jitter: Tuple[float, float] = (2.0, 6.0)) -> None:
    """If no session is ready now, sleep until the earliest ready moment (plus jitter).

    v10.1: Writes a clear message when ALL sessions are waiting, so it doesn't look like the bot froze.
    For long waits, sleeps in chunks and prints progress occasionally.
    """
    now = _now()
    soonest = None
    for st in states:
        if st.banned:
            continue
        ready_at = max(st.blocked_until, st.frozen_until, st.next_invite_at)
        if soonest is None or ready_at < soonest:
            soonest = ready_at
    if soonest is None:
        return

    wait = max(0.0, soonest - now)
    if wait <= 0:
        return

    # Add small jitter so sessions don't all wake at the exact same moment
    wait = wait + random.uniform(*extra_jitter)

    def _fmt(sec: float) -> str:
        sec = int(max(0, sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if h > 0:
            return f"{h}ч {m}м {s}с"
        if m > 0:
            return f"{m}м {s}с"
        return f"{s}с"

    msg = f"Все сессии на паузе — жду ближайшую примерно через {_fmt(wait)}"
    try:
        print('ℹ️ ' + msg, flush=True)
    except Exception:
        pass
    log_pause(msg)

    # For long waits, sleep in chunks and occasionally report remaining time
    remaining = wait
    last_report = 0.0
    while remaining > 0:
        chunk = 60.0 if remaining > 90 else remaining
        time.sleep(chunk)
        remaining -= chunk
        last_report += chunk
        # report roughly every 5 minutes if still waiting
        if remaining > 120 and last_report >= 300:
            last_report = 0.0
            msg2 = f"Все еще жду: осталось примерно {_fmt(remaining)}"
            try:
                print('ℹ️ ' + msg2, flush=True)
            except Exception:
                pass
            log_pause(msg2)

# -------------------- USER REF HELPERS --------------------

def id_ref_from_userobj(user: Any) -> str:
    """Portable user reference: only Telegram user ID, never access_hash."""
    try:
        uid = getattr(user, "id", None)
        return str(int(uid)) if uid is not None else ""
    except (TypeError, ValueError):
        return ""


def parse_user_ref(raw: Any) -> Tuple[str, Optional[int], Optional[str], UserCandidate]:
    candidate = candidate_from_raw(raw)
    return candidate.key, candidate.user_id, candidate.username, candidate


def resolve_user_for_client(client: TelegramClient, raw: Any) -> Any:
    """Resolve a user independently inside the current Telegram session."""
    candidate = candidate_from_raw(raw)
    if candidate.username:
        try:
            return client.get_input_entity("@" + candidate.username)
        except Exception:
            pass
    if candidate.user_id is not None:
        # This succeeds only when this session knows the entity/access_hash.
        return client.get_input_entity(int(candidate.user_id))
    raise ValueError("Не удалось определить пользователя в текущей сессии")

def prune_users_files(

def prune_users_files(target: Union[str, int, Any], statuses: Tuple[str, ...] = ("ok","already","privacy","invalid"), include_excluded: bool = True) -> Tuple[int,int]:
    """Удаляет из usernames.txt и userids.txt тех, кто уже обработан по target (ledger) и/или в excluded_users.

    Возвращает (removed, kept).
    Делает backup файлов *.bak-YYYYmmdd-HHMMSS
    """
    conn = _db()
    target_key = _target_key(target)

    removed = 0
    kept = 0

    # load excluded cache
    excl = excluded_load_all(conn) if include_excluded else set()

    # build set of processed user_keys for target
    q = "SELECT user_key, status FROM invites WHERE target=?"
    proc = {}
    for uk, st in conn.execute(q, (target_key,)).fetchall():
        proc[uk] = st

    def should_remove(user_key: str) -> bool:
        st = proc.get(user_key)
        if st and st in statuses:
            return True
        if include_excluded and user_key in excl:
            return True
        return False

    import shutil
    from datetime import datetime
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')

    # userids.txt
    path_ids = 'userids.txt'
    if os.path.exists(path_ids):
        shutil.copy2(path_ids, f'{path_ids}.bak-{ts}')
        out_lines = []
        with open(path_ids, 'r', encoding='utf-8') as f:
            for line in f:
                s=line.strip()
                if not s:
                    continue
                # supports id:hash format
                key = None
                if ':' in s:
                    # user_key uses id part
                    id_part = s.split(':',1)[0]
                    if id_part.isdigit():
                        key = f"id:{id_part}"
                elif s.isdigit():
                    key = f'id:{s}'
                if key is None:
                    out_lines.append(line)
                    kept += 1
                    continue
                if should_remove(key):
                    removed += 1
                else:
                    out_lines.append(line)
                    kept += 1
        with open(path_ids, 'w', encoding='utf-8') as f:
            f.writelines(out_lines)

    # usernames.txt
    path_names = 'usernames.txt'
    if os.path.exists(path_names):
        shutil.copy2(path_names, f'{path_names}.bak-{ts}')
        out_lines = []
        with open(path_names, 'r', encoding='utf-8') as f:
            for line in f:
                s=line.strip()
                if not s:
                    continue
                if s.startswith('@'):
                    s=s[1:]
                key = f"u:{s.lower()}"
                if should_remove(key):
                    removed += 1
                else:
                    out_lines.append(line)
                    kept += 1
        with open(path_names, 'w', encoding='utf-8') as f:
            f.writelines(out_lines)

    conn.close()
    return removed, kept

def inviting_rotate_sessions(
    api_id: int,
    api_hash: str,
    session_files: List[str],
    target: Union[str, int, Any],
    users: List[Union[str, int]],
    base_delay: float = 2.0,
    switch_on_floodwait_seconds: int = 60,
    rotate_every: int = 0,
    max_attempts_per_session: int = 0,
    # Soft limits (0 = off)
    per_hour_limit: int = 0,
    per_day_limit: int = 0,
    # Pro additions (safe defaults)
    jitter_min: float = 0.3,
    jitter_max: float = 1.2,
    max_user_attempts: int = 3,
    peerflood_freeze_hours: int = 24,
    floodwait_buffer_seconds: int = 60,
    # Night mode
    night_mode: bool = False,
    night_start: Tuple[int, int] = (2, 0),
    night_end: Tuple[int, int] = (7, 0),
    night_sleep_jitter: Tuple[float, float] = (30.0, 120.0),
) -> None:
    """Invite with smart session orchestration.

    This is a "pro mode" upgrade inspired by ProMax20 inviter:
    - per-session gating (blocked/frozen/banned)
    - fair picking: earliest ready session
    - jittered delays and backoff
    - optional night mode pause window
    - per-user attempt cap (prevents infinite loops)

    Existing behavior kept:
    - ledger skip for ok/already/privacy/invalid
    - switch on big FloodWait; immediate switch on PeerFlood
    """

    if not session_files:
        raise ValueError("Не переданы session_files")

    conn = _db()
    target_key = _target_key(target)

    # global exclude cache (пользователи с вечными ошибками / уже исключённые)
    excluded_cache = excluded_load_all(conn)

    delay = max(1.0, float(base_delay))

    # session states (persisted)
    st_map = session_stats_load(conn, session_files)
    states = [st_map[sf] for sf in session_files]

    state_by_sf = {st.session_file: st for st in states}

    # counters
    ok_cnt = 0
    skip_cnt = 0
    fail_cnt = 0

    # per-session diagnostics counters
    ses_stats: Dict[str, Dict[str, int]] = {sf: {
        "ok": 0,
        "privacy": 0,
        "forbidden": 0,
        "not_mutual": 0,
        "user_kicked": 0,
        "user_blocked": 0,
        "user_channels_too_much": 0,
        "floodwait": 0,
        "peerflood": 0,
        "invalid": 0,
        "network": 0,
        "rpc_other": 0,
        "other": 0,
    } for sf in session_files}

    # per-session counters for planned rotation/attempt limits
    ok_in_session = {sf: 0 for sf in session_files}
    attempts_in_session = {sf: 0 for sf in session_files}

    # per-user attempts in this run
    user_attempts: Dict[str, int] = {}

    # cache of connected clients (keep it small to reduce reconnect storms)
    client_cache: Dict[str, TelegramClient] = {}
    def get_client(sf: str):
        """Возвращает подключенный client или None, если сессия не авторизована/битая."""
        c = client_cache.get(sf)
        if c is not None:
            return c
        try:
            c = _make_client(sf, api_id, api_hash)
        except RuntimeError as e:
            # Не валим весь прогон из-за одной сессии
            st = state_by_sf.get(sf)
            if st:
                st.banned = True
                st.fail += 1
                st.attempts += 1
                st.next_invite_at = max(st.next_invite_at, _now() + 3600)
                try:
                    session_stats_save(conn, st)
                except Exception:
                    pass
            log_warn(f"⚠️ Пропуск сессии {sf}: {e}")
            return None
        client_cache[sf] = c
        return c

    def close_all_clients() -> None:
        for c in list(client_cache.values()):
            try:
                c.disconnect()
            except Exception:
                pass
        client_cache.clear()

    log_info(
        f"🚀 Старт инвайта (PRO) в: {target_key}. Кандидатов: {len(users)}. Сессий: {len(session_files)}"
    )

    for raw in users:
        # Night mode pause
        if night_mode:
            now = _now()
            if _is_time_in_window(now, night_start[0], night_start[1], night_end[0], night_end[1]):
                sec_left = _seconds_until_window_end(now, night_start[0], night_start[1], night_end[0], night_end[1])
                if sec_left > 0:
                    log_pause(f"🌙 Ночной режим: пауза до конца окна ({sec_left//60} мин).")
                    time.sleep(sec_left + random.uniform(*night_sleep_jitter))
        # normalize user
        user_key, user_id, username, entity = parse_user_ref(raw)

        # global exclude (вечные отказы/неинвайтабельные)
        if user_key in excluded_cache:
            skip_cnt += 1
            # дополнительно фиксируем в ledger как skip, чтобы было видно в БД
            try:
                rsn = excluded_reason(conn, user_key)
            except Exception:
                rsn = 'excluded'
            ledger_put(conn, target_key, user_key, user_id, username, 'skip', f'excluded:{rsn}')
            continue


        prev = ledger_get(conn, target_key, user_key)
        if prev and prev[0] in ("ok", "already", "privacy", "invalid"):
            skip_cnt += 1
            continue

        # cap attempts per user (in this run)
        user_attempts[user_key] = user_attempts.get(user_key, 0) + 1
        if max_user_attempts and user_attempts[user_key] > int(max_user_attempts):
            ledger_put(conn, target_key, user_key, user_id, username, "skip", f"max_attempts={max_user_attempts}")
            skip_cnt += 1
            log_warn(f"⏭️ Пропуск (лимит попыток) для {('@'+username) if username else user_key}")
            continue

        # apply per-session soft limits (hour/day)
        if per_hour_limit or per_day_limit:
            for _st in states:
                due = session_next_time_due_to_limits(_st, per_hour_limit, per_day_limit)
                if due and due > _now():
                    _st.next_invite_at = max(_st.next_invite_at, due)

        # pick session
        st = _pick_best_session(states)
        if st is None:
            log_stop("⛔ Нет доступных сессий.")
            break

        # if all sessions are waiting, sleep until any ready
        ready_at = max(st.blocked_until, st.frozen_until, st.next_invite_at)
        if ready_at > _now():
            _sleep_until_ready(states)

        st = _pick_best_session(states)
        if st is None:
            log_stop("⛔ Нет доступных сессий.")
            break

        sf = st.session_file
        client = get_client(sf)
        if client is None:
            # сессия помечена как невалидная в get_client; пробуем следующую
            continue

        # jitter before action
        time.sleep(delay + random.uniform(float(jitter_min), float(jitter_max)))

        # Resolve both target and user independently in the selected session.
        try:
            target_entity = resolve_target_for_client(client, target)
            invitee_entity = resolve_user_for_client(client, entity)
        except ValueError:
            ledger_put(conn, target_key, user_key, user_id, username, "failed", "cannot_resolve_in_session")
            st.fail += 1
            fail_cnt += 1
            log_warn(f"⏭️ {sf}: не удалось резолвить пользователя {user_key} в этой сессии")
            try:
                session_stats_save(conn, st)
            except Exception:
                pass
            continue
        except Exception as exc:
            st.fail += 1
            fail_cnt += 1
            st.blocked_until = max(st.blocked_until, _now() + 60)
            log_warn(f"🌐 {sf}: не удалось резолвить цель/пользователя ({type(exc).__name__})")
            try:
                session_stats_save(conn, st)
            except Exception:
                pass
            continue

        try:
            st.attempts += 1
            attempts_in_session[sf] = attempts_in_session.get(sf, 0) + 1

            client(InviteToChannelRequest(channel=target_entity, users=[invitee_entity]))

            ledger_put(conn, target_key, user_key, user_id, username, "ok", f"session={sf}")
            # consume rolling limits
            session_consume_invite_token(st, per_hour_limit, per_day_limit)
            ok_cnt += 1
            st.ok += 1
            ok_in_session[sf] = ok_in_session.get(sf, 0) + 1
            st.last_invite_at = _now()
            st.next_invite_at = st.last_invite_at + max(1.0, delay)

            log_ok(f"✅ Инвайт отправлен: {('@'+username) if username else user_key} → {target_key} | {sf}")

            # gentle adaptive delay
            delay = min(10.0, max(1.5, delay + random.uniform(-0.15, 0.35)))

            # planned rotation by successes on a session
            if rotate_every and ok_in_session.get(sf, 0) >= int(rotate_every):
                ok_in_session[sf] = 0
                # add a small penalty so other sessions get picked
                st.next_invite_at = max(st.next_invite_at, _now() + random.uniform(3.0, 8.0))

        except UserAlreadyParticipantError:
            ledger_put(conn, target_key, user_key, user_id, username, "already", "уже участник")
            skip_cnt += 1
            log_info(f"👤 Уже в чате: {('@'+username) if username else user_key}")

        except UserPrivacyRestrictedError:
            ledger_put(conn, target_key, user_key, user_id, username, "privacy", "закрыты инвайты")
            try:
                excluded_add(conn, user_key, user_id, username, "privacy")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            ses_stats[sf]["privacy"] += 1
            log_warn(f"🔒 Закрыты инвайты: {('@'+username) if username else user_key}")

        except UserNotMutualContactError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "not_mutual_contact")
            try:
                excluded_add(conn, user_key, user_id, username, "not_mutual_contact")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            ses_stats[sf]["not_mutual"] += 1
            log_warn(f"🙅‍♂️ Не взаимный контакт/нельзя инвайтить: {('@'+username) if username else user_key}")

        except UserChannelsTooMuchError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_channels_too_much")
            try:
                excluded_add(conn, user_key, user_id, username, "user_channels_too_much")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            ses_stats[sf]["user_channels_too_much"] += 1
            log_warn(f"📛 У пользователя слишком много чатов/каналов: {('@'+username) if username else user_key}")

        except UserKickedError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_kicked")
            try:
                excluded_add(conn, user_key, user_id, username, "user_kicked")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            ses_stats[sf]["user_kicked"] += 1
            log_warn(f"🚫 Пользователь кикнут/забанен в цели: {('@'+username) if username else user_key}")

        except UserBlockedError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_blocked")
            try:
                excluded_add(conn, user_key, user_id, username, "user_blocked")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            ses_stats[sf]["user_blocked"] += 1
            log_warn(f"🚫 Пользователь заблокирован/недоступен: {('@'+username) if username else user_key}")

        except ChatWriteForbiddenError as e:
            # Обычно означает ограничение/запрет на стороне ИМЕННО этой сессии в цели.
            diag = _diagnose_invite_context(client, target_entity)
            try:
                if isinstance(diag, dict) and (diag.get('participant_error') == 'UserNotParticipantError' or diag.get('perm_error') == 'UserNotParticipantError'):
                    excluded_add(conn, user_key, user_id, username, 'user_not_participant')
                    excluded_cache.add(user_key)
            except Exception:
                pass
            try:
                if str(diag.get('participant_error') or '') == 'UserNotParticipantError':
                    excluded_add(conn, user_key, user_id, username, 'user_not_participant')
                    excluded_cache.add(user_key)
            except Exception:
                pass
            ledger_put(conn, target_key, user_key, user_id, username, "forbidden", f"{type(e).__name__}")
            st.fail += 1
            fail_cnt += 1

            # Не долбим эту сессию — отложим на 7 дней (можно поменять позже)
            st.blocked_until = max(st.blocked_until, _now() + 7 * 24 * 3600)
            log_warn(
                f"🚫 ChatWriteForbidden на {sf} при инвайте {('@'+username) if username else user_key} → {target_key}. "
                f"Диагностика: {diag}"
            )

        except FloodWaitError as e:
            sec = int(getattr(e, "seconds", 0) or 0)
            ledger_put(conn, target_key, user_key, user_id, username, "floodwait", f"{sec}")

            st.fail += 1
            fail_cnt += 1

            # block this session for wait + buffer
            st.blocked_until = max(st.blocked_until, _now() + sec + int(floodwait_buffer_seconds))

            if sec > int(switch_on_floodwait_seconds):
                log_pause(f"💤 FloodWait {sec}s (>{switch_on_floodwait_seconds}). Блокирую {sf} и продолжаю другой сессией…")
            else:
                log_pause(f"💤 FloodWait {sec}s. Блокирую {sf} и продолжаю…")

            # backoff for global delay
            delay = min(15.0, max(delay, 6.0))

        except (UsernameInvalidError, UserIdInvalidError):
            ledger_put(conn, target_key, user_key, user_id, username, "invalid", "некорректный пользователь")
            try:
                excluded_add(conn, user_key, user_id, username, "invalid_user")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            ses_stats[sf]["invalid"] += 1
            log_warn(f"❌ Невалидный пользователь: {raw}")

        except ChatAdminRequiredError:
            ledger_put(conn, target_key, user_key, user_id, username, "stop", "нет прав на инвайт")
            log_stop(f"⛔ Нет прав на инвайт в {target_key}. Останавливаю прогон.")
            break

        except PeerFloodError:
            ledger_put(conn, target_key, user_key, user_id, username, "peerflood", "PeerFlood/лимит на аккаунте")
            st.fail += 1
            fail_cnt += 1
            # freeze session for long time
            freeze_sec = int(peerflood_freeze_hours) * 3600
            st.frozen_until = max(st.frozen_until, _now() + freeze_sec)
            log_stop(f"⛔ PeerFlood на {sf}: замораживаю на {peerflood_freeze_hours}ч и продолжаю другой сессией.")
        except ValueError:
            # Обычно это значит: по одному user_id не хватает access_hash (Telethon не может резолвить)
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "нет access_hash / не могу резолвить по id")
            try:
                excluded_add(conn, user_key, user_id, username, "no_access_hash")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            log_warn(f"⏭️ Пропуск: не могу инвайтить {raw} (нужен @username или id:access_hash).")

        except (ConnectionResetError, ConnectionError, OSError) as e:
            # Сетевой сбой/ресет соединения — не вина пользователя.
            st.fail += 1
            fail_cnt += 1
            st.blocked_until = max(st.blocked_until, _now() + 60)
            log_warn(f"🌐 Сеть/соединение для {sf}: {type(e).__name__}. Пауза 60с и продолжаю другой сессией…")
            try:
                client.disconnect()
            except Exception:
                pass

        except RPCError as e:
            ledger_put(conn, target_key, user_key, user_id, username, "failed", f"{type(e).__name__}")
            st.fail += 1
            fail_cnt += 1
            log_warn(f"⚠️ Ошибка RPC ({type(e).__name__}) для {raw}")

        except Exception as e:
            ledger_put(conn, target_key, user_key, user_id, username, "failed", f"{type(e).__name__}")
            st.fail += 1
            fail_cnt += 1
            log_warn(f"⚠️ Неизвестная ошибка ({type(e).__name__}) для {raw}")

        # persist session state
        try:
            session_stats_save(conn, st)
        except Exception:
            pass

        # per-session attempt cap (if enabled)
        if max_attempts_per_session and attempts_in_session.get(sf, 0) >= int(max_attempts_per_session):
            attempts_in_session[sf] = 0
            st.next_invite_at = max(st.next_invite_at, _now() + random.uniform(10.0, 25.0))
            log_info(f"🔁 Лимит попыток на {sf}: делаю паузу для этой сессии.")

    log_ok(f"🏁 Инвайт завершён. Успех: {ok_cnt}, пропуск: {skip_cnt}, ошибки: {fail_cnt}")

    # Session summary (helps to understand why some accounts fail)
    try:
        for sf in session_files:
            s = ses_stats.get(sf) or {}
            log_info(
                f"📊 Итоги сессии {sf}: "
                f"ok={s.get('ok',0)} forbidden={s.get('forbidden',0)} privacy={s.get('privacy',0)} "
                f"not_mutual={s.get('not_mutual',0)} user_blocked={s.get('user_blocked',0)} user_kicked={s.get('user_kicked',0)} "
                f"user_channels_too_much={s.get('user_channels_too_much',0)} "
                f"floodwait={s.get('floodwait',0)} peerflood={s.get('peerflood',0)} network={s.get('network',0)} "
                f"invalid={s.get('invalid',0)} rpc_other={s.get('rpc_other',0)} other={s.get('other',0)}"
            )
    except Exception:
        pass
    close_all_clients()
    conn.close()



def inviting(client: TelegramClient, target: Union[str, int, Any], users: List[Union[str, int]], base_delay: float = 2.0) -> None:
    """Инвайт одним клиентом (1 сессия).

    - учитывает ledger (не трогает уже обработанных для этой цели)
    - учитывает global excluded_users (вечные отказы)
    """
    conn = _db()
    target_key = _target_key(target)
    excluded_cache = excluded_load_all(conn)

    log_info(f"🚀 Старт инвайта в: {target_key}. Кандидатов: {len(users)}")
    ok_cnt = 0
    skip_cnt = 0
    fail_cnt = 0

    delay = max(1.0, float(base_delay))

    target_entity = resolve_target_for_client(client, target)

    for raw in users:
        user_key, user_id, username, entity = parse_user_ref(raw)

        if user_key in excluded_cache:
            skip_cnt += 1
            continue

        prev = ledger_get(conn, target_key, user_key)
        if prev and prev[0] in ("ok", "already", "privacy", "invalid"):
            skip_cnt += 1
            continue

        time.sleep(delay + random.uniform(0.3, 1.2))

        try:
            invitee_entity = resolve_user_for_client(client, entity)
            client(InviteToChannelRequest(channel=target_entity, users=[invitee_entity]))
            ledger_put(conn, target_key, user_key, user_id, username, "ok", "ok")
            ok_cnt += 1
            log_ok(f"✅ Инвайт отправлен: {('@'+username) if username else user_key} → {target_key}")
            delay = min(8.0, max(1.5, delay + random.uniform(-0.2, 0.4)))

        except UserAlreadyParticipantError:
            ledger_put(conn, target_key, user_key, user_id, username, "already", "уже участник")
            skip_cnt += 1
            log_info(f"👤 Уже в чате: {('@'+username) if username else user_key}")

        except UserPrivacyRestrictedError:
            ledger_put(conn, target_key, user_key, user_id, username, "privacy", "закрыты инвайты")
            try:
                excluded_add(conn, user_key, user_id, username, "privacy")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            log_warn(f"🔒 Закрыты инвайты: {('@'+username) if username else user_key}")

        except UserNotMutualContactError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "not_mutual_contact")
            try:
                excluded_add(conn, user_key, user_id, username, "not_mutual_contact")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            log_warn(f"🙅‍♂️ Не взаимный контакт/нельзя инвайтить: {('@'+username) if username else user_key}")

        except UserChannelsTooMuchError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_channels_too_much")
            try:
                excluded_add(conn, user_key, user_id, username, "user_channels_too_much")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            log_warn(f"📛 У пользователя слишком много чатов/каналов: {('@'+username) if username else user_key}")

        except UserKickedError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_kicked")
            try:
                excluded_add(conn, user_key, user_id, username, "user_kicked")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            log_warn(f"🚫 Пользователь кикнут/забанен в цели: {('@'+username) if username else user_key}")

        except UserBlockedError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_blocked")
            try:
                excluded_add(conn, user_key, user_id, username, "user_blocked")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            log_warn(f"🚫 Пользователь заблокирован/недоступен: {('@'+username) if username else user_key}")

        except ChatWriteForbiddenError as e:
            diag = _diagnose_invite_context(client, target_entity)
            try:
                if isinstance(diag, dict) and (diag.get('participant_error') == 'UserNotParticipantError' or diag.get('perm_error') == 'UserNotParticipantError'):
                    excluded_add(conn, user_key, user_id, username, 'user_not_participant')
                    excluded_cache.add(user_key)
            except Exception:
                pass
            ledger_put(conn, target_key, user_key, user_id, username, "forbidden", f"{type(e).__name__}")
            fail_cnt += 1
            log_warn(f"🚫 ChatWriteForbidden при инвайте {('@'+username) if username else user_key} → {target_key}. Диагностика: {diag}")

        except FloodWaitError as e:
            sec = int(getattr(e, "seconds", 0) or 0)
            ledger_put(conn, target_key, user_key, user_id, username, "floodwait", f"{sec}")
            log_pause(f"💤 FloodWait {sec} сек. Ожидаю и продолжаю…")
            time.sleep(sec + random.uniform(1.0, 3.0))
            delay = min(12.0, max(delay, 6.0))
            fail_cnt += 1

        except (UsernameInvalidError, UserIdInvalidError):
            ledger_put(conn, target_key, user_key, user_id, username, "invalid", "некорректный пользователь")
            try:
                excluded_add(conn, user_key, user_id, username, "invalid_user")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            log_warn(f"❌ Невалидный пользователь: {raw}")

        except ChatAdminRequiredError:
            ledger_put(conn, target_key, user_key, user_id, username, "stop", "нет прав на инвайт")
            log_stop(f"⛔ Нет прав на инвайт в {target_key}. Останавливаю прогон.")
            break

        except PeerFloodError:
            ledger_put(conn, target_key, user_key, user_id, username, "peerflood", "PeerFlood/лимит на аккаунте")
            log_stop("⛔ PeerFlood: аккаунт под лимитом/подозрением. Останавливаю прогон, чтобы не улететь в бан.")
            break

        except ValueError:
            ledger_put(conn, target_key, user_key, user_id, username, "skip", "нет access_hash / не могу резолвить по id")
            try:
                excluded_add(conn, user_key, user_id, username, "no_access_hash")
                excluded_cache.add(user_key)
            except Exception:
                pass
            skip_cnt += 1
            log_warn(f"⏭️ Пропуск: не могу инвайтить {raw} (нужен @username или id:access_hash).")

        except (ConnectionResetError, ConnectionError, OSError) as e:
            ledger_put(conn, target_key, user_key, user_id, username, "failed", f"{type(e).__name__}")
            fail_cnt += 1
            log_warn(f"🌐 Сеть/соединение: {type(e).__name__}. Пауза 30с и продолжаю…")
            time.sleep(30)

        except RPCError as e:
            ledger_put(conn, target_key, user_key, user_id, username, "failed", f"{type(e).__name__}")
            fail_cnt += 1
            log_warn(f"⚠️ Ошибка RPC ({type(e).__name__}) для {raw}")

        except Exception as e:
            ledger_put(conn, target_key, user_key, user_id, username, "failed", f"{type(e).__name__}")
            fail_cnt += 1
            log_warn(f"⚠️ Неизвестная ошибка ({type(e).__name__}) для {raw}")

    log_ok(f"🏁 Инвайт завершён. Успех: {ok_cnt}, пропуск: {skip_cnt}, ошибки: {fail_cnt}")
    conn.close()

# -------------------- CONFIG UI --------------------

def _list_sessions() -> List[str]:
    return list_session_files()

def _create_account_session(api_id: int, api_hash: str) -> None:
    os.system("cls||clear")
    phone = input("Введите номер телефона аккаунта (формат +79991234567): ").strip()
    if not phone:
        print("Пустой номер.")
        time.sleep(1.5)
        return

    # ВАЖНО: session = path/name (БЕЗ .session). Telethon создаст sessoins/<phone>.session
    session_name = session_name_from_file(f"{phone}.session")
    client = TelegramClient(session_name, api_id, api_hash, flood_sleep_threshold=0)
    print("Сейчас придёт код в Telegram. Введите код и (если спросит) пароль 2FA.")
    client.start(phone=phone)
    client.disconnect()
    if os.name != "nt":
        try:
            os.chmod(session_name + ".session", 0o600)
        except OSError:
            pass

    log_ok(f"📲 Аккаунт добавлен: {phone}.session (папка {SESSIONS_DIR}/)")
    print("Готово. Сессия создана.")
    time.sleep(1.5)

def config() -> None:
    ensure_options()
    while True:
        os.system("cls||clear")
        options = getoptions()
        sessions = _list_sessions()

        print("=== НАСТРОЙКИ ===")
        print(f"1 - Обновить api_id   [{options[0].strip()}]")
        _hash = options[1].strip()
        _masked_hash = (
            (_hash[:4] + "****" + _hash[-4:])
            if _hash not in ("", "NONEHASH") and len(_hash) > 8
            else ("****" if _hash not in ("", "NONEHASH") else _hash)
        )
        print(f"2 - Обновить api_hash [{_masked_hash}]")
        print(f"3 - Парсить user-id   [{options[2].strip()}]")
        print(f"4 - Парсить user-name [{options[3].strip()}]")
        print(f"5 - Добавить аккаунт  [{len(sessions)}]")
        print("6 - Сбросить настройки")
        print("e - Выход")
        key = input("Ввод: ").strip()

        if key == "1":
            os.system("cls||clear")
            options[0] = input("Введите API_ID: ").strip() + "\n"
        elif key == "2":
            os.system("cls||clear")
            options[1] = input("Введите API_HASH: ").strip() + "\n"
        elif key == "3":
            options[2] = "False\n" if options[2].strip() == "True" else "True\n"
        elif key == "4":
            options[3] = "False\n" if options[3].strip() == "True" else "True\n"
        elif key == "5":
            # создать новую сессию
            if options[0].strip() in ("NONEID", "") or options[1].strip() in ("NONEHASH", ""):
                print("Сначала задайте API_ID и API_HASH.")
                time.sleep(1.8)
                continue
            try:
                api_id = int(options[0].strip())
            except Exception:
                print("API_ID должен быть числом.")
                time.sleep(1.8)
                continue
            _create_account_session(api_id, options[1].strip())
        elif key == "6":
            os.system("cls||clear")
            answer = input("Сбросить API_ID/API_HASH и опции парсинга?\n1 - Да\n2 - Нет\nВвод: ").strip()
            if answer == "1":
                options = DEFAULT_OPTIONS.copy()
        elif key.lower() == "e":
            break
        else:
            print("Неверный пункт.")
            time.sleep(1.0)
            continue

        # сохраняем изменения настроек
        with open("options.txt", "w", encoding="utf-8") as f:
            f.writelines(options)

        # небольшая пауза, чтобы меню не "мигало"
        time.sleep(0.2)




# -------------------- PRE-FLIGHT (PRO) --------------------

def _ensure_in_target(client: TelegramClient, target_entity, auto_join: bool = True) -> Tuple[bool, str]:
    """Return (ok, reason).

    Reasons:
      ok | joined | cannot_join | channel_private | banned_in_channel | flood_wait | network | unknown
    """
    try:
        me = client.get_me()
        client(GetParticipantRequest(channel=target_entity, participant=me))
        return True, "ok"
    except UserNotParticipantError:
        if not auto_join:
            return False, "not_participant"
        try:
            client(JoinChannelRequest(target_entity))
            me = client.get_me()
            client(GetParticipantRequest(channel=target_entity, participant=me))
            return True, "joined"
        except FloodWaitError:
            return False, "flood_wait"
        except ChannelPrivateError:
            return False, "channel_private"
        except UserBannedInChannelError:
            return False, "banned_in_channel"
        except (OSError, ConnectionError):
            return False, "network"
        except Exception:
            return False, "cannot_join"
    except FloodWaitError:
        return False, "flood_wait"
    except ChannelPrivateError:
        return False, "channel_private"
    except UserBannedInChannelError:
        return False, "banned_in_channel"
    except (OSError, ConnectionError):
        return False, "network"
    except RPCError as e:
        # give caller a hint what exactly happened
        return False, f"rpc_{e.__class__.__name__}"
    except Exception:
        return False, "unknown"


def preflight_sessions_for_target(
    api_id: int,
    api_hash: str,
    session_files: List[str],
    target,
    auto_join: bool = True,
    block_cannot_join_hours: int = 24,
) -> Dict[str, List[str]]:
    """PRO preflight: checks auth + membership, optionally joins target.

    Returns dict with lists:
      ok, joined, not_authorized, cannot_join, no_rights, flood_wait, network, unknown
    """
    report: Dict[str, List[str]] = {
        "ok": [],
        "joined": [],
        "not_authorized": [],
        "cannot_join": [],
        "no_rights": [],
        "flood_wait": [],
        "network": [],
        "unknown": [],
    }

    if not session_files:
        return report

    conn = _db()
    st_map = session_stats_load(conn, session_files)

    # Resolve target to entity if needed
    target_entity = target
    if isinstance(target, str):
        resolved = None
        for sf in session_files:
            try:
                c = _make_client(sf, api_id, api_hash)
                resolved = c.get_entity(target)
                try:
                    c.disconnect()
                except Exception:
                    pass
                break
            except Exception:
                try:
                    c.disconnect()
                except Exception:
                    pass
                continue
        if resolved is None:
            raise RuntimeError("Не удалось резолвить цель для preflight")
        target_entity = resolved

    now = int(time.time())
    block_sec = max(0, int(block_cannot_join_hours)) * 3600

    for sf in session_files:
        st = st_map.get(sf)
        try:
            client = _make_client(sf, api_id, api_hash)
        except RuntimeError:
            # not authorized
            if st:
                st.banned = True
                st.fail += 1
                st.attempts += 1
                st.next_invite_at = max(st.next_invite_at, now + 3600)
                try:
                    session_stats_save(conn, st)
                except Exception:
                    pass
            report["not_authorized"].append(sf)
            log_warn(f"⚠️ Preflight: {sf} — сессия не авторизована")
            continue
        except Exception as e:
            report["unknown"].append(sf)
            log_warn(f"⚠️ Preflight: {sf} — ошибка создания клиента: {e}")
            continue

        try:
            ok, reason = _ensure_in_target(client, target_entity, auto_join=auto_join)
            if ok and reason == "ok":
                report["ok"].append(sf)
                log_ok(f"✅ Preflight: {sf} — уже в цели")
            elif ok and reason == "joined":
                report["joined"].append(sf)
                log_ok(f"✅ Preflight: {sf} — вступил в цель")
            else:
                if reason in ("cannot_join", "not_participant", "channel_private", "banned_in_channel"):
                    report["cannot_join"].append(sf)
                    if reason == "channel_private":
                        log_warn(f"⛔ Preflight: {sf} — цель приватная/нет доступа")
                    elif reason == "banned_in_channel":
                        log_warn(f"⛔ Preflight: {sf} — аккаунт забанен в цели")
                    else:
                        log_warn(f"⛔ Preflight: {sf} — не смог вступить/нет доступа")
                    if st:
                        st.blocked_until = max(st.blocked_until, now + (block_sec or 3600))
                        st.fail += 1
                        st.attempts += 1
                        try:
                            session_stats_save(conn, st)
                        except Exception:
                            pass
                elif reason == "flood_wait":
                    report["flood_wait"].append(sf)
                    log_warn(f"⏳ Preflight: {sf} — FloodWait (пауза)")
                    if st:
                        # минимально на 10 минут, дальше уже inviter поймает точное время
                        st.blocked_until = max(st.blocked_until, now + 600)
                        st.fail += 1
                        st.attempts += 1
                        try:
                            session_stats_save(conn, st)
                        except Exception:
                            pass
                elif reason == "network":
                    report["network"].append(sf)
                    log_warn(f"🌐 Preflight: {sf} — сетевой сбой (пропуск)")
                    if st:
                        st.blocked_until = max(st.blocked_until, now + 120)
                        st.fail += 1
                        st.attempts += 1
                        try:
                            session_stats_save(conn, st)
                        except Exception:
                            pass
                else:
                    report["unknown"].append(sf)
                    if isinstance(reason, str) and reason.startswith("rpc_"):
                        log_warn(f"⚠️ Preflight: {sf} — RPC ошибка при проверке: {reason}")
                    else:
                        log_warn(f"⚠️ Preflight: {sf} — неизвестная ошибка проверки: {reason}")

            # Если мы в цели — дополнительно проверим права приглашать
            if (sf in report["ok"] or sf in report["joined"]):
                try:
                    perms = client.get_permissions(target_entity, "me")
                    invite_flag = getattr(perms, "invite_users", None)
                    # если атрибут есть и явно False — значит прав нет
                    if invite_flag is False:
                        report["no_rights"].append(sf)
                        # выкинем из ok/joined чтобы не бралась в ротацию
                        if sf in report["ok"]:
                            report["ok"].remove(sf)
                        if sf in report["joined"]:
                            report["joined"].remove(sf)
                        log_warn(f"🚫 Preflight: {sf} — нет прав приглашать (invite_users=False)")
                        if st:
                            st.blocked_until = max(st.blocked_until, now + 86400)
                            st.fail += 1
                            st.attempts += 1
                            try:
                                session_stats_save(conn, st)
                            except Exception:
                                pass
                except ChatWriteForbiddenError:
                    report["no_rights"].append(sf)
                    if sf in report["ok"]:
                        report["ok"].remove(sf)
                    if sf in report["joined"]:
                        report["joined"].remove(sf)
                    log_warn(f"🚫 Preflight: {sf} — ChatWriteForbidden (нет прав/ограничен)")
                    if st:
                        st.blocked_until = max(st.blocked_until, now + 86400)
                        st.fail += 1
                        st.attempts += 1
                        try:
                            session_stats_save(conn, st)
                        except Exception:
                            pass
                except (OSError, ConnectionError):
                    # не считаем критичным: просто отметим сеть
                    if sf not in report["network"]:
                        report["network"].append(sf)
                    log_warn(f"🌐 Preflight: {sf} — сетевой сбой при проверке прав")
                except Exception:
                    # если не смогли проверить права — оставим как есть
                    pass
        except Exception as e:
            report["unknown"].append(sf)
            log_warn(f"⚠️ Preflight: {sf} — ошибка: {e}")
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    return report
# -------------------------------------------------------------------
# (Опционально) экспортируем публичные функции для удобного импорта
__all__ = [
    "config",
    "getoptions",
    "parsing",
    "parsing_from_messages",
    "inviting",
    "inviting_rotate_sessions",
    "preflight_sessions_for_target",
    "target_ref",
]
