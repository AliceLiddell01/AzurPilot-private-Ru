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
| redis | ephemeral runtime cache без доменных данных | redis:8.10.1 |
| redisinsight | постоянная локальная диагностика Redis | redis/redisinsight:3.8.0 |
| loki | хранение logs | grafana/loki:3.7.4 |
| prometheus | хранение metrics и remote-write receiver | prom/prometheus:v3.14.0 |
| tempo | хранение traces и OTLP receiver | grafana/tempo:3.0.3 |
| grafana | локальная визуализация подключённых data sources | grafana/grafana:13.2.1 |
| pgadmin | веб-администрирование PostgreSQL | dpage/pgadmin4:9.17 |

В compose.yaml для каждого образа зафиксированы version tag и digest.
Образы являются официальными образами соответствующих проектов.

Alloy принимает OTLP по 127.0.0.1:4317 (gRPC) и 127.0.0.1:4318 (HTTP).
Grafana доступна по 127.0.0.1:3000. Loki, Prometheus и Tempo не публикуются
на host: Alloy и Grafana обращаются к ним через стандартную Compose network и
service DNS.
pgAdmin доступен только по 127.0.0.1:5050. Redis публикуется только на
127.0.0.1:6379, а RedisInsight — только на 127.0.0.1:5540. RedisInsight не
является зависимостью приложения или runtime cache.
Caddy включается отдельным профилем `remote-ingress`, потому что публичный
endpoint является opt-in конфигурацией. Он работает в том же Compose project,
использует read-only bind `infrastructure/caddy` и обращается к host-side
backend через `host.docker.internal`; Dev и Game процессы по-прежнему слушают
только `127.0.0.1:8765` и `127.0.0.1:8766`. На host публикуются только TCP
`80`, TCP `443` и UDP `443`, используемый текущим HTTP/3 deployment. Admin API
`2019`, backend-порты, PostgreSQL, WebUI и telemetry ports не публикуются.
Путь `/api/notification-agent/*` в том же Caddy направляется на единственный
host-side WebUI (по умолчанию `host.docker.internal:25548`) через
`AZURPILOT_NOTIFICATION_AGENT_BACKEND`; второй WebUI или публичный PostgreSQL
для Desktop Agent не создаются. Сам Agent использует только исходящие
проверенные HTTPS-соединения и отдельный authenticated ACK.

## Данные и секрет

Состояние хранится в именованных volumes:

- azurpilot-postgres-data;
- azurpilot-redis-data;
- azurpilot-redisinsight-data;
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
`AZURPILOT_CADDY_HOST` и `AZURPILOT_GAME_MCP_PUBLIC_HOST`; backend Agent
задаётся `AZURPILOT_NOTIFICATION_AGENT_BACKEND`. OAuth-переменные Dev/Game
и Agent credential остаются в том же защищённом локальном `.env` и не
записываются в Git. Полный список Agent runtime-переменных и их scope описан
в `docs/notification-platform.md`.

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

Если persisted Grafana admin credential нужно восстановить, используйте штатный
Compose lifecycle из раздела выше и отдельную процедуру проекта. Direct MCP
adapter не изменяет admin password, named volumes или Compose state: он только
проверяет read-only endpoint и возвращает bounded состояние.
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

Для Redis задайте отдельные app/admin secrets и ключ шифрования RedisInsight:

    AZURPILOT_REDIS_HOST=127.0.0.1
    AZURPILOT_REDIS_PORT=6379
    AZURPILOT_REDIS_USERNAME=azurpilot_app
    AZURPILOT_REDIS_PASSWORD=<случайный_секрет_приложения>
    AZURPILOT_REDIS_ADMIN_PASSWORD=<отдельный_секрет_администратора>
    AZURPILOT_REDISINSIGHT_ENCRYPTION_KEY=<случайный_ключ_шифрования>
    AZURPILOT_REDISINSIGHT_PORT=5540

Redis secrets не передаются в argv и не записываются в Compose evidence. App
пользователь ограничен namespace `azurpilot:*` и командами cache; admin secret
нужен только для операторского подключения. RedisInsight получает endpoint и
username через официальные environment-параметры, а пароль вводится в его UI
при первом подключении. Все строки выше относятся к защищённому локальному
`.env` и не должны попадать в Git, логи или скриншоты `docker inspect`.

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

Обычный `up` запускает постоянные PostgreSQL, Redis и RedisInsight рядом с
остальными сервисами:

    docker compose --env-file ../../.env up --detach
    docker compose --env-file ../../.env run --rm --no-deps postgres-bootstrap

`postgres-bootstrap` — одноразовый шаг выдачи app/migrator ролей и прав;
повторный запуск идемпотентен.
Если `AZURPILOT_REDISINSIGHT_ENCRYPTION_KEY` отсутствует, RedisInsight остаётся
в fail-closed состоянии и не блокирует PostgreSQL или Redis; после добавления
ключа достаточно повторить `docker compose ... up --detach redisinsight`.
Сервис не является зависимостью приложения или runtime cache.
Владелец lifecycle — Docker Compose/Docker Desktop; Arch WSL2 сохраняется только
как rollback safety и не требует `systemctl start postgresql`.
Для восстановления Caddy после входа в Windows в Docker Desktop должна быть
включена настройка General → Start Docker Desktop when you sign in. Команда
`azur start` не изменяет эту пользовательскую настройку; после её
включения перезагрузка проверяет Docker Desktop → Compose project → Caddy.

### Redis runtime cache

Redis — reconstructable cache, а не durable source of truth: PostgreSQL остаётся
единственным владельцем доменных данных. В Redis не размещаются Commission,
AP/RewardDorm, Main fallback или другие игровые схемы. При cache miss приложение
получает `None`; недоступность Redis не превращается в silent in-memory fallback.
Ошибки boundary различаются как `NOT_CONFIGURED`, `UNAVAILABLE`, `TIMEOUT`,
`AUTH_FAILED`, `INVALID_DATA` и `UNKNOWN`. Клиент создаётся lazy для текущего
PID, после fork/spawn соединения не переиспользуются, timeout ограничен, а
retry-on-timeout выключен.

Внутри Compose приложение использует `redis:6379`, а host-side runtime —
`127.0.0.1:${AZURPILOT_REDIS_PORT:-6379}`. Docker deployment доказывает
canonical project, healthy Redis container, Compose network и DNS alias до
передачи transport overrides приложению. Redis включён с AOF и
`appendfsync everysec`; named volume `azurpilot-redis-data` сохраняет cache
между обычным restart Redis, но данные всё равно считаются временными и могут
быть пересозданы приложением.

Проверка без публикации secret:

    docker compose --env-file ../../.env exec -T redis sh -c 'REDISCLI_AUTH="$(cat /run/secrets/redis_app_password)" redis-cli --user azurpilot_app ping'
    docker compose --env-file ../../.env ps redis redisinsight
    Invoke-WebRequest http://127.0.0.1:5540/api/health/

В RedisInsight откройте [http://127.0.0.1:5540](http://127.0.0.1:5540), выберите
предложенное подключение `AzurPilot Redis`, при необходимости укажите username
`azurpilot_admin` и введите admin secret из локального `.env`. Endpoint должен
оставаться `redis:6379`, не опубликованный host-порт. `/api/health/` — штатный
health endpoint RedisInsight.

Удаление cache volume является осознанной destructive операцией и не требуется
для обычного restart:

    docker compose --env-file ../../.env stop redis redisinsight
    docker volume rm azurpilot-redis-data azurpilot-redisinsight-data

После удаления выполните `up --detach --wait redis redisinsight`; Redis ACL и
пустой cache будут созданы заново из локальных secrets. Не используйте
`docker volume prune` или `docker system prune`.

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
проверяются datasources, PromQL, LogQL и Tempo trace reads через direct
read-only Grafana adapter.

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
entrypoint (`azur start`, GUI, scheduler worker, `alas`, `ap` и OCR
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

### Задержка доставки в Grafana

Для локального Compose-контура цепочка доставки настроена на короткую, но
ограниченную задержку: application BatchLogRecordProcessor и
BatchSpanProcessor используют значение по умолчанию 500 мс, application
metrics — export interval 1 с, Alloy сбрасывает неполный batch через 500 мс, а
provisioned dashboards обновляются
каждые 5 с. Поэтому после завершения task обычно достаточно нескольких секунд;
точная задержка всё ещё зависит от доступности backend и очередей exporter-а.

Явные `OTEL_BLRP_SCHEDULE_DELAY`, `OTEL_BSP_SCHEDULE_DELAY` и
`OTEL_METRIC_EXPORT_INTERVAL` могут увеличить эту задержку, но проходят через
ограниченный контракт. В дашборде `overview` панель с outcome показывает
итоговое число запусков за выбранный диапазон, а панель p50/p95 считает
распределение длительностей по завершённым root spans через Tempo
`quantile_over_time`.

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
Prometheus rate. Для duration используются Tempo TraceQL metrics
`quantile_over_time` с группировкой по `azurpilot.profile` и `azurpilot.task`,
поэтому p50/p95 разных profile/task не смешиваются. Grafana явно переименовывает
возвращаемые Tempo labels в вид `p50 <profile> / <task>` и `p95 <profile> /
<task>`; сырой набор `{p=..., span...}` в operator UX не показывается.

Оба dashboard имеют общий semantic selector `Environment` по
`deployment.environment.name`, а также bounded selectors `Profile` и `Task`.
Эти selectors применяются одинаково к TraceQL, PromQL и LogQL и позволяют
отделить synthetic reliability run от normal telemetry без task-specific
hardcode. В Loki `azurpilot_profile` и `azurpilot_task` используются как
structured metadata, а не как index labels. Поэтому при `Task=All` записи без
task context остаются видимыми; при выборе конкретного Task показываются только
записи, которые несут совпадающий canonical task, и записи без task context
намеренно не приписываются выбранной задаче. `AzurPilot Errors / Incidents`
содержит bounded error log view,
ошибочные и медленные traces, а также доступные
`azurpilot.device.screenshot` / `azurpilot.ocr.process` spans. В Grafana не
добавляется выдуманный общий runtime-health signal: для Prometheus показывается
только его собственный `prometheus_ready`, а недоступность остальных backend-ов
определяется по фактической ошибке datasource/query.

Значения переменных `Environment`, `Profile` и `Task` обнаруживаются по
каноническим Prometheus labels `deployment_environment_name`, `azurpilot_profile`
и `azurpilot_task`. Это намеренная зависимость discovery: соответствующие поля
в Loki являются structured metadata, а не index labels, поэтому Loki используется
для фильтрации записей, но не для `label_values`-списков.

Tempo 3 использует локальные `vParquet4` blocks и trace WAL; metrics-generator
в локальном профиле не включён. `query_frontend.metrics.max_duration` покрывает
bounded operator window. Grafana instant query используется для exact counters и outcome graph в
горизонтальном `bargauge` без синтетической временной оси,
а bounded range query с reduce — для aggregate table. При отсутствии событий
counters показывают нулевое значение, а success share остаётся `нет данных`,
без `0/0` и NaN. Если backend явно возвращает нулевой total как
числовой ряд, защитное math-выражение показывает bounded `0%` вместо Inf/NaN;
это не трактуется как запуск и не меняет семантику отсутствующего ряда.

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
исходную ошибку. Dev Runtime не создаёт локальную копию normal application
logs: для поиска app logs используется отдельный read-only Grafana MCP через
`query_loki_logs`, а bounded incident context остаётся независимым локальным
evidence.

Portable base Compose не содержит Windows drive letters, WSL paths,
host.docker.internal, захардкоженные IP, host networking или публичные
bindings. Межсервисные URL используют service DNS, конфигурации подключаются
repository-relative paths, а persistent state отделён named volumes. Поэтому
тот же base contract можно перенести на VPS с отдельными secrets, host
bindings и внешним endpoint в deployment-specific настройках, не меняя
топологию сервисов.

## Прямые внешние интеграции и MCP status

Developer tooling использует шесть типизированных direct integrations:

| Семейство | Канонический transport | Credential и граница |
| --- | --- | --- |
| CodeRabbit | WSL2 agent через isolated checkout | явный или однозначно обнаруженный WSL2 runtime; agent review advisory |
| Semgrep | локальный CLI | только явно заданный staged/committed/path scope |
| Grafana | официальный контейнерный MCP server, stdio | явный endpoint и credential; read-only server flags |
| Context7 | официальный streamable HTTP endpoint | user-scoped credential, без repository secret |
| Docker Docs | официальный streamable HTTP endpoint | read-only public documentation |
| Docker Hub | официальный контейнерный MCP server, stdio | public probe допускается без auth; write tools запрещены |

Источники конфигурации имеют приоритет explicit CLI, validated user/machine
configuration, repository registration и deterministic discovery. Значения
credential никогда не попадают в Git, CLI arguments, logs или operator-facing
evidence. В evidence сохраняются только source, безопасное имя или file
provenance, факт настройки, authenticated/not_observable и bounded reason code.

### CLI и bounded evidence

Проверка всех семейств:

    azur integrations status
    azur integrations status --json
    azur integrations doctor
    azur integrations doctor --json

Проверка одного семейства использует только typed leaves:

    azur integrations coderabbit status
    azur integrations coderabbit doctor
    azur integrations semgrep status
    azur integrations semgrep scan --staged
    azur integrations semgrep scan --changed --base <exact-sha>
    azur integrations grafana probe
    azur integrations context7 probe
    azur integrations docker-docs probe
    azur integrations docker-hub probe

Semgrep не запускает полный repository scan по умолчанию. Для custom path
scope передавайте повторяемый --paths с validated относительными файлами.
Адаптер использует tracked local ruleset и отключает telemetry metrics.
Committed scan требует exact base commit; текущий HEAD определяется локальным
Git и не подменяется историческим evidence.

azur doctor по умолчанию выполняет дешёвую локальную диагностику; флаг
`azur doctor --full` добавляет шесть внешних integration summaries. Оба режима
read-only: они не создают credential, не запускают full scan и не изменяют
Compose или runtime. dev_tools.mcp_status использует тот же
IntegrationService и публикует только bounded status, source/runtime
provenance и machine-readable reason codes:

    uv run --locked --no-sync python -m dev_tools.mcp_status
    uv run --locked --no-sync python -m dev_tools.mcp_status --json
    uv run --locked --no-sync python -m dev_tools.mcp_status --json --strict
    uv run --locked --no-sync python -m dev_tools.mcp_status --json --emit-metrics
    uv run --locked --no-sync python -m dev_tools.mcp_status --watch --interval-seconds 60

--strict fail-closed требует clean source, согласованный first-party contract
и READY для всех шести direct integrations. `effective_codex_registration`
остаётся `not_observable` и проверяется отдельной live acceptance; это
состояние не маскируется под READY.

### CodeRabbit

CodeRabbit выполняется только в постоянном isolated WSL2 review clone. Перед
review проверяются exact canonical repository identity, detached clean checkout,
точный committed HEAD, explicit base SHA, non-root Linux user и доступность
официальной команды:

    azur integrations coderabbit status
    azur integrations coderabbit doctor
    azur integrations coderabbit review --base <exact-base-sha> --head <exact-head-sha>

Agent NDJSON разбирается с bounded size/line limits. Findings получают одну из
классификаций confirmed, partially confirmed, false positive или insufficient
evidence. Адаптер не исполняет provider snippets или codegen instructions;
первые две категории только становятся candidates для отдельного исправления.
Review budget ограничен тремя содержательными итерациями. Rate limit или
недоступная credential фиксируются как RATE_LIMITED/UNAUTHENTICATED и не
превращаются в бесконечный retry.

WSL inventory читается через wsl.exe --list --quiet и --list --verbose.
При заданном exact distro проверяются WSL2, non-root user и usable clone. Без
заданного имени используется только один WSL2 candidate; ноль даёт
NOT_CONFIGURED, несколько дают AMBIGUOUS. Машинное имя distro, домашний
каталог и путь clone не встраиваются в source или документацию.

### Семантика direct MCP adapters

Grafana запускается pinned immutable image через stdio с disable-write,
disable-api и явными bounded categories; query execution остаётся включённым,
поскольку observability contract требует Loki/Prometheus reads. Текущий image
публикует Tempo/TraceQL только через proxied Tempo MCP, поэтому disable-proxied
на этом route не используется: он удаляет required Tempo tools. Вместо этого
Codex registration использует deny-by-default allowlist из typed
`GRAFANA_READ_ONLY_TOOLS`, а exact runtime catalog gate отклоняет неизвестную
proxied surface до любого tool call. Endpoint передаётся через validated
AZURPILOT_GRAFANA_URL или bounded discovery текущей Compose topology, а
credential выбирается через поддержанный environment или validated file
reference.
Create/update/delete, generic API, admin, plugin, annotation и alert mutation
tools не попадают в registration и дополнительно блокируются typed policy.
Проверка доступности не заявляет более широкую роль, чем подтверждённый
credential.

Context7 использует официальный endpoint
https://mcp.context7.com/mcp; anonymous read-only probe допустим, а
authenticated readiness требует user-scoped credential. Docker Docs использует
https://mcp-docs.docker.com/mcp и bounded fetch_docker_docs call. Public
Docker Hub использует pinned mcp/dockerhub image и допускает
checkRepository/info/tag reads без PAT; createRepository, updateRepositoryInfo и
deleteRepository никогда не вызываются адаптером.

### Compose и observability lifecycle

Direct integrations не владеют Compose lifecycle. Docker Desktop/Engine/Compose,
Grafana, Loki, Tempo, Prometheus, PostgreSQL, Caddy, pgAdmin, Alloy,
healthchecks, named volumes и существующие backup/recovery workflows остаются
в infrastructure/observability/compose.yaml и связанных lifecycle-модулях.
Замена developer-tooling integrations не удаляет volumes, не пересоздаёт
observability project и не переводит application logs/traces на новый storage.

Grafana direct adapter читает explicit endpoint или безопасно подтверждённую
локальную Compose topology; он не создаёт service account, не ротирует token и
не сбрасывает admin password. Compose credentials и persistence остаются
отдельным operator-owned контуром. Унаследованный Gateway/Secrets Engine
контур Docker не используется как credential boundary для direct adapter. При
недоступном endpoint или credential status честно остаётся NOT_CONFIGURED,
UNAUTHENTICATED или UNAVAILABLE.

### Приёмка и ограничения

Для финальной приёмки фиксируются на одном exact HEAD:

- human и JSON output для integrations и dev_tools.mcp_status;
- Semgrep staged/changed scope с доказанным ограничением файловой области;
- Grafana list/query reads;
- Context7 resolve/search;
- Docker Docs search/fetch;
- Docker Hub repository/info/tag reads;
- Compose health и сохранность observability volumes;
- CodeRabbit dogfood review с canonical clone evidence.

Каждая поверхность имеет собственный READY/NOT_CONFIGURED/UNAVAILABLE/
UNAUTHENTICATED/RATE_LIMITED/INCOMPATIBLE/DEGRADED/UNKNOWN state. Public
unauthenticated probe, синтетический notifier, bounded test fixture или
статическая конфигурация не заменяют фактическое authenticated runtime
evidence. Deferred write capabilities и старые operator shell workflows не
входят в direct adapter scope и не считаются успешной частью приёмки.
