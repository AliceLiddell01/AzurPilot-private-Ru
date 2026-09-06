# WebUI, MCP и инфраструктура

## WebUI

`module/webui/` обычно содержит:

- создание приложения;
- страницы/виджеты;
- конфигурацию deploy;
- управление экземплярами и процессами;
- lifecycle и restart;
- API/streaming endpoints;
- локализацию интерфейса.

При изменении определить, является ли состояние:

- глобальным для WebUI;
- привязанным к конфигурационному экземпляру;
- принадлежащим дочернему процессу;
- сериализуемым через multiprocessing;
- вычисляемым из пользовательского config.

## Процессы

Особенно проверять Windows spawn:

- импортируемость target-функции;
- отсутствие несерилизуемого состояния;
- защиту entry point;
- закрытие process/manager/pipe;
- повторный запуск;
- поведение при падении ребёнка;
- отсутствие orphan processes.

## MCP

MCP-инструменты делятся на read-only и меняющие состояние. Для меняющих инструментов нужна строгая валидация.

Не передавать наружу без необходимости:

- полный локальный config;
- секреты уведомлений;
- пути пользователя;
- необработанные логи с identifiers;
- screenshot с чувствительными данными.

Dev MCP для локальной Codex-интеграции находится в `module/dev_mcp` и работает
через stdio. Для ChatGPT есть отдельный `module.dev_mcp.remote` с HTTPS
Streamable HTTP `/mcp`; оба entrypoint-а используют один тонкий adapter к
существующим `DevSessionManager` и отдельным `RuntimeControlManager` с target,
разрешённым каноническим registry (default policy применяется только при
отсутствии marker). Remote backend
bind-ится только на `127.0.0.1`, требует внешний OAuth/OIDC access token и не
добавляет generic shell/config tools или управление production profiles.
Game MCP и Dev MCP остаются независимыми продуктами и используют нейтральные
общие компоненты `module.mcp_shared` только для authenticated Streamable HTTP.
WebUI не монтирует MCP transport; игровые и development endpoints запускаются
отдельными entrypoint-ами с собственными scope и runtime boundaries.

## Canonical Plugin AzurPilot

`plugins/azurpilot/` — source-controlled package, сгенерированный текущим
Plugin Creator. Его machine-readable ID — `azurpilot`, display name —
`AzurPilot`; текущий пакет публикует три разделённых skill:
`azurpilot-development`, `azurpilot-game-control` и
`azurpilot-troubleshooting`. `.app.json` содержит только references на
существующие приложения `AzurPilot Development Verified` и `AzurPilot Game`.
Пакет не содержит ChatGPT app state, tunnel profile, credentials, screenshots,
archives или runtime cache и не регистрирует второй MCP implementation.

Codex использует project-scoped `azurpilot-dev` через прямой local stdio и
`module.dev_mcp`. ChatGPT использует подключённое приложение с
authenticated public URL `https://<public-host>/mcp`, Caddy reverse proxy в
Docker Compose profile `remote-ingress` и внешним OAuth/OIDC provider; Caddy
обращается к host-side loopback backend через `host.docker.internal`, custom
authorization server и Secure MCP Tunnel
для этого пути не требуются. `module.dev_mcp.contract` публикует read-only boundary с
версиями API/Smoke schemas, required feature flags, capability families и
result outcomes. Runtime status/control не раскрывают serial, package, пути или
команды и хранят bounded operation state в ignored `config/state/`; control
operation сохраняет target identity и fingerprint критической конфигурации и
fail-closed при их изменении.
Плагин обязан остановиться с `PLUGIN_RUNTIME_INCOMPATIBLE` до mutating calls при
любом несовпадении.

Developer-only capability `Game` публикуется через односторонний bridge,
привязанный к target, к нейтральному `module.application`: `GameReadService` и
persistence-backed morale projection. Dev MCP, Smoke, Evidence и диагностика
базы данных остаются developer-only; обратная зависимость application от Dev
Runtime запрещена. Диагностика базы данных использует фиксированный read-only
catalog поверх отдельного process-local lazy PostgreSQL engine/UoW, собранного
из canonical marker и app passfile без production bootstrap/provider и
`os.environ` mutation; arbitrary SQL, dump, secrets и Alembic mutation не
выдаются. Пустой repair catalog является допустимым честным результатом.

Standalone Game MCP находится в `module.game_mcp` и не является режимом Dev
MCP. Его stateless read/control tools используют canonical `profile` в каждом
target-dependent запросе, нейтральные application services и отдельные
authenticated Game scopes `azurpilot:game.read` и `azurpilot:game.control`.
Общий Streamable HTTP/auth transport code находится в `module.mcp_shared`; Game
MCP не импортирует Dev MCP или Dev Runtime. Lifecycle, config/scheduler
mutation, emulator/ADB control, DB internals, Smoke/Evidence и Git state
остаются отдельными границами, а mutation scope проверяется до side effect.

## Статистика

Статистика schema v1 хранится только в production PostgreSQL через
`module.application`; SQLite доступен только offline migration adapter. CSV
является явным export, а не canonical cache. File-owned config/scheduler/event
state остаётся вне PostgreSQL. При изменении границы выяснить:

- владельца схемы;
- ключ экземпляра/устройства;
- thread safety;
- миграцию старых данных;
- retention;
- формат времени и timezone;
- кто читает данные в WebUI;
- можно ли отключить сбор.

Direct `.db` upload через WebUI запрещён. Storage failure не превращать в
нулевую или пустую статистику. После первого PostgreSQL write допускается только
forward-fix, автоматический rollback на SQLite запрещён.

## Уведомления и внешние API

Проверять:

- отсутствие токенов в log;
- timeout и retry policy;
- отключаемость;
- поведение без сети;
- sanitization payload;
- различие warning и fatal error;
- отсутствие блокировки главного игрового цикла.

## Персональный эксплуатационный контур

Четыре команды имеют разные обязанности:

```text
Start  — запуск подготовленной установки
Update — безопасное fast-forward обновление
Repair — диагностика и транзакционное восстановление
Build  — подготовка уже полученного checkout
```

Изменения в `deploy/`, `.venv`, Python executable, `uv.lock` или этих скриптах относятся к расширенному режиму и требуют проверки сквозного пользовательского пути.
