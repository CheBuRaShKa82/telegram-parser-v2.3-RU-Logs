# Telegram Parser / Inviter v2.4

Консольный инструмент на Python + Telethon для сбора доступной аудитории Telegram, хранения пользователей в SQLite и приглашения пользователей в целевые группы с поддержкой нескольких аккаунтов.

Версия v2.4 — переработанная ветка исходного `telegram-parser-v2.3-RU-Logs`: исправлена multi-account логика, убран перенос `access_hash` между аккаунтами, добавлены checkpoint/resume, комментарии каналов, SQLite-модель пользователей, история инвайтов, безопасное хранение runtime-файлов и модульная архитектура.

> Инструмент работает только с данными и действиями, которые Telegram API разрешает конкретному аккаунту. Он не получает «скрытый полный список подписчиков» broadcast-канала и не обходит FloodWait, privacy restrictions или другие ограничения Telegram.

## Возможности

### Парсинг

Поддерживаются три режима:

1. **Видимый список участников**
   - используется `iter_participants()`;
   - подходит для групп/супергрупп, где аккаунту доступен список участников;
   - результат сразу сохраняется в SQLite.

2. **Активные авторы сообщений**
   - используется история сообщений;
   - подходит, когда полный список участников недоступен;
   - поддерживает checkpoint и продолжение с последнего message ID.

3. **Авторы комментариев broadcast-канала**
   - обрабатываются ответы/комментарии к постам;
   - проверяется наличие связанной discussion-группы;
   - сохраняется `source_type = channel_comments`;
   - это активная аудитория комментариев, а не полный список подписчиков канала.

### Фильтры пользователей

Перед парсингом можно настроить:

- исключение ботов — включено;
- исключение deleted — включено;
- исключение scam/fake — включено;
- обязательный username — по выбору;
- обязательное фото — по выбору;
- активность — не учитывать / 7 / 30 / 90 дней.

Пользователь без username может сохраняться по `user_id`, если username не требуется фильтром.

### Хранение пользователей

Основное хранилище — `invite_ledger.db`.

Пользователи дедуплицируются по Telegram `user_id`. Хранятся:

- `user_id`;
- `username`;
- `first_name`;
- `last_name`;
- источник;
- тип источника;
- время первого и последнего обнаружения.

`usernames.txt` и `userids.txt` больше не являются основной базой. Они используются только как совместимый экспорт для старого workflow.

### Инвайтинг

Поддерживаются:

- одна Telegram-сессия;
- несколько Telegram-сессий;
- отдельный entity resolution для каждого аккаунта;
- retry текущего пользователя через другую доступную сессию;
- per-session FloodWait;
- PeerFlood freeze;
- ограничения попыток на пользователя;
- ограничения попыток одной сессии за запуск;
- лимиты на час/сутки;
- planned rotation;
- ночной режим;
- preflight каждой сессии;
- восстановление ранее неавторизованной сессии после повторного login.

FloodWait всегда сохраняется и соблюдается для конкретной сессии. Ротация не сбрасывает и не сокращает server-side таймер Telegram.

## Требования

- Python 3.11+
- Telegram account
- Telegram API ID и API Hash
- Windows 10/11, Linux/VPS или macOS

CI проекта проверяет Python:

- 3.11
- 3.12
- 3.13

## Установка

Клонировать репозиторий и установить зависимости:

```bash
git clone https://github.com/CheBuRaShKa82/telegram-parser-v2.3-RU-Logs.git
cd telegram-parser-v2.3-RU-Logs

python -m pip install -r requirements.txt
```

На Linux обычно:

```bash
python3 -m pip install -r requirements.txt
```

Запуск:

```bash
python main.py
```

или:

```bash
python3 main.py
```

## Получение API ID / API Hash

1. Открыть `my.telegram.org`.
2. Войти под своим Telegram-аккаунтом.
3. Открыть **API development tools**.
4. Создать приложение.
5. Сохранить `api_id` и `api_hash`.
6. В программе открыть **Настройки** и указать их.

API Hash не выводится полностью в меню.

## Конфигурация

Начиная с v2.4 канонический конфиг:

```text
config.json
```

Пример структуры:

```json
{
  "api_id": 123456,
  "api_hash": "your_api_hash",
  "parse_user_id": true,
  "parse_username": true
}
```

`config.json` находится в `.gitignore`.

### Миграция с v2.3

Если при первом запуске существует старый:

```text
options.txt
```

и ещё нет `config.json`, приложение автоматически:

1. читает старые четыре значения;
2. создаёт `config.json`;
3. переименовывает старый файл в:

```text
options.txt.migrated
```

На Linux конфигу назначаются права `0600`. Если `config.json` повреждён или содержит некорректный JSON-root, исходный файл сначала переносится в `config.json.broken-<timestamp>`, и только после этого создаётся конфигурация по умолчанию.

## Telegram-сессии

Все session-файлы находятся в:

```text
sessoins/
```

Название папки сохранено для совместимости с исходным проектом.

Новые файлы называются примерно так:

```text
account_20261002_035500.session
```

Номер телефона в имени файла не используется.

На Linux:

- каталог `sessoins/` получает `0700`;
- `.session` файлы получают `0600`.

Session-файлы нельзя публиковать или коммитить в Git — их утечка может дать доступ к Telegram-аккаунту.

## Главное меню

```text
=== TELEGRAM PARSER / INVITER v2.4 ===

1 - Настройки
2 - Парсинг видимых участников
3 - Парсинг активных авторов сообщений
4 - Парсинг авторов комментариев канала
5 - Экспорт SQLite → CSV / JSON / TXT
6 - Инвайт из базы пользователей
7 - Выход
```

## Checkpoint / Resume

Таблица `parser_checkpoints` хранит состояние парсинга.

Для сообщений и комментариев сохраняется cursor ID. Если процесс оборвался, при следующем запуске можно продолжить с последнего checkpoint.

Для списка участников Telegram high-level iterator не предоставляет универсальный переносимый offset. Поэтому при повторном запуске список безопасно перечитывается, а SQLite-дедуп не создаёт повторных пользователей.

Результат сохраняется батчами, поэтому уже обработанные данные не теряются при сетевой ошибке.

## SQLite

Основная база:

```text
invite_ledger.db
```

Ключевые таблицы:

- `users` — канонические пользователи;
- `user_sources` — источники пользователей;
- `parser_checkpoints` — состояние parser resume;
- `user_exclusions` — scoped-исключения;
- `invite_state` — текущее состояние user × target;
- `invite_events` — append-only история всех попыток;
- `session_stats` — состояние и лимиты Telegram-сессий;
- `invites` — legacy snapshot для обратной совместимости.

### Почему нет общего access_hash

Telegram `access_hash` зависит от аккаунта/session. Поэтому v2.4 не переносит `InputPeerUser(user_id, access_hash)` между разными Telegram-аккаунтами.

Каждая сессия самостоятельно резолвит пользователя и target перед действием. ID-кэш проверяется раньше username lookup; FloodWait из resolve-пути не скрывается и ставит конкретную session на паузу. Для ID-only пользователей сохраняется `preferred_session` — сессия, которая ранее видела этого пользователя.

## Экспорт

Пункт меню **Экспорт** создаёт каталог:

```text
exports/
```

Форматы:

- CSV;
- JSON;
- TXT.

CSV записывается как UTF-8 with BOM, чтобы его было удобно открывать в Excel. Текстовые значения, начинающиеся с формульных префиксов Excel/LibreOffice, экранируются. На POSIX каталог `exports/` создаётся с `0700`, а файлы CSV/JSON/TXT — с `0600`.

## Invite history

В v2.4 история не перезаписывается.

`invite_state` содержит последнее состояние пользователя для target.

`invite_events` сохраняет каждую попытку отдельно:

- timestamp;
- user;
- target;
- session;
- status;
- reason/error;
- FloodWait seconds при наличии.

Это позволяет понять полный ход обработки, а не только последний результат.

## Scoped exclusions

Исключения больше не являются одним глобальным чёрным списком.

Поддерживаются scopes:

- global;
- target;
- session;
- target + session.

Например, ошибка конкретного аккаунта в конкретной группе не блокирует пользователя для остальных групп и аккаунтов.

## Логи

Файл:

```text
app.log
```

Используется `RotatingFileHandler`:

- максимум примерно 2 MiB на файл;
- до 5 backup-файлов.

Уровни:

- INFO;
- WARNING;
- ERROR.

API Hash, auth-коды, 2FA-пароли и содержимое session-файлов логироваться не должны. В рабочем inviter-пути идентификаторы пользователей в логах псевдонимизируются стабильным `user#...` вместо raw `user_id/@username`. На POSIX `app.log` и его новые rotated-файлы создаются с `0600`. Файл лога создаётся только при первой реальной записи, а не при импорте модуля.

## Структура проекта

```text
.
├── main.py              CLI / сценарии
├── config_store.py      config.json + миграция options.txt
├── config_ui.py         интерактивные настройки
├── sessions.py          работа с .session
├── parser.py            фильтры, parser, checkpoint, comments, export
├── inviter.py           inviter, scheduler, ledger, preflight
├── storage.py           SQLite schema / persistence
├── logging_setup.py     rotating logs
├── defunc.py            compatibility facade для старых импортов
├── tests/
│   ├── test_stage_b.py
│   ├── test_stage_c.py
│   ├── test_stage_d.py
│   └── test_audit_regressions.py
├── docs/
│   └── TZ-v2.4.md
├── requirements.txt
└── .github/workflows/
    └── v2.4-smoke.yml
```

Новый код не должен добавляться обратно в `defunc.py`. Этот файл оставлен только для обратной совместимости.

## Тесты

Локально:

```bash
python -m unittest discover -s tests -v
```

Проверка синтаксиса:

```bash
python -m py_compile \
  main.py defunc.py config_store.py config_ui.py \
  logging_setup.py sessions.py storage.py parser.py inviter.py
```

GitHub Actions автоматически выполняет:

- установку зависимостей;
- `pip-audit` по `requirements.txt`;
- fatal-checks через ruff;
- compile check;
- unit/regression tests;
- SQLite smoke test;

на Python 3.11, 3.12 и 3.13. Workflow запускается для `main`, `fix/v2.4-audit` и pull requests.

## Runtime-файлы

В Git не должны попадать:

```text
config.json*
options.txt*
sessoins/
*.session
invite_ledger.db*
app.log*
usernames.txt
userids.txt
exports/
*.bak-*
```

Это уже настроено в `.gitignore`.


## Локальные персональные данные

SQLite, exports и legacy TXT могут содержать Telegram ID, usernames и имена третьих лиц. Используйте их только при наличии законного основания и удаляйте после окончания задачи в соответствии с применимыми правилами хранения данных.

Для полной ручной очистки пользовательских данных при остановленном приложении можно удалить:

```text
invite_ledger.db*
app.log*
exports/
usernames.txt
userids.txt
```

`config.json` и каталог `sessoins/` в этот список не входят: они относятся к вашей конфигурации и авторизованным Telegram-сессиям.

## Ограничения Telegram

Важно понимать:

- полный список участников доступен не во всех чатах;
- обычный подписчик broadcast-канала не получает через API полный список подписчиков;
- режим сообщений собирает видимых активных авторов;
- режим комментариев собирает видимых авторов комментариев;
- privacy restrictions пользователя нужно уважать;
- FloodWait и PeerFlood — ограничения Telegram, а не ошибки, которые нужно «обходить»;
- аккаунту могут потребоваться соответствующие права в целевой группе.

## Обновление с v2.3

Рекомендуемый порядок:

1. сделать backup старой папки;
2. сохранить свои `.session`;
3. перейти на v2.4;
4. установить зависимости из нового `requirements.txt`;
5. запустить программу;
6. проверить автоматическую миграцию `options.txt → config.json`;
7. проверить аккаунты через меню;
8. сначала протестировать парсинг на небольшой группе;
9. затем проверить preflight перед multi-account invite.

Старая SQLite база обновляется автоматически через `CREATE TABLE IF NOT EXISTS` / schema migration и не требует ручного удаления.

## Разработка

Техническое задание v2.4 находится в:

```text
docs/TZ-v2.4.md
```

Основной принцип архитектуры v2.4:

```text
Telegram
   ↓
parser.py
   ↓
storage.py / SQLite
   ↓
inviter.py
   ↓
Telegram target
```

Конфигурация, логирование и session-management вынесены в отдельные модули и не смешиваются с parser/inviter logic.
