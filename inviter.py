# -*- coding: utf-8 -*-
"""Invitation orchestration, ledger, scheduler and preflight for v2.4."""

from __future__ import annotations

import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from telethon import utils as tl_utils
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    PeerFloodError,
    RPCError,
    UserAlreadyParticipantError,
    UserIdInvalidError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
    UsernameInvalidError,
)
from telethon.errors.rpcerrorlist import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UserBlockedError,
    UserChannelsTooMuchError,
    UserKickedError,
    UserNotMutualContactError,
)
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import (
    GetParticipantRequest,
    InviteToChannelRequest,
    JoinChannelRequest,
)
from telethon.tl.functions.messages import AddChatUserRequest

from logging_setup import log_info, log_ok, log_pause, log_stop, log_warn
from sessions import session_name_from_file
from storage import (
    UserCandidate,
    candidate_from_raw,
    connect_db,
    exclusion_add,
    exclusion_has,
    exclusion_load_keys,
    exclusion_reason,
    invite_record,
    invite_state_get,
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


LEDGER_DB = "invite_ledger.db"


# -------------------- LEDGER (SQLite) --------------------

def _db() -> sqlite3.Connection:
    """Open the canonical storage schema for inviter operations."""
    return connect_db(LEDGER_DB)

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
        commit=False,
    )
    # Keep the legacy snapshot table updated for backwards compatibility.
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
    """Return the next allowed time; negative limits are treated as disabled."""
    per_hour_limit = max(0, int(per_hour_limit or 0))
    per_day_limit = max(0, int(per_day_limit or 0))
    now = _now()
    next_due = 0.0
    if per_hour_limit and getattr(st, "hour_count", 0) >= per_hour_limit:
        next_due = max(next_due, float(getattr(st, "hour_window_start", now)) + 3600)
    if per_day_limit and getattr(st, "day_count", 0) >= per_day_limit:
        next_due = max(next_due, float(getattr(st, "day_window_start", now)) + 86400)
    return next_due


def session_consume_invite_token(st: "SessionState", per_hour_limit: int, per_day_limit: int) -> None:
    """Consume one rolling invite slot; negative limits are disabled."""
    per_hour_limit = max(0, int(per_hour_limit or 0))
    per_day_limit = max(0, int(per_day_limit or 0))
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

def canonical_target_key(target: Any) -> str:
    """Return a collision-resistant ledger key for common Telegram refs."""
    if isinstance(target, str):
        raw = target.strip()
        if not raw:
            return "ref:"

        if raw.startswith("@"):
            return "@" + raw[1:].lower()

        # Private post link: t.me/c/<internal_channel_id>/<message_id>
        match = re.match(
            r"^(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)(?:/\d+)?/?$",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            return f"peer:-100{int(match.group(1))}"

        # Invite links are unique references, not usernames.
        match = re.match(
            r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)([^/?#]+)",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            return "invite:" + match.group(1).lower()

        # Public username links.
        match = re.match(
            r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)(?:/.*)?$",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            name = match.group(1).lower()
            if name not in {"c", "joinchat"}:
                return "@" + name

        if raw.lower().startswith("id:"):
            maybe_id = raw[3:].strip()
            if re.fullmatch(r"-?\d+", maybe_id):
                value = int(maybe_id)
                return (
                    f"peer:{value}"
                    if value < 0
                    else f"rawid:{value}"
                )

        if re.fullmatch(r"-?\d+", raw):
            value = int(raw)
            return f"peer:{value}" if value < 0 else f"rawid:{value}"

        return "ref:" + raw.lower()

    if isinstance(target, int):
        return (
            f"peer:{int(target)}"
            if int(target) < 0
            else f"rawid:{int(target)}"
        )

    try:
        username = getattr(target, "username", None)
        if username:
            return "@" + str(username).lstrip("@").lower()
    except Exception:
        pass

    try:
        peer_id = tl_utils.get_peer_id(target)
        if isinstance(peer_id, int):
            return f"peer:{peer_id}"
    except Exception:
        pass

    try:
        target_id = getattr(target, "id", None)
        if target_id is not None:
            return f"rawid:{int(target_id)}"
    except Exception:
        pass

    return "ref:" + str(target).strip().lower()

def _target_key(target: Any) -> str:
    """Backward-compatible alias for the canonical target key."""
    return canonical_target_key(target)

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
            return "@" + str(uname).lstrip("@").lower()
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
    """Resolve target inside the current session without swallowing FloodWait."""
    ref = target_ref(target)
    try:
        return client.get_input_entity(ref)
    except FloodWaitError:
        raise
    except (ValueError, TypeError):
        return client.get_entity(ref)


def _basic_chat_id(target_entity: Any) -> Optional[int]:
    """Return chat_id for legacy/basic Telegram groups, else None."""
    cls_name = type(target_entity).__name__
    if cls_name in ("Chat", "PeerChat", "InputPeerChat"):
        value = getattr(target_entity, "chat_id", None)
        if value is None:
            value = getattr(target_entity, "id", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _invite_one(
    client: TelegramClient,
    target_entity: Any,
    invitee_entity: Any,
) -> Any:
    """Invite one user using the request appropriate for the target type."""
    chat_id = _basic_chat_id(target_entity)
    if chat_id is not None:
        return client(
            AddChatUserRequest(
                chat_id=chat_id,
                user_id=invitee_entity,
                fwd_limit=10,
            )
        )
    return client(
        InviteToChannelRequest(
            channel=target_entity,
            users=[invitee_entity],
        )
    )


def _has_missing_invitee(result: Any) -> bool:
    """InviteToChannel may return success envelope with missing_invitees."""
    missing = getattr(result, "missing_invitees", None)
    if missing is None:
        return False
    try:
        return len(missing) > 0
    except TypeError:
        return bool(missing)


def _make_client(session_file: str, api_id: int, api_hash: str) -> TelegramClient:
    """Create an authorized client and close partial connections on failure."""
    session_name = session_name_from_file(session_file)
    client = TelegramClient(
        session_name,
        api_id,
        api_hash,
        flood_sleep_threshold=0,
    )
    try:
        client.connect()
        if not client.is_user_authorized():
            raise RuntimeError(f"Сессия не авторизована: {session_file}")
        return client
    except Exception:
        try:
            client.disconnect()
        except Exception:
            pass
        raise




# -------------------- INVITE ORCHESTRATION (PRO MODE) --------------------


def _classify_rpc_error(exc: Exception) -> str:
    """Classify generated Telethon RPC errors without brittle imports."""
    name = type(exc).__name__
    if name == "UsersTooMuchError":
        return "target_full"
    if name in {
        "AuthKeyUnregisteredError",
        "UserDeactivatedBanError",
        "UserDeactivatedError",
    }:
        return "session_dead"
    if name == "InputUserDeactivatedError":
        return "user_deactivated"
    return "other"


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

    Writes a clear message when all sessions are waiting so the CLI does not look frozen.
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
    """Resolve a user in this session; prefer local ID cache before network lookup."""
    candidate = candidate_from_raw(raw)

    if candidate.user_id is not None:
        try:
            return client.get_input_entity(int(candidate.user_id))
        except FloodWaitError:
            raise
        except (ValueError, TypeError):
            pass

    if candidate.username:
        username = "@" + candidate.username.lstrip("@")
        try:
            return client.get_input_entity(username)
        except FloodWaitError:
            raise
        except (ValueError, TypeError):
            # Explicit username lookup may use the network; FloodWait must
            # propagate to the scheduler and block this session.
            return client.get_entity(username)

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
    max_attempts_per_session = max(0, int(max_attempts_per_session or 0))
    per_hour_limit = max(0, int(per_hour_limit or 0))
    per_day_limit = max(0, int(per_day_limit or 0))

    st_map = session_stats_load(conn, session_files)
    states = [st_map[sf] for sf in session_files]
    state_by_sf = {st.session_file: st for st in states}

    ok_cnt = 0
    skip_cnt = 0
    fail_cnt = 0
    exhausted_cnt = 0

    stat_keys = (
        "ok", "already", "privacy", "forbidden", "not_mutual",
        "missing_invitee", "user_kicked", "user_blocked",
        "user_channels_too_much",
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
            retired_for_run.add(sf)
            persist(st)
            log_warn(
                f"🌐 {sf}: ошибка подключения {type(exc).__name__}; "
                "сессия исключена до следующего запуска."
            )
            return None
        except Exception as exc:
            st.fail += 1
            st.attempts += 1
            _set_session_status(st, "temporary_blocked", type(exc).__name__)
            retired_for_run.add(sf)
            persist(st)
            log_warn(
                f"⚠️ {sf}: ошибка открытия сессии {type(exc).__name__}; "
                "сессия исключена из текущего запуска."
            )
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

            prev = ledger_get(conn, target_key, user_key)

            if excluded_has(conn, user_key, target_key=target_key):
                # Do not append a new event on every run for a persistent
                # exclusion; the original reason already exists in storage.
                skip_cnt += 1
                continue

            # Terminal target/global states skip future attempts. Session-scoped
            # exclusions are evaluated later against each individual session.
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

                candidates = [
                    candidate
                    for candidate in available_states(tried_sessions)
                    if not excluded_has(
                        conn,
                        user_key,
                        target_key=target_key,
                        session_file=candidate.session_file,
                    )
                ]

                if not candidates:
                    all_usable = available_states(set())
                    eligible_for_user = [
                        candidate
                        for candidate in all_usable
                        if not excluded_has(
                            conn,
                            user_key,
                            target_key=target_key,
                            session_file=candidate.session_file,
                        )
                    ]
                    if not all_usable:
                        log_stop(
                            "⛔ Не осталось доступных сессий для продолжения."
                        )
                        stop_all = True
                        break
                    if not eligible_for_user:
                        if prev != ("skip", "excluded_all_sessions"):
                            ledger_put(
                                conn, target_key, user_key, user_id, username,
                                "skip", "excluded_all_sessions",
                                count_attempt=False,
                            )
                        skip_cnt += 1
                        completed = True
                        log_info(
                            f"⏭️ {display_user}: исключён для всех "
                            "доступных сессий."
                        )
                        break

                    # All eligible sessions were already tried for this user.
                    # Start another pass only while the per-user cap permits it.
                    tried_sessions.clear()
                    candidates = eligible_for_user

                preferred_session = getattr(
                    entity, "preferred_session", None
                )
                preferred = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.session_file == preferred_session
                        and max(
                            candidate.blocked_until,
                            candidate.frozen_until,
                            candidate.next_invite_at,
                        ) <= _now()
                    ),
                    None,
                )
                st = preferred or _pick_best_session(
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
                    except FloodWaitError as exc:
                        sec = max(0, int(getattr(exc, "seconds", 0) or 0))
                        ses_stats[sf]["floodwait"] += 1
                        st.fail += 1
                        fail_cnt += 1
                        st.blocked_until = max(
                            st.blocked_until,
                            _now() + sec + int(floodwait_buffer_seconds),
                        )
                        _set_session_status(st, "flood_wait", f"{sec}s")
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "floodwait", f"resolve:{sec}",
                            session_file=sf,
                            flood_seconds=sec,
                        )
                        tried_sessions.add(sf)
                        log_pause(
                            f"💤 {sf}: FloodWait {sec}s во время resolve; "
                            "сессия поставлена на паузу."
                        )
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
                    invite_result = _invite_one(
                        client,
                        target_entity,
                        invitee_entity,
                    )
                    if _has_missing_invitee(invite_result):
                        # The API call consumed an invite attempt even though
                        # Telegram declined to add this user.
                        session_consume_invite_token(
                            st, per_hour_limit, per_day_limit
                        )
                        st.fail += 1
                        st.last_invite_at = _now()
                        st.next_invite_at = (
                            st.last_invite_at + max(1.0, delay)
                        )
                        ses_stats[sf]["missing_invitee"] += 1
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "skip", "missing_invitee",
                            session_file=sf,
                        )
                        excluded_add(
                            conn,
                            user_key,
                            user_id,
                            username,
                            "missing_invitee",
                            target_key=target_key,
                        )
                        skip_cnt += 1
                        completed = True
                        log_warn(
                            f"⏭️ Telegram не добавил {display_user}: "
                            "missing_invitees."
                        )
                        continue

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

                except (ChannelPrivateError, UserBannedInChannelError) as exc:
                    ses_stats[sf]["forbidden"] += 1
                    st.fail += 1
                    fail_cnt += 1
                    st.blocked_until = max(st.blocked_until, _now() + 86400)
                    _set_session_status(
                        st, "temporary_blocked", type(exc).__name__
                    )
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "forbidden", type(exc).__name__,
                        session_file=sf,
                    )
                    retired_for_run.add(sf)
                    tried_sessions.add(sf)
                    log_warn(
                        f"🚫 {sf}: цель недоступна для этой сессии "
                        f"({type(exc).__name__})."
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
                    retired_for_run.add(sf)
                    log_warn(
                        f"🌐 {sf}: {type(exc).__name__}; "
                        "сессия исключена до следующего запуска."
                    )

                except RPCError as exc:
                    rpc_kind = _classify_rpc_error(exc)
                    rpc_name = type(exc).__name__

                    if rpc_kind == "target_full":
                        ses_stats[sf]["forbidden"] += 1
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "skip", "target_full",
                            session_file=sf,
                        )
                        skip_cnt += 1
                        completed = True
                        stop_all = True
                        log_stop(
                            f"⛔ Цель {target_key} больше не принимает "
                            f"участников ({rpc_name}); прогон остановлен."
                        )

                    elif rpc_kind == "session_dead":
                        ses_stats[sf]["rpc_other"] += 1
                        st.fail += 1
                        fail_cnt += 1
                        st.banned = True
                        _set_session_status(st, "unauthorized", rpc_name)
                        retired_for_run.add(sf)
                        tried_sessions.add(sf)
                        drop_client(sf)
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "failed", rpc_name,
                            session_file=sf,
                        )
                        log_warn(
                            f"🚫 {sf}: сессия больше не пригодна "
                            f"({rpc_name}); исключена из запуска."
                        )

                    elif rpc_kind == "user_deactivated":
                        ses_stats[sf]["invalid"] += 1
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "invalid", "user_deactivated",
                            session_file=sf,
                        )
                        excluded_add(
                            conn, user_key, user_id, username,
                            "user_deactivated",
                            global_scope=True,
                        )
                        skip_cnt += 1
                        completed = True

                    else:
                        ses_stats[sf]["rpc_other"] += 1
                        st.fail += 1
                        fail_cnt += 1
                        ledger_put(
                            conn, target_key, user_key, user_id, username,
                            "failed", rpc_name,
                            session_file=sf,
                        )
                        tried_sessions.add(sf)
                        log_warn(
                            f"⚠️ {sf}: RPC {rpc_name}; "
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
                invite_result = _invite_one(client, target_entity, invitee_entity)
                if _has_missing_invitee(invite_result):
                    ledger_put(
                        conn, target_key, user_key, user_id, username,
                        "skip", "missing_invitee",
                    )
                    skip_cnt += 1
                    log_warn(
                        f"⏭️ Telegram не добавил "
                        f"{('@'+username) if username else user_key}: "
                        "missing_invitees."
                    )
                    continue
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


