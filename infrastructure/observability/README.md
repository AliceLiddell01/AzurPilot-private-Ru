# Локальная observability-инфраструктура AzurPilot

Эта папка содержит переносимый Docker Compose-контур для приёма OTLP и
локального хранения logs, metrics и traces. Compose project имеет постоянное
имя azurpilot-infrastructure.

## Состав

| Сервис | Назначение | Образ |
| --- | --- | --- |
| caddy | публичный HTTPS reverse proxy для host-side Dev/Game MCP | caddy:2.11.4-alpine |
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
Caddy включается отдельным профилем `remote-ingress`, потому что публичный
endpoint является opt-in конфигурацией. Он работает в том же Compose project,
использует read-only bind `infrastructure/caddy` и обращается к host-side
backend через `host.docker.internal`; Dev и Game процессы по-прежнему слушают
только `127.0.0.1:8765` и `127.0.0.1:8766`. На host публикуются только TCP
`80`, TCP `443` и UDP `443`, используемый текущим HTTP/3 deployment. Admin API
`2019`, backend-порты, PostgreSQL, WebUI и telemetry ports не публикуются.

## Данные и секрет

Состояние хранится в именованных volumes:

- azurpilot-postgres-data;
- azurpilot-observability_alloy-data;
- azurpilot-observability_loki-data;
- azurpilot-observability_prometheus-data;
- azurpilot-observability_tempo-data;
- azurpilot-observability_grafana-data;
- azurpilot-pgadmin-data;
- azurpilot-caddy-data;
- azurpilot-caddy-config.

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
Для Grafana это именно initial admin secret: `GF_SECURITY_ADMIN_PASSWORD__FILE`
читается при первом создании admin state в `/var/lib/grafana`. После появления
внешнего `azurpilot-observability_grafana-data` пароль администратора хранится
как persisted credential в Grafana DB; изменение `.env` или Compose secret само
по себе его не синхронизирует. `GF_SECURITY_ADMIN_USER` также относится к
первичному созданию; для уже существующего volume canonical admin user должен
совпадать с persisted login.
Порт pgAdmin задаётся через `AZURPILOT_OBSERVABILITY_PGADMIN_PORT`; по умолчанию
используется `5050`. Публичные Dev/Game hosts задаются не секретными ключами
`AZURPILOT_CADDY_HOST` и `AZURPILOT_GAME_MCP_PUBLIC_HOST`; OAuth-переменные Dev/Game остаются в том же защищённом
локальном `.env` и не записываются в Git.

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

Если `.env` уже содержит новый canonical Grafana admin password, а существующий
volume был создан со старым password, выполните отдельное явное recovery из
корня checkout:

    uv run --locked --no-sync python -m dev_tools.observability_mcp recover-admin

Recovery не является побочным эффектом `ensure-identity`. Команда проверяет
canonical Compose и наличие именно `azurpilot-observability_grafana-data`,
останавливает только основной `grafana`, запускает официальный
`grafana cli admin reset-admin-password --password-from-stdin --user-id 1` в
одноразовом контейнере с теми же Compose mounts/config/secrets, затем штатно
поднимает Grafana с healthcheck. Пароль передаётся только через stdin и не
попадает в CLI arguments, logs, traceback или временный plaintext-файл. Volume
не удаляется и не пересоздаётся.

После reset recovery проверяет Grafana Admin API, запускает обычный
`ensure-identity` и отдельно выполняет Gateway probe. `ensure-identity` всегда
проверяет Admin API, canonical service account
`azurpilot-observability-mcp`, его роль `Viewer` и enabled state; успешный
Viewer/Gateway token не используется как обход этой проверки. HTTP 401 означает
`MCP_GRAFANA_ADMIN_CREDENTIALS_REJECTED` — текущие admin credentials отклонены;
это само по себе не доказывает stale volume и может означать другой неверный
user/password.

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

Для включённого public HTTPS ingress используйте тот же Compose project и
профиль `remote-ingress`:

    docker compose --env-file ../../.env --profile remote-ingress config --quiet
    docker compose --env-file ../../.env --profile remote-ingress up --detach --wait caddy
    docker compose --env-file ../../.env --profile remote-ingress ps

Проверка и reload выполняются внутри Compose Caddy; отдельный host-side запуск
Caddy не используется:

    docker compose --env-file ../../.env --profile remote-ingress exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
    docker compose --env-file ../../.env --profile remote-ingress exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
    uv run --locked --no-sync python -m dev_tools.infrastructure_doctor --repository-root ../.. doctor
    uv run --locked --no-sync python -m dev_tools.infrastructure_doctor --repository-root ../.. probe

Диагностика разделена: `infrastructure_doctor` сообщает состояние Docker
Caddy/container/healthcheck и опубликованных портов, а
`module.dev_mcp.remote doctor` и `module.game_mcp.remote doctor` проверяют только
собственную loopback-конфигурацию. Команда `probe` дополнительно проверяет OAuth
metadata, DNS/TLS и read-only MCP contract через публичные endpoints.

Для штатного старта только базы используйте:

    docker compose --env-file ../../.env up --detach --wait postgres
    docker compose --env-file ../../.env run --rm --no-deps postgres-bootstrap

`postgres-bootstrap` — одноразовый шаг выдачи app/migrator ролей и прав;
повторный запуск идемпотентен.
Владелец lifecycle — Docker Compose/Docker Desktop; Arch WSL2 сохраняется только
как rollback safety и не требует `systemctl start postgresql`.
Для восстановления Caddy после входа в Windows в Docker Desktop должна быть
включена настройка General → Start Docker Desktop when you sign in. Скрипт
`Start-AzurPilot.ps1` не изменяет эту пользовательскую настройку; после её
включения перезагрузка проверяет Docker Desktop → Compose project → Caddy.

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

Для остановки только observability выполните:

    docker compose --env-file ../../.env stop alloy loki prometheus tempo grafana
    docker compose --env-file ../../.env start loki prometheus tempo alloy grafana

Общий `compose down`, `down -v`, удаление или пересоздание volumes запрещены
для observability maintenance: этот проект также обслуживает PostgreSQL,
pgAdmin и Caddy. Управляйте только явно выбранными services.

## Хранилище и retention

Контур рассчитан на один локальный узел. Loki и Tempo используют filesystem
storage в своих named volumes; Prometheus хранит TSDB в отдельном volume.
Retention ограничен 7 днями для Loki и Tempo и 15 днями для Prometheus.
Эти настройки предназначены для локального контура и могут быть заменены
deployment-specific override без изменения service topology.

### Reliability policy и bounded recovery

Игровой runtime не зависит от доставки telemetry. `RuntimeStateStore` и exact
worker identity определяют execution через Game MCP; очереди scheduler, Loki и
traces не являются доказательством текущей игровой задачи.

Alloy использует bounded in-memory queue по 16 MiB для каждого OTLP logs/traces
exporter, два consumers и retry с backoff 5–30 секунд в течение 5 минут.
`block_on_overflow=false` сохраняет fail-open поведение: при переполнении новый
batch отклоняется и локальные file/incident artifacts остаются доступными.
После restart Alloy неподтверждённые logs/traces не обещают durable replay.
Preview `otelcol.storage.file` намеренно не включён.

`prometheus.remote_write.local` сохраняет metrics в существующий WAL volume
`azurpilot-observability_alloy-data`; `truncate_frequency=2h`,
`min_keepalive_time=5m`, `max_keepalive_time=8h`. Это bounded age window:
после него backlog может быть потерян. Loki retention — 7 дней с TSDB/v13,
24-часовым индексом, singleton Compactor и persistent delete markers. Tempo
blocks — 7 дней в `/var/tempo`; Prometheus TSDB — 15 дней в `/prometheus`.
Оставляйте не менее 20% свободного Docker filesystem для compaction. Размер
очереди и WAL контролируйте по internal metrics и volume usage; fixed
machine-specific `retention.size` не задаётся.

Internal metrics Alloy (`otelcol_exporter_queue_size`,
`otelcol_exporter_queue_capacity`, enqueue/send failures,
`otelcol_receiver_refused_*`, remote-write pending/retries/WAL и RSS) доступны
через его loopback admin endpoint внутри Compose network. Отсутствующая failure
series не считается нулём: она появляется после соответствующего события.

Для воспроизводимой проверки используйте canonical tooling:

    uv run --locked --no-sync python -m dev_tools.infrastructure_doctor observability
    uv run --locked --no-sync python -m dev_tools.observability_reliability inventory
    uv run --locked --no-sync python -m dev_tools.observability_reliability metrics
    uv run --locked --no-sync python -m dev_tools.observability_reliability outage --services tempo --output artifacts/observability/tempo

Harness сначала создаёт unique marker настоящим application OTel bootstrap и
общей scheduler telemetry boundary, затем проверяет Loki/Prometheus/Tempo,
local log, incident metadata и correlation trace ID. Он допускает только
`alloy`, `loki`, `prometheus`, `tempo`, `grafana`, пишет recovery journal до
первого stop, фиксирует container IDs/volumes и в `finally` запускает только
сервисы, изменённые этим запуском. PostgreSQL, Caddy и pgAdmin сверяются с
baseline и не останавливаются; `compose down`, `down -v`, удаление и
пересоздание volumes запрещены.

Матрица включает отдельные outages `tempo`, `loki`, `prometheus`, `grafana`,
`alloy` и одновременный outage всех пяти services. Для Loki/Tempo остальные
signals продолжают поступать, после recovery queued и fresh markers доступны;
Prometheus догоняет WAL. При Alloy проверяются local fallback и новые события
после recovery, без backfill уже отброшенных SDK данных. При Grafana direct
backend ingestion продолжается, а Grafana MCP failure ожидаем; после recovery
проверяются datasources, PromQL, LogQL и Tempo trace reads через
`azurpilot-observability`.

Параметр `--hold-seconds` задаёт дополнительную паузу после readiness и signal
checks. Полное время outage включает baseline и recovery ожидания и может быть
больше этого значения.

Нельзя прерывать host/Docker во время outage. Если процесс был прерван, не
удаляйте `recovery.json`: восстановите только его `attempted` container IDs,
сверьте прежние volumes и повторите health/inventory. Synthetic boundary не
исполняет игровую задачу и не заменяет отдельную live game acceptance.

## Подключение application logs

AzurPilot подключает application logs явно после настройки штатного runtime
logger-а через `configure_runtime_logging()`. Подключение не выполняется при
импорте модулей. Без OTLP endpoint приложение работает в допустимом offline-
режиме с console/WebUI и bounded in-memory incident context. При включённом
endpoint используется официальный OpenTelemetry Logs bridge и
OTLP/HTTP protobuf `BatchLogRecordProcessor`; прямых зависимостей от Loki,
Prometheus, Tempo или Grafana в application code нет.

Для локального Compose deployment корневой `.env` является единственным
каноническим источником application OTLP и Compose-настроек. Каждый штатный
entrypoint (`Start-AzurPilot.ps1`, GUI, scheduler worker, `alas`, `ap` и OCR
RPC) вызывает `configure_runtime_logging()`, который загружает из этого файла
только ключи `OTEL_*`; значения уже существующего окружения имеют приоритет.
Одного заполнения `.env` достаточно, отдельный PowerShell-сеанс перед каждым
запуском не нужен:

    OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
    OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/protobuf
    OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://127.0.0.1:4318/v1/metrics
    OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http/protobuf
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces
    OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
    OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=local
    OTEL_PYTHON_LOG_HANDLER_LEVEL=INFO

Список выше — пример для loopback Alloy, а не безусловный endpoint в коде.
Signal-specific endpoint или общий `OTEL_EXPORTER_OTLP_ENDPOINT` должны быть
явно заданы deployment-ом. `OTEL_SDK_DISABLED=true` отключает все application
signals, сохраняя offline-режим. Ошибка чтения `.env` не останавливает runtime:
используется окружение процесса и bounded fail-open политика.

В удалённую запись попадают стабильный `service.name=azurpilot`, окружение,
`profile`, canonical task context, component, run id, process id/command и
структурированные exception attributes. Человеческое body очищается от ANSI,
Rich markup, секретов и локальных абсолютных путей; локальный `LogRecord` не
изменяется. В Loki только `service.name` и
`deployment.environment.name` являются index labels, остальные metadata остаются
structured metadata. Ошибка exporter, его недоступность или bounded shutdown не
останавливают gameplay, WebUI и console. После исчерпания bounded queue/retry
часть normal remote log может быть потеряна; persistent local runtime copy при
этом не обещается, но bounded context остаётся доступен для реального incident.

## Подключение application metrics

Application metrics подключаются независимо от logs. В процессе используется один
process-local `MeterProvider` с официальным `PeriodicExportingMetricReader` и
OTLP/HTTP protobuf exporter. Без metrics endpoint приложение не создаёт metrics
provider и не выполняет сетевых запросов.

Для локального Compose-контура добавьте metrics endpoint в тот же корневой
`.env`, как показано в разделе logs. Metrics подключаются независимо от logs;
`OTEL_METRIC_EXPORT_INTERVAL` и `OTEL_METRIC_EXPORT_TIMEOUT` являются
необязательными bounded параметрами этого же источника.

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

Для локального Compose-контура добавьте traces endpoint в тот же корневой
`.env`, как показано в разделе logs. `OTEL_TRACES_SAMPLER`, signal-specific
timeouts и bounded `OTEL_BSP_*` параметры также задаются там же.

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

## Корреляция incidents

При `Error_SaveError=true` scheduler сохраняет локальный incident bundle в
`log/error/<canonical-profile>/`. Новый каталог получает UTC timestamp,
безопасный тип исключения и collision-safe суффикс. Внутри остаются прежние
локальные `log.txt` и снимки, а `incident.json` содержит только версию схемы,
UTC timestamp, canonical profile/task, bounded exception type и текущие
валидные OTel `trace_id`/`span_id`; при отключённом tracing оба ID равны `null`.
Сообщение исключения, stacktrace, raw exception object и снимки в telemetry или
`incident.json` не копируются. Сбой записи metadata не маскирует исходную
ошибку; старые каталоги не мигрируются.

Grafana provisioning связывает стабильные data source UID `loki` и `tempo`:
Loki derived field по label/structured-metadata key `trace_id` открывает Tempo,
а Tempo `tracesToLogsV2` ищет Loki по trace ID и сопоставляет
`service.name` с Loki key `service_name`. `trace_id` и `span_id` не являются
индексными labels Loki; это сохраняет bounded label cardinality и оставляет
корреляцию декларативной в Grafana.

## Операторский UX Grafana

Grafana `13.2.1` получает постоянное состояние только из репозитория:
`grafana/provisioning/dashboards/providers.yaml` подключает JSON dashboards из
`grafana/dashboards/`, а `grafana/provisioning/alerting/alert-rules.yaml`
подключает generic alert rules. Dashboard provider запрещает UI updates и
удаляет из Grafana DB dashboard, исчезнувший из provisioning source; ручное
состояние volume не является источником истины.

`AzurPilot Overview` содержит task runs, отдельные success / failure /
recoverable / stopped counters, success rate, достоверный `prometheus_ready`, outcome
breakdown, p50/p95 task duration, разбивку по profile/task/outcome, последние
ошибки Loki, последние traces и текущие alerts. Точные counters, outcome graph и
aggregate table считают только канонические root spans `azurpilot.task.run`
через Tempo TraceQL metrics; они не используют `increase()` или округление
Prometheus rate. Для duration остаётся Prometheus histogram, сгруппированный по
`azurpilot_task`, поэтому p50/p95 разных task не смешиваются.

Оба dashboard имеют общий semantic selector `Environment` по
`deployment.environment.name`, а также bounded selectors `Profile` и `Task`.
Эти selectors применяются одинаково к TraceQL, PromQL и LogQL и позволяют
отделить synthetic reliability run от normal telemetry без task-specific
hardcode. `AzurPilot Errors / Incidents` содержит bounded error log view,
ошибочные и медленные traces, а также доступные
`azurpilot.device.screenshot` / `azurpilot.ocr.process` spans. В Grafana не
добавляется выдуманный общий runtime-health signal: для Prometheus показывается
только его собственный `prometheus_ready`, а недоступность остальных backend-ов
определяется по фактической ошибке datasource/query.

Tempo metrics-generator использует `local-blocks` с persistent generator WAL и
trace WAL; `query_frontend.metrics.max_duration` покрывает bounded operator
window. Grafana instant query используется для exact counters, а bounded range
query с reduce — для aggregate table и событийного outcome graph. При отсутствии
событий counters показывают нулевое значение, а success share остаётся `нет
данных`, без `0/0` и NaN.

Alerts ограничены одним источником с достоверным generic-контрактом:
ненулевой поток failure task за 15 минут, сохраняющийся пять минут. Alert не
привязан к конкретному profile/task/event. Отдельный alert по p95 task duration
не добавляется: текущая schema не содержит authoritative per-task SLA, а
агрегация всех task в один global p95 не позволяет отличить штатную долгую
задачу от деградации без noisy false positives. p50/p95 остаются доступными в
dashboard для операторской диагностики. Нет отдельного alert «нет запусков»,
потому что scheduler не публикует authoritative expected-run schedule.

Prometheus exemplars не используются как workaround для exact counters:
`trace_id` не добавляется в metric labels, а Prometheus `increase()` не является
источником истины для числа run-ов. Tempo TraceQL metrics может вернуть
exemplar trace ID от того же canonical root span для корреляции с trace, но
корректность counters не зависит от наличия exemplar и остаётся проверяемой по
root-span count и grouped outcome.

После изменения provisioning нужно перезапустить Grafana или выполнить
поддержанный Admin API reload, затем автоматически проверить dashboard UIDs,
alert provenance, datasource UID и каждую panel query через Grafana API.
Остановка и запуск только observability services с прежними named volumes
должны восстановить тот же operator UX.

Исторические `log/`-артефакты не импортируются. Обычная работа GUI, `alas`,
`ap`, scheduler worker, OCR RPC и симулятора не создаёт runtime `.txt`/`.log`
файлы, `diagnostic/` или `bak/`; console/WebUI и Loki остаются разными
поверхностями одного runtime logger-а. `log/error/<profile>/<incident>/` —
единственный локальный persistent namespace для реального incident fallback:
sanitized bounded `log.txt`, `incident.json` и снимки из существующей
screenshot deque. `Error_SaveErrorCount` ограничивает число каталогов для
каждого profile, а `DiagnosticContextHandler` хранит только ограниченный
thread-safe in-memory ring до incident-а. PNG/JPG не отправляются в Loki и
отдельный binary/object store для этого контура не добавляется. CSV/JSON
существующих data/export и legacy storage не считаются application logs.

При недоступности Alloy/Loki normal remote log может быть потерян после
bounded queue/retry policy, но gameplay, WebUI и console продолжают работу.
При реальном исключении `save_error_log()` использует накопленный sanitized
context и создаёт только incident bundle; сбой записи этого bundle не маскирует
исходную ошибку. Dev Runtime сохраняет отдельный stdout/stderr evidence в
`config/state/dev-runtime-gui.log`, а явные benchmark/debug-инструменты могут
создавать собственные артефакты вне application runtime contract.

Portable base Compose не содержит Windows drive letters, WSL paths,
host.docker.internal, захардкоженные IP, host networking или публичные
bindings. Межсервисные URL используют service DNS, конфигурации подключаются
repository-relative paths, а persistent state отделён named volumes. Поэтому
тот же base contract можно перенести на VPS с отдельными secrets, host
bindings и внешним endpoint в deployment-specific настройках, не меняя
топологию сервисов.

## MCP-профиль наблюдаемости

`azurpilot-observability` — отдельный Docker MCP Toolkit profile для
диагностики observability. Он не управляет AzurPilot, игровыми профилями,
эмулятором или Docker-инфраструктурой: Development MCP и Game MCP остаются
самостоятельными поверхностями, а Docker CLI для намеренных outage/recovery
действий относится к отдельному эксплуатационному workflow.

Единственный version-controlled источник определения profile —
`.docker/azurpilot-observability-profile.json`. Он одновременно является
portable export и canonical server definition; отдельная вручную поддерживаемая
копия Grafana MCP server не используется.

Profile содержит только один pinned image `mcp/grafana` и не содержит volume,
Docker socket, host port или произвольный host filesystem bind. Grafana MCP
запускается с `--disable-write` и `--max-loki-log-limit=50`. Profile export
не содержит token: credential подставляется только из отдельного secret store.

### Восстановление profile и подключение Codex

Из корня checkout canonical profile сначала проверяется и импортируется без
повторения GUI-действий:

```powershell
uv run --locked --no-sync python -m dev_tools.observability_mcp profile --import
```

После импорта нужно установить bounded static boundary. Dynamic MCP является
global feature Docker MCP Toolkit, а не свойством одного profile. Встроенные
`mcp-*`, `mcp-exec` и `code-mode` tools поэтому должны быть отключены до
подключения client:

```powershell
uv run --locked --no-sync python -m dev_tools.observability_mcp ensure-boundary
uv run --locked --no-sync python -m dev_tools.observability_mcp runtime-tools
```

В repository Development/Game surfaces используют собственные local stdio или
authenticated MCP entrypoints и не зависят от Docker Dynamic MCP. Глобальное
отключение feature меняет только Docker MCP Toolkit; оно не добавляет и не
удаляет `azurpilot-dev` или Game MCP configuration.

Адрес Grafana внутри MCP container —
`http://host.docker.internal:3000`; сама Grafana остаётся доступной только на
`127.0.0.1:3000`. Для подключения глобального Codex client используется
фактический CLI contract Docker MCP Toolkit:

```powershell
docker mcp client connect codex --profile azurpilot-observability --global
```

Команда должна сохранить существующий `azurpilot-dev` и добавить отдельный
`MCP_DOCKER` с тем же profile. После изменения глобальной конфигурации Codex
может потребовать перезапуск клиента.

### Доступные MCP tools

Allowlist берётся из canonical profile и включает только read-only Grafana и
Tempo proxied tools:

```text
check_datasources_health
get_dashboard_panel_queries
get_dashboard_property
get_dashboard_summary
get_datasource
list_datasources
list_loki_label_names
list_loki_label_values
list_prometheus_label_names
list_prometheus_label_values
list_prometheus_metric_metadata
list_prometheus_metric_names
query_loki_logs
query_prometheus
query_prometheus_histogram
search_dashboards
generate_deeplink
alerting_manage_rules
tempo_docs-traceql
tempo_get-attribute-names
tempo_get-attribute-values
tempo_get-trace
tempo_traceql-metrics-instant
tempo_traceql-metrics-range
tempo_traceql-search
```

`alerting_manage_rules` оставлен только вместе с backend-флагом `--disable-write`:
текущая реализация Grafana MCP объединяет чтение и управление rules в одном
catalog tool, а write operations должны быть отброшены самим server mode.
Dashboard create/update/delete, snapshots, plugin/admin/OnCall/Sift/Pyroscope,
generic API и Docker-control tools в profile отсутствуют.

### Service account и token

Для MCP используется отдельный Grafana service account
`azurpilot-observability-mcp` с ролью `Viewer`; Grafana admin password,
PostgreSQL credentials и пользовательские credentials для MCP не передаются.
Значение service account token не хранится в Git, `.env`, profile export,
README, аргументах команд или временном plaintext-файле. Стабильное имя secret
в Docker MCP contract — `grafana.api_key`, а pinned server передаёт его через
`GRAFANA_SERVICE_ACCOUNT_TOKEN`.

Фактический pinned runtime проверяется через `tools/list`. В текущем image
официальный `user_info` отсутствует, поэтому `list_datasources` не считается
доказательством identity. При наличии доступного credential `ensure-identity`
проверяет его тем же bearer token через официальный Grafana read-only endpoint
`/api/access-control/user/permissions` и обязательный заголовок
`X-Grafana-Identity-Id: service-account:<id>`. Отсутствующий endpoint не заменяется
проверкой доступа к datasource: identity verification завершается
`MCP_GRAFANA_TOKEN_IDENTITY_UNAVAILABLE`. Отсутствующий или foreign header,
malformed permissions, invalid или более широкая роль приводят к
fail-closed/rotation. После записи нового token в secret store identity
проверяется повторно; obsolete tokens удаляются только у canonical account. Если
pinned Gateway не предоставляет `user_info`, а credential нельзя получить
официальным способом для identity-проверки, команда завершается диагностируемой
ошибкой и не объявляет token canonical.

Идемпотентный bootstrap выполняется из корня checkout. Он использует
Grafana admin credentials из локального `.env`, проверяет или создаёт ровно
один canonical service account, приводит его к роли `Viewer`, проверяет
существующий Gateway secret и создаёт replacement token только при
authentication failure. Token передаётся в Docker secret store через stdin
в bounded process и никогда не печатается:

```powershell
uv run --locked --no-sync python -m dev_tools.observability_mcp ensure-identity
```

Команда использует официальный service-account API pinned Grafana 13.2.1:
поиск, создание/обновление account и создание token выполняются через
`/api/serviceaccounts/*`; secret value не сохраняется в repository, environment,
CLI arguments, logs, traceback или artifact. При rotation новый token сначала
проверяется напрямую и через Gateway, затем старые tokens с canonical name
отзываются по metadata. При неуспешной проверке replacement token отзывается,
а старый token не отзывается. Admin API bootstrap принимает только loopback
Grafana URL (`127.0.0.1`, `localhost` или `::1`).

Если Docker Secrets Engine недоступен, bootstrap завершается с ошибкой и не
создаёт новый token. Исправлять нужно именно credential transport, а не
обходить его plaintext-файлом или переменной в profile.

### Question-driven diagnostic workflow

Начинай с discovery и узких запросов, затем связывай один фактически
существующий AzurPilot task run между сигналами:

1. `list_datasources`, `check_datasources_health`, `search_dashboards` и
   `get_dashboard_summary` подтверждают identity и доступные источники.
2. `query_prometheus` ищет `azurpilot_task_run_total`, а
   `query_prometheus_histogram` — базу
   `azurpilot_task_duration_seconds`; для воспроизводимого bounded запроса
   передавай явные `startTime`, `endTime` и `stepSeconds`, а range и labels
   ограничивай нужным окном времени и profile/task.
3. `query_loki_logs` использует bounded LogQL с `service_name="azurpilot"`
   и, при необходимости, `trace_id` в structured metadata; не запрашивай весь
   retention window.
4. `get_dashboard_property` и `get_dashboard_panel_queries` показывают, какая
   panel и query визуализируют найденный run; для перехода используй
   `generate_deeplink`.
5. Через Gateway выполняются TraceQL search с RFC3339 `start`/`end`, attribute
   discovery и `tempo_get-trace` для trace ID, извлечённого из того же task run;
   прямой второй Tempo MCP connection не создаётся.

Tempo MCP включён в `tempo/config.yaml` через
`query_frontend.mcp_server.enabled: true`. Tempo `3200` не опубликован на
host: Grafana обращается к нему через Compose network и datasource proxy.
Если `tempo_*` tools не появились, проверь `tempo` logs и `/api/mcp` из
Grafana container, затем перезапусти именно Grafana MCP Gateway после
перезапуска Tempo. Не расширяй profile и не добавляй второй Tempo server как
обходной путь.

Отсутствие application metric, log или trace — это `INCOMPLETE`, а не PASS.
Нельзя писать synthetic records напрямую в Prometheus, Loki или Tempo только
ради acceptance. Полный результат требует цепочку `task metric → task/profile
→ log → trace_id → tempo_get-trace`; результат MCP для выбранных metric,
dashboard, alert и trace по возможности сверяется независимым Grafana/backend
API.

Synthetic и Docker outage harness не запускают Azur Lane. Live game acceptance
намеренно остаётся `PENDING USER AUTHORIZATION` до отдельного разрешения
пользователя.

### Проверка безопасности и восстановление Docker Desktop

Перед live query проверь только несекретные свойства:

```powershell
uv run --locked --no-sync python -m dev_tools.observability_mcp preflight
uv run --locked --no-sync python -m dev_tools.observability_mcp runtime-tools
```

На Windows Docker Desktop `docker mcp secret ls` и Gateway могут завершаться
ошибкой вида:

```text
secrets engine is not available: unavailable: dial unix ...docker-secrets-engine...engine.sock: connect: An invalid argument was supplied
```

Сначала убедись, что Docker Desktop запущен, а доступные WSL distributions
имеют рабочее состояние через `wsl --list --verbose`; имя конкретной
distribution не является контрактом AzurPilot. Если после аварийного
завершения Docker родительский каталог `docker-secrets-engine` содержит только
нерабочий reparse-point socket, допустима recoverable-процедура: остановить
Docker Desktop, переименовать ровно этот каталог в backup с timestamp, запустить
Docker Desktop и проверить появление нового socket. Backup не удаляется
автоматически; не используй `Remove-Item`, `git clean` или broad recursive
delete.

Отдельная ошибка `docker mcp secret ls` может быть диагностическим warning, но
она не оправдывает обход secret store. Успешность определяется реальными
`initialize`, `tools/list` и read-only calls через тот же Gateway; `EOF` или
`0 tools` означают `BLOCKED`.

Переключение Docker Desktop с WSL2 backend на Hyper-V/VM не является заменой
secret-store recovery. Такой режим имеет смысл проверять только если сама WSL
integration не запускается; переход не должен ослаблять loopback bindings,
secret policy или allowlist.

Одного созданного profile или прямого Grafana API недостаточно для Gateway/MCP
acceptance. Required evidence — exact runtime allowlist, положительные reads
через сам Gateway для datasources, Prometheus, Loki, dashboards, alert read и
Tempo, отрицательная проверка Grafana write, а также завершённая cross-signal
цепочка для одного реального task run. Если trace ID не найден, acceptance
остаётся незавершённой и не маскируется независимыми backend pings.
