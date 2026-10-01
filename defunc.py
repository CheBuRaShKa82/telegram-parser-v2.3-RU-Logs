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
import csv
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Iterable, List, Optional, Tuple, Union, Dict, Any

from storage import (
    UserCandidate,
    candidate_from_raw,
    checkpoint_clear,
    checkpoint_get,
    checkpoint_put,
    connect_db,
    exclusion_add,
    exclusion_has,
    exclusion_load_keys,
    exclusion_reason,
    invite_record,
    invite_state_get,
    export_users_rows,
    upsert_user,
)

from telethon.sync import TelegramClient
from telethon import utils as tl_utils
from telethon.tl.functions.channels import (
    InviteToChannelRequest,
    JoinChannelRequest,
    GetParticipantRequest,
    GetFullChannelRequest,
)
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
    if os.name != "nt":
        try:
            os.chmod(SESSIONS_DIR, 0o700)
        except OSError:
            pass
    # Мягкая миграция: если старые .session лежат рядом со скриптом — перенесём их в sessoins/
    try:
        for sf in Path(".").glob("*.session"):
            if not sf.is_file():
                continue
            dst = Path(SESSIONS_DIR) / sf.name
            if dst.exists():
                continue
            sf.rename(dst)
        if os.name != "nt":
            for session_path in Path(SESSIONS_DIR).glob("*.session"):
                try:
                    os.chmod(session_path, 0o600)
                except OSError:
                    pass
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
        if os.name != "nt":
            try:
                os.chmod("options.txt", 0o600)
            except OSError:
                pass
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
    if filters.require_photo and not getattr(user, "photo", None):
        return False, "нет фото"
    if filters.active_days > 0 and not _is_active_within(
        getattr(user, "status", None), filters.active_days
    ):
        return False, "не активен"
    return True, "ok"


def quality_hard(user: Any) -> Tuple[bool, str]:
    """Legacy v2.3 hard filter kept for compatibility."""
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
            status TEXT DEFAULT 'active',
            status_reason TEXT DEFAULT '',
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
    cols = {row[1] for row in conn.execute("PRAGMA table_info(session_stats)").fetchall()}
    if "status" not in cols:
        conn.execute("ALTER TABLE session_stats ADD COLUMN status TEXT DEFAULT 'active'")
    if "status_reason" not in cols:
        conn.execute("ALTER TABLE session_stats ADD COLUMN status_reason TEXT DEFAULT ''")
    conn.commit()
    return conn

def ledger_get(
    conn: sqlite3.Connection,
    target: str,
    user_key: str,
) -> Optional[Tuple[str, str]]:
    return invite_state_get(conn, target, user_key)


def ledger_put(
    conn: sqlite3.Connection,
    target: str,
    user_key: str,
    user_id: Optional[int],
    username: Optional[str],
    status: str,
    reason: str = "",
    *,
    session_file: Optional[str] = None,
    flood_seconds: Optional[int] = None,
    count_attempt: bool = True,
) -> None:
    invite_record(
        conn,
        target=target,
        user_key=user_key,
        user_id=user_id,
        username=username,
        status=status,
        reason=reason,
        session_file=session_file,
        flood_seconds=flood_seconds,
        count_attempt=count_attempt,
    )
    # Keep the v2.3 snapshot table updated for backwards compatibility.
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO invites(target, user_key, user_id, username, status, reason, ts)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(target, user_key) DO UPDATE SET
            user_id=excluded.user_id,
            username=excluded.username,
            status=excluded.status,
            reason=excluded.reason,
            ts=excluded.ts
        """,
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

def session_stats_load(conn: sqlite3.Connection, session_files: List[str]) -> Dict[str, "SessionState"]:
    """Load persisted session states from DB (blocked/frozen/banned + rolling counters).

    Returns dict session_file -> SessionState. Missing sessions get defaults and are inserted.
    """
    out: Dict[str, SessionState] = {}
    now = _now()
    for sf in session_files:
        cur = conn.execute(
            "SELECT blocked_until,frozen_until,banned,status,status_reason,ok,fail,attempts,last_invite_at,next_invite_at,"
            "hour_window_start,hour_count,day_window_start,day_count FROM session_stats WHERE session_file=?",
            (sf,),
        )
        row = cur.fetchone()
        if row:
            st = SessionState(session_file=sf)
            st.blocked_until = float(row[0] or 0)
            st.frozen_until = float(row[1] or 0)
            st.banned = bool(row[2] or 0)
            st.status = str(row[3] or "active")
            st.status_reason = str(row[4] or "")
            st.ok = int(row[5] or 0)
            st.fail = int(row[6] or 0)
            st.attempts = int(row[7] or 0)
            st.last_invite_at = float(row[8] or 0)
            st.next_invite_at = float(row[9] or 0)
            st.hour_window_start = float(row[10] or 0)
            st.hour_count = int(row[11] or 0)
            st.day_window_start = float(row[12] or 0)
            st.day_count = int(row[13] or 0)
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
        "UPDATE session_stats SET blocked_until=?, frozen_until=?, banned=?, status=?, status_reason=?, ok=?, fail=?, attempts=?, "
        "last_invite_at=?, next_invite_at=?, hour_window_start=?, hour_count=?, day_window_start=?, day_count=?, updated_at=? "
        "WHERE session_file=?",
        (
            float(st.blocked_until or 0),
            float(st.frozen_until or 0),
            1 if st.banned else 0,
            str(getattr(st, "status", "active") or "active"),
            str(getattr(st, "status_reason", "") or ""),
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

def _source_metadata(
    chat_entity: Any,
    source_type: str,
) -> Tuple[Optional[str], Optional[str], str]:
    source_id = getattr(chat_entity, "id", None)
    source_title = getattr(chat_entity, "title", None)
    if source_title is None:
        source_title = getattr(chat_entity, "username", None)
    try:
        if source_id is None and not isinstance(chat_entity, (str, int)):
            source_id = tl_utils.get_peer_id(chat_entity)
    except Exception:
        pass

    # Manual @username / t.me link / numeric ID must get its own stable
    # checkpoint instead of collapsing into "unknown".
    if source_id is None and isinstance(chat_entity, int):
        source_id = chat_entity
    if source_id is None and isinstance(chat_entity, str):
        raw = chat_entity.strip()
        if raw:
            source_id = raw
            if not source_title:
                source_title = raw

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
    batch = max(1, int(checkpoint_batch or 100))
    good_usernames: List[str] = []
    good_ids: List[str] = []
    total = 0
    kept = 0
    skipped: Dict[str, int] = {}
    conn = _db()
    source_id, source_title, source_type = _source_metadata(
        chat_entity, "participants"
    )
    cp_key = _parser_checkpoint_key(source_id, source_title, source_type)
    error_name: Optional[str] = None

    log_info(
        f"🔍 Парсинг участников: {source_title or source_id or chat_entity} | "
        f"checkpoint={cp_key}"
    )

    try:
        try:
            for user in client.iter_participants(chat_entity):
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
    """Collect active message authors with cursor-based resume."""
    filters = filters or DEFAULT_PARSER_FILTERS
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
    conn = _db()
    source_id, source_title, source_type = _source_metadata(
        chat_entity, "messages"
    )
    cp_key = _parser_checkpoint_key(source_id, source_title, source_type)
    cp = checkpoint_get(conn, cp_key) if resume else None
    offset_id = int(cp.cursor_int) if cp and cp.cursor_int else 0
    last_cursor = offset_id or None
    error_name: Optional[str] = None

    if not resume:
        checkpoint_clear(conn, cp_key)

    log_info(
        f"🔍 Авторы сообщений: {source_title or source_id or chat_entity} | "
        f"limit={limit_messages}, age_days={max_age_days}, "
        f"resume_offset={offset_id or 'start'}"
    )

    try:
        try:
            for msg in client.iter_messages(
                chat_entity,
                limit=max(1, int(limit_messages)),
                offset_id=offset_id,
            ):
                scanned += 1
                if getattr(msg, "id", None):
                    last_cursor = int(msg.id)

                msg_date = getattr(msg, "date", None)
                if cutoff is not None and msg_date is not None and msg_date < cutoff:
                    break

                sid = getattr(msg, "sender_id", None)
                if not sid:
                    if scanned % batch == 0:
                        checkpoint_put(
                            conn,
                            checkpoint_key=cp_key,
                            mode=source_type,
                            source_id=source_id,
                            source_title=source_title,
                            cursor_int=last_cursor,
                            processed=scanned,
                            saved=kept,
                            status="running",
                        )
                    continue

                sid = int(sid)
                if sid in seen_user_ids:
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
                    skipped["не удалось получить пользователя"] = (
                        skipped.get("не удалось получить пользователя", 0) + 1
                    )
                    continue

                ok, reason = quality_user(user, filters)
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
                    seen_at=msg_date.isoformat() if msg_date else None,
                )
                if parse_name and getattr(user, "username", None):
                    good_usernames.append(str(user.username))
                if parse_id and getattr(user, "id", None) is not None:
                    good_ids.append(str(int(user.id)))

                if scanned % batch == 0:
                    checkpoint_put(
                        conn,
                        checkpoint_key=cp_key,
                        mode=source_type,
                        source_id=source_id,
                        source_title=source_title,
                        cursor_int=last_cursor,
                        processed=scanned,
                        saved=kept,
                        status="running",
                    )
                    log_info(
                        f"💾 Checkpoint сообщений: id={last_cursor}, "
                        f"processed={scanned}, saved={kept}"
                    )
        except Exception as exc:
            error_name = type(exc).__name__
            log_warn(
                f"⚠️ Парсинг сообщений прерван ({error_name}); "
                "следующий запуск может продолжить с checkpoint."
            )

        checkpoint_put(
            conn,
            checkpoint_key=cp_key,
            mode=source_type,
            source_id=source_id,
            source_title=source_title,
            cursor_int=last_cursor,
            processed=scanned,
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
    conn = _db()
    cp = checkpoint_get(conn, cp_key) if resume else None
    offset_id = int(cp.cursor_int) if cp and cp.cursor_int else 0
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
                posts_scanned += 1
                last_post_id = int(post.id)

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
                        reply_to=int(post.id),
                        limit=reply_limit,
                    )
                    for reply in replies:
                        comments_scanned += 1
                        sid = getattr(reply, "sender_id", None)
                        if not sid:
                            continue
                        sid = int(sid)
                        if sid in seen_user_ids:
                            continue
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
                            continue

                        ok, reason = quality_user(user, filters)
                        if not ok:
                            skipped[reason] = skipped.get(reason, 0) + 1
                            continue

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
                except RPCError as exc:
                    skipped[f"comments:{type(exc).__name__}"] = (
                        skipped.get(f"comments:{type(exc).__name__}", 0) + 1
                    )
                    log_warn(
                        f"⚠️ Пост {post.id}: комментарии недоступны "
                        f"({type(exc).__name__})"
                    )

                if posts_scanned % batch == 0:
                    checkpoint_put(
                        conn,
                        checkpoint_key=cp_key,
                        mode=source_type,
                        source_id=source_id,
                        source_title=source_title,
                        cursor_int=last_post_id,
                        processed=posts_scanned,
                        saved=kept,
                        status="running",
                    )
                    log_info(
                        f"💾 Checkpoint комментариев: post={last_post_id}, "
                        f"posts={posts_scanned}, comments={comments_scanned}, "
                        f"saved={kept}"
                    )
        except Exception as exc:
            error_name = type(exc).__name__
            log_warn(
                f"⚠️ Парсинг комментариев прерван ({error_name}); "
                "результат и checkpoint сохранены."
            )

        checkpoint_put(
            conn,
            checkpoint_key=cp_key,
            mode=source_type,
            source_id=source_id,
            source_title=source_title,
            cursor_int=last_post_id,
            processed=posts_scanned,
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
    conn = _db()
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


@dataclass
class SessionState:
    session_file: str
    blocked_until: float = 0.0   # unix timestamp
    frozen_until: float = 0.0    # unix timestamp (PeerFlood etc)
    banned: bool = False
    status: str = "active"
    status_reason: str = ""
    last_invite_at: float = 0.0
    next_invite_at: float = 0.0
    hour_window_start: float = 0.0
    hour_count: int = 0
    day_window_start: float = 0.0
    day_count: int = 0
    ok: int = 0
    fail: int = 0
    attempts: int = 0


def _set_session_status(st: SessionState, status: str, reason: str = "") -> None:
    st.status = status
    st.status_reason = reason


def _refresh_session_status(st: SessionState) -> None:
    now = time.time()
    if st.banned:
        _set_session_status(st, "unauthorized", st.status_reason or "not_authorized")
        return
    if st.frozen_until > now:
        _set_session_status(st, "peer_flood", st.status_reason or "peer_flood")
        return
    if st.blocked_until > now:
        if st.status not in ("flood_wait", "temporary_blocked"):
            _set_session_status(st, "temporary_blocked", st.status_reason)
        return
    if st.status != "disabled":
        _set_session_status(st, "active", "")


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


def _pick_best_session(
    states: List[SessionState],
    excluded: Optional[set[str]] = None,
) -> Optional[SessionState]:
    """Pick the next usable session, excluding sessions retired for this user/run."""
    excluded = excluded or set()
    candidates = []
    for st in states:
        _refresh_session_status(st)
        if st.session_file in excluded or st.banned or st.status == "disabled":
            continue
        ready_at = max(st.blocked_until, st.frozen_until, st.next_invite_at)
        candidates.append(
            (ready_at, st.last_invite_at, st.attempts, st.session_file, st)
        )
    if not candidates:
        return None
    # Stable string tie-breaker avoids comparing SessionState objects.
    candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    return candidates[0][4]


def _sleep_until_ready(
    states: List[SessionState],
    excluded: Optional[set[str]] = None,
    extra_jitter: Tuple[float, float] = (2.0, 6.0),
) -> None:
    """If no session is ready now, sleep until the earliest ready moment (plus jitter).

    v10.1: Writes a clear message when ALL sessions are waiting, so it doesn't look like the bot froze.
    For long waits, sleeps in chunks and prints progress occasionally.
    """
    now = _now()
    excluded = excluded or set()
    soonest = None
    for st in states:
        _refresh_session_status(st)
        if st.session_file in excluded or st.banned or st.status == "disabled":
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
    per_hour_limit: int = 0,
    per_day_limit: int = 0,
    jitter_min: float = 0.3,
    jitter_max: float = 1.2,
    max_user_attempts: int = 3,
    peerflood_freeze_hours: int = 24,
    floodwait_buffer_seconds: int = 60,
    night_mode: bool = False,
    night_start: Tuple[int, int] = (2, 0),
    night_end: Tuple[int, int] = (7, 0),
    night_sleep_jitter: Tuple[float, float] = (30.0, 120.0),
) -> None:
    """Invite with per-user retry and per-session isolation.

    Telegram server-side waits are always respected per session. Rotation only
    moves work to another independently available session; it never clears or
    shortens FloodWait/PeerFlood timers.
    """
    if not session_files:
        raise ValueError("Не переданы session_files")

    conn = _db()
    target_key = _target_key(target)
    delay = max(1.0, float(base_delay))
    user_limit = max(1, int(max_user_attempts or len(session_files) or 1))

    st_map = session_stats_load(conn, session_files)
    states = [st_map[sf] for sf in session_files]
    state_by_sf = {st.session_file: st for st in states}

    ok_cnt = 0
    skip_cnt = 0
    fail_cnt = 0
    exhausted_cnt = 0

    stat_keys = (
        "ok", "already", "privacy", "forbidden", "not_mutual",
        "user_kicked", "user_blocked", "user_channels_too_much",
        "floodwait", "peerflood", "invalid", "network", "resolve",
        "rpc_other", "other",
    )
    ses_stats: Dict[str, Dict[str, int]] = {
        sf: {key: 0 for key in stat_keys} for sf in session_files
    }

    ok_in_session = {sf: 0 for sf in session_files}
    attempts_in_session = {sf: 0 for sf in session_files}
    retired_for_run: set[str] = set()
    client_cache: Dict[str, TelegramClient] = {}

    def persist(st: SessionState) -> None:
        try:
            session_stats_save(conn, st)
        except Exception as exc:
            log_warn(
                f"⚠️ Не удалось сохранить состояние {st.session_file}: "
                f"{type(exc).__name__}"
            )

    def retire_if_run_limit(st: SessionState) -> None:
        sf = st.session_file
        if (
            max_attempts_per_session
            and attempts_in_session.get(sf, 0) >= int(max_attempts_per_session)
            and sf not in retired_for_run
        ):
            retired_for_run.add(sf)
            log_info(
                f"🛑 {sf}: достигнут лимит {max_attempts_per_session} "
                "попыток за этот запуск; сессия больше не используется."
            )

    def get_client(sf: str) -> Optional[TelegramClient]:
        c = client_cache.get(sf)
        if c is not None:
            return c
        st = state_by_sf[sf]
        try:
            c = _make_client(sf, api_id, api_hash)
            st.banned = False
            _set_session_status(st, "active", "")
            persist(st)
            client_cache[sf] = c
            return c
        except RuntimeError:
            st.banned = True
            st.fail += 1
            st.attempts += 1
            _set_session_status(st, "unauthorized", "not_authorized")
            retired_for_run.add(sf)
            persist(st)
            log_warn(f"⚠️ {sf}: сессия не авторизована; исключена из запуска.")
            return None
        except (OSError, ConnectionError) as exc:
            st.fail += 1
            st.attempts += 1
            st.blocked_until = max(st.blocked_until, _now() + 60)
            _set_session_status(st, "temporary_blocked", type(exc).__name__)
            persist(st)
            log_warn(f"🌐 {sf}: ошибка подключения {type(exc).__name__}.")
            return None

    def drop_client(sf: str) -> None:
        c = client_cache.pop(sf, None)
        if c is not None:
            try:
                c.disconnect()
            except Exception:
                pass

    def close_all_clients() -> None:
        for sf in list(client_cache):
            drop_client(sf)

    def available_states(excluded: set[str]) -> List[SessionState]:
        result: List[SessionState] = []
        for st in states:
            retire_if_run_limit(st)
            _refresh_session_status(st)
            if (
                st.session_file in retired_for_run
                or st.session_file in excluded
                or st.banned
                or st.status == "disabled"
            ):
                continue
            result.append(st)
        return result

    # Re-check sessions that were unauthorized on a previous run. A repaired
    # .session must be able to return to service without manual DB cleanup.
    for st in states:
        if st.banned or st.status == "unauthorized":
            get_client(st.session_file)

    try:
        log_info(
            f"🚀 Старт инвайта v2.4 в {target_key}. "
            f"Кандидатов={len(users)}, сессий={len(session_files)}, "
            f"max_user_attempts={user_limit}"
        )

        stop_all = False
        for raw in users:
            if stop_all:
                break

            if night_mode:
                now = _now()
                if _is_time_in_window(
                    now,
                    night_start[0], night_start[1],
                    night_end[0], night_end[1],
                ):
                    sec_left = _seconds_until_window_end(
                        now,
                        night_start[0], night_start[1],
                        night_end[0], night_end[1],
                    )
                    if sec_left > 0:
                        log_pause(
                            f"🌙 Ночной режим: пауза до конца окна "
                            f"({sec_left // 60} мин)."
                        )
                        time.sleep(
                            sec_left + random.uniform(*night_sleep_jitter)
                        )

            user_key, user_id, username, entity = parse_user_ref(raw)
            display_user = ("@" + username) if username else user_key

            if excluded_has(conn, user_key, target_key=target_key):
                rsn = (
                    excluded_reason(conn, user_key, target_key=target_key)
                    or "excluded"
                )
                ledger_put(
                    conn, target_key, user_key, user_id, username,
                    "skip", f"excluded:{rsn}", count_attempt=False,
                )
                skip_cnt += 1
                continue

            prev = ledger_get(conn, target_key, user_key)
            # Privacy/not-mutual are session-scoped in v2.4 and must not block
            # other sessions. Only truly terminal target/global states skip here.
            if prev and prev[0] in ("ok", "already", "invalid"):
                skip_cnt += 1
                continue

            attempts_for_user = 0
            tried_sessions: set[str] = set()
            completed = False

            while attempts_for_user < user_limit and not completed:
                # Rolling hour/day limits become a next_invite_at gate.
                for st in states:
                    due = session_next_time_due_to_limits(
                        st, per_hour_limit, per_day_limit
                    )
                    if due and due > _now():
                        st.next_invite_at = max(st.next_invite_at, due)
                        persist(st)

                candidates = available_states(tried_sessions)

                if not candidates:
                    all_usable = available_states(set())
                    if not all_usable:
                        log_stop(
                            "⛔ Не осталось доступных сессий для продолжения."
                        )
                        stop_all = True
                        break
                    # Every usable session was already tried for this user.
                    # Start another pass only if the per-user cap permits it.
                    tried_sessions.clear()
                    candidates = all_usable

                st = _pick_best_session(
                    candidates,
                    excluded=retired_for_run,
                )
                if st is None:
                    stop_all = True
                    log_stop("⛔ Планировщик не нашёл доступную сессию.")
                    break

                ready_at = max(
                    st.blocked_until, st.frozen_until, st.next_invite_at
                )
                if ready_at > _now():
                    # If another previously-tried session is already ready,
                    # prefer a retry over an unnecessary long wait.
                    fallback = _pick_best_session(
                        available_states(set()),
                        excluded=retired_for_run,
                    )
                    if fallback is not None and max(
                        fallback.blocked_until,
                        fallback.frozen_until,
                        fallback.next_invite_at,
                    ) <= _now():
                        tried_sessions.clear()
                        st = fallback
                    else:
                        _sleep_until_ready(
                            candidates,
                            excluded=retired_for_run,
                        )
                        st = _pick_best_session(
                            candidates,
                            excluded=retired_for_run,
                        )
                        if st is None:
                            continue

                sf = st.session_file

                if excluded_has(
                    conn,
                    user_key,
                    target_key=target_key,
                    session_file=sf,
                ):
                    tried_sessions.add(sf)
                    continue

                client = get_client(sf)
                if client is None:
                    tried_sessions.add(sf)
                    continue

                attempts_for_user += 1
                attempts_in_session[sf] += 1
                st.attempts += 1
                retire_after_attempt = False

                try:
                    try:
                        target_entity = resolve_target_for_client(client, target)
                        invitee_entity = resolve_user_for_client(client, entity)
                    except ValueError:
                        ses_stats[sf]["resolve"] += 1
                        st.fail += 1
                        fail_cnt += 1
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "failed", "cannot_resolve_in_session",
                            session_file=sf,
                        )
                        log_warn(
                            f"⏭️ {sf}: не удалось резолвить {display_user}; "
                            "пробую другую сессию."
                        )
                        tried_sessions.add(sf)
                        continue
                    except (OSError, ConnectionError) as exc:
                        ses_stats[sf]["network"] += 1
                        st.fail += 1
                        fail_cnt += 1
                        st.blocked_until = max(
                            st.blocked_until, _now() + 60
                        )
                        _set_session_status(
                            st, "temporary_blocked", type(exc).__name__
                        )
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "failed", type(exc).__name__,
                            session_file=sf,
                        )
                        drop_client(sf)
                        tried_sessions.add(sf)
                        continue
                    except Exception as exc:
                        ses_stats[sf]["rpc_other"] += 1
                        st.fail += 1
                        fail_cnt += 1
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "failed", f"resolve:{type(exc).__name__}",
                            session_file=sf,
                        )
                        tried_sessions.add(sf)
                        continue

                    time.sleep(
                        delay
                        + random.uniform(
                            float(jitter_min), float(jitter_max)
                        )
                    )
                    client(
                        InviteToChannelRequest(
                            channel=target_entity,
                            users=[invitee_entity],
                        )
                    )

                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "ok", "invited", session_file=sf,
                    )
                    session_consume_invite_token(
                        st, per_hour_limit, per_day_limit
                    )
                    st.ok += 1
                    st.last_invite_at = _now()
                    st.next_invite_at = (
                        st.last_invite_at + max(1.0, delay)
                    )
                    _set_session_status(st, "active", "")
                    ses_stats[sf]["ok"] += 1
                    ok_in_session[sf] += 1
                    ok_cnt += 1
                    completed = True
                    log_ok(
                        f"✅ {display_user} → {target_key} | {sf} "
                        f"(попытка {attempts_for_user}/{user_limit})"
                    )

                    delay = min(
                        10.0,
                        max(
                            1.5,
                            delay + random.uniform(-0.15, 0.35),
                        ),
                    )
                    if (
                        rotate_every
                        and ok_in_session[sf] >= int(rotate_every)
                    ):
                        ok_in_session[sf] = 0
                        st.next_invite_at = max(
                            st.next_invite_at,
                            _now() + random.uniform(3.0, 8.0),
                        )

                except UserAlreadyParticipantError:
                    ses_stats[sf]["already"] += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "already", "already_participant",
                        session_file=sf,
                    )
                    skip_cnt += 1
                    completed = True
                    log_info(f"👤 Уже в цели: {display_user}")

                except UserPrivacyRestrictedError:
                    # Privacy is terminal for this run. Do not rotate accounts
                    # to work around a user's privacy restriction.
                    ses_stats[sf]["privacy"] += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "privacy", "privacy_restricted",
                        session_file=sf,
                    )
                    excluded_add(
                        conn, user_key, user_id, username, "privacy",
                        target_key=target_key,
                    )
                    skip_cnt += 1
                    completed = True
                    log_warn(f"🔒 Privacy restriction: {display_user}")

                except UserNotMutualContactError:
                    ses_stats[sf]["not_mutual"] += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "skip", "not_mutual_contact",
                        session_file=sf,
                    )
                    excluded_add(
                        conn, user_key, user_id, username,
                        "not_mutual_contact",
                        target_key=target_key, session_file=sf,
                    )
                    skip_cnt += 1
                    completed = True
                    log_warn(
                        f"🙅 Нельзя пригласить {display_user}: "
                        "не взаимный контакт."
                    )

                except UserChannelsTooMuchError:
                    ses_stats[sf]["user_channels_too_much"] += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "skip", "user_channels_too_much",
                        session_file=sf,
                    )
                    excluded_add(
                        conn, user_key, user_id, username,
                        "user_channels_too_much",
                        target_key=target_key,
                    )
                    skip_cnt += 1
                    completed = True

                except UserKickedError:
                    ses_stats[sf]["user_kicked"] += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "skip", "user_kicked",
                        session_file=sf,
                    )
                    excluded_add(
                        conn, user_key, user_id, username, "user_kicked",
                        target_key=target_key,
                    )
                    skip_cnt += 1
                    completed = True

                except UserBlockedError:
                    ses_stats[sf]["user_blocked"] += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "skip", "user_blocked",
                        session_file=sf,
                    )
                    excluded_add(
                        conn, user_key, user_id, username, "user_blocked",
                        target_key=target_key, session_file=sf,
                    )
                    skip_cnt += 1
                    completed = True

                except (UsernameInvalidError, UserIdInvalidError):
                    ses_stats[sf]["invalid"] += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "invalid", "invalid_user",
                        session_file=sf,
                    )
                    excluded_add(
                        conn, user_key, user_id, username, "invalid_user",
                        global_scope=True,
                    )
                    skip_cnt += 1
                    completed = True
                    log_warn(f"❌ Невалидный пользователь: {display_user}")

                except (ChatWriteForbiddenError, ChatAdminRequiredError) as exc:
                    # This is a problem with this inviter session for the
                    # current target, not with the user being invited.
                    ses_stats[sf]["forbidden"] += 1
                    st.fail += 1
                    fail_cnt += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "forbidden", type(exc).__name__,
                        session_file=sf,
                    )
                    retired_for_run.add(sf)
                    tried_sessions.add(sf)
                    log_warn(
                        f"🚫 {sf}: нет прав инвайта в {target_key}; "
                        "исключаю только эту сессию из текущего запуска."
                    )

                except FloodWaitError as exc:
                    sec = max(0, int(getattr(exc, "seconds", 0) or 0))
                    ses_stats[sf]["floodwait"] += 1
                    st.fail += 1
                    fail_cnt += 1
                    st.blocked_until = max(
                        st.blocked_until,
                        _now() + sec + int(floodwait_buffer_seconds),
                    )
                    _set_session_status(
                        st, "flood_wait", f"{sec}s"
                    )
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "floodwait", f"{sec}",
                        session_file=sf,
                        flood_seconds=sec,
                    )
                    tried_sessions.add(sf)
                    delay = min(15.0, max(delay, 6.0))
                    log_pause(
                        f"💤 {sf}: FloodWait {sec}s"
                        + (
                            f" (>{switch_on_floodwait_seconds})"
                            if sec > int(switch_on_floodwait_seconds)
                            else ""
                        )
                        + "; таймер сохраняю, текущего пользователя "
                          "пробую другой доступной сессией."
                    )

                except PeerFloodError:
                    ses_stats[sf]["peerflood"] += 1
                    st.fail += 1
                    fail_cnt += 1
                    freeze_sec = max(
                        1, int(peerflood_freeze_hours)
                    ) * 3600
                    st.frozen_until = max(
                        st.frozen_until, _now() + freeze_sec
                    )
                    _set_session_status(
                        st, "peer_flood",
                        f"{peerflood_freeze_hours}h",
                    )
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "peerflood", "peer_flood",
                        session_file=sf,
                    )
                    tried_sessions.add(sf)
                    log_stop(
                        f"⛔ {sf}: PeerFlood; заморожена на "
                        f"{peerflood_freeze_hours}ч."
                    )

                except (ConnectionResetError, ConnectionError, OSError) as exc:
                    ses_stats[sf]["network"] += 1
                    st.fail += 1
                    fail_cnt += 1
                    st.blocked_until = max(
                        st.blocked_until, _now() + 60
                    )
                    _set_session_status(
                        st, "temporary_blocked", type(exc).__name__
                    )
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "failed", type(exc).__name__,
                        session_file=sf,
                    )
                    drop_client(sf)
                    tried_sessions.add(sf)
                    log_warn(
                        f"🌐 {sf}: {type(exc).__name__}; "
                        "пауза 60с, пробую другую сессию."
                    )

                except RPCError as exc:
                    ses_stats[sf]["rpc_other"] += 1
                    st.fail += 1
                    fail_cnt += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "failed", type(exc).__name__,
                        session_file=sf,
                    )
                    tried_sessions.add(sf)
                    log_warn(
                        f"⚠️ {sf}: RPC {type(exc).__name__}; "
                        "пробую другую сессию."
                    )

                except Exception as exc:
                    ses_stats[sf]["other"] += 1
                    st.fail += 1
                    fail_cnt += 1
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "failed", type(exc).__name__,
                        session_file=sf,
                    )
                    tried_sessions.add(sf)
                    log_warn(
                        f"⚠️ {sf}: {type(exc).__name__}; "
                        "пробую другую сессию."
                    )

                finally:
                    persist(st)
                    retire_if_run_limit(st)

            if not completed and not stop_all:
                exhausted_cnt += 1
                skip_cnt += 1
                ledger_put(
                    conn, target_key, user_key, user_id, username,
                    "skip", f"max_user_attempts={user_limit}",
                    count_attempt=False,
                )
                log_warn(
                    f"⏭️ {display_user}: исчерпан лимит "
                    f"{user_limit} попыток."
                )

        log_ok(
            f"🏁 Инвайт завершён. Успех={ok_cnt}, пропуск={skip_cnt}, "
            f"ошибок попыток={fail_cnt}, исчерпан лимит={exhausted_cnt}"
        )
        for sf in session_files:
            st = state_by_sf[sf]
            _refresh_session_status(st)
            persist(st)
            stats = ses_stats[sf]
            log_info(
                f"📊 {sf}: status={st.status} "
                f"attempts_run={attempts_in_session[sf]} "
                f"ok={stats['ok']} already={stats['already']} "
                f"privacy={stats['privacy']} forbidden={stats['forbidden']} "
                f"floodwait={stats['floodwait']} "
                f"peerflood={stats['peerflood']} "
                f"network={stats['network']} resolve={stats['resolve']} "
                f"invalid={stats['invalid']} rpc={stats['rpc_other']} "
                f"other={stats['other']}"
            )
    finally:
        close_all_clients()
        conn.close()


def inviting(client: TelegramClient, target: Union[str, int, Any], users: List[Union[str, int]], base_delay: float = 2.0) -> None:
    """Инвайт одним клиентом (1 сессия).

    - учитывает ledger (не трогает уже обработанных для этой цели)
    - учитывает global excluded_users (вечные отказы)
    """
    conn = _db()
    try:
        target_key = _target_key(target)
        log_info(f"🚀 Старт инвайта в: {target_key}. Кандидатов: {len(users)}")
        ok_cnt = 0
        skip_cnt = 0
        fail_cnt = 0

        delay = max(1.0, float(base_delay))

        target_entity = resolve_target_for_client(client, target)

        for raw in users:
            user_key, user_id, username, entity = parse_user_ref(raw)

            if excluded_has(conn, user_key, target_key=target_key):
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
                    excluded_add(
                        conn, user_key, user_id, username, "privacy",
                        target_key=target_key
                    )
                except Exception:
                    pass
                skip_cnt += 1
                log_warn(f"🔒 Закрыты инвайты: {('@'+username) if username else user_key}")

            except UserNotMutualContactError:
                ledger_put(conn, target_key, user_key, user_id, username, "skip", "not_mutual_contact")
                try:
                    excluded_add(
                        conn, user_key, user_id, username, "not_mutual_contact",
                        target_key=target_key
                    )
                except Exception:
                    pass
                skip_cnt += 1
                log_warn(f"🙅‍♂️ Не взаимный контакт/нельзя инвайтить: {('@'+username) if username else user_key}")

            except UserChannelsTooMuchError:
                ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_channels_too_much")
                try:
                    excluded_add(
                        conn, user_key, user_id, username, "user_channels_too_much",
                        target_key=target_key
                    )
                except Exception:
                    pass
                skip_cnt += 1
                log_warn(f"📛 У пользователя слишком много чатов/каналов: {('@'+username) if username else user_key}")

            except UserKickedError:
                ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_kicked")
                try:
                    excluded_add(
                        conn, user_key, user_id, username, "user_kicked",
                        target_key=target_key
                    )
                except Exception:
                    pass
                skip_cnt += 1
                log_warn(f"🚫 Пользователь кикнут/забанен в цели: {('@'+username) if username else user_key}")

            except UserBlockedError:
                ledger_put(conn, target_key, user_key, user_id, username, "skip", "user_blocked")
                try:
                    excluded_add(
                        conn, user_key, user_id, username, "user_blocked",
                        target_key=target_key
                    )
                except Exception:
                    pass
                skip_cnt += 1
                log_warn(f"🚫 Пользователь заблокирован/недоступен: {('@'+username) if username else user_key}")

            except ChatWriteForbiddenError as e:
                diag = _diagnose_invite_context(client, target_entity)
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
                    excluded_add(
                        conn, user_key, user_id, username, "invalid_user",
                        global_scope=True
                    )
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
                ledger_put(conn, target_key, user_key, user_id, username, "failed", "cannot_resolve_in_session")
                fail_cnt += 1
                log_warn(f"⏭️ Не удалось резолвить {user_key} в текущей сессии.")

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
    finally:
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

    # Не используем номер телефона в имени session-файла.
    alias = datetime.now().strftime("account_%Y%m%d_%H%M%S")
    session_name = session_name_from_file(f"{alias}.session")
    client = TelegramClient(session_name, api_id, api_hash, flood_sleep_threshold=0)
    print("Сейчас придёт код в Telegram. Введите код и (если спросит) пароль 2FA.")
    client.start(phone=phone)
    client.disconnect()
    if os.name != "nt":
        try:
            os.chmod(session_name + ".session", 0o600)
        except OSError:
            pass

    log_ok(f"📲 Аккаунт добавлен: {alias}.session (папка {SESSIONS_DIR}/)")
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
        if os.name != "nt":
            try:
                os.chmod("options.txt", 0o600)
            except OSError:
                pass

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
    """Check auth/membership/rights independently for every Telegram session."""
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
    try:
        st_map = session_stats_load(conn, session_files)
        now = int(time.time())
        block_sec = max(0, int(block_cannot_join_hours)) * 3600

        for sf in session_files:
            st = st_map.get(sf)
            client: Optional[TelegramClient] = None
            try:
                try:
                    client = _make_client(sf, api_id, api_hash)
                    if st and st.banned:
                        st.banned = False
                        session_stats_save(conn, st)
                        log_info(f"♻️ Preflight: {sf} — авторизация восстановлена")
                except RuntimeError:
                    if st:
                        st.banned = True
                        st.fail += 1
                        st.attempts += 1
                        st.next_invite_at = max(st.next_invite_at, now + 3600)
                        session_stats_save(conn, st)
                    report["not_authorized"].append(sf)
                    log_warn(f"⚠️ Preflight: {sf} — сессия не авторизована")
                    continue
                except (OSError, ConnectionError) as exc:
                    report["network"].append(sf)
                    log_warn(f"🌐 Preflight: {sf} — ошибка подключения: {type(exc).__name__}")
                    continue
                except Exception as exc:
                    report["unknown"].append(sf)
                    log_warn(f"⚠️ Preflight: {sf} — ошибка создания клиента: {type(exc).__name__}")
                    continue

                # Critical v2.4 rule: every session resolves its own target entity.
                try:
                    target_entity = resolve_target_for_client(client, target)
                except ChannelPrivateError:
                    report["cannot_join"].append(sf)
                    log_warn(f"⛔ Preflight: {sf} — цель приватная/нет доступа")
                    if st:
                        st.blocked_until = max(st.blocked_until, now + (block_sec or 3600))
                        session_stats_save(conn, st)
                    continue
                except (OSError, ConnectionError):
                    report["network"].append(sf)
                    if st:
                        st.blocked_until = max(st.blocked_until, now + 120)
                        session_stats_save(conn, st)
                    continue
                except Exception as exc:
                    report["unknown"].append(sf)
                    log_warn(
                        f"⚠️ Preflight: {sf} — не удалось резолвить цель "
                        f"({type(exc).__name__})"
                    )
                    continue

                ok, reason = _ensure_in_target(
                    client, target_entity, auto_join=auto_join
                )
                if ok:
                    bucket = "joined" if reason == "joined" else "ok"
                    report[bucket].append(sf)
                    log_ok(
                        f"✅ Preflight: {sf} — "
                        + ("вступил в цель" if bucket == "joined" else "уже в цели")
                    )
                elif reason in (
                    "cannot_join",
                    "not_participant",
                    "channel_private",
                    "banned_in_channel",
                ):
                    report["cannot_join"].append(sf)
                    if st:
                        st.blocked_until = max(
                            st.blocked_until, now + (block_sec or 3600)
                        )
                        st.fail += 1
                        st.attempts += 1
                        session_stats_save(conn, st)
                    log_warn(f"⛔ Preflight: {sf} — не смог вступить/нет доступа")
                    continue
                elif reason == "flood_wait":
                    report["flood_wait"].append(sf)
                    if st:
                        st.blocked_until = max(st.blocked_until, now + 600)
                        st.fail += 1
                        st.attempts += 1
                        session_stats_save(conn, st)
                    log_warn(f"⏳ Preflight: {sf} — FloodWait")
                    continue
                elif reason == "network":
                    report["network"].append(sf)
                    if st:
                        st.blocked_until = max(st.blocked_until, now + 120)
                        session_stats_save(conn, st)
                    continue
                else:
                    report["unknown"].append(sf)
                    log_warn(f"⚠️ Preflight: {sf} — ошибка проверки: {reason}")
                    continue

                # Permission check uses this session's target entity too.
                try:
                    perms = client.get_permissions(target_entity, "me")
                    if getattr(perms, "invite_users", None) is False:
                        report["no_rights"].append(sf)
                        if sf in report["ok"]:
                            report["ok"].remove(sf)
                        if sf in report["joined"]:
                            report["joined"].remove(sf)
                        if st:
                            st.blocked_until = max(st.blocked_until, now + 86400)
                            st.fail += 1
                            st.attempts += 1
                            session_stats_save(conn, st)
                        log_warn(f"🚫 Preflight: {sf} — нет прав приглашать")
                except ChatWriteForbiddenError:
                    report["no_rights"].append(sf)
                    if sf in report["ok"]:
                        report["ok"].remove(sf)
                    if sf in report["joined"]:
                        report["joined"].remove(sf)
                    if st:
                        st.blocked_until = max(st.blocked_until, now + 86400)
                        st.fail += 1
                        st.attempts += 1
                        session_stats_save(conn, st)
                    log_warn(f"🚫 Preflight: {sf} — ChatWriteForbidden")
                except (OSError, ConnectionError):
                    if sf not in report["network"]:
                        report["network"].append(sf)
                    log_warn(f"🌐 Preflight: {sf} — сеть при проверке прав")
                except Exception as exc:
                    log_warn(
                        f"⚠️ Preflight: {sf} — права не удалось проверить "
                        f"({type(exc).__name__})"
                    )
            finally:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
        return report
    finally:
        conn.close()

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
