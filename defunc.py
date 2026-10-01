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
from datetime import datetime
from typing import List

from config_store import AppConfig, legacy_options, load_config, save_legacy_options
from logging_setup import LOG_FILE, log_info, log_ok, log_pause, log_stop, log_warn
from sessions import (
    SESSIONS_DIR,
    ensure_sessions_dir,
    list_session_files,
    secure_session_file,
    session_name_from_file,
)
from parser import (
    DEFAULT_PARSER_FILTERS,
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

from telethon.sync import TelegramClient



# -------------------- COMPAT CONFIG WRAPPERS --------------------

DEFAULT_OPTIONS = legacy_options(AppConfig())


def ensure_options() -> None:
    """Compatibility wrapper: canonical config is config.json."""
    load_config()


def getoptions() -> List[str]:
    """Compatibility view used by the existing CLI."""
    return legacy_options()


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
    secure_session_file(session_name)

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

        # config.json is canonical; options.txt is only migrated once.
        save_legacy_options(options)

        # небольшая пауза, чтобы меню не "мигало"
        time.sleep(0.2)




# -------------------------------------------------------------------
# (Опционально) экспортируем публичные функции для удобного импорта
__all__ = [
    "ParserFilterConfig",
    "config",
    "getoptions",
    "parsing",
    "parsing_from_messages",
    "parsing_channel_comments",
    "export_users",
    "inviting",
    "inviting_rotate_sessions",
    "preflight_sessions_for_target",
    "target_ref",
]
