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
отдельными entrypoint-ами с собственными областями и границами runtime.

Внешние developer integrations не являются ещё одним Dev/Game MCP transport.
Текущий владелец — `azurpilot.integrations`. Точный каталог адаптеров берётся из
`IntegrationName`/`ADAPTER_ORDER`, а не дублируется в этом документе. Критический
путь не должен возвращаться к Docker MCP Gateway/Toolkit или generic proxy.
Credentials и локальная машинная маршрутизация остаются вне tracked source;
граница read-only/mutation проверяется самим adapter и integration contract gate.

## Canonical Plugin AzurPilot

`plugins/azurpilot/` — source-controlled package, сгенерированный текущим
Plugin Creator. Его machine-readable ID — `azurpilot`, display name —
`AzurPilot`; текущий пакет публикует три разделённых skill:
`azurpilot-development`, `azurpilot-game-control` и
`azurpilot-troubleshooting`. Плагин поставляет только skills и metadata: в
пакете отсутствуют `.app.json`, `.mcp.json` и MCP registration source. Пакет не
содержит ChatGPT app state, tunnel profile, credentials, screenshots, archives
или runtime cache и не регистрирует второй MCP implementation.

Standalone Codex CLI использует project-scoped `azurpilot-dev` и
`azurpilot-game` через прямой local stdio и соответственно `module.dev_mcp` и
`module.game_mcp`. Codex Desktop при Windows stdio bootstrap failure использует
отдельные loopback aliases `azurpilot_dev` и `azurpilot_game` с bearer token из
user environment и `transport=local_http`; protocol identities не меняются.
Единственный repository-level источник регистрации — `.codex/config.toml`.
ChatGPT использует
явно выбранное подключённое приложение с
authenticated public URL `https://<public-host>/mcp`, Caddy reverse proxy в
Docker Compose profile `remote-ingress` и внешним OAuth/OIDC provider; Caddy
обращается к host-side loopback backend через `host.docker.internal`, custom
authorization server и Secure MCP Tunnel
для этого пути не требуются. `module.dev_mcp.contract` публикует read-only границу с
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

Notification handover использует существующий process-local PostgreSQL
Engine. Headless Bot Runtime собирает notifier из полной Agent configuration,
поэтому публикация событий и работа dispatcher доступны при выключенном WebUI.
Для состояния `DELIVERED` требуется проверенный durable Agent ACK через
UI-facing WebUI API; при недоступности API bounded ожидание завершается с
отказом закрытого типа. WebUI отдельно обслуживает Agent API и `DesktopAgentClientRuntime`;
его process/restart lifecycle не является владельцем Bot Runtime workers.
`State.init()` подключает
`DesktopAgentNotificationRuntime` только при полной Agent configuration;
`GET /api/notification-agent/stream` является durable profile-scoped SSE
projection, а `POST /api/notification-agent/ack` — отдельной authenticated
mutation. Outbound-only Agent не открывает inbound listener. Queue acceptance,
`PROVIDER_ACCEPTED` и HTTP success не дают `DELIVERED`: это состояние возможно
только после проверенного durable Agent ACK с текущей delivery/lease identity.
При изменении этой границы отдельно проверять Migration
`0010_notification_agent_ack` и `0011_agent_session_identity`, cursor
gap-fill/reconnect, stale ACK rejection, Caddy flush/timeout и bounded handover
waiter.

## Персональный эксплуатационный контур

Четыре команды имеют разные обязанности:

```text
Start  — запуск подготовленной установки
Update — безопасное fast-forward обновление
Repair — диагностика и транзакционное восстановление
Build  — подготовка уже полученного checkout
```

Изменения в `deploy/`, `.venv`, Python executable, `uv.lock` или этих скриптах относятся к расширенному режиму и требуют проверки сквозного пользовательского пути.
