# -*- coding: utf-8 -*-
"""Persistent storage primitives for telegram-parser v2.4.

This module owns schema creation/migrations for parsed users and scoped
exclusions.  Telegram access_hash values are intentionally NOT persisted as
portable user identifiers because they are session-specific.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional


DB_PATH = "invite_ledger.db"
SCHEMA_VERSION = 3


@dataclass(frozen=True)
class UserCandidate:
    user_id: Optional[int]
    username: Optional[str]

    @property
    def key(self) -> str:
        if self.user_id is not None:
            return f"id:{int(self.user_id)}"
        if self.username:
            return f"u:{self.username.lower().lstrip('@')}"
        return "empty"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            source_id TEXT,
            source_title TEXT,
            source_type TEXT,
            parsed_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sources (
            user_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            source_id TEXT,
            source_title TEXT,
            source_type TEXT,
            parsed_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(user_id, source_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_exclusions (
            user_key TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            target_key TEXT,
            session_file TEXT,
            reason TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 1,
            first_ts TEXT NOT NULL,
            last_ts TEXT NOT NULL,
            PRIMARY KEY(user_key, scope_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_exclusions_target ON user_exclusions(target_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_exclusions_session ON user_exclusions(session_file)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invite_state (
            target TEXT NOT NULL,
            user_key TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            status TEXT NOT NULL,
            reason TEXT,
            session_file TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(target, user_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invite_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            user_key TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            session_file TEXT,
            status TEXT NOT NULL,
            reason TEXT,
            flood_seconds INTEGER,
            ts TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_invite_events_target_user "
        "ON invite_events(target, user_key, id)"
    )
    # One-time compatibility migration from the v2.3 snapshot table.
    old_invites = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invites'"
    ).fetchone()
    if old_invites:
        conn.execute(
            """
            INSERT OR IGNORE INTO invite_state(
                target, user_key, user_id, username, status, reason,
                session_file, attempt_count, updated_at
            )
            SELECT target, user_key, user_id, username, status, reason,
                   NULL, 1, ts
            FROM invites
            """
        )
    conn.execute(
        """
        INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def connect_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    ensure_schema(conn)
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn


def _clean_username(username: Optional[str]) -> Optional[str]:
    if not username:
        return None
    value = str(username).strip().lstrip("@")
    return value or None


def upsert_user(
    conn: sqlite3.Connection,
    user: Any,
    *,
    source_id: Optional[str],
    source_title: Optional[str],
    source_type: str,
    seen_at: Optional[str] = None,
) -> Optional[int]:
    uid = getattr(user, "id", None)
    if uid is None:
        return None

    uid = int(uid)
    username = _clean_username(getattr(user, "username", None))
    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    now = seen_at or utcnow_iso()
    source_id_text = str(source_id) if source_id is not None else None
    source_key = f"{source_type}:{source_id_text or 'unknown'}"

    conn.execute(
        """
        INSERT INTO users(
            user_id, username, first_name, last_name,
            source_id, source_title, source_type, parsed_at, last_seen_at
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=COALESCE(excluded.username, users.username),
            first_name=COALESCE(excluded.first_name, users.first_name),
            last_name=COALESCE(excluded.last_name, users.last_name),
            source_id=excluded.source_id,
            source_title=excluded.source_title,
            source_type=excluded.source_type,
            last_seen_at=excluded.last_seen_at
        """,
        (
            uid,
            username,
            first_name,
            last_name,
            source_id_text,
            source_title,
            source_type,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO user_sources(
            user_id, source_key, source_id, source_title,
            source_type, parsed_at, last_seen_at
        )
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(user_id, source_key) DO UPDATE SET
            source_title=excluded.source_title,
            last_seen_at=excluded.last_seen_at
        """,
        (uid, source_key, source_id_text, source_title, source_type, now, now),
    )
    return uid


def load_user_candidates(conn: sqlite3.Connection) -> List[UserCandidate]:
    rows = conn.execute(
        """
        SELECT user_id, username
        FROM users
        ORDER BY last_seen_at DESC, user_id ASC
        """
    ).fetchall()
    return [
        UserCandidate(user_id=int(uid), username=_clean_username(username))
        for uid, username in rows
    ]


def candidate_from_raw(raw: Any) -> UserCandidate:
    if isinstance(raw, UserCandidate):
        return raw

    if isinstance(raw, int):
        return UserCandidate(int(raw), None)

    s = str(raw).strip()
    if not s:
        return UserCandidate(None, None)

    if s.startswith("@"):
        return UserCandidate(None, _clean_username(s))

    # Legacy v2.3 format was user_id:access_hash.  The hash is deliberately
    # discarded because it cannot safely be reused by another Telegram session.
    if ":" in s:
        left, _right = s.split(":", 1)
        if left.isdigit():
            return UserCandidate(int(left), None)

    if s.isdigit():
        return UserCandidate(int(s), None)

    return UserCandidate(None, _clean_username(s))


def _scope_key(
    *,
    global_scope: bool,
    target_key: Optional[str],
    session_file: Optional[str],
) -> str:
    if global_scope:
        return "global"
    if target_key and session_file:
        return f"target_session:{target_key}|{session_file}"
    if target_key:
        return f"target:{target_key}"
    if session_file:
        return f"session:{session_file}"
    # Safe default: no accidental global blacklist.
    return "local"


def exclusion_add(
    conn: sqlite3.Connection,
    user_key: str,
    *,
    user_id: Optional[int],
    username: Optional[str],
    reason: str,
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
    global_scope: bool = False,
) -> None:
    now = utcnow_iso()
    scope_key = _scope_key(
        global_scope=global_scope,
        target_key=target_key,
        session_file=session_file,
    )
    conn.execute(
        """
        INSERT INTO user_exclusions(
            user_key, scope_key, user_id, username,
            target_key, session_file, reason, hits, first_ts, last_ts
        )
        VALUES(?,?,?,?,?,?,?,1,?,?)
        ON CONFLICT(user_key, scope_key) DO UPDATE SET
            user_id=COALESCE(excluded.user_id, user_exclusions.user_id),
            username=COALESCE(excluded.username, user_exclusions.username),
            reason=excluded.reason,
            hits=user_exclusions.hits + 1,
            last_ts=excluded.last_ts
        """,
        (
            user_key,
            scope_key,
            user_id,
            _clean_username(username),
            target_key,
            session_file,
            reason,
            now,
            now,
        ),
    )
    conn.commit()


def applicable_scope_keys(
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
) -> List[str]:
    keys = ["global"]
    if target_key:
        keys.append(f"target:{target_key}")
    if session_file:
        keys.append(f"session:{session_file}")
    if target_key and session_file:
        keys.append(f"target_session:{target_key}|{session_file}")
    return keys


def exclusion_has(
    conn: sqlite3.Connection,
    user_key: str,
    *,
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
) -> bool:
    scopes = applicable_scope_keys(target_key, session_file)
    placeholders = ",".join("?" for _ in scopes)
    row = conn.execute(
        f"""
        SELECT 1 FROM user_exclusions
        WHERE user_key=? AND scope_key IN ({placeholders})
        LIMIT 1
        """,
        [user_key, *scopes],
    ).fetchone()
    return row is not None


def exclusion_reason(
    conn: sqlite3.Connection,
    user_key: str,
    *,
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
) -> str:
    scopes = applicable_scope_keys(target_key, session_file)
    placeholders = ",".join("?" for _ in scopes)
    row = conn.execute(
        f"""
        SELECT reason FROM user_exclusions
        WHERE user_key=? AND scope_key IN ({placeholders})
        ORDER BY CASE WHEN scope_key='global' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        [user_key, *scopes],
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def exclusion_load_keys(
    conn: sqlite3.Connection,
    *,
    target_key: Optional[str] = None,
    session_file: Optional[str] = None,
) -> set[str]:
    scopes = applicable_scope_keys(target_key, session_file)
    placeholders = ",".join("?" for _ in scopes)
    rows = conn.execute(
        f"""
        SELECT DISTINCT user_key FROM user_exclusions
        WHERE scope_key IN ({placeholders})
        """,
        scopes,
    ).fetchall()
    return {str(row[0]) for row in rows}


def invite_state_get(
    conn: sqlite3.Connection,
    target: str,
    user_key: str,
) -> Optional[tuple[str, str]]:
    row = conn.execute(
        """
        SELECT status, COALESCE(reason, '')
        FROM invite_state
        WHERE target=? AND user_key=?
        LIMIT 1
        """,
        (target, user_key),
    ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def invite_record(
    conn: sqlite3.Connection,
    *,
    target: str,
    user_key: str,
    user_id: Optional[int],
    username: Optional[str],
    status: str,
    reason: str = "",
    session_file: Optional[str] = None,
    flood_seconds: Optional[int] = None,
    count_attempt: bool = True,
) -> None:
    """Append one immutable event and update the current invite state."""
    now = utcnow_iso()
    clean_username = _clean_username(username)
    conn.execute(
        """
        INSERT INTO invite_events(
            target, user_key, user_id, username, session_file,
            status, reason, flood_seconds, ts
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            target,
            user_key,
            user_id,
            clean_username,
            session_file,
            status,
            reason,
            int(flood_seconds) if flood_seconds is not None else None,
            now,
        ),
    )
    attempt_delta = 1 if count_attempt else 0
    conn.execute(
        """
        INSERT INTO invite_state(
            target, user_key, user_id, username, status, reason,
            session_file, attempt_count, updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(target, user_key) DO UPDATE SET
            user_id=COALESCE(excluded.user_id, invite_state.user_id),
            username=COALESCE(excluded.username, invite_state.username),
            status=excluded.status,
            reason=excluded.reason,
            session_file=excluded.session_file,
            attempt_count=invite_state.attempt_count + ?,
            updated_at=excluded.updated_at
        """,
        (
            target,
            user_key,
            user_id,
            clean_username,
            status,
            reason,
            session_file,
            attempt_delta,
            now,
            attempt_delta,
        ),
    )
    conn.commit()


def invite_event_count(
    conn: sqlite3.Connection,
    target: str,
    user_key: str,
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM invite_events WHERE target=? AND user_key=?",
        (target, user_key),
    ).fetchone()
    return int(row[0] or 0) if row else 0
