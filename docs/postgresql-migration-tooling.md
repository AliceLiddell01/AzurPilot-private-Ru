# Offline migration tooling PostgreSQL

## Offline-граница Stage 3 и production-режим Stage 4

Stage 3 добавляет только автономный конвейер чтения legacy-хранилищ,
транзакционного импорта в заранее подготовленную PostgreSQL schema v1 и
reconciliation. Stage 4 использует тот же importer для final maintenance
cutover, не превращая его в runtime dependency. Production entry points никогда
не открывают legacy SQLite.

Конвейер разделён на три слоя:

- `module.application.migration_*` — модели, порты, orchestration и readiness;
- `module.persistence.legacy` — строго read-only и path-bounded adapters SQLite,
  CL1 JSON и derived CSV;
- `module.persistence.migration_target` — PostgreSQL transactions, import ledger,
  idempotency и проверка фактических domain rows.

Ни один импорт этих модулей не создаёт подключение, DDL или production
singleton. Legacy SQLite открывается через URI `mode=ro&immutable=1`, с
`query_only`, schema fingerprint и `integrity_check`. Для согласованной
репетиции SQLite копируется штатным backup API, остальные bounded-файлы — только
после проверки стабильного SHA-256. Symlink/path escape отклоняется.

## Семантика переноса

Поддерживаются доказанные Stage 1 семейства: CL1 monthly aggregates, Akashi AP,
AP/currency snapshots, commission parent/items, Meow samples/aggregates,
Siren stats/events, AP notification state, `resource_snapshots` с 12 typed
resource columns и `opsi_items` one-to-one. Aggregate не превращается в
events, отсутствующая история не достраивается, пустая таблица остаётся пустой.

Identity сопоставляется только по exact profile evidence. Неоднозначное имя
получает стабильный unresolved alias digest; raw instance/device identifiers не
становятся domain key и не включаются в report. Device ID используется только
как явно переданный offline decryption provenance. Naive timestamp получает
обязательную IANA timezone; ambiguous/nonexistent local time отклоняется.

Import batch имеет deterministic manifest key. Каждый bounded chunk выполняется
в отдельной transaction; commission parent, children и ledger атомарны.
Повтор `(source object, locator, digest)` пропускается, другой digest для того же
locator является hard conflict. Failed batch можно продолжить тем же manifest;
completed batch даёт нулевую дельту.

## CLI

Единая точка входа:

```text
uv run python -m dev_tools.postgresql_migration \
  --source-root <explicit-root> \
  --legacy-timezone <IANA-zone> \
  --report <new-temporary-json> \
  inspect|import|reconcile|full-rehearsal|full-cutover
```

`--report` обязателен и создаётся только как новый файл; существующий файл не
перезаписывается. В stdout выводится только bounded status/reason summary, а не
factual report. `inspect` не требует PostgreSQL и не пишет source. Остальные режимы читают
структурные `AZURPILOT_POSTGRES_*` settings; password не принимается в argv.
Report нельзя создавать непосредственно как `config/<name>.json`: корень
`config/*.json` является profile namespace и такой target отклоняется до записи.
Для локального persistent state используется `config/state/`, а фактические
cutover reports по-прежнему предпочтительно хранить во внешней Stage-owned
директории.
`full-rehearsal` дополнительно требует `--scratch-database`,
`AZURPILOT_POSTGRES_DISPOSABLE=1` и точное совпадение guard-переменных host,
port, database, user и scratch database. `pg_dump`/`pg_restore` берутся из PATH
или `AZURPILOT_PG_DUMP`/`AZURPILOT_PG_RESTORE`.

`import` и `reconcile` — диагностические offline-команды: они намеренно не
подтверждают dump/restore и поэтому завершаются `STATUS:NOT_READY` с кодом `4`.
`full-cutover` сначала загружает локальный `.env` через
`load_local_postgres_environment(role="migrator")`: migrator-контракт заменяет
канонические `AZURPILOT_POSTGRES_*` settings для maintenance-подключения.
Команда требует `AZURPILOT_POSTGRES_CUTOVER=1`, точных guard-значений
`AZURPILOT_POSTGRES_CUTOVER_HOST`, `AZURPILOT_POSTGRES_CUTOVER_PORT`,
`AZURPILOT_POSTGRES_CUTOVER_DATABASE` и
`AZURPILOT_POSTGRES_CUTOVER_USER=azurpilot_migrator`, а также
`AZURPILOT_POSTGRES_CUTOVER_SCRATCH_DATABASE` и
`--confirm FINAL-PRODUCTION-CUTOVER`. Он формирует READY только после
import, repeat zero-delta, dump/list, restore и restored reconciliation. Сырой stderr
PostgreSQL utilities намеренно подавляется, потому что может содержать DSN,
локальные пути или значения окружения; наружу возвращается только bounded code.

Exit codes: `0` — ready, `2` — legacy/source/guard error, `3` — безопасно
классифицированная storage error, `4` — reconciliation завершён, но not-ready.
CLI не печатает DSN, credentials, raw payload, raw identity или абсолютные
source paths.

## Reconciliation report

Детерминированный JSON содержит logical manifest, размеры и SHA-256, SQLite
integrity/schema fingerprints, source/target dataset counts, safe
identity-digest/month counts, scalar sums/ranges, timestamp ranges, resource
NULL matrix, commission parent/item totals, import delta, coverage, unresolved
identities, PostgreSQL major, Alembic head, semantic parity, repeat zero-delta,
dump/restore parity и bounded reason codes.

Semantic parity сверяет import ledger и реальные строки domain tables, включая
commission children. Report не содержит raw identifiers, DSN/user/password,
credential paths, decrypted CL1 payload, абсолютные пользовательские paths или
произвольные DBAPI/SQL exceptions. Report по фактическим данным хранится только
во временной Stage-owned директории и удаляется после извлечения безопасной
сводки.

## Проверка и CI

Job `Python` использует pinned PostgreSQL 18 service, выполняет Alembic
`base → head → base → head`, sanitized fixtures, full importer, conflicts,
rollback/retry, repeat zero-delta и реальный custom-format dump/restore в
отдельную scratch database. Затем полный `pytest`, generators и clean-tree
contract остаются обязательными. Windows job проверяет read-only/path/encoding
и CLI contracts без требования Docker Desktop; authoritative security gate —
существующий GitHub Actions job `Security`.

Локальная rehearsal никогда не является cutover. Разрешён только точно
помеченный disposable PostgreSQL 18 target; перед удалением container, volume и
database повторно проверяются exact names и ownership labels.
