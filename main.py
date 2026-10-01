# -*- coding: utf-8 -*-
"""
telegram-parser-v2.3 (main)
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

from storage import UserCandidate, candidate_from_raw, connect_db, load_user_candidates
from telethon.sync import TelegramClient

from defunc import (
    config,
    getoptions,
    parsing,
    parsing_from_messages,
    inviting,
    inviting_rotate_sessions,
    preflight_sessions_for_target,
    target_ref,
    prune_users_files,
    SESSIONS_DIR,
    ensure_sessions_dir,
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
        parsing(client, src, parse_id=parse_id, parse_name=parse_name)
        print("Готово. Смотри usernames.txt / userids.txt и app.log")
    finally:
        client.disconnect()
        time.sleep(1.5)


def _load_users_from_files() -> List[Union[UserCandidate, str, int]]:
    """Load the canonical SQLite user queue; fall back to legacy TXT exports."""
    conn = connect_db()
    try:
        candidates = load_user_candidates(conn)
    finally:
        conn.close()
    if candidates:
        return candidates

    legacy: List[Union[UserCandidate, str, int]] = []
    for path in ("userids.txt", "usernames.txt"):
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
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

    try:
        print("Запускаю парсинг из сообщений…", flush=True)
        parsing_from_messages(
            client,
            src,
            parse_id=parse_id,
            parse_name=parse_name,
            limit_messages=limit_messages,
            max_age_days=max_days,
        )
        print("Готово. Смотри usernames.txt / userids.txt и app.log")
    finally:
        client.disconnect()
        time.sleep(1.5)


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

    users = _load_users_from_files()
    if not users:
        print("Списки пустые. Сначала сделай Парсинг.")
        client.disconnect()
        time.sleep(2)
        return

    raw_delay = input("Базовая задержка между попытками (сек), по умолчанию 2.0: ").strip()
    try:
        base_delay = float(raw_delay) if raw_delay else 2.0
    except Exception:
        base_delay = 2.0

    try:
        if len(sess_list) == 1:
            inviting(client, target_entity, users, base_delay=base_delay)
            if yn("Очистить usernames.txt / userids.txt от уже обработанных (ускорить следующий прогон)? (y/n): "):
                removed, kept = prune_users_files(target)
                print(f"Очищено записей: {removed}. Осталось: {kept}. Бэкап: *.bak-...")
                input("Нажми Enter...")
        else:
            # закрываем первый клиент, дальше будут открываться по мере ротации
            client.disconnect()

            re_raw = input(
                "Плановая смена сессии каждые N успешных инвайтов (0 = только по флуду), по умолчанию 0: "
            ).strip()
            ma_raw = input("Максимум попыток одной сессии ЗА ЭТОТ ЗАПУСК (0 = без лимита), по умолчанию 0: ").strip()
            try:
                rotate_every = int(re_raw) if re_raw else 0
            except Exception:
                rotate_every = 0
            try:
                max_attempts = int(ma_raw) if ma_raw else 0
            except Exception:
                max_attempts = 0


            nm = yn("Ночной режим (пауза ночью)? (y/n): ")
            night_start = (2, 0)
            night_end = (7, 0)
            if nm:
                ns = input("Окно ночи START HH:MM (по умолчанию 02:00): ").strip()
                ne = input("Окно ночи END   HH:MM (по умолчанию 07:00): ").strip()
                def _parse_hm(v, d):
                    if not v:
                        return d
                    try:
                        h,m = v.split(":",1)
                        h=int(h); m=int(m)
                        if 0<=h<=23 and 0<=m<=59:
                            return (h,m)
                    except Exception:
                        pass
                    return d
                night_start = _parse_hm(ns, (2,0))
                night_end = _parse_hm(ne, (7,0))

            ua_raw = input("Лимит попыток на одного юзера (0 = по одной на каждую доступную сессию, по умолчанию 3): ").strip()
            try:
                max_user_attempts = int(ua_raw) if ua_raw else 3
            except Exception:
                max_user_attempts = 3

            pf_raw = input("Заморозка сессии при PeerFlood (часы, по умолчанию 24): ").strip()
            try:
                peerflood_hours = int(pf_raw) if pf_raw else 24
            except Exception:
                peerflood_hours = 24

            j_raw = input("Джиттер (случайная прибавка) min-max сек, по умолчанию 0.3-1.2: ").strip()
            jitter_min, jitter_max = 0.3, 1.2
            if j_raw:
                try:
                    a,b = j_raw.split("-",1)
                    jitter_min = float(a.strip())
                    jitter_max = float(b.strip())
                except Exception:
                    jitter_min, jitter_max = 0.3, 1.2

            ph_raw = input("Лимит инвайтов на сессию В ЧАС (0 = выключено), по умолчанию 0: ").strip()
            pd_raw = input("Лимит инвайтов на сессию В СУТКИ (0 = выключено), по умолчанию 0: ").strip()
            try:
                per_hour = int(ph_raw) if ph_raw else 0
            except Exception:
                per_hour = 0
            try:
                per_day = int(pd_raw) if pd_raw else 0
            except Exception:
                per_day = 0


            if yn("Сделать PRO-preflight (проверка сессий + авто-вступление в цель)? (y/n): "):
                rep = preflight_sessions_for_target(
                    api_id=api_id,
                    api_hash=api_hash,
                    session_files=sess_list,
                    target=target,
                    auto_join=True,
                    block_cannot_join_hours=24,
                )
                ok_list = list(rep.get('ok', [])) + list(rep.get('joined', []))
                print("\n=== PRE-FLIGHT REPORT ===")
                print(f"OK (уже в цели): {len(rep.get('ok', []))}")
                print(f"JOINED (вступил): {len(rep.get('joined', []))}")
                print(f"NOT AUTH (не авториз): {len(rep.get('not_authorized', []))}")
                print(f"CANNOT JOIN (нет доступа/приват): {len(rep.get('cannot_join', []))}")
                print(f"NO RIGHTS (нет прав/ограничен): {len(rep.get('no_rights', []))}")
                print(f"FLOOD WAIT (пауза): {len(rep.get('flood_wait', []))}")
                print(f"NETWORK (сеть): {len(rep.get('network', []))}")
                print(f"UNKNOWN: {len(rep.get('unknown', []))}")
                if not ok_list:
                    print("Нет подходящих сессий после preflight. Останавливаю.")
                    input("Нажми Enter...")
                    return
                sess_list = ok_list
                input("Это не зависание 🙂 Тут пауза после отчета. Нажми Enter чтобы продолжить...")

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

            # Опциональная очистка базы: убираем уже обработанных из файлов
            if yn("Очистить usernames.txt / userids.txt от уже обработанных (ускорить следующий прогон)? (y/n): "):
                removed, kept = prune_users_files(target)
                print(f"Очищено записей: {removed}. Осталось: {kept}. Бэкап: *.bak-...")
                input("Нажми Enter...")
        print("Готово. Смотри invite_ledger.db и app.log")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        time.sleep(1.5)


def main() -> None:
    while True:
        clear()
        print("=== TELEGRAM PARSER / INVITER v2.3 ===")
        print("1 - Настройки")
        print("2 - Парсинг участников (если список виден)")
        print("3 - Парсинг из сообщений (если список скрыт)")
        print("4 - Инвайт из usernames.txt / userids.txt (с учётом ledger)")
        print("5 - Выход")
        key = input("Ввод: ").strip()

        if key == "1":
            config()
        elif key == "2":
            do_parsing()
        elif key == "3":
            do_parsing_messages()
        elif key == "4":
            do_inviting()
        elif key == "5":
            break
        else:
            print("Неверный пункт.")
            time.sleep(1)


if __name__ == "__main__":
    main()
