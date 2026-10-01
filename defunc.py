# -*- coding: utf-8 -*-
"""Compatibility facade for telegram-parser v2.4.

New code should import directly from:
- config_ui.py
- parser.py
- inviter.py
- sessions.py

This module remains to avoid breaking older imports.
"""

from config_ui import config, ensure_options, getoptions
from inviter import (
    LEDGER_DB,
    SessionState,
    _db,
    _is_time_in_window,
    _pick_best_session,
    _seconds_until_window_end,
    excluded_add,
    excluded_has,
    excluded_load_all,
    excluded_reason,
    id_ref_from_userobj,
    inviting,
    inviting_rotate_sessions,
    ledger_get,
    ledger_put,
    parse_user_ref,
    preflight_sessions_for_target,
    prune_users_files,
    resolve_target_for_client,
    resolve_user_for_client,
    session_consume_invite_token,
    session_next_time_due_to_limits,
    session_stats_load,
    session_stats_save,
    target_ref,
)
from parser import (
    DEFAULT_PARSER_FILTERS,
    DB_PATH,
    ParserFilterConfig,
    _parser_checkpoint_key,
    _source_metadata,
    export_users,
    parsing,
    parsing_channel_comments,
    parsing_from_messages,
    quality_hard,
    quality_user,
)
from sessions import (
    SESSIONS_DIR,
    ensure_sessions_dir,
    list_session_files,
    secure_session_file,
    session_name_from_file,
)


__all__ = [
    "ParserFilterConfig",
    "SessionState",
    "config",
    "ensure_options",
    "getoptions",
    "parsing",
    "parsing_from_messages",
    "parsing_channel_comments",
    "export_users",
    "inviting",
    "inviting_rotate_sessions",
    "preflight_sessions_for_target",
    "target_ref",
    "prune_users_files",
    "list_session_files",
    "session_name_from_file",
    "SESSIONS_DIR",
]
