# -*- coding: utf-8 -*-
"""
telegram-parser v2.4 (main)
Парсер + инвайтер. Жёсткий фильтр качества. RU-логи.

Файлы:
- options.txt
- usernames.txt / userids.txt
- invite_ledger.db
- app.log
"""

import os
import time
from typing import Any, List, Optional, Union

from storage import (
    UserCandidate,
    candidate_from_raw,
    connect_db,
    list_user_sources,
    load_user_candidates,
)
from telethon.sync import TelegramClient

from config_ui import config, getoptions
from inviter import (
    inviting_rotate_sessions,
    preflight_sessions_for_target,
    target_ref,
)
from parser import (
    ParserFilterConfig,
    export_users,
    parsing,
    parsing_channel_comments,
    parsing_from_messages,
)
from sessions import (
    SESSIONS_DIR,
    list_session_files,
    session_name_from_file,
)


def yn(prompt: str) -> bool:
    raw = input(prompt).strip().lower()
    return raw in ("y", "yes", "д", "да")


def _fmt_dialog(d) -> str:
    ent = d.entity
    username = getattr(ent, "username", None)
    did = getattr(ent, "id", None)
    kind = "Чат"
    cls = ent.__class__.__name__.lower()
    if "channel" in cls:
        kind = "Канал"
    if "chat" in cls and "channel" not in cls:
        kind = "Группа"
    name = (d.name or "").strip() or "(без названия)"
    if username:
        return f"{kind}: {name}  (@{username})"
    if did is not None:
        return f"{kind}: {name}  (id:{did})"
    return f"{kind}: {name}"


def pick_dialog(client: TelegramClient, title: str):
    """Показывает список диалогов и возвращает entity (предпочтительно) либо введённую строку."""
    try:
        dialogs = client.get_dialogs(limit=200)
    except Exception:
        dialogs = []

    if not dialogs:
        print("Не удалось получить список диалогов. Вставь @username/ссылку/id вручную.")
        return input(title).strip() or None

    flt = input("Фильтр (часть названия) или Enter чтобы показать последние 50: ").strip().lower()
    if flt:
        dialogs = [d for d in dialogs if flt in (d.name or "").lower()]

    dialogs = dialogs[:50]
    print("\n=== ТВОИ ДИАЛОГИ (последние/по фильтру) ===")
    for i, d in enumerate(dialogs, 1):
        print(f"{i}. {_fmt_dialog(d)}")
    print("0. Ввести вручную")
    raw = input("Выбор: ").strip()
    if raw == "0":
        return input(title).strip() or None
    if not raw.isdigit():
        return None
    idx = int(raw)
    if idx < 1 or idx > len(dialogs):
        return None

    # ВАЖНО: возвращаем entity, а не id.
    return dialogs[idx - 1].entity


def clear() -> None:
    os.system("cls||clear")


def ask_parser_filters() -> ParserFilterConfig:
    print("\n=== ФИЛЬТР ПОЛЬЗОВАТЕЛЕЙ ===")
    print("Enter = безопасные значения по умолчанию.")
    require_username = yn("Требовать username? (y/n, default n): ")
    require_photo = yn("Требовать фото профиля? (y/n, default n): ")
    raw_days = input(
        "Активность: 0=не учитывать, 7/30/90 дней (default 0): "
    ).strip()
    try:
        active_days = int(raw_days) if raw_days else 0
    except ValueError:
        active_days = 0
    if active_days not in (0, 7, 30, 90):
        active_days = 0
    return ParserFilterConfig(
        exclude_bots=True,
        exclude_deleted=True,
        exclude_scam_fake=True,
        require_username=require_username,
        require_photo=require_photo,
        active_days=active_days,
    )


def list_sessions() -> List[str]:
    return list_session_files()


def pick_session() -> Optional[str]:
    sessions = list_sessions()
    if not sessions:
        print("Сессии не найдены. Зайди в Настройки → Добавить аккаунт.")
        time.sleep(2)
        return None

    print("=== АККАУНТЫ (.session) ===")
    for i, s in enumerate(sessions, 1):
        print(f"{i}. {s}")
    raw = input("Выбери номер аккаунта: ").strip()
    if not raw.isdigit():
        return None
    idx = int(raw)
    if idx < 1 or idx > len(sessions):
        return None
    return sessions[idx - 1]


def pick_sessions() -> List[str]:
    """Выбор нескольких .session для ротации.

    Ввод:
      - all
      - 1,2,5
      - 3
    """
    sessions = list_sessions()
    if not sessions:
        print("Сессии не найдены. Зайди в Настройки → Добавить аккаунт.")
        time.sleep(2)
        return []

    print("=== АККАУНТЫ (.session) ===")
    for i, s in enumerate(sessions, 1):
        print(f"{i}. {s}")

    raw = input("Выбери аккаунты (all или номера через запятую): ").strip().lower()
    if not raw:
        return []
    if raw == "all":
        return sessions

    out: List[str] = []
    for part in raw.split(","):
        p = part.strip()
        if not p.isdigit():
            continue
        idx = int(p)
        if 1 <= idx <= len(sessions):
            out.append(sessions[idx - 1])

    seen = set()
    uniq: List[str] = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


def make_client(session_file: str, api_id: int, api_hash: str) -> TelegramClient:
    # session_file хранится как '<name>.session' (basename), а сами файлы лежат в папке sessoins/
    session_name = session_name_from_file(session_file)
    client = TelegramClient(session_name, api_id, api_hash, flood_sleep_threshold=0)
    client.connect()
    if not client.is_user_authorized():
        print(f"Сессия не авторизована. Создай её заново в Настройках (пункт 5). Папка: {SESSIONS_DIR}/")
        raise SystemExit(1)
    return client


def do_parsing() -> None:
    clear()
    opts = getoptions()
    if opts[0].strip() in ("NONEID", "") or opts[1].strip() in ("NONEHASH", ""):
        print("Сначала задай API_ID и API_HASH в Настройках.")
        time.sleep(2)
        return

    sess = pick_session()
    if not sess:
        return

    api_id = int(opts[0].strip())
    api_hash = opts[1].strip()

    parse_id = (opts[2].strip() == "True")
    parse_name = (opts[3].strip() == "True")

    client = make_client(sess, api_id, api_hash)
    src = pick_dialog(client, "Источник (чат/канал) для парсинга (@username/ссылка/id): ")
    if not src:
        client.disconnect()
        return
    try:
        filters = ask_parser_filters()
        parsing(
            client,
            src,
            parse_id=parse_id,
            parse_name=parse_name,
            filters=filters,
            checkpoint_batch=100,
        )
        print("Готово. Основная база: invite_ledger.db; TXT — совместимый экспорт.")
    finally:
        client.disconnect()
        time.sleep(1.5)


def _load_users_from_files(
    *,
    source_id: Optional[str] = None,
    source_type: Optional[str] = None,
) -> List[Union[UserCandidate, str, int]]:
    """Load SQLite queue; legacy TXT fallback is used only for the full queue."""
    conn = connect_db()
    try:
        candidates = load_user_candidates(
            conn,
            source_id=source_id,
            source_type=source_type,
        )
    finally:
        conn.close()
    if candidates or source_id is not None or source_type is not None:
        return candidates

    legacy: List[Union[UserCandidate, str, int]] = []
    for path in ("userids.txt", "usernames.txt"):
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                candidate = candidate_from_raw(raw)
                if candidate.key != "empty":
                    legacy.append(candidate)

    seen = set()
    uniq: List[Union[UserCandidate, str, int]] = []
    for user in legacy:
        candidate = candidate_from_raw(user)
        if candidate.key in seen:
            continue
        seen.add(candidate.key)
        uniq.append(candidate)
    return uniq


def pick_user_queue() -> List[Union[UserCandidate, str, int]]:
    conn = connect_db()
    try:
        sources = list_user_sources(conn)
    finally:
        conn.close()

    if not sources:
        return _load_users_from_files()

    print("\n=== ИСТОЧНИК ПОЛЬЗОВАТЕЛЕЙ ===")
    print("0. Вся база")
    for index, source in enumerate(sources, 1):
        title = source.get("source_title") or source.get("source_id") or "unknown"
        source_type = source.get("source_type") or "unknown"
        count = source.get("users") or 0
        print(f"{index}. {title} [{source_type}] — {count} users")

    raw = input("Выбери источник очереди (default 0): ").strip()
    if not raw:
        return _load_users_from_files()
    if not raw.isdigit():
        return []
    index = int(raw)
    if index == 0:
        return _load_users_from_files()
    if index < 1 or index > len(sources):
        return []

    selected = sources[index - 1]
    return _load_users_from_files(
        source_id=selected.get("source_id"),
        source_type=selected.get("source_type"),
    )


def do_parsing_messages() -> None:
    clear()
    opts = getoptions()
    if opts[0].strip() in ("NONEID", "") or opts[1].strip() in ("NONEHASH", ""):
        print("Сначала задай API_ID и API_HASH в Настройках.")
        time.sleep(2)
        return

    sess = pick_session()
    if not sess:
        return

    api_id = int(opts[0].strip())
    api_hash = opts[1].strip()

    client = make_client(sess, api_id, api_hash)
    src = pick_dialog(client, "Источник (чат/канал/группа): ")
    if not src:
        client.disconnect()
        return

    parse_name = yn("Парсить usernames? (y/n): ")
    parse_id = yn("Парсить user ids? (y/n): ")
    if not (parse_name or parse_id):
        print("Нечего парсить — выбери хотя бы usernames или ids.")
        client.disconnect()
        time.sleep(2)
        return

    lm_raw = input("Сколько сообщений смотреть? (по умолчанию 5000): ").strip()
    limit_messages = int(lm_raw) if lm_raw.isdigit() else 5000
    if limit_messages > 200000:
        print("⚠️ Очень большой лимит сообщений. Обычно хватает 5000–50000.", flush=True)

    days_raw = input("Макс. возраст сообщений в днях (по умолчанию 7): ").strip()
    max_days = int(days_raw) if days_raw.isdigit() else 7
    if max_days > 30:
        print("⚠️ Возраст 30+ дней увеличит время и снизит качество базы.", flush=True)

    filters = ask_parser_filters()
    resume = yn("Продолжить с последнего checkpoint, если он есть? (y/n): ")

    try:
        print("Запускаю парсинг из сообщений…", flush=True)
        parsing_from_messages(
            client,
            src,
            parse_id=parse_id,
            parse_name=parse_name,
            limit_messages=limit_messages,
            max_age_days=max_days,
            filters=filters,
            checkpoint_batch=100,
            resume=resume,
        )
        print("Готово. Смотри usernames.txt / userids.txt и app.log")
    finally:
        client.disconnect()
        time.sleep(1.5)


def do_parsing_comments() -> None:
    clear()
    opts = getoptions()
    if opts[0].strip() in ("NONEID", "") or opts[1].strip() in ("NONEHASH", ""):
        print("Сначала задай API_ID и API_HASH в Настройках.")
        time.sleep(2)
        return

    sess = pick_session()
    if not sess:
        return

    api_id = int(opts[0].strip())
    api_hash = opts[1].strip()
    client = make_client(sess, api_id, api_hash)
    src = pick_dialog(client, "Broadcast-канал с комментариями: ")
    if not src:
        client.disconnect()
        return

    parse_name = yn("Экспортировать usernames? (y/n): ")
    parse_id = yn("Экспортировать user ids? (y/n): ")
    if not (parse_name or parse_id):
        print("SQLite всё равно хранит user_id; включаю ID-экспорт.")
        parse_id = True

    posts_raw = input("Сколько постов смотреть? (default 200): ").strip()
    comments_raw = input(
        "Максимум комментариев на пост (0 = все, default 0): "
    ).strip()
    days_raw = input(
        "Макс. возраст постов в днях (0 = без ограничения, default 30): "
    ).strip()

    try:
        limit_posts = int(posts_raw) if posts_raw else 200
    except ValueError:
        limit_posts = 200
    try:
        comments_per_post = int(comments_raw) if comments_raw else 0
    except ValueError:
        comments_per_post = 0
    try:
        max_days = int(days_raw) if days_raw else 30
    except ValueError:
        max_days = 30

    filters = ask_parser_filters()
    resume = yn("Продолжить с последнего checkpoint, если он есть? (y/n): ")

    try:
        parsing_channel_comments(
            client,
            src,
            parse_id=parse_id,
            parse_name=parse_name,
            limit_posts=max(1, limit_posts),
            comments_per_post=max(0, comments_per_post),
            max_age_days=max(0, max_days),
            filters=filters,
            checkpoint_batch=10,
            resume=resume,
        )
        print(
            "Готово. Это активная аудитория из комментариев, "
            "а не полный список подписчиков."
        )
    except ValueError as exc:
        print(f"Ошибка: {exc}")
    finally:
        client.disconnect()
        time.sleep(1.5)


def do_export_users() -> None:
    clear()
    paths = export_users(formats=("csv", "json", "txt"))
    print("Экспорт завершён:")
    if not paths:
        print("Нет данных для экспорта.")
    else:
        for fmt, path in paths.items():
            print(f"  {fmt.upper()}: {path}")
    input("Нажми Enter...")


def do_inviting() -> None:
    clear()
    opts = getoptions()
    if opts[0].strip() in ("NONEID", "") or opts[1].strip() in ("NONEHASH", ""):
        print("Сначала задай API_ID и API_HASH в Настройках.")
        time.sleep(2)
        return

    sess_list = pick_sessions()
    if not sess_list:
        return

    api_id = int(opts[0].strip())
    api_hash = opts[1].strip()

    # Берём первую сессию, чтобы выбрать цель из диалогов
    client = make_client(sess_list[0], api_id, api_hash)
    target_entity = pick_dialog(client, "Куда инвайтить? (@username/ссылка/id): ")
    if not target_entity:
        client.disconnect()
        return

    # Важно: делаем target переносимым между сессиями
    target = target_ref(target_entity)

    users = pick_user_queue()
    if not users:
        print("Списки пустые. Сначала сделай Парсинг.")
        client.disconnect()
        time.sleep(2)
        return

    # Selector client is no longer used for the actual invite run.
    client.disconnect()

    raw_delay = input(
        "Базовая задержка между попытками (сек), по умолчанию 5.0: "
    ).strip()
    try:
        base_delay = float(raw_delay) if raw_delay else 5.0
    except ValueError:
        base_delay = 5.0

    if len(sess_list) > 1:
        re_raw = input(
            "Плановая смена сессии каждые N успешных инвайтов "
            "(0 = только по состоянию), по умолчанию 0: "
        ).strip()
        try:
            rotate_every = int(re_raw) if re_raw else 0
        except ValueError:
            rotate_every = 0
    else:
        rotate_every = 0

    ma_raw = input(
        "Максимум попыток одной сессии за запуск "
        "(0 = без лимита), по умолчанию 20: "
    ).strip()
    try:
        max_attempts = int(ma_raw) if ma_raw else 20
    except ValueError:
        max_attempts = 20

    nm = yn("Ночной режим (пауза ночью)? (y/n): ")
    night_start = (2, 0)
    night_end = (7, 0)
    if nm:
        ns = input("Окно ночи START HH:MM (по умолчанию 02:00): ").strip()
        ne = input("Окно ночи END   HH:MM (по умолчанию 07:00): ").strip()

        def _parse_hm(value, default):
            if not value:
                return default
            try:
                hour, minute = value.split(":", 1)
                hour = int(hour)
                minute = int(minute)
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return (hour, minute)
            except (TypeError, ValueError):
                pass
            return default

        night_start = _parse_hm(ns, (2, 0))
        night_end = _parse_hm(ne, (7, 0))

    default_user_attempts = 1 if len(sess_list) == 1 else min(3, len(sess_list))
    ua_raw = input(
        "Лимит попыток на одного пользователя "
        f"(default {default_user_attempts}): "
    ).strip()
    try:
        max_user_attempts = (
            int(ua_raw) if ua_raw else default_user_attempts
        )
    except ValueError:
        max_user_attempts = default_user_attempts

    pf_raw = input(
        "Заморозка сессии при PeerFlood (часы, по умолчанию 24): "
    ).strip()
    try:
        peerflood_hours = int(pf_raw) if pf_raw else 24
    except ValueError:
        peerflood_hours = 24

    j_raw = input(
        "Джиттер min-max сек, по умолчанию 0.5-1.5: "
    ).strip()
    jitter_min, jitter_max = 0.5, 1.5
    if j_raw:
        try:
            left, right = j_raw.split("-", 1)
            jitter_min = float(left.strip())
            jitter_max = float(right.strip())
        except (TypeError, ValueError):
            jitter_min, jitter_max = 0.5, 1.5

    ph_raw = input(
        "Лимит успешных инвайтов на сессию В ЧАС "
        "(0 = выключить), по умолчанию 10: "
    ).strip()
    pd_raw = input(
        "Лимит успешных инвайтов на сессию В СУТКИ "
        "(0 = выключить), по умолчанию 30: "
    ).strip()
    try:
        per_hour = int(ph_raw) if ph_raw else 10
    except ValueError:
        per_hour = 10
    try:
        per_day = int(pd_raw) if pd_raw else 30
    except ValueError:
        per_day = 30

    if (per_hour == 0 or per_day == 0) and not yn(
        "Один из лимитов отключён. Подтвердить отключение? (y/n): "
    ):
        per_hour = per_hour or 10
        per_day = per_day or 30

    if yn(
        "Сделать preflight (проверка сессий + авто-вступление в цель)? (y/n): "
    ):
        rep = preflight_sessions_for_target(
            api_id=api_id,
            api_hash=api_hash,
            session_files=sess_list,
            target=target,
            auto_join=True,
            block_cannot_join_hours=24,
        )
        ok_list = list(rep.get("ok", [])) + list(rep.get("joined", []))
        print("\n=== PRE-FLIGHT REPORT ===")
        print(f"OK (уже в цели): {len(rep.get('ok', []))}")
        print(f"JOINED (вступил): {len(rep.get('joined', []))}")
        print(f"NOT AUTH: {len(rep.get('not_authorized', []))}")
        print(f"CANNOT JOIN: {len(rep.get('cannot_join', []))}")
        print(f"NO RIGHTS: {len(rep.get('no_rights', []))}")
        print(f"FLOOD WAIT: {len(rep.get('flood_wait', []))}")
        print(f"NETWORK: {len(rep.get('network', []))}")
        print(f"UNKNOWN: {len(rep.get('unknown', []))}")
        if not ok_list:
            print("Нет подходящих сессий после preflight. Останавливаю.")
            input("Нажми Enter...")
            return
        sess_list = ok_list
        input("Нажми Enter, чтобы продолжить...")

    inviting_rotate_sessions(
        api_id=api_id,
        api_hash=api_hash,
        session_files=sess_list,
        target=target,
        users=users,
        base_delay=base_delay,
        rotate_every=rotate_every,
        max_attempts_per_session=max_attempts,
        jitter_min=jitter_min,
        jitter_max=jitter_max,
        max_user_attempts=max_user_attempts,
        peerflood_freeze_hours=peerflood_hours,
        night_mode=nm,
        night_start=night_start,
        night_end=night_end,
        per_hour_limit=per_hour,
        per_day_limit=per_day,
    )

    print(
        "Готово. Основная очередь и статусы находятся в SQLite; "
        "TXT-файлы — только legacy/export."
    )

    time.sleep(1.5)


def main() -> None:
    while True:
        clear()
        print("=== TELEGRAM PARSER / INVITER v2.4 ===")
        print("1 - Настройки")
        print("2 - Парсинг видимых участников")
        print("3 - Парсинг активных авторов сообщений")
        print("4 - Парсинг авторов комментариев канала")
        print("5 - Экспорт SQLite → CSV / JSON / TXT")
        print("6 - Инвайт из базы пользователей")
        print("7 - Выход")
        key = input("Ввод: ").strip()

        if key == "1":
            config()
        elif key == "2":
            do_parsing()
        elif key == "3":
            do_parsing_messages()
        elif key == "4":
            do_parsing_comments()
        elif key == "5":
            do_export_users()
        elif key == "6":
            do_inviting()
        elif key == "7":
            break
        else:
            print("Неверный пункт.")
            time.sleep(1)


if __name__ == "__main__":
    main()
