# AzurPilot Game MCP Read/Control Plane

Game MCP — отдельная stateless read/control поверхность для игровых клиентов. Она не
является режимом Dev MCP и не заменяет переходный `mcp_server_sse.py`.

## Точки входа

Локальный transport — stateless stdio:

```text
uv run --locked --no-sync python -m module.game_mcp
```

Remote transport — authenticated Streamable HTTP через loopback backend:

```text
uv run --locked --no-sync python -m module.game_mcp.remote serve
```

Backend слушает только `127.0.0.1:8766`. Внешний HTTPS reverse proxy должен
предоставлять путь `/mcp`, передавать только разрешённые Origin и не
добавлять credentials в URL. Remote resource и OAuth scope принадлежат только
Game MCP:

```text
AZURPILOT_GAME_MCP_PUBLIC_URL
AZURPILOT_GAME_MCP_OAUTH_ISSUER
AZURPILOT_GAME_MCP_OAUTH_AUDIENCE
AZURPILOT_GAME_MCP_OAUTH_JWKS_URL
AZURPILOT_GAME_MCP_OAUTH_SUBJECT
AZURPILOT_GAME_MCP_ALLOWED_ORIGINS
azurpilot:game.read
azurpilot:game.control
```

Долгоживущие authenticated `GET /mcp` streams используют отдельный bounded
limiter и не занимают capacity для обычных `POST /mcp` запросов.

Эти значения не имеют fallback к Dev MCP и не должны содержать секреты в
репозитории. Remote verifier проверяет RS256, issuer, audience, subject,
expiry и один из scopes resource policy. Если provider дополнительно возвращает JWT claim
`resource`, он должен совпадать с настроенным public URL; отсутствие этого
нестандартного claim допустимо при корректной проверке `aud`.

## Публичная модель

Каждый target-dependent запрос получает канонический `profile` в собственных
аргументах. Сервер не хранит выбранный профиль, не меняет target между
запросами и не кэширует профильные данные под глобальным ключом.
`game_list_profiles` использует существующий canonical instance owner и
возвращает только безопасные идентификаторы. Локальный screenshot path читает
существующий framebuffer только через прямой пассивный ADB primitive
`exec-out screencap -p`: он не создаёт `Device`, не запускает emulator,
benchmark или night-commission handling и не выполняет input/config writes.
При отсутствии однозначного готового ADB target чтение завершается fail-closed.
Только read-only aliases эмулятора могут кратко кэшироваться внутри одного
адаптера; свежий `adb devices` выполняется для каждого screenshot-запроса.

Контракт и инструменты регистрируются в `module.game_mcp.server`. Текущий
read catalog включает contract, profiles, profile status, resources, current
task, scheduler queue, task catalog/help, Fleet State, morale, redacted config,
bounded logs и validated screenshot. Отдельный control catalog включает
`game_start_profile`, `game_stop_profile`, `game_trigger_task`,
`game_clear_scheduler_queue`, `game_update_config`,
`game_restart_emulator`, `game_restart_runtime`, `game_login_runtime` и
`game_restart_adb`.
Стабильный contract находится в
`module.game_mcp.contract`; application DTO и bounded serialization собираются
в `module.game_mcp.adapter`. Добавление control catalog и отдельной control
authorization policy совместимо с текущим read contract, поэтому
`contract_schema_version` и `game_mcp_api_version` остаются равными `1`.

## Application и persistence

Composition root использует `GameReadService`, `InstanceQueryService`,
`TaskCatalogService`, `FleetStateReadService` и `MoraleService`. Legacy sources
подключаются только при явном создании backend. Persistence builder импортируется
лениво из `module.persistence.runtime` и использует отдельную lazy read-only
composition: marker не мигрируется, environment не изменяется, production
provider не устанавливается, а `FleetStateReadService` разрешает только уже
известный DB instance alias. `GameMcpBackend.dispose()` освобождает engine и
закрывает backend: после этого lazy persistence services не создаются заново.

Fleet State сохраняет observation/run provenance, timestamp, completeness,
slot identity и unknown/ambiguous state. Morale сохраняет `EXACT`, `PROJECTED`
или `UNKNOWN`, recovery и Dorm provenance. Отсутствующие данные не превращаются
в fake baseline, пустой корабль или успешный scan.

## Границы безопасности

Локальный stdio и remote Streamable HTTP используют stateless
self-describing request semantics MCP `2026-07-28`; legacy initialize flow
сохраняется только как совместимость SDK. Cache hints для инструментов явно
не задаются: MCP SDK `2.1.1` по умолчанию использует `ttlMs=0` и
`cacheScope=private`, что сохраняет актуальность и изоляцию профильных данных.

Read-инструменты имеют read-only annotations, а control-инструменты публикуют
честные mutation/destructive/idempotency hints. Все инструменты используют
строгие схемы с `additionalProperties: false`. Профили, задачи и fleet
selection ограничены по типу и размеру. Config mutation принимает только
`profile/task/group/argument/value`, сверяется с generated metadata, запрещает
sensitive arguments и после записи перечитывает authoritative value. Config
read проходит generated redaction и дополнительную defense-in-depth
sanitization. Logs ограничены числом строк и размером,
удаляют ANSI, traceback, paths и secret-like значения. Screenshots разрешают
только PNG/JPEG с проверенными MIME, размером, пикселями и dimensions; наружу
отдаётся native MCP image content, а не путь к файлу.

Каждая mutation требует `azurpilot:game.control`; read-инструменты требуют
`azurpilot:game.read`. Scope проверяется до создания control backend и до
side effect, а stdio остаётся локальной authority без OAuth. Control-операции
используют явный canonical `profile` и общую для Game MCP и legacy
application-level file lock в `config/state/game-control`; bounded
per-profile serialization не оставляет stale ownership после завершения
процесса. Автоматических transport retries нет. Если профиль занят другой
control-операцией дольше допустимого ожидания, операция не передаётся backend и
возвращает `GAME_RESOURCE_BUSY`. Lifecycle публикует только подтверждённые
`STARTED/STOPPED/ALREADY_*`, scheduler и config — только после authoritative
postcondition readback. Scheduler ограничен generated registry; произвольные
shell/module/function/natural-language actions отсутствуют.
Каталог `tools/list` намеренно общий для read и control: read-only token видит
описание control tools, но сервер повторно проверяет required scope на
фактическом `tools/call` и отклоняет mutation до backend acquisition.

`game_get_contract` возвращает `tool_count` и `tool_catalog_sha256`. Fingerprint
строится только по каноническим именам tools: имена сортируются лексикографически,
соединяются переводом строки без завершающего перевода строки, кодируются UTF-8 и
хешируются SHA-256. В `details.request_context` backend сообщает только
эффективный transport, факт аутентификации, local authority, разрешённые Game
scopes и effective read/control access; token, JWT claims, subject, headers и
provider internals наружу не передаются.

ADB restart допускается только после fresh inventory и доказанной
instance/serial ownership; `adb kill-server` сериализуется стабильным
пользовательским host lock, identity которого строится по ADB server endpoint,
а не по checkout. На POSIX host runtime root создаётся с режимом `0700`,
принадлежит текущему пользователю и не может проходить через symlink/junction.
Post-restart inventory bounded-поллингом должен подтвердить
тот же ready target. Пассивный screenshot кратко захватывает lock того же
endpoint, чтобы `adb devices` и `screencap` не пересекались с
`adb kill-server`; control path он по-прежнему не вызывает. DB
diagnostics, SQL,
произвольная файловая система, DevSession, Smoke, Evidence и Git state в Game
surface отсутствуют. `module.game_mcp` не импортирует Dev MCP или Dev Runtime;
общий authenticated HTTP код находится в нейтральном `module.mcp_shared`.
Emulator restart использует existing recovery Platform owner. После bounded
ожидания штатной остановки живой target повторно проверяется по актуальной
exact MuMu instance identity перед вызовом platform-specific instance-scoped
force-stop primitive. Результат force-stop проверяется тем же authoritative
stopped postcondition: ошибка команды не перекрывает уже подтверждённое
состояние stopped, но неподтверждённое или неоднозначное состояние запрещает
launch. Только после подтверждённого stopped owner вызывает `launch_player` и
ждёт running postcondition. Это одна bounded lifecycle transition, а не
повторный запрос Game MCP restart; generic kill по имени процесса или PID не
входит в Game MCP surface. Для платформ без доказуемой instance-safe проверки
операция завершается без mutation success.
`game_restart_emulator` на этом заканчивает свою transition: он не запускает
игровой процесс. Для составного сценария существует отдельный
`game_restart_runtime`: после той же подтверждённой emulator transition он
проверяет готовность exact ADB target, при необходимости один раз вызывает
штатный `AppControl.app_start_adb()` с package из конфигурации профиля и
bounded-поллингом подтверждает, что настроенное приложение running и
foreground. Этот action не вызывает `Device`, `LoginHandler`, scheduler или
profile start; отсутствие ADB ownership, настроенного package или foreground
postcondition завершается fail-closed. Ошибка emulator-фазы и ошибка запуска
игры используют существующие коды, но различаются полем `details.phase`.
Отдельный recovery scope не вводится: emulator/ADB остаются частью
`azurpilot:game.control`, а ownership и postcondition checks fail-closed
ограничивают recovery mutation тем же control boundary.

`game_login_runtime` — отдельная bounded mutation для уже запущенного launch/login
экрана. Контрактный аудит legacy lifecycle показывает: `AzurLaneAutoScript.start()`
вызывает `LoginHandler.app_start()`, `restart()` дополнительно управляет scheduler
timing через `delay_due_restart()`/`delay_next_restart()`, а `goto_main()` при уже
запущенном приложении сразу вызывает `UI.ui_goto_main()` и не проходит
`LoginHandler`. Поэтому Game MCP не запускает `AzurLaneAutoScript`, scheduler или
обычный task loop: application adapter создаёт профильный `AzurLaneConfig` без
task, использует существующий `Device`, выбирает `LoginHandler.handle_app_login()`
для уже foreground игры либо `LoginHandler.app_start()` для запуска приложения,
затем делает свежий screenshot и проверяет authoritative `UI.is_in_main()`.
Пакет, server и account берутся существующими config/UI abstractions; в action
нет их hardcode. Только после этой UI-проверки и повторного exact ADB readback
`game_running`/`game_foreground` результат получает `GAME_RUNTIME_LOGIN_CONFIRMED`.
Login flow bounded по одному timeout и не имеет automatic retry; отсутствие
главного экрана или ADB postcondition возвращает fail-closed ошибку с
`details.phase=login`.

## Legacy parity

Переходный `mcp_server_sse.py` остаётся для совместимых старых клиентов,
документации и regression tests. Его обработчики используют тот же
application boundary, но старый SSE transport не является backend для нового
Game MCP:

| Legacy tool | Game MCP tool |
| --- | --- |
| `list_instances` | `game_list_profiles` |
| `get_status` | `game_get_profile_status` |
| `list_tasks` | `game_list_tasks` |
| `get_task_help` | `game_get_task_help` |
| `get_resources` | `game_get_resources` |
| `get_config` | `game_get_config` |
| `update_config` | `game_update_config` |
| `get_recent_logs` | `game_get_recent_logs` |
| `start_instance` | `game_start_profile` |
| `stop_instance` | `game_stop_profile` |
| `get_screenshot` | `game_get_screenshot` |
| `get_current_running_task` | `game_get_current_task` |
| `get_scheduler_queue` | `game_get_scheduler_queue` |
| `trigger_task` | `game_trigger_task` |
| `clear_scheduler_queue` | `game_clear_scheduler_queue` |
| `restart_emulator` | `game_restart_emulator` |
| `restart_adb` | `game_restart_adb` |

`game_restart_runtime` намеренно остаётся новой standalone Game MCP
capability: legacy SSE не получает отдельный composite mutation без отдельного
legacy contract и acceptance. `game_login_runtime` имеет ту же standalone
границу и также не добавляется в legacy SSE.

Снятие legacy entrypoint отложено: для него пока существуют startup/client
совместимость и contract tests. Удаление возможно только после отдельного
доказательства отсутствия deployment и client зависимости.

Для отсутствующего профиля, неработающего профиля, недоступной capability,
неизвестных данных и service failure используются разные безопасные коды.
`UNKNOWN` для domain state — валидный результат чтения, а не подмена ошибки
нулевым значением.

Отдельный Game client skill не добавляется: текущего server-side MCP surface
достаточно, а `azurpilot-development` остаётся developer-only и не получает
Game/Dev объединённый контракт.
