# Локальная observability-инфраструктура AzurPilot

Эта папка содержит переносимый Docker Compose-контур для приёма OTLP и
локального хранения logs, metrics и traces. Compose project имеет постоянное
имя azurpilot-infrastructure.

## Состав

| Сервис | Назначение | Образ |
| --- | --- | --- |
| alloy | loopback OTLP endpoint и маршрутизация telemetry | grafana/alloy:v1.19.2 |
| postgres | каноническое production-хранилище AzurPilot | postgres:18 |
| postgres-bootstrap | одноразовое создание app/migrator ролей и прав | postgres:18 |
| loki | хранение logs | grafana/loki:3.7.4 |
| prometheus | хранение metrics и remote-write receiver | prom/prometheus:v3.14.0 |
| tempo | хранение traces и OTLP receiver | grafana/tempo:2.10.5 |
| grafana | локальная визуализация подключённых data sources | grafana/grafana:13.2.1 |
| pgadmin | веб-администрирование PostgreSQL | dpage/pgadmin4:9.17 |

В compose.yaml для каждого образа зафиксированы version tag и digest.
Образы являются официальными образами соответствующих проектов.

Alloy принимает OTLP по 127.0.0.1:4317 (gRPC) и 127.0.0.1:4318 (HTTP).
Grafana доступна по 127.0.0.1:3000. Loki, Prometheus и Tempo не публикуются
на host: Alloy и Grafana обращаются к ним через стандартную Compose network и
service DNS.
pgAdmin доступен только по 127.0.0.1:5050.

## Данные и секрет

Состояние хранится в именованных volumes:

- azurpilot-postgres-data;
- azurpilot-observability_alloy-data;
- azurpilot-observability_loki-data;
- azurpilot-observability_prometheus-data;
- azurpilot-observability_tempo-data;
- azurpilot-observability_grafana-data;
- azurpilot-pgadmin-data.

Имена observability volumes намеренно сохранены с прежним префиксом
`azurpilot-observability`: это существующие внешние volumes, и переименование
Compose project не должно создавать второй набор данных или терять накопленное
состояние.

Существующие observability volumes объявлены как `external` с явными
engine-level именами. Это намеренная fail-closed граница миграции: при
отсутствии ресурса Compose остановится вместо того, чтобы молча создать пустой
volume с тем же логическим ключом. PostgreSQL volume имеет явное имя, но
остаётся Compose-managed; Compose создаёт его при запуске подготовленного
target service, а не отдельной ручной командой создания пустого volume.
PgAdmin volume также остаётся Compose-managed: он создаётся автоматически при
первом запуске и хранит configuration database, users и импортированные server
definitions.

Все секреты этого контура хранятся в общем локальном .env, игнорируемом Git,
в корне репозитория. Сейчас используются переменные
AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER и
AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD; новые секреты этой
архитектуры нужно добавлять туда же. Для pgAdmin используются
AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL,
AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_PASSWORD и
AZURPILOT_OBSERVABILITY_PGADMIN_PGPASS. Пароль начального администратора
Grafana и pgAdmin передаётся через Compose secret и не попадает в репозиторий.
Порт pgAdmin задаётся через `AZURPILOT_OBSERVABILITY_PGADMIN_PORT`; по умолчанию
используется `5050`.

Если переменных ещё нет, добавьте их в корневой .env. Для ротации уже
добавленного пароля используйте PowerShell-команду ниже: она сохраняет новое
значение напрямую в .env и ничего не выводит в stdout.

    AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=admin
    AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=<случайный_секрет>
    AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL=admin@azurpilot.dev
    AZURPILOT_OBSERVABILITY_PGADMIN_PORT=5050
    AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_PASSWORD=<случайный_секрет>
    AZURPILOT_OBSERVABILITY_PGADMIN_PGPASS=postgres:5432:*:azurpilot_migrator:<пароль_azurpilot_migrator>

    $envFile = Resolve-Path ..\..\.env
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $password = [Convert]::ToHexString($bytes).ToLowerInvariant()
    $lines = Get-Content -LiteralPath $envFile
    $lines -replace '^AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=.*$', "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=$password" |
        Set-Content -LiteralPath $envFile -Encoding utf8NoBOM
    Remove-Variable password

Для Docker PostgreSQL дополнительно требуется локальный bootstrap secret
`AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD`. Он нужен только Compose для
первичного создания superuser. App и migrator secrets монтируются только в
одноразовый `postgres-bootstrap`, который создаёт или обновляет роли и права;
в долгоживший `postgres` они не попадают. Secret генерируется локально и не
добавляется в Git:

    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $password = [Convert]::ToHexString($bytes).ToLowerInvariant()
    $envFile = Resolve-Path ..\..\.env
    $key = 'AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD'
    $lines = @(Get-Content -LiteralPath $envFile)
    $bootstrapLines = @($lines | Where-Object { $_ -match "^$key=" })
    if ($bootstrapLines.Count -gt 1) { throw "В .env найден дублированный bootstrap key." }
    if ($bootstrapLines.Count -eq 1) {
        $lines = $lines -replace "^$key=.*$", "$key=$password"
    } else {
        $lines += "$key=$password"
    }
    Set-Content -LiteralPath $envFile -Value $lines -Encoding utf8NoBOM
    Remove-Variable password

## Запуск и обслуживание

Из этой папки:

Перед первым запуском или обновлением выполните из корня репозитория
каноническую проверку и миграцию Compose project:

    uv run --locked --no-sync python -m dev_tools.observability_compose_migration --repository-root . inventory
    uv run --locked --no-sync python -m dev_tools.observability_compose_migration --repository-root . migrate

Команда читает Docker labels/state, поэтому отличает fresh install от уже
существующей установки. На существующей установке она требует все пять
ожидаемых observability volumes, останавливает и удаляет только контейнеры и
сеть проекта `azurpilot-observability`, не удаляя volumes, поднимает
`azurpilot-infrastructure` и проверяет health/state и привязку прежних volumes
к Alloy/Grafana/Loki/Prometheus/Tempo. Отсутствующий volume в этом режиме
останавливает миграцию вместо создания пустой замены. На действительно fresh
install создаются только пять явно перечисленных external observability
volumes; PostgreSQL и pgAdmin остаются Compose-managed.

Обычные команды после успешной миграции:

    docker compose --env-file ../../.env config
    docker compose --env-file ../../.env pull
    docker compose --env-file ../../.env up -d
    docker compose --env-file ../../.env ps
    docker compose --env-file ../../.env logs --tail=100 postgres alloy loki prometheus tempo grafana pgadmin

Для штатного старта только базы используйте:

    docker compose --env-file ../../.env up --detach --wait postgres
    docker compose --env-file ../../.env run --rm --no-deps postgres-bootstrap

`postgres-bootstrap` — одноразовый шаг выдачи app/migrator ролей и прав;
повторный запуск идемпотентен.
Владелец lifecycle — Docker Compose/Docker Desktop; Arch WSL2 сохраняется только
как rollback safety и не требует `systemctl start postgresql`.

### pgAdmin

Откройте [http://127.0.0.1:5050](http://127.0.0.1:5050) и войдите под email из
`AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL` и паролем из
`AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_PASSWORD`. По умолчанию email —
`admin@azurpilot.dev`; пароль хранится только в локальном `.env`.

При первом запуске pgAdmin автоматически импортирует сервер
`AzurPilot PostgreSQL` из `pgadmin/servers.json`. Подключение идёт через
Compose DNS `postgres`, а не через опубликованный host-порт PostgreSQL, и
начинает работу со стабильной служебной БД `postgres`. Для данных AzurPilot
выберите в pgAdmin application database из `AZURPILOT_POSTGRES_DATABASE`
(по умолчанию `azurpilot`); подключение использует роль
`azurpilot_migrator` с правами владельца схемы. Пароль роли
передаётся через `PGPASS_FILE` из Compose secret
`AZURPILOT_OBSERVABILITY_PGADMIN_PGPASS`; он также остаётся только в `.env`.
Файл серверов импортируется только при инициализации нового
`azurpilot-pgadmin-data`, поэтому пользовательские подключения после первого
запуска не перезаписываются.

Если pgAdmin volume уже существует, добавьте или обновите подключение через
веб-интерфейс либо создайте отдельный disposable volume для повторного
импорта. Не добавляйте пароли в `servers.json`: pgAdmin не импортирует password
fields из этого файла.

Проверка базы и внешняя резервная копия:

    docker compose --env-file ../../.env exec -T --user postgres postgres sh -c 'pg_isready -U postgres -d "$POSTGRES_DB"'
    docker volume inspect azurpilot-postgres-data
    uv run --locked --no-sync python -m dev_tools.postgresql_runtime backup --transport docker --output <внешний-путь>.dump

Backup создаётся в custom format вне репозитория и проверяется через
`pg_restore --list` внутри контейнера. Для restore остановите consumers, сделайте
новый внешний backup, остановите только target service и восстановите дамп в
Docker database штатным `pg_restore` от роли `azurpilot_migrator` с явным
переключением на `azurpilot_owner`:

    pg_restore --username azurpilot_migrator --exit-on-error --clean --if-exists --no-owner --no-acl --role azurpilot_owner --dbname <target-database> <backup.dump>

После restore примените `postgres/grant-app.sql` от того же migrator-контракта.
Канонический `dev_tools.postgresql_migration` дополнительно проверяет owner
database/schema, owners tables/sequences/functions, role membership, app
grants, extension `plpgsql` и Alembic head, после чего выполняйте `runtime
health`, Alembic и app checks. Не восстанавливайте custom dump под случайным
superuser без явного `--role azurpilot_owner`.
Старый WSL data directory не удаляйте. Rollback: остановите Docker PostgreSQL,
верните прежний endpoint при необходимости и запустите Arch service только как
аварийный rollback-контур.

Не используйте `docker compose --env-file ../../.env down -v`,
`docker volume prune`, `docker system prune` или копирование raw PGDATA.

docker compose --env-file ../../.env config проверяет итоговую топологию и отсутствие
неожиданного host binding. pull загружает зафиксированные образы после чистого
clone. Повторный up -d должен быть идемпотентным.

Для штатной остановки выполните:

    docker compose --env-file ../../.env stop
    docker compose --env-file ../../.env start

Не используйте docker compose --env-file ../../.env down -v: volumes содержат
накопленные данные. Если нужно удалить сам Compose-контур без данных,
используйте docker compose --env-file ../../.env down; volumes и их содержимое
останутся доступными для последующего запуска.

## Хранилище и retention

Контур рассчитан на один локальный узел. Loki и Tempo используют filesystem
storage в своих named volumes; Prometheus хранит TSDB в отдельном volume.
Retention ограничен 7 днями для Loki и Tempo и 15 днями для Prometheus.
Эти настройки предназначены для локального контура и могут быть заменены
deployment-specific override без изменения service topology.

## Подключение application logs

AzurPilot подключает application logs явно после настройки штатного локального
logger-а через `set_file_logger()`. Подключение не выполняется при импорте
модулей: пока endpoint не задан, приложение работает в обычном offline-режиме
только с console/WebUI/file handlers. После opt-in приложение использует
официальные OpenTelemetry Logs bridge и OTLP/HTTP protobuf BatchLogRecordProcessor;
прямых зависимостей от Loki, Prometheus, Tempo или Grafana в application code
нет.

Для локального Compose-контура перед запуском AzurPilot передайте процессу
стандартные `OTEL_*` переменные:

    $env:OTEL_EXPORTER_OTLP_LOGS_ENDPOINT = 'http://127.0.0.1:4318/v1/logs'
    $env:OTEL_EXPORTER_OTLP_LOGS_PROTOCOL = 'http/protobuf'
    $env:OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=local'
    $env:OTEL_PYTHON_LOG_HANDLER_LEVEL = 'INFO'

Допустим также общий `OTEL_EXPORTER_OTLP_ENDPOINT`; для logs используется
`http/protobuf`. Таймауты и bounded batch-параметры читаются из стандартных
`OTEL_EXPORTER_OTLP_LOGS_TIMEOUT`, `OTEL_BLRP_SCHEDULE_DELAY`,
`OTEL_BLRP_MAX_QUEUE_SIZE`, `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` и
`OTEL_BLRP_EXPORT_TIMEOUT`. `OTEL_SDK_DISABLED=true` отключает все application
signals независимо от endpoint. `.env` Compose автоматически не загружается
в процесс AzurPilot: это намеренно отдельные границы конфигурации.

В удалённую запись попадают стабильный `service.name=azurpilot`, окружение,
`profile`, canonical task context, component, run id, process id/command и
структурированные exception attributes. Человеческое body очищается от ANSI,
Rich markup, секретов и локальных абсолютных путей; локальный `LogRecord` не
изменяется. В Loki только `service.name` и
`deployment.environment.name` являются index labels, остальные metadata остаются
structured metadata. Ошибка exporter, его недоступность или bounded shutdown не
останавливают gameplay, WebUI, console и локальный file fallback.

## Подключение application metrics

Application metrics подключаются независимо от logs. В процессе используется один
process-local `MeterProvider` с официальным `PeriodicExportingMetricReader` и
OTLP/HTTP protobuf exporter. Без metrics endpoint приложение не создаёт metrics
provider и не выполняет сетевых запросов.

Для локального Compose-контура перед запуском AzurPilot задайте:

    $env:OTEL_EXPORTER_OTLP_METRICS_ENDPOINT = 'http://127.0.0.1:4318/v1/metrics'
    $env:OTEL_EXPORTER_OTLP_METRICS_PROTOCOL = 'http/protobuf'
    $env:OTEL_METRIC_EXPORT_INTERVAL = '60000'
    $env:OTEL_METRIC_EXPORT_TIMEOUT = '30000'

Signal-specific endpoint передаётся exporter-у как полный URL. При использовании
общего `OTEL_EXPORTER_OTLP_ENDPOINT` официальный exporter добавляет стандартный
путь `/v1/metrics`; `OTEL_EXPORTER_OTLP_METRICS_TIMEOUT` имеет приоритет над
общим `OTEL_EXPORTER_OTLP_TIMEOUT`. Поддерживается только `http/protobuf`.
`OTEL_SDK_DISABLED=true` отключает все application signals.

Стандартный `OTEL_METRICS_EXEMPLAR_FILTER` остаётся под управлением OTel SDK.
При активной scheduler task SDK может связать exemplar с текущим application
trace; application code не добавляет trace/span IDs в metric labels. Наличие
exemplar проверяется только по фактически собранному SDK reader, а не по
предположению о downstream storage.

В текущей конфигурации отправляются два инструмента на одной canonical task
boundary scheduler-а в `Alas.loop`:

| OTel name | Type | Unit | Attributes |
| --- | --- | --- | --- |
| `azurpilot.task.run` | Counter | `{run}` | `azurpilot.profile`, `azurpilot.task`, `azurpilot.task.outcome` |
| `azurpilot.task.duration` | Histogram | `s` | `azurpilot.profile`, `azurpilot.task`, `azurpilot.task.outcome` |

`azurpilot.task.outcome` ограничен значениями `success`, `recoverable`,
`failure`, `stopped` и `unknown`. Значение profile проверяется через canonical
project identity и допускает Unicode и внутренние пробелы, а task принимает
только bounded ASCII-имя из registry. Неизвестные или небезопасные значения
становятся `unknown`; новый произвольный task не создаёт новую metric series.
Scheduler queue gauge намеренно не добавляется: у scheduler нет единственного
authoritative queue snapshot для корректного значения.

SDK использует cumulative temporality, совместимую с текущим Alloy metrics
path. При явном отличном от `cumulative` значении
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` application metrics
отключаются fail-open, потому что downstream Prometheus path не принимает
delta series.

Alloy переносит только `service.name` и `deployment.environment.name` из
resource attributes в datapoint attributes. `resource_to_telemetry_conversion`
остаётся выключенным; `target_info`, `otel_scope_info` и scope labels также не
создаются, поэтому произвольные resource attributes не превращаются в
Prometheus labels. Logs и traces проходят по прежним маршрутам. Prometheus не
является прямой application dependency.

Пример bounded PromQL для числа запусков по outcome:

    sum by (azurpilot_task_outcome) (rate(azurpilot_task_run_total[5m]))

Для безопасной локальной проверки без публикации Prometheus на host выполните
запрос из существующего Compose network:

    docker compose --env-file ../../.env exec -T prometheus wget -qO- 'http://127.0.0.1:9090/api/v1/query?query=azurpilot_task_run_total'

Недоступность metrics exporter, ошибка записи и bounded shutdown не меняют
результат задачи и не останавливают scheduler. Локальные logs продолжают
работать, даже если metrics signal не удалось инициализировать или отправить.

## Подключение application traces

Application traces подключаются независимо от logs и metrics. Включение
происходит только после явного endpoint opt-in; без trace endpoint приложение не
импортирует OTel SDK, не запускает worker и не выполняет сетевых запросов.
Используется официальный OTLP/HTTP protobuf exporter через существующий Alloy,
без прямых зависимостей application code от Tempo.

Для локального Compose-контура перед запуском AzurPilot задайте:

    $env:OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = 'http://127.0.0.1:4318/v1/traces'
    $env:OTEL_EXPORTER_OTLP_TRACES_PROTOCOL = 'http/protobuf'
    $env:OTEL_EXPORTER_OTLP_TRACES_TIMEOUT = '5000'
    $env:OTEL_TRACES_SAMPLER = 'parentbased_always_on'

Допустим общий `OTEL_EXPORTER_OTLP_ENDPOINT`; signal-specific endpoint имеет
приоритет над ним. Для traces также поддерживаются общий
`OTEL_EXPORTER_OTLP_PROTOCOL` и `OTEL_EXPORTER_OTLP_TIMEOUT`, а
`OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` имеет приоритет над общим таймаутом.
Значения выше локального предела 5 000 мс ограничиваются этим пределом.
Поддерживается только `http/protobuf`. При signal-specific endpoint полный
путь используется как задан; при общем endpoint SDK добавляет ровно
`/v1/traces`, поэтому дублирование этого пути не допускается. Параметры
`OTEL_BSP_SCHEDULE_DELAY`, `OTEL_BSP_MAX_QUEUE_SIZE`,
`OTEL_BSP_MAX_EXPORT_BATCH_SIZE` и `OTEL_BSP_EXPORT_TIMEOUT` ограничиваются
локальным bounded contract. `OTEL_TRACES_SAMPLER` и
`OTEL_TRACES_SAMPLER_ARG` передаются стандартному SDK. `OTEL_SDK_DISABLED=true`
отключает traces вместе с logs и metrics.

Корневой span создаётся один раз на фактическую scheduler task в границе
`Alas._run_scheduler_task` с именем `azurpilot.task.run`. В нём находятся
только bounded canonical `azurpilot.profile`, `azurpilot.task` и исход
`azurpilot.task.outcome`; `success`, `stopped` и `recoverable` не получают
ошибочный статус, а `failure` и необработанное исключение получают `ERROR`.
Внутри этой границы допускаются только значимые стабильные операции, например
`azurpilot.device.screenshot`, `azurpilot.ocr.process` и
`azurpilot.ui.wait`; generic `Alas.run("goto_main")` отдельный task root не
создаёт. Root и child spans используют `BatchSpanProcessor`; они не создаются
для каждого клика или события.

Trace runtime process-local, идемпотентен и fail-open. После fork унаследованный
provider отключается и child process требует свежего bootstrap; network и
OTel worker не стартуют при импорте. Shutdown выполняет bounded flush и
закрытие provider, а ошибка exporter или timeout не останавливает scheduler,
WebUI, gameplay или остальные signals. Logs получают correlation из текущего
OTel context через штатный logging bridge: `trace_id` и `span_id` доступны в
структурированном log record и не становятся Loki index labels.

Новая tracing-инструментация не добавляет raw OCR/UI data в span names,
span attributes или exception events. Абсолютные пути, credentials, токены,
cookies и необработанные exception objects в trace payload не передаются;
ошибки записываются только в bounded sanitized форме. Это не изменяет
существующую локальную OCR-диагностику: при `SHOW_LOG` её debug-сообщение
может содержать распознанный результат и не является частью tracing payload.
Trace IDs не используются как metric labels; связь metric exemplar с активным
span зависит от фактически поддержанного SDK reader и проверяется измерением.

Проверка локального пути выполняется через существующий Compose project:
`docker compose --env-file ../../.env config --quiet` и `ps` должны быть
успешны, приложение отправляет OTLP только на loopback Alloy, а запросы к
Loki, Prometheus и Tempo выполняются из соответствующего Compose network.

Исторические `log/`-артефакты не импортируются и не удаляются. `log/error/`
остаётся локальным incident store со скриншотами и `log.txt`, диагностические и
архивные каталоги сохраняются, CSV/JSON относятся к data/export или legacy
storage и не считаются application logs. Скриншоты и object-store слой в этот
контур не отправляются.

Portable base Compose не содержит Windows drive letters, WSL paths,
host.docker.internal, захардкоженные IP, host networking или публичные
bindings. Межсервисные URL используют service DNS, конфигурации подключаются
repository-relative paths, а persistent state отделён named volumes. Поэтому
тот же base contract можно перенести на VPS с отдельными secrets, host
bindings и внешним endpoint в deployment-specific настройках, не меняя
топологию сервисов.
