# Проверки и критерии готовности

Этот файл — единственный владелец общей матрицы проверок и критериев готовности.
Проверки выбираются по фактическому diff и изменённой архитектурной границе;
соседние документы и skills только направляют сюда, а не создают вторую матрицу.

## Нулевая проверка

До изменения файлов выполнить только дешёвый минимальный preflight:

- подтвердить репозиторий, целевую ветку и base SHA;
- подтвердить, что рабочая среда соответствует заявленному checkout, а пользовательские изменения не будут затронуты; для штатной последовательной разработки используется основной checkout;
- проверить инструменты, без которых нельзя начать именно эту задачу.

Остальные capabilities проверяются **лениво, непосредственно перед первым gate, которому они нужны**:

- GitHub push/PR/checks/merge — перед соответствующей GitHub-операцией;
- Windows-native runtime checks — только если затронут соответствующий Python
  adapter или Windows integration;
- secret/security scanner — перед соответствующим verification checkpoint;
- browser/GUI/emulator/game — только если изменение реально требует такого acceptance;
- production/network capabilities — только перед production/network gate.

Не тратить начало задачи на доказательство доступности будущих инструментов, которые могут вообще не понадобиться. Если обязательный gate оказался недоступен в момент, когда он действительно нужен, результат получает статус `blocked`: Codex не выдаёт непроверенный артефакт как готовый и не перекладывает рутинный запуск на пользователя.

Постоянная проблема среды должна устраняться в bootstrap/setup или canonical project runner, а не диагностироваться заново в каждой feature-задаче. Не устанавливать и не перенастраивать глобальные инструменты «на всякий случай».

## Постоянный CI

Единственный постоянный pull-request workflow — `.github/workflows/ci.yml`. Он
запускается для каждого pull request независимо от target/base branch и без
`paths`-фильтров. Для защищённой `personal/stable` ruleset делает required три
устойчивых context:

- `Python`;
- `Windows`;
- `Security`.

Исторические номера этапов, committed evidence, stage-specific baselines и временные migration gates не являются постоянными quality gates. Подробный фактический контракт, локальные эквиваленты и правила изменения CI находятся в `docs/ci.md`.

Локально использовать именно repository-defined команды/runner из `docs/ci.md`; не реконструировать CI environment вручную, если проект уже предоставляет канонический способ запуска.

## Режимы

### Fast-track

Для документации, опечатки и очевидного локального изменения без control flow.

Минимум:

- проверить целевой файл и контекст;
- минимальный diff;
- format/syntax;
- итоговый diff;
- secret scan перед публикацией изменения.

### Стандартный

Для обычного fix/feature в одной известной подсистеме.

Дополнительно:

- проследить релевантные вызовы;
- проверить аналогичную реализацию;
- запустить точечные tests;
- проверить сквозное поведение в разумной границе;
- выполнить самостоятельную проверку итогового diff;
- внешнюю проверку запускать только по явному запросу или обязательному правилу проекта.

### Расширенный

Для:

- upstream/master/personal stable;
- Start/Update/Repair/Build;
- Python/dependencies/uv.lock;
- device/input/OCR/combat/Operation Siren;
- MCP/security/privacy;
- production/data migration;
- нескольких подсистем.

Требует архитектурного анализа, подходящего набора проверок, самостоятельной проверки изменений и явного описания рисков. Внешние проверки нужны только по явному запросу или обязательному правилу проекта.

## Внешняя проверка

Codex самостоятельно проверяет каждое изменение. Внешнего проверяющего, включая
CodeRabbit, запускай только по явной команде пользователя или обязательному
правилу проекта. Незапрошенный CodeRabbit остаётся в состоянии `NOT_RUN` и не требует попытки
или отметки об ограничении. Если проверка запрошена, соответствующий навык описывает запуск,
подтверждение точного коммита и разбор результатов; значимые замечания нужно устранить до готовности.

После заморозки MCP-relevant candidate выполни один `azur mcp sync --base
<exact-base-sha>`. Он сам классифицирует committed и working-tree изменения;
`NO_CHANGES` — terminal no-op, а `SYNCED` подтверждает base-aware generated
bundle, безопасное восстановление owned runtime и fresh-client acceptance.
Не собирай normal gate из отдельных `impact`, `reconcile`, `status` и `accept`.
Изменение MCP source-set инвалидирует предыдущий sync; повторный sync
пересчитывает SemVer от exact base и текущего candidate. Foreign/unknown
ownership, port conflict, ошибка stop/start или нарушенный postcondition
остаются fail-closed. Состояние текущей внешней Codex session не является
postcondition; hot reload не предполагается.

Codex effective registration — отдельная необязательная integration check. Если
изменение затрагивает Codex/plugin registration, client-visible tool schema или
routing, её можно выполнить через [единый контракт cross-thread continuation](../../.agents/skills/azurpilot-repository-development/references/cross-thread-task-delegation.md).
Wrong-HEAD, недоступный `create_thread` или другой platform failure фиксируется
в этой check как external Codex limitation и не переводит успешный MCP client
gate в `FAIL`/`BLOCKED_PRECONDITION`.

## Pre-merge и post-merge outcomes

Pre-merge Definition of Done заканчивается после commit/push draft PR, проверки
required `Python`, `Windows`, `Security` на exact head, secret scan, self-review
и обработки явно запрошенных или обязательных замечаний. Итоговый статус —
`READY_FOR_CHATGPT_REVIEW`: финальное ревью выполняет пользователь, а merge не
выполняется без отдельной текущей команды пользователя.

Типизированная оценка готовности отдельно учитывает реализацию, обязательные проверки и запрошенную внешнюю
проверку, `READY_FOR_CHATGPT_REVIEW` и готовность к слиянию. `NOT_RUN` без явного запроса —
нормальное состояние, а не ограничение. Обязательная проверка имеет
terminal state `PASS`, `FAIL`, `BLOCKED_PRECONDITION` или `NOT_REQUIRED`; при
`MCP impact=REQUIRED` `fresh_mcp_client_acceptance` обязателен и `NOT_REQUIRED`
для него недопустим; `PASS` требует evidence независимой свежей MCP client
session;
`FAIL`/`BLOCKED_PRECONDITION` сохраняет полезный Draft, но задаёт общий итог `BLOCKED`
и не допускает готовность к слиянию. Результат запрошенного CodeRabbit
фиксируется отдельно и сам по себе не заменяет обязательные продуктовые проверки. Проверка регистрации Codex
хранится отдельно и сама по себе не блокирует готовность продукта.

Post-merge verification и cleanup являются отдельным этапом и выполняются только
после подтверждённого merge. Перед ним нужно повторно проверить exact head,
required CI, relevant diff и review blockers. Успешный CI или CodeRabbit сам по
себе не является разрешением на merge.

## Итерации и заморозка candidate

При ошибке сначала повторяй конкретный failing test/node; после fix запускай
только затронутую дешёвую проверку. Subsystem suite запускай один раз после
стабилизации области, а полный relevant suite — один раз на замороженном
candidate. Существенный code/config diff после него требует повторного gate;
изменение только PR body/report не требует. При изменении текста команды,
diagnostic или документа сначала обнови связанные literal/contract assertions.
Для lint используй repository-defined canonical gate, а не bare `ruff check .`.
Исследование можно остановить после определения owner, call path, ближайшего
контракта/tests и root cause. Пока CI выполняется, заверши независимую проверку
diff/report; не опрашивай CI часто, а при сбое сразу исследуй конкретный job.

## Типовая матрица

### Python

- `uv lock --check` и repository-defined locked sync для постоянного CI;
- compile/import затронутого модуля;
- существующий ruff-профиль;
- точечные tests во время реализации;
- полный связанный набор один раз перед PR/финальным checkpoint, если после него не было существенного code diff;
- generator check;
- чистое рабочее дерево после генераторов.

Не повторять полный suite после каждого небольшого fix, если targeted checks покрывают изменённую область. После PR не дублировать локально тот же полный CI без причины: доверять exact-head required checks, а локальный повтор делать при диагностике падения или существенном post-CI изменении.

### Game resource evidence

- dashboard `game_get_resources` явно помечен как snapshot/history-derived
  projection и сохраняет `Dashboard.<resource>.Record` в `last_update`;
- неизвестный или старый timestamp не считается current state;
- обязательный live gate использует fresh current observation, а не
  dashboard snapshot;
- displayed Oil `limit`/`MAX` не проверяется как hard storage cap: `25000` при
  `17050` является допустимым структурным состоянием.

### Конфигурация

- генератор;
- отсутствие неожиданного generated diff;
- загрузка старого config;
- migration idempotency;
- ru-RU keys/placeholders;
- текущий EN runtime; унаследованные варианты других регионов проверяются только при явной задаче совместимости.

### Распознавание

- положительные screenshots;
- отрицательные screenshots;
- thresholds;
- переходные кадры;
- варианты темы; fixtures других регионов — только при явной задаче совместимости;
- range validation OCR.

Для UI-driven Formation/Fleet scanner дополнительно проверять:

- одиночный переходный detector-positive кадр не запускает физический scanner;
- открытие Info требует ограниченной последовательности свежих подтверждений состояния;
- закрытие Info требует устойчивой границы Formation до выбора следующего флота;
- scanner-layer exception сохраняет физическую диагностику слоя и типа;
- структурный `complete == False` остаётся отдельным результатом распознавания и не превращается в physical failure;
- recoverable continuation разрешён только после доказанного восстановления детерминированного UI состояния;
- при неизвестном UI состоянии batch останавливается fail-closed;
- `failed_fleet_index` и итоговые `PARTIAL`/`FAILED` не допускают false success после физического сбоя.

Если production-изменение затрагивает сам переход между флотами, после automated gates нужен один контролируемый реальный device acceptance полного диапазона Surface Fleet 1..6. Не повторять несколько эквивалентных ручных прогонов без нового evidence или существенного code diff.

Реальные device/OCR acceptance и benchmarks выполняются локальными инструментами из `tools/acceptance/` и `tools/benchmarks/`. Они не становятся required checks каждого PR без отдельного устойчивого обоснования.

### Runtime localization integrity

Общий pytest suite запускает `tests/contracts/localization/test_runtime_russianization_audit.py`. Тест выполняет permanent semantic audit текущих production consumer sites и Global/EN identity, а self-tests обязаны доказывать обе стороны контракта:

- FAIL: CJK operator prose, обычное непереведённое английское предложение, locale/server/package/assets/OCR alias другого региона;
- PASS: русский контекст, ADB/OCR/API/URL/path/package/game identifiers, deferred exception text и feature structure вне display sink.

Для explicit translation PR этот guard дополняет, но не заменяет dynamic base→head structural gate. Для feature/bugfix/refactor structural parity не применяется, permanent integrity остаётся обязательной частью обычных product tests.

### Windows Python tooling

- `azur` CLI и JSON envelope на Windows;
- lifecycle, update, repair, build, shortcut и Docker capability checks через
  Python services;
- идемпотентный повторный запуск там, где идемпотентность является контрактом.

### WebUI

- импорт/создание app;
- endpoint/unit tests;
- lifecycle smoke;
- Windows process semantics;
- автоматизированная DOM/security-проверка через browser runner;
- visual acceptance только если она обязательна для конкретного изменения и доступна безопасная среда.

### Production PostgreSQL

- PostgreSQL 18, exact Alembic head и authenticated app health;
- application service/repository integration и atomic concurrency;
- marker absent/corrupt/sqlite и outage fail-closed без fallback;
- create-only migration валидного marker в `config/state/`, rejection повреждённого legacy marker и отсутствие runtime-state JSON в profile discovery;
- `.env`/passfile contract, distinct app/migrator secrets и old-credential negative auth;
- app DML положительно, DDL/role/database отрицательно;
- Start/Update/Repair/Build ownership и Windows Python tooling gates;
- final import, repeat zero-delta, dump/list, scratch restore и reconciliation;
- после canary legacy `.db` и canonical CSV не создаются повторно.

Production/network acceptance выполняется после реализации и локальной верификации, а не как общий preflight каждой задачи.

## Secret scan

Перед commit/PR и перед merge, если после последнего scan менялся relevant diff:

- staged/final diff;
- новые архивы и binaries;
- `.env`, config, logs, dumps, backups;
- API tokens, webhooks, credentials, cookies;
- device/user identifiers.

Обязателен фактически запущенный secret scanner. Ручной паттерн-аудит может быть только дополнительной проверкой и не заменяет scanner. Если scanner недоступен в момент обязательного gate, задача блокируется.

Постоянный job `Security` проверяет текущие исходники и релевантный диапазон коммитов PR. Диагностика должна редактировать секреты и загружаться только при падении.

## Git delivery и PR publication

Normal publication — один вызов `azur delivery publish --message ...` с
необязательными `--path` для явного scope. Tooling сам строит закрытый typed
manifest в памяти из exact repository/base/head/remote и snapshot выбранных
путей. Внешний JSON manifest и предварительный `validate MANIFEST` не нужны.
Read-only `validate MANIFEST` остаётся диагностикой. Publish добавляет только
зафиксированный allowlist, подтверждает staged scope, выполняет scoped Gitleaks
по index и exact committed range, создаёт commit с заданным message, делает
обычный explicit push без force/force-with-lease и после него проверяет exact
remote SHA. Timeout или неизвестный push переводится в journal и read-only
`recover`; blind retry запрещён. По умолчанию в один coherent commit входят все
изменённые candidate paths, в том числе актуальные generated MCP artifacts.

До staging сервис сохраняет candidate raw SHA/size postimage и его Git-clean
blob representation, а после staging повторно сравнивает обе формы с manifest и
index. Изменение target в этом окне останавливает delivery fail-closed. Для
delete index existence проверяется отдельным bounded запросом: только доказанное
отсутствие считается успехом, ошибка/timeout/truncation считается неизвестным
состоянием. Publication remote и `base_remote_name` оба проходят canonical
repository identity check; совпавший SHA неправильного remote не принимается.

Human `delivery validate` показывает bounded Rich `Delivery Package` и список
изменений `A`/`M`/`D`, заканчивая строкой `Изменения не применены.`. Его
`--json` counterpart остаётся одним strict envelope с полными SHA, target count,
change information и typed evidence без ANSI или human diagnostics.

Для draft PR обязательны explicit repository/base/head identity, exact local и
remote SHA, typed structured body и публикация через временный внешний файл с
`--body-file`. После provider call выполняется read-back PR identity и полный
body digest. После ambiguous/timeout/unknown `edit` mutation read-back выполняется
до любой дальнейшей классификации, а blind retry запрещён. Provider mismatch,
cross-repository PR, duplicate candidate или неподтверждённый create являются
blocking failure.
Body должен быть подробным русскоязычным отчётом: цель, область и границы,
подсистемы/файлы, фактическая реализация, локальные/live-проверки, exact-head
CI, security/secret scan, CodeRabbit disposition, rollback/migration и
ограничения. Короткие общие абзацы без фактов и маркированных списков не
принимаются renderer-ом.

Только если diff затрагивает `azur delivery`, `azur pr`, общий контракт CLI/
tooling или семантику публикации, приёмка включает человекочитаемый вызов и
вызов для агента с `--json`. JSON обязан содержать ровно один закрытый
результирующий конверт. Для несвязанного combat/OCR/documentation-исправления
этот CLI-gate не применяется. Эта живая проверка не заменяет CI-контексты
`Python`, `Windows`, `Security` на точном head PR.

## Критерии готовности

### Pre-merge `READY_FOR_CHATGPT_REVIEW`

- правильная ветка и base SHA;
- минимальный связный diff;
- архитектурные границы соблюдены;
- generated-файлы согласованы;
- все **релевантные** локальные gates выполнены;
- required checks `Python`, `Windows`, `Security` зелёные на exact head;
- на exact head отсутствуют старые параллельные evidence workflow;
- упавшие проверки исправлены и повторены в затронутой области;
- полный suite не повторялся без существенного изменения или диагностической причины;
- secret scan выполнен на финальном relevant diff;
- Codex adversarial self-review завершён;
- явно запрошенные или обязательные внешние проверки обработаны;
- незапрошенный CodeRabbit остаётся в состоянии `NOT_RUN`, а не считается ограничением;
- security review завершён в требуемом объёме;
- открытые blocking review threads отсутствуют;
- документация обновлена;
- draft PR создан или обновлён и содержит актуальный scope, base SHA, gates и ограничения;
- PR ожидает финального пользовательского ревью;
- дальнейший Git/PR lifecycle определяется только `GIT-WORKFLOW.md`;
- ограничения перечислены;
- от пользователя не требуется рутинных технических действий.

### После подтверждённого merge

Для уже слитой задачи дополнительно обязательны:

- проверка фактического merged head;
- относящиеся к изменению проверки после merge;
- подтверждение отсутствия новой регрессии в затронутой области.

Правила разрешения merge и cleanup принадлежат `GIT-WORKFLOW.md`.
