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
остаётся Compose-managed, чтобы новый host мог создать его до проверенного
logical restore.
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

Если переменных ещё нет, добавьте их в корневой .env. Для ротации уже
добавленного пароля используйте PowerShell-команду ниже: она сохраняет новое
значение напрямую в .env и ничего не выводит в stdout.

    AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=admin
    AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=<случайный_секрет>
    AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL=admin@azurpilot.dev
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

На новом Docker host сначала проверьте или явно создайте exact external
volumes. Для PostgreSQL выполняйте это только в рамках подготовленного
logical restore; пустой volume не является заменой backup:

    $volumeNames = @(
        'azurpilot-postgres-data'
        'azurpilot-observability_alloy-data'
        'azurpilot-observability_loki-data'
        'azurpilot-observability_prometheus-data'
        'azurpilot-observability_tempo-data'
        'azurpilot-observability_grafana-data'
    )
    foreach ($volumeName in $volumeNames) {
        $volumeInspection = docker volume inspect $volumeName 2>$null
        $volumeInspectionExitCode = $LASTEXITCODE
        if ($volumeInspectionExitCode -ne 0) {
            docker volume create $volumeName | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Не удалось создать Docker volume: $volumeName"
            }
        }
    }

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
использует роль `azurpilot_migrator` с правами владельца схемы. Пароль роли
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

    docker compose --env-file ../../.env exec -T --user postgres postgres pg_isready -U postgres -d azurpilot
    docker volume inspect azurpilot-postgres-data
    uv run --locked --no-sync python -m dev_tools.postgresql_runtime backup --transport docker --output <внешний-путь>.dump

Backup создаётся в custom format вне репозитория и проверяется через
`pg_restore --list` внутри контейнера. Для restore остановите consumers, сделайте
новый внешний backup, остановите только target service и восстановите дамп в
Docker volume штатным `pg_restore` через локальный peer-admin с
`--no-owner --no-acl`; после restore примените
`postgres/grant-app.sql`, затем выполните `runtime health`, Alembic и app checks.
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
`OTEL_BLRP_EXPORT_TIMEOUT`. `OTEL_SDK_DISABLED=true` отключает application
exporter независимо от endpoint. `.env` Compose автоматически не загружается
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
`OTEL_SDK_DISABLED=true` отключает оба application signal-а.

Стандартный `OTEL_METRICS_EXEMPLAR_FILTER` остаётся под управлением OTel SDK;
собственные traces для exemplars этим контуром не создаются.

В текущей конфигурации отправляются два инструмента на одной canonical task
boundary scheduler-а в `Alas.loop`:

| OTel name | Type | Unit | Attributes |
| --- | --- | --- | --- |
| `azurpilot.task.run` | Counter | `{run}` | `azurpilot.profile`, `azurpilot.task`, `azurpilot.task.outcome` |
| `azurpilot.task.duration` | Histogram | `s` | `azurpilot.profile`, `azurpilot.task`, `azurpilot.task.outcome` |

`azurpilot.task.outcome` ограничен значениями `success`, `recoverable`,
`failure`, `stopped` и `unknown`. Значения profile/task нормализуются к
bounded label contract; неизвестные или небезопасные значения становятся
`unknown`. Scheduler queue gauge намеренно не добавляется: у scheduler нет
единственного authoritative queue snapshot для корректного значения.

SDK использует cumulative temporality, совместимую с текущим Alloy metrics
path. При явном отличном от `cumulative` значении
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` application metrics
отключаются fail-open, потому что downstream Prometheus path не принимает
delta series.

Alloy переносит только `service.name` и `deployment.environment.name` из
resource attributes в datapoint attributes. `resource_to_telemetry_conversion`
остаётся выключенным; `target_info`, `otel_scope_info` и scope labels также не
создаются, поэтому произвольные resource attributes не превращаются в
Prometheus labels. Logs и traces проходят по прежним маршрутам. Application
traces пока не instrumented; Tempo остаётся готовым инфраструктурным
приёмником. Prometheus не является прямой application dependency.

Пример bounded PromQL для числа запусков по outcome:

    sum by (azurpilot_task_outcome) (rate(azurpilot_task_run_total[5m]))

Для безопасной локальной проверки без публикации Prometheus на host выполните
запрос из существующего Compose network:

    docker compose --env-file ../../.env exec -T prometheus wget -qO- 'http://127.0.0.1:9090/api/v1/query?query=azurpilot_task_run_total'

Недоступность metrics exporter, ошибка записи и bounded shutdown не меняют
результат задачи и не останавливают scheduler. Локальные logs продолжают
работать, даже если metrics signal не удалось инициализировать или отправить.

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
