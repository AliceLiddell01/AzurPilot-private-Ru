# PostgreSQL Storage Foundation

## Граница владения

Stage 2 добавляет неиспользуемую production runtime, но исполняемую foundation:

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
`module/persistence/` не импортирует SQLite и не открывает соединение при
импорте. Существующие `module/statistics/` и WebUI/MCP consumers продолжают
использовать прежние SQLite/JSON пути. Silent fallback между backend запрещён.

## Schema v1

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
```

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
```

Реальные значения не хранятся в repository, `config/deploy.yaml` или generated
application config. Password исключён из repr; SQLAlchemy URL по умолчанию
маскирует его. Transport/auth/schema diagnostics возвращают только безопасные
typed ошибки без DSN, SQL и внутренних DBAPI сообщений.

## Границы следующих этапов

Stage 3 реализует read-only legacy parsers, importer и reconciliation. Stage 4
владеет provisioning, backup, maintenance cutover и переключением production
consumers. До Stage 4 новая foundation не читает пользовательские SQLite/JSON,
не создаёт role/database на startup, не делает dual-write и не меняет WSL HBA.
