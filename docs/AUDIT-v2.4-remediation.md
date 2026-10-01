# Remediation по внешнему аудиту v2.4

Дата: 2026-10-02  
Ветка: `fix/v2.4-audit`

Этот документ фиксирует результаты повторной проверки замечаний к v2.4 и внесённые исправления.

## Исправлено

### 1. Hot-loop при session-scoped exclusions — исправлено
Сессии, для которых пользователь уже имеет scoped-исключение, фильтруются до выбора кандидата. Если пользователь исключён для всех доступных сессий, он получает terminal skip `excluded_all_sessions`, цикл завершается без spin-loop.

Добавлен regression test.

### 2. Несогласованные target keys — исправлено
Добавлена единая `canonical_target_key()`.

Нормализуются:
- entity;
- portable peer ID;
- numeric ID;
- `@username`;
- публичные `t.me/username` ссылки;
- регистр username.

Entity и результат `target_ref()` дают одинаковый ledger key.

Добавлены regression tests.

### 3. `missing_invitees` — исправлено
Результат `InviteToChannelRequest` проверяется. Непустой `missing_invitees` больше не записывается как `ok`; используется `skip / missing_invitee`.

Добавлен regression test.

### 4. Completed checkpoint использовался как resume — исправлено
Resume cursor применяется только для:
- `running`;
- `interrupted`;
- `error:*`.

Checkpoint со статусом `completed` начинает новый проход с начала.

Добавлены tests для completed и error resume.

### 5. FloodWait в parser — исправлено
Parser возвращён к стандартному short auto-sleep Telethon через `flood_sleep_threshold=60`.

Для более длинного FloodWait:
- ошибка ловится отдельно;
- checkpoint сохраняется;
- parser прекращает новые API-запросы;
- comments-parser больше не проглатывает FloodWait как обычный RPCError и не переходит к следующему посту.

Текущий незавершённый пост комментариев не считается завершённым и будет повторён при resume.

### 6. Потеря parser progress — исправлено
- сообщения checkpoint-ятся независимо от duplicate/filter/empty sender веток;
- Ctrl+C сохраняет checkpoint со статусом `interrupted`;
- participants сохраняют checkpoint при interruption;
- длинные comment threads периодически commit-ятся;
- cursor продвигается только после обработки элемента/поста.

Добавлен KeyboardInterrupt regression test.

### 7. Дублирующий single-session inviter — устранён из CLI
Пользовательский CLI теперь всегда использует `inviting_rotate_sessions()`, даже с одной сессией.

Старая `inviting()` оставлена только как compatibility API.

### 8. Ошибки Telegram классифицируются точнее — исправлено
Добавлено:
- basic Chat → `AddChatUserRequest`;
- Channel/megagroup → `InviteToChannelRequest`;
- `ChannelPrivateError` / `UserBannedInChannelError` выводят только конкретную session;
- `UsersTooMuchError` останавливает target run;
- `AuthKeyUnregisteredError` / deactivated current account выводят session из запуска;
- `InputUserDeactivatedError` терминально исключает удалённого пользователя.

Добавлены classification tests.

### 9. Небезопасные CLI defaults — улучшено
CLI defaults изменены:
- base delay: 5s;
- per-session/hour: 10;
- per-session/day: 30;
- max session attempts/run: 20;
- jitter: 0.5–1.5s.

Отключение часового/суточного лимита требует дополнительного подтверждения.

Это не гарантирует отсутствие Telegram restrictions; реальные лимиты Telegram не публикуются и зависят от аккаунта/контекста.

### 10. Queue model — улучшено
- пользователь может выбрать очередь по parsed source;
- SQLite остаётся канонической базой;
- удалён legacy TXT-prune workflow из пользовательского пути;
- повторные persistent-exclusion skips больше не создают новое `invite_events` событие на каждом запуске;
- добавлена `user_session_seen`;
- ID-only кандидат получает `preferred_session` — аккаунт, который реально видел этого пользователя, пробуется первым.

### 11. SQLite schema — исправлено
- `session_stats` и legacy `invites` перенесены в `storage.ensure_schema()`;
- legacy import выполняется один раз через `schema_meta`;
- `invite_record + legacy snapshot` в inviter теперь одна транзакция;
- schema version поднята;
- `parsed_at` — время парсинга;
- `last_seen_at` — время наблюдения и больше не может откатиться назад;
- БД на POSIX предварительно создаётся с mode `0600`.

Добавлены storage regression tests.

## Security / CI

Исправлено:
- `config.json.tmp` на POSIX создаётся сразу с `0600`;
- API hash вводится через скрытый `getpass`;
- CI запускается для `main`, `fix/v2.4-audit` и pull requests;
- workflow permission: `contents: read`;
- GitHub Actions закреплены на конкретные commit SHA;
- добавлен `pip-audit`;
- добавлен fatal ruff lint;
- compile + unit + SQLite smoke остаются на Python 3.11 / 3.12 / 3.13.

## Дополнительные исправления

- `UserProfilePhotoEmpty` теперь корректно считается отсутствующей фотографией при `require_photo=True`;
- manual source перед parser по возможности резолвится в entity, поэтому checkpoint/source identity совпадает с выбором из dialogs;
- public username target keys приводятся к lowercase.

## Операционные ограничения, которые нельзя подтвердить unit-тестом

Перед merge/release остаётся живой smoke-test с тестовыми Telegram accounts:

1. login/session creation;
2. visible participants;
3. message authors;
4. channel comments;
5. preflight;
6. basic-group invite;
7. megagroup invite;
8. controlled missing-invitee/privacy case;
9. restart after FloodWait/checkpoint.

CI не может проверить реальные права аккаунтов, реальные server-side Telegram limits и состояние конкретных sessions.

## Остающиеся optional improvements

Не являются блокерами текущего remediation:

- reproducible dependency lock с hash-файлом;
- отдельная команда retention/purge для локальных PII/log/export данных;
- полный style lint вместо fatal-only rules;
- platform-specific ACL management на Windows;
- окончательное удаление compatibility `inviting()` и `defunc.py` в следующем major release.
