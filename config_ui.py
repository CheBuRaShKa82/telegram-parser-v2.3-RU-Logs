# -*- coding: utf-8 -*-
"""Interactive configuration UI for telegram-parser v2.4."""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import List

from telethon.sync import TelegramClient

from config_store import AppConfig, legacy_options, load_config, save_legacy_options
from logging_setup import log_ok
from sessions import (
    SESSIONS_DIR,
    list_session_files,
    secure_session_file,
    session_name_from_file,
)


DEFAULT_OPTIONS = legacy_options(AppConfig())


def ensure_options() -> None:
    """Compatibility name; config.json is the canonical store."""
    load_config()


def getoptions() -> List[str]:
    return legacy_options()


def _create_account_session(api_id: int, api_hash: str) -> None:
    os.system("cls||clear")
    phone = input("Введите номер телефона аккаунта (формат +79991234567): ").strip()
    if not phone:
        print("Пустой номер.")
        time.sleep(1.5)
        return

    alias = datetime.now().strftime("account_%Y%m%d_%H%M%S")
    session_name = session_name_from_file(f"{alias}.session")
    client = TelegramClient(
        session_name,
        api_id,
        api_hash,
        flood_sleep_threshold=0,
    )
    try:
        print(
            "Сейчас придёт код в Telegram. Введите код и "
            "(если спросит) пароль 2FA."
        )
        client.start(phone=phone)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass

    secure_session_file(session_name)
    log_ok(f"Аккаунт добавлен: {alias}.session (папка {SESSIONS_DIR}/)")
    print("Готово. Сессия создана.")
    time.sleep(1.5)


def config() -> None:
    ensure_options()
    while True:
        os.system("cls||clear")
        options = getoptions()
        sessions = list_session_files()

        print("=== НАСТРОЙКИ v2.4 ===")
        print(f"1 - Обновить api_id   [{options[0].strip()}]")
        raw_hash = options[1].strip()
        masked_hash = (
            raw_hash[:4] + "****" + raw_hash[-4:]
            if raw_hash not in ("", "NONEHASH") and len(raw_hash) > 8
            else ("****" if raw_hash not in ("", "NONEHASH") else raw_hash)
        )
        print(f"2 - Обновить api_hash [{masked_hash}]")
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
            options[2] = (
                "False\n" if options[2].strip() == "True" else "True\n"
            )
        elif key == "4":
            options[3] = (
                "False\n" if options[3].strip() == "True" else "True\n"
            )
        elif key == "5":
            if (
                options[0].strip() in ("NONEID", "")
                or options[1].strip() in ("NONEHASH", "")
            ):
                print("Сначала задайте API_ID и API_HASH.")
                time.sleep(1.8)
                continue
            try:
                api_id = int(options[0].strip())
            except ValueError:
                print("API_ID должен быть числом.")
                time.sleep(1.8)
                continue
            _create_account_session(api_id, options[1].strip())
        elif key == "6":
            os.system("cls||clear")
            answer = input(
                "Сбросить API_ID/API_HASH и опции парсинга?\n"
                "1 - Да\n2 - Нет\nВвод: "
            ).strip()
            if answer == "1":
                options = DEFAULT_OPTIONS.copy()
        elif key.lower() == "e":
            break
        else:
            print("Неверный пункт.")
            time.sleep(1.0)
            continue

        save_legacy_options(options)
        time.sleep(0.2)
