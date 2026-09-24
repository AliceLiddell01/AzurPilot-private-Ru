# Архитектура выполнения

## Главный процесс задач

Типовой поток:

```text
alas.py
  → загрузка конфигурации экземпляра
  → инициализация Device
  → выбор следующей задачи планировщиком
  → ленивый импорт обработчика
  → выполнение run/handler
  → сохранение результата и NextRun
  → следующая задача или ожидание
```

Перед изменением диспетчеризации проверить:

- как задача называется в конфигурации;
- где команда связывается с методом;
- какой тип результата ожидает вызывающий код;
- какие исключения считаются нормальным окончанием, recoverable-сбоем или ручным takeover;
- когда перечитывается конфигурация;
- кто владеет перезапуском приложения или эмулятора.

## Модель ModuleBase

Большинство игровых классов получают через базовые слои:

- конфигурацию;
- устройство и текущий screenshot;
- методы `appear`, `match`, `click` и OCR;
- общий logger;
- фоновые операции и таймеры.

Из-за mixin-архитектуры реализация метода может находиться не в классе верхнего уровня. Перед рефакторингом проверить MRO, импорты и все override.

## Цикл состояния

Надёжный обработчик повторяет:

1. получить новый screenshot, кроме явно обоснованного первого прохода;
2. проверить условия завершения;
3. обработать общие или приоритетные состояния;
4. выполнить не более одного меняющего экран действия;
5. начать новый проход.

Почему это важно:

- задержки эмулятора непостоянны;
- клики могут не сработать;
- может появиться диалог или переходный кадр;
- фиксированный `sleep` не подтверждает состояние;
- защита от повторных кликов основана на последовательности операций.

## Общие обработчики

`module/handler/` может обрабатывать login, информационные окна, auto-search, enemy searching, fast-forward и другие состояния. При «необъяснимом» переходе проверь, не срабатывает ли общий handler до целевого кода.

## Bot Runtime и WebUI

Bot Runtime — нейтральный headless owner worker-профилей, worker registry,
runtime state и локального typed control plane. WebUI — отдельный клиент: он
читает состояние и запрашивает lifecycle-операции через `BotRuntimeClient`, но
не создаёт workers и не владеет registry.

```text
azur bot start|stop|status
  → BotRuntimeService
  → headless module/bot_runtime.py
  → BotRuntimeOwner
  → workers и канонические runtime state/registry

azur webui start|stop|status
  → azurpilot.tooling.lifecycle
  → gui.py / ASGI и PyWebIO
  → BotRuntimeClient → Bot Runtime
```

`azur start` и `azur stop` сохранены как deprecated aliases для прежнего
WebUI lifecycle; новый операторский текст использует `azur webui ...`.
Остановка WebUI освобождает только его UI-ресурсы. Она не останавливает Bot
Runtime и workers. Game MCP, Dev MCP, Smoke и CLI могут bootstrap-ить Bot Runtime
и работать без WebUI; их проверки не требуют свободного WebUI-порта.

При изменении lifecycle проверять:

- Windows spawn-семантику отдельно для headless owner и WebUI;
- точные identity и ownership проверки для owner и worker-процессов;
- cleanup WebUI без остановки Bot Runtime;
- штатный STOP_RUNTIME, handover, idempotency и fail-closed recovery;
- независимость Game MCP, Dev MCP, Smoke и CLI от WebUI;
- совместимость с typed `azurpilot.tooling.lifecycle`.

Windows WebUI lifecycle пользовательской установки принадлежит
`azurpilot.tooling.lifecycle`: `azur webui start` запускает подготовленный
`gui.py`, `azur webui stop` останавливает только точно принадлежащий checkout
процесс WebUI. `azur bot start|stop|status` управляет отдельным headless owner.
Foreground WebUI Start, который сам создал backend, сохраняет управление через
`Ctrl+C`; один лишь занятый порт не доказывает ownership.

## Dev Runtime Foundation

Локальный developer runtime живёт в импортируемом пакете `module.dev_runtime` и
работает с target из канонического registry, loopback `127.0.0.1` и отдельным
портом `25549`. Target хранится в repository-scoped marker под `config/state/`,
проверяется структурным profile discovery; при отсутствии marker read-only
разрешается default из tracked target policy (`ap` после проверки профиля).
Переключение target требует явного согласия пользователя, а публичный lifecycle
API не принимает произвольный профиль. Adapter перепривязывает новые вызовы к
текущему registry target, а уже принятая control operation сохраняет immutable
target identity и fingerprint критической конфигурации; mismatch не может
молча перенаправить мутацию на другой профиль и завершается fail-closed.

Обычный Dev Runtime использует `BotRuntimeFacade` и штатный
`BotRuntimeBootstrapper`: headless `module/bot_runtime.py` исполняет lifecycle,
а DevSession привязывает операцию к текущему canonical target. Preflight
требует уже подготовленное окружение: pending dependency-sync marker блокирует
старт, поэтому Dev Runtime сам не запускает `uv sync`, upgrade или repair.
Готовность подтверждается по точной identity Bot Runtime owner, worker registry,
worker process и свежему runtime state с совпадающими `session_id` и identity
worker. Порт WebUI и HTTP readiness в этом пути не используются. Сохранённый
`standalone_process` режим обслуживает legacy state и не является обычным
production lifecycle.

DevSession хранит repository-scoped marker и lock под `config/state/`. Marker
также сохраняет назначенный profile сессии: уже запущенный процесс и его Evidence
не перепривязываются к новому target marker до завершения старой сессии. Ownership
процесса включает PID, время создания, executable, command line и cwd; PID или
занятый порт сами по себе не дают права на остановку. `stop`/`recover` работают
fail-closed и не завершают процесс при неоднозначном владении. `status` и
`doctor` не мигрируют worker registry и не создают его lock-файлы. Повреждённый
или stale marker классифицируется отдельно; повторный старт разрешён только
после безопасного доказанного восстановления. Создание DevSession, SmokeRun и
control operation сериализуется общей repository-scoped coordination lock, а
каждый собственный marker служит durable reservation до завершения владельца.

Этот слой остаётся основой Dev MCP и не меняет жизненный цикл игрового
планировщика и рабочих задач.
Dev Runtime хранит подтверждающие данные текущей сессии в отдельном
`module.dev_runtime.evidence`: игнорируемые артефакты живут под
`config/state/dev-runtime-runs/<session-id>/`, используют атомарные метаданные,
межпроцессную блокировку, ограниченное хранение и типизированное состояние. Снимок Git,
каноническая хронология, происхождение задач и зависимостей, bounded observability-
координаты для поиска application logs через Grafana MCP, структурированные ошибки
и явный запрос снимка принадлежат точной рабочей копии, `session_id` и настроенному
development target. Dev MCP не читает локальные application logs и не является
Grafana proxy.

## MCP

MCP не должен становиться обходом конфигурационных и безопасностных границ. Для каждого инструмента проверить:

- schema входа;
- валидацию имени экземпляра и параметров;
- side effects;
- сериализацию ответа;
- обработку ошибок;
- доступ к screenshot, логам и пользовательским данным;
- отключаемость интеграции.

`module/dev_mcp` — отдельный stdio-адаптер только для разработки поверх
`DevSessionManager` и `RuntimeControlManager`. Он использует только target,
разрешённый registry (включая policy default при отсутствии marker), создаёт
менеджер лениво и не связан с Game MCP. При смене marker
создаётся новый manager только для новых операций; старые DevSession/Evidence
разрешают записанный profile. Запуск не должен
читать профиль или запускать runtime; схема и безопасная сериализация остаются
границей адаптера, а владение, политика задач и очистка принадлежат
`DevSessionManager`; runtime control владеет отдельными persistent operations.
Диагностические инструменты вызывают API менеджера для
подтверждающих данных, хронологии, ограниченного журнала сессии и явного снимка экрана.
Обработчик MCP не читает артефакты, не запускает Git и не создаёт второй
`Device`; рабочий процесс снимка экрана обслуживает только явный запрос текущим кадром
уже существующего runtime. Обычный рабочий процесс без проверенной активной DevSession не
создаёт подтверждающие данные.

MCP server использует официальную low-level API установленной стабильной MCP
SDK v2 для общей регистрации tools в stdio и Streamable HTTP. Один и тот же
adapter обслуживает modern protocol `2026-07-28` и legacy negotiation; native
MCP Tasks не эмулируются. `SmokeRun` и `DevRuntimeControlOperation` остаются
application-level persistent entities.

First-party Dev/Game service layer публикует одну transport-neutral compatibility
model для direct stdio и authenticated loopback HTTP. Единственный canonical
bundle находится в `config/mcp-versions.toml` и содержит идентификаторы сервера,
API и контракта, отпечатки инструментов и возможностей, контрольные суммы наборов исходников,
версию плагина и редакцию набора навыков. `azur mcp sync --base <exact-base-sha>`
согласует frozen candidate, generated bundle, owned runtime и fresh-client acceptance
одним вызовом; `NO_CHANGES` — terminal no-op. `status`, `versions`, `impact`,
`reconcile`, `start`, `stop`, `restart` и `accept` остаются диагностическими или
admin-capabilities. Состояние внешней Codex session не является postcondition
sync, hot reload не предполагается. Команда `azur app state <state-id> --profile <profile>`
читает зарегистрированное состояние приложения без запуска WebUI. Согласование
среды выполнения не редактирует отслеживаемые исходники, а устаревшее состояние плагина или сессии
классифицируется как `RELOAD_REQUIRED`. Наборы исходников задаются явной
ограниченной картой фактических вызовов приложения и хранилищ данных; средства
управления MCP/Git/репозиторием не включаются в идентификатор серверной части.
Постоянный compatibility gate отдельно проверяет целостность текущего дерева и
base-to-head policy по переданному exact base SHA.

Текущий development-контур предоставляет developer-only односторонний Game
Bridge и диагностику базы данных. Game Bridge вызывает только нейтральные
типизированные application services: `GameReadService` и persistence-backed
morale projection; Dev MCP, Smoke, Evidence и DB diagnostics остаются
developer-only. Каждый snapshot имеет неизменяемую target/session/checkpoint
provenance, ограниченный payload и checksum. Smoke Harness сохраняет `before`,
`final` и объявленные промежуточные checkpoints в изолированном sidecar, а
unknown/unavailable/missing required snapshot не может дать `PASS`.
Для обычного ограниченного Smoke операция `dev_run_smoke` синхронно владеет
запуском, ожиданием, automatic triggered checkpoints, очисткой и конечным
типизированным результатом. Ручной checkpoint tool не входит в основной Dev MCP
catalog; длительный или визуальный сценарий запускается отдельным публичным
`dev_start_smoke`, возвращающим `DEV_SMOKE_STARTED`; его состояние читается через
`dev_get_smoke`, а visual evaluation — через отдельные evaluation tools.
Приложение может публиковать ограниченные
структурированные подтверждения, связанные с `session` и `Smoke`; конечный
результат отдельно хранит исход выполнения приложения, полноту подтверждений,
состояние Smoke и вмешательство оператора.

Standalone Game MCP находится в `module.game_mcp` и использует тот же
нейтральный application/domain слой через собственную lazy composition root.
Он работает через stateless stdio и authenticated Streamable HTTP. Для Codex
Desktop предусмотрен отдельный strict-loopback local HTTP supervisor с
`transport=local_http`, `local_authority=true` и user-level bearer token;
public remote HTTP остаётся `transport=remote_http`, `local_authority=false`.
Сервер принимает
канонический `profile` в каждом target-dependent запросе и не импортирует Dev
MCP или Dev Runtime. Его remote resource и scopes `azurpilot:game.read` и
`azurpilot:game.control` отделены от Dev MCP, а общий transport/auth код
размещён в `module.mcp_shared`. Fleet State и morale читаются без регистрации
профиля, физического scan или скрытого commit; lifecycle, config/scheduler,
emulator/ADB control, DB internals и developer evidence в Game MCP выдаются
только через отдельные bounded control contracts либо не выдаются вовсе.

Диагностика базы данных принадлежит persistence adapter, но наружу выходит
через типизированный `module.application` port и фиксированный bounded
catalog. Она использует отдельный process-local lazy app-role engine/health/UoW
composition для developer diagnostics, не запускает production bootstrap и не
создаёт global provider. Явный lifecycle позволяет dispose диагностического
engine; arbitrary SQL, dump или Alembic mutation по-прежнему запрещены. Repair catalog может быть пустым, если безопасного
зарегистрированного repair нет.

Не фиксировать в документации точное количество инструментов: оно меняется. Источник истины — регистрация tools в текущем коде.

## Основа хранения

Нейтральные DTO и порты хранения принадлежат `module.application`,
PostgreSQL-адаптеры — `module.persistence`. Типы SQLAlchemy/Psycopg не выходят
за инфраструктурную границу. Engine создаётся лениво отдельно в каждом PID
после запуска процесса; импорт пакета не подключается к БД и не выполняет DDL.

Schema изменяется только явной Alembic-командой. Для доменов schema v1 игровые,
WebUI и MCP consumers используют application storage services; только process
composition roots импортируют `module.persistence.runtime`. Обязательный
PostgreSQL marker проверяется fail-closed; SQLite fallback и dual-write
запрещены.

Per-ship Morale Core опирается на append-only Formation Fleet State как на
единственный источник состава. Dorm scanner наблюдает только UI-факты, а
reconciliation связывает их с физическим slot set-based и fail-closed. Exact
Dorm observation хранит baseline/rate/floor; complete двухэтажное отсутствие
хранит `unknown` morale с доказанным outside-Dorm recovery, не fake baseline.
Partial scan, замена occupant, смена формы, stale Fleet State или неоднозначный
slot не переносят состояние. Legacy Combat path к этой границе persistence не подключён.
Canonical marker и другие runtime-state JSON находятся под `config/state/`, а
корневой `config/*.json` является только пространством кандидатов: игровым
профилем считается безопасный regular JSON, прошедший единый structural
classifier `module.config.profile`; произвольный JSON отчёта/состояния профилем не
становится. Runtime state хранится только в `config/state/`.
Локальный `.env` загружается одним владельцем persistence и направляет libpq к
защищённым app/migrator passfiles без постоянного `PGPASSWORD`.

Offline migration pipeline проходит через application-owned порты. Legacy
SQLite/JSON adapters живут только в `module.persistence.legacy`, открывают
source read-only и path-bounded; PostgreSQL target пишет bounded chunks и затем
проверяет import ledger вместе с фактическими domain rows. Этот pipeline не
является runtime backend и не запускается из production entry points. После
cutover он сохраняется только для offline recovery из restricted archive.
