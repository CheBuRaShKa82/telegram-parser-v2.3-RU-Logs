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
SCHEMA_VERSION = 6


@dataclass(frozen=True)
class UserCandidate:
    user_id: Optional[int]
    username: Optional[str]
    preferred_session: Optional[str] = None

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
        CREATE TABLE IF NOT EXISTS user_session_seen (
            user_id INTEGER NOT NULL,
            session_file TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(user_id, session_file)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_session_seen_last "
        "ON user_session_seen(user_id, last_seen_at DESC)"
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
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_inv_unique "
        "ON invites(target, user_key)"
    )
    conn.execute(
        """
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
        """
    )
    session_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(session_stats)").fetchall()
    }
    if "status" not in session_cols:
        conn.execute(
            "ALTER TABLE session_stats "
            "ADD COLUMN status TEXT DEFAULT 'active'"
        )
    if "status_reason" not in session_cols:
        conn.execute(
            "ALTER TABLE session_stats "
            "ADD COLUMN status_reason TEXT DEFAULT ''"
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS parser_checkpoints (
            checkpoint_key TEXT PRIMARY KEY,
            source_id TEXT,
            source_title TEXT,
            mode TEXT NOT NULL,
            cursor_int INTEGER,
            processed INTEGER NOT NULL DEFAULT 0,
            saved INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'running',
            updated_at TEXT NOT NULL
        )
        """
    )
    # Import the legacy invite snapshot exactly once.
    legacy_imported = conn.execute(
        "SELECT value FROM schema_meta "
        "WHERE key='legacy_invites_imported'"
    ).fetchone()
    if not legacy_imported:
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
            INSERT INTO schema_meta(key, value)
            VALUES('legacy_invites_imported', '1')
            ON CONFLICT(key) DO UPDATE SET value='1'
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
    if os.name != "nt" and not os.path.exists(path):
        try:
            fd = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
            os.close(fd)
        except FileExistsError:
            pass

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
    session_file: Optional[str] = None,
) -> Optional[int]:
    uid = getattr(user, "id", None)
    if uid is None:
        return None

    uid = int(uid)
    username = _clean_username(getattr(user, "username", None))
    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    parsed_now = utcnow_iso()
    observed_at = seen_at or parsed_now
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
            last_seen_at=CASE
                WHEN users.last_seen_at >= excluded.last_seen_at
                    THEN users.last_seen_at
                ELSE excluded.last_seen_at
            END
        """,
        (
            uid,
            username,
            first_name,
            last_name,
            source_id_text,
            source_title,
            source_type,
            parsed_now,
            observed_at,
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
            last_seen_at=CASE
                WHEN user_sources.last_seen_at >= excluded.last_seen_at
                    THEN user_sources.last_seen_at
                ELSE excluded.last_seen_at
            END
        """,
        (
            uid,
            source_key,
            source_id_text,
            source_title,
            source_type,
            parsed_now,
            observed_at,
        ),
    )
    if session_file:
        session_name = os.path.basename(str(session_file))
        conn.execute(
            """
            INSERT INTO user_session_seen(
                user_id, session_file, first_seen_at, last_seen_at
            )
            VALUES(?,?,?,?)
            ON CONFLICT(user_id, session_file) DO UPDATE SET
                last_seen_at=CASE
                    WHEN user_session_seen.last_seen_at >= excluded.last_seen_at
                        THEN user_session_seen.last_seen_at
                    ELSE excluded.last_seen_at
                END
            """,
            (uid, session_name, parsed_now, observed_at),
        )

    return uid


def load_user_candidates(
    conn: sqlite3.Connection,
    *,
    source_id: Optional[str] = None,
    source_type: Optional[str] = None,
) -> List[UserCandidate]:
    where: List[str] = []
    params: List[Any] = []

    if source_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM user_sources us "
            "WHERE us.user_id=u.user_id AND us.source_id=?)"
        )
        params.append(str(source_id))
    if source_type is not None:
        where.append(
            "EXISTS (SELECT 1 FROM user_sources us "
            "WHERE us.user_id=u.user_id AND us.source_type=?)"
        )
        params.append(str(source_type))

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        """
        SELECT
            u.user_id,
            u.username,
            (
                SELECT ss.session_file
                FROM user_session_seen ss
                WHERE ss.user_id=u.user_id
                ORDER BY ss.last_seen_at DESC, ss.session_file ASC
                LIMIT 1
            ) AS preferred_session
        FROM users u
        """
        + where_sql
        + " ORDER BY u.last_seen_at DESC, u.user_id ASC",
        params,
    ).fetchall()
    return [
        UserCandidate(
            user_id=int(uid),
            username=_clean_username(username),
            preferred_session=(
                str(preferred_session)
                if preferred_session is not None
                else None
            ),
        )
        for uid, username, preferred_session in rows
    ]


def list_user_sources(conn: sqlite3.Connection) -> List[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_id, source_title, source_type, COUNT(DISTINCT user_id)
        FROM user_sources
        GROUP BY source_id, source_title, source_type
        ORDER BY MAX(last_seen_at) DESC, source_title ASC
        """
    ).fetchall()
    return [
        {
            "source_id": row[0],
            "source_title": row[1],
            "source_type": row[2],
            "users": int(row[3] or 0),
        }
        for row in rows
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
    commit: bool = True,
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
    if commit:
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



@dataclass(frozen=True)
class ParserCheckpoint:
    checkpoint_key: str
    source_id: Optional[str]
    source_title: Optional[str]
    mode: str
    cursor_int: Optional[int]
    processed: int
    saved: int
    status: str
    updated_at: str


def checkpoint_get(
    conn: sqlite3.Connection,
    checkpoint_key: str,
) -> Optional[ParserCheckpoint]:
    row = conn.execute(
        """
        SELECT checkpoint_key, source_id, source_title, mode, cursor_int,
               processed, saved, status, updated_at
        FROM parser_checkpoints
        WHERE checkpoint_key=?
        LIMIT 1
        """,
        (checkpoint_key,),
    ).fetchone()
    if not row:
        return None
    return ParserCheckpoint(
        checkpoint_key=str(row[0]),
        source_id=str(row[1]) if row[1] is not None else None,
        source_title=str(row[2]) if row[2] is not None else None,
        mode=str(row[3]),
        cursor_int=int(row[4]) if row[4] is not None else None,
        processed=int(row[5] or 0),
        saved=int(row[6] or 0),
        status=str(row[7] or "running"),
        updated_at=str(row[8]),
    )


def checkpoint_put(
    conn: sqlite3.Connection,
    *,
    checkpoint_key: str,
    mode: str,
    source_id: Optional[str],
    source_title: Optional[str],
    cursor_int: Optional[int],
    processed: int,
    saved: int,
    status: str = "running",
) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO parser_checkpoints(
            checkpoint_key, source_id, source_title, mode, cursor_int,
            processed, saved, status, updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(checkpoint_key) DO UPDATE SET
            source_id=excluded.source_id,
            source_title=excluded.source_title,
            mode=excluded.mode,
            cursor_int=excluded.cursor_int,
            processed=excluded.processed,
            saved=excluded.saved,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (
            checkpoint_key,
            source_id,
            source_title,
            mode,
            cursor_int,
            int(processed),
            int(saved),
            status,
            now,
        ),
    )
    conn.commit()


def checkpoint_clear(conn: sqlite3.Connection, checkpoint_key: str) -> None:
    conn.execute(
        "DELETE FROM parser_checkpoints WHERE checkpoint_key=?",
        (checkpoint_key,),
    )
    conn.commit()


def export_users_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT user_id, username, first_name, last_name,
               source_id, source_title, source_type, parsed_at, last_seen_at
        FROM users
        ORDER BY last_seen_at DESC, user_id ASC
        """
    ).fetchall()
    columns = (
        "user_id", "username", "first_name", "last_name",
        "source_id", "source_title", "source_type", "parsed_at", "last_seen_at",
    )
    return [dict(zip(columns, row)) for row in rows]
