# Основа хранения PostgreSQL

## Граница владения

Stage 2 создал исполняемую основу, а Stage 4 подключил её к production runtime:

```text
game / WebUI / MCP / future migration tooling
                 ↓
module.application DTO + repository ports + Unit of Work contract
                 ↓
module.persistence PostgreSQL adapters
                 ↓
SQLAlchemy Core → Psycopg sync driver → PostgreSQL
```

`module/application/` не импортирует SQLAlchemy, Psycopg или Alembic.
Основные `module/persistence/` adapters не импортируют SQLite и не открывают
соединение при импорте. Узкое исключение — `module.persistence.legacy`: это
strictly read-only offline adapter Stage 3, который не импортируется production
consumers. Production consumers используют application services. SQLite
сохранён только в offline legacy importer; silent fallback между backend
запрещён.

## Schema v1 и Fleet State

Application tables находятся в PostgreSQL namespace `azurpilot`. Schema
содержит только доказанные SQLite-owned domains и необходимую foundation:

- внутреннюю identity и digest-only legacy aliases;
- import batch/record provenance и bounded quarantine metadata;
- CL1 monthly aggregates, AP purchases, AP/currency/resource snapshots и
  notification state;
- Opsi item events;
- commission income header и полностью принадлежащие ему child items;
- Meow timing/hazard и Siren research device events/aggregates;
- bounded current resource state с optimistic version.

Новые timestamps используют `timestamptz`. Для legacy naive timestamps
предусмотрены отдельные literal/provenance columns без угадывания UTC. Ресурсы
и counters не используют float. Известные payload shapes типизированы; JSONB
разрешён только для ограниченной quarantine metadata.

EventObservation/EventPlan/Event priority, config/scheduler/deploy state, logs,
screenshots, worker registry и generated artifacts остаются file-owned и в
schema v1 не входят.

## Транзакции и процессы

`LazyEngine` создаёт bounded `QueuePool` только при первом вызове в текущем PID.
После смены PID inherited pool отсоединяется без закрытия parent connections.
Каждый процесс имеет собственный budget, по умолчанию два соединения и один
overflow. Connect/pool timeout ограничены, `pool_pre_ping` включён.

Repository не выполняет скрытый commit. `PostgresUnitOfWork` владеет одним
Connection и одной короткой транзакцией: без OCR, ADB, UI или network wait.
Atomic counters используют PostgreSQL UPSERT, append commands — bounded unique
idempotency keys и payload digest, current state — row lock + optimistic
version. Same key/same digest является idempotent skip; same key/different
digest возвращает явный conflict.

Formation Surface Fleet хранится как append-only цепочка scan run → snapshot →
шесть slot rows. Каждый успешно распознанный флот фиксируется отдельной короткой
транзакцией; одинаковый состав в другой момент остаётся новым наблюдением.
Application API `FleetStateService` выбирает latest/history по `app_instance` и
явной refresh policy, а PostgreSQL adapters не управляют Formation UI.

Опциональный Fleet AutoScan запускается планировщиком на безопасной границе
перед обычной задачей и использует тот же `Device`, `LazyEngine` и
`FleetStateService`. Настройки `Alas.FleetAutoScan.Mode` и `Fleets` по умолчанию
отключают автосканирование; `daily` определяет календарный день в
`AZURPILOT_POSTGRES_RUNTIME_TIMEZONE`, а `every_start` хранит состояние только
в текущем процессе. Неполный или неудачный скан повторяется не чаще одного раза
в 30 минут. Для политики используются append-only данные schema v1, поэтому
отдельная миграция не требуется.

## Alembic

Alembic — единственный schema-version mechanism. Приложение не вызывает
`metadata.create_all()`. Initial revision создаёт и удаляет namespace только
при явной Alembic-команде. Health check требует PostgreSQL 18 и ровно один
ожидаемый head.

Для явно disposable database:

```text
uv run --locked alembic upgrade head
uv run --locked alembic current --check-heads
uv run --locked alembic check
uv run --locked alembic downgrade base
uv run --locked alembic upgrade head
uv run --locked alembic current --check-heads
uv run --locked alembic check
```

Integration-тесты дополнительно требуют явный test-only opt-in
`AZURPILOT_POSTGRES_DISPOSABLE=1`; без него destructive fixture пропускается.
Этот флаг является только подтверждением оператора и сам по себе не доказывает
изоляцию БД. Destructive Alembic downgrade разрешён лишь для отдельной test-only
БД с учётными данными, недоступными production deployments.
Внешний Alembic entry point дополнительно требует точного совпадения фактических
`HOST`, `PORT`, `DATABASE` и `USER` с отдельными переменными подтверждения
`AZURPILOT_POSTGRES_DISPOSABLE_HOST`, `_PORT`, `_DATABASE` и `_USER`.

Downgrade запрещено применять к пользовательской БД. Stage 2 CI выполняет цикл
только в одноразовом PostgreSQL 18 service container.

## Конфигурация и секреты

Foundation читает structured environment contract:

```text
AZURPILOT_POSTGRES_HOST
AZURPILOT_POSTGRES_PORT
AZURPILOT_POSTGRES_DATABASE
AZURPILOT_POSTGRES_USER
AZURPILOT_POSTGRES_PASSWORD   # optional при PGPASSFILE/libpq credential source
AZURPILOT_POSTGRES_SSLMODE
AZURPILOT_POSTGRES_RUNTIME_TIMEZONE
AZURPILOT_POSTGRES_PGPASSFILE
AZURPILOT_POSTGRES_MIGRATOR_HOST / PORT / DATABASE / USER
AZURPILOT_POSTGRES_MIGRATOR_PASSWORD / SSLMODE / RUNTIME_TIMEZONE / PGPASSFILE
AZURPILOT_WSL_DISTRO
AZURPILOT_WSL_PGPASSFILE
AZURPILOT_POSTGRES_DISPOSABLE          # только для test-only destructive runs
AZURPILOT_POSTGRES_DISPOSABLE_HOST     # точное подтверждение target
AZURPILOT_POSTGRES_DISPOSABLE_PORT
AZURPILOT_POSTGRES_DISPOSABLE_DATABASE
AZURPILOT_POSTGRES_DISPOSABLE_USER
```

В production эти значения хранятся в gitignored `.env`. Owner-loader не
публикует `*_PASSWORD` в process environment: libpq consumers используют
защищённые `PGPASSFILE`, а maintenance-команды отдельно выбирают migrator
contract. `PGPASSWORD` не является постоянным credential transport.

Если `SSLMODE` не задан, foundation проверяет TLS-сертификат и имя сервера
(`verify-full`); режим `require` и более слабые режимы доступны только через
явную настройку среды.

Реальные значения не хранятся в repository, `config/deploy.yaml` или generated
application config. Password исключён из repr; SQLAlchemy URL по умолчанию
маскирует его. Transport/auth/schema diagnostics возвращают только безопасные
typed ошибки без DSN, SQL и внутренних DBAPI сообщений.

## Production cutover

Stage 3 read-only parsers, importer и reconciliation остаются offline-only.
Production marker, роли, backup/restore, lifecycle и forward-fix policy описаны
в `postgresql-production-cutover.md`. Runtime не создаёт role/database, не
выполняет DDL и не меняет HBA.
