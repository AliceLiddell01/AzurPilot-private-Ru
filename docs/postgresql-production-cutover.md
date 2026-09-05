# Production PostgreSQL и cutover

## Единственный runtime backend

Для доменов schema v1 единственным production-хранилищем является PostgreSQL
18 в WSL Archlinux. `module.application.runtime_storage` определяет DTO,
команды, запросы и типизированные ошибки. `module.persistence.runtime` —
единственная process-safe точка сборки ленивого per-PID engine, Unit of Work и
сервиса. Игровые модули, статистика и WebUI не импортируют SQLAlchemy, Psycopg
или Alembic.

`config/state/storage_backend.json` — атомарный non-secret marker в выделенном
runtime-state namespace. Он обязателен,
разрешает только `postgresql`, фиксирует schema head, SHA-256 итогового
reconciliation report, reviewed head, merge commit и IANA timezone. Отсутствующий, повреждённый, SQLite или
несовместимый marker останавливает runtime. Наличие legacy `.db` ничего не
переключает; fallback и dual-write отсутствуют.

Валидный legacy `config/storage_backend.json` переносится create-only hard-link
migration после полной проверки содержимого. Повреждённый legacy marker не
переносится и может быть выведен из эксплуатации только guarded cutover-командой
после нового READY reconciliation с exact SHA-256 recovery guard. После успешной
migration root-level alias не остаётся, поэтому marker не попадает в исторический
namespace игровых профилей `config/*.json`.

## Матрица production-вызовов

| Домен | Вызовы | Команда/запрос и транзакция | Ошибка и проверка |
|---|---|---|---|
| Monthly CL1/Meow | `opsi_runtime`, `opsi_month`, `os.map` | атомарный UPSERT и детерминированная месячная проекция в короткой UoW | storage error проходит наверх; concurrency и projection tests |
| AP/Akashi | `log_res`, `os_shop.shop` | append snapshot/purchase и counter в одной UoW; aware UTC с календарём runtime timezone | idempotency/conflict и readback |
| Валюта | `os_status`, `hazard_leveling` | согласованный append пары монет; unchanged pair пропускается | ordering и отсутствие нулевого fallback |
| Commission | `commission`, `commission_income_stats` | parent и children атомарны; месячные границы переводятся в UTC | rollback и day/week/month parity |
| Meow/Siren | `opsi_runtime`, `app_stat_opsi`, simulator | typed timing/event/aggregate; неизвестный hazard блокирует запись | exact hazard, deterministic limits |
| Ресурсы | `log_res`, `resource_stats` | append-only typed snapshot | ordered bounded timeline |
| Opsi items | `azurstats`, WebUI export | OCR завершается до UoW; затем bounded append событий и query | PostgreSQL failure не становится пустым CSV |
| AP notification | `scheduling` | versioned current state в короткой UoW | write/readback и outage fail-closed |

OCR, ADB, UI, sleep и network wait не входят в DB transaction. Все новые
timestamps записываются как aware UTC; календарные месяцы и операторские
периоды вычисляются в timezone marker. Identity сначала разрешается по точному
digest alias Stage 3, затем сверяется со стабильным UUID provenance.

## Роли, сеть и credentials

Production contour:

- `azurpilot_owner` — `NOLOGIN`, владелец database/schema/objects;
- `azurpilot_migrator` — отдельный `LOGIN`, член owner для Alembic и restore;
- `azurpilot_app` — `LOGIN` только с CONNECT, USAGE и необходимыми DML/sequence
  privileges.

Все роли — без SUPERUSER, CREATEDB, CREATEROLE, REPLICATION и BYPASSRLS.
Listener остаётся на loopback. TCP HBA для app/migrator использует
`scram-sha-256`; unrestricted `trust` запрещён. После изменения HBA обязательны
backup exact file, `pg_hba_file_rules.error IS NULL`, reload, положительный и
отрицательный auth tests.

Пароли не находятся в repository, marker, argv или журналах. Локальный `.env`
является user-owned источником полного PostgreSQL contract и никогда не
коммитится. Единый loader экспортирует только несекретные параметры и
`PGPASSFILE`; app и migrator secrets остаются в защищённых Windows/WSL libpq
passfiles. Migrator выбирается только maintenance-командами. ACL проверяется
фактически; логи и typed errors не содержат DSN, SQL или raw DBAPI diagnostics.

## Lifecycle

- `Start-AzurPilot.ps1` будит только exact Archlinux и выполняет PostgreSQL
  preflight только после подтверждения, что текущий Start действительно будет
  запускать backend. Уже активная до запуска служба считается внешней и не
  останавливается. Если текущий Start поднял подготовленную службу из
  `inactive`/`failed`, он останавливает её после завершения backend и до
  освобождения lifecycle ownership. Повторный Start, который только открывает
  существующий WebUI, PostgreSQL не трогает. Marker, app auth и head проверяются
  до запуска GUI.
- `Update-AzurPilot.ps1` после graceful stop создаёт новый `pg_dump -Fc`, затем
  применяет reviewed Alembic код отдельным migrator и проверяет app health.
  Ошибка backup блокирует update; автоматического pruning нет.
- `Repair-AzurPilot.ps1` диагностирует WSL/service/auth/head, loopback listener,
  SCRAM и разобранные HBA rules; он не меняет HBA, роли, database или пароль.
- `Build-AzurPilot.ps1` готовит checkout и зависимости, но не provision и не
  мигрирует production data.

`dev_tools.postgresql_runtime` содержит только bounded health, backup и upgrade.
Финальный production import использует `postgresql_migration full-cutover` с
точным environment guard. `dev_tools.postgresql_cutover` создаёт marker только
из нового report с `cutover_ready=true`, пустыми reason codes, успешным app
health и точной строкой подтверждения.
`dev_tools.postgresql_credentials` ротирует app/migrator SCRAM secrets только
после проверки внешнего custom dump и role contract; пароли передаются `psql`
через stdin, а новые и старые credentials проходят positive/negative auth tests.

## Maintenance cutover

1. Зафиксировать reviewed commit/head, target и abort criteria; остановить все
   известные writers и доказать bounded drain.
2. Создать вне repository новый restricted legacy archive и byte-consistent
   backup. Manifest включает exact logical paths, размеры, SHA-256, SQLite
   integrity/schema fingerprint, WAL/SHM state и только sanitized counts.
3. Создать pre-cutover custom dump и проверить `pg_restore --list`.
4. Применить Alembic exact head от migrator. Выполнить final import,
   reconciliation и repeat zero-delta из captured source.
5. Создать post-import dump, восстановить его в отдельную scratch database и
   повторить head/reconciliation. Только READY-report разрешает marker.
6. Установить reviewed stable code. Canary проверяет startup health, безопасный
   synthetic write/readback, concurrent counter, resource/CL1/Opsi/commission/
   Meow/Siren projections, WebUI export, notification state и restart.
7. После полного smoke создать post-canary dump и зафиксировать sanitized
   first/last watermark. Затем поштучно переместить активные legacy-файлы в
   restricted immutable archive и доказать отсутствие их пересоздания.

До первого PostgreSQL write cutover можно abort без marker. После первого write
автоматический rollback на SQLite запрещён: writers останавливаются, создаётся
post-failure dump, а исправление выполняется только forward. Archive остаётся
evidence и никогда не является live backend.

## Backup, restore и ограничения импорта

Backup имеет create-only timestamped имя вне repository, custom format,
ограниченный ACL и bounded timeout. Проверка включает `pg_restore --list` и
фактический restore в отдельную scratch database. PITR/WAL archiving и
автоматическое удаление backup находятся вне scope.

WebUI отклоняет любой `.db` до чтения содержимого. Legacy SQLite разрешён только
offline importer из `module.persistence.legacy` при остановленных writers.
Статистический CSV создаётся только явным export и не является источником
истины. Физическое уничтожение archive требует отдельного решения владельца.

## Безопасная диагностика

Операторские причины ограничены категориями: marker отсутствует/повреждён,
служба недоступна, аутентификация отклонена, schema head несовместим, backup или
reconciliation не подтверждены. Raw HBA, credentials, DSN, абсолютные archive
paths и пользовательские значения не публикуются.
