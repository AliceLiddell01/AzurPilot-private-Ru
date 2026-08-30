# Проверки и Definition of Done

## Нулевая проверка

До изменения файлов выполнить только дешёвый минимальный preflight:

- подтвердить репозиторий, целевую ветку и base SHA;
- подтвердить, что рабочая среда соответствует заявленному checkout, а пользовательские изменения не будут затронуты; для штатной последовательной разработки допустим основной checkout `C:\AzurPilot`;
- проверить инструменты, без которых нельзя начать именно эту задачу.

Остальные capabilities проверяются **лениво, непосредственно перед первым gate, которому они нужны**:

- GitHub push/PR/checks/merge — перед соответствующей GitHub-операцией;
- PowerShell Parser/PSScriptAnalyzer — перед проверкой затронутого PowerShell;
- secret/security scanner — перед соответствующим verification checkpoint;
- browser/GUI/emulator/game — только если изменение реально требует такого acceptance;
- production/network capabilities — только перед production/network gate.

Не тратить начало задачи на доказательство доступности будущих инструментов, которые могут вообще не понадобиться. Если обязательный gate оказался недоступен в момент, когда он действительно нужен, результат получает статус `blocked`: Codex не выдаёт непроверенный артефакт как готовый и не перекладывает рутинный запуск на пользователя.

Постоянная проблема среды должна устраняться в bootstrap/setup или canonical project runner, а не диагностироваться заново в каждой feature-задаче. Не устанавливать и не перенастраивать глобальные инструменты «на всякий случай».

## Постоянный CI

Единственный постоянный pull-request workflow — `.github/workflows/ci.yml`. Он должен запускаться для каждого PR в `personal/stable` без `paths`-фильтров и публиковать три устойчивых context, которые ruleset обязан сделать required:

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
- выполнить Codex self-review итогового diff до внешнего review checkpoint.

### Расширенный

Для:

- upstream/master/personal stable;
- Start/Update/Repair/Build;
- Python/dependencies/uv.lock;
- device/input/OCR/combat/Operation Siren;
- MCP/security/privacy;
- production/data migration;
- нескольких подсистем.

Требует архитектурного анализа, полного релевантного набора проверок, Codex self-review, промежуточных external-review checkpoints на завершённых рискованных слоях и явных рисков.

## Review checkpoints

Внешнее ревью не откладывается обязательно до самого конца и не запускается после каждого мелкого исправления.

Для стандартной/расширенной задачи:

1. Codex завершает логически цельный слой реализации.
2. Запускает релевантные targeted checks.
3. Перечитывает base→head diff как незнакомое изменение и выполняет adversarial self-review.
4. Если завершён существенный или рискованный слой — запускает внешний review checkpoint.
5. Исправления после внешнего finding проходят self-review и targeted checks.
6. Новый внешний review нужен, если после прошлого checkpoint появился существенный новый code diff, изменился контракт/архитектура/безопасность или предыдущий reviewer явно требует повторной проверки.
7. Незначительные правки документации, тестовых ожиданий или механические fixes сами по себе не запускают полный внешний review заново.

Если обязательный внешний reviewer упёрся в rate limit/cooldown, **не ждать cooldown внутри активного прогона**. Сохранить состояние и завершить текущий прогон как ожидающий review; продолжение выполняется новым прогоном после доступности reviewer.

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

### Конфигурация

- генератор;
- отсутствие неожиданного generated diff;
- загрузка старого config;
- migration idempotency;
- ru-RU keys/placeholders;
- server variants.

### Распознавание

- положительные screenshots;
- отрицательные screenshots;
- thresholds;
- переходные кадры;
- server/theme variants;
- range validation OCR.

Для UI-driven Formation/Fleet scanner дополнительно проверять:

- одиночный переходный detector-positive кадр не запускает физический scanner;
- открытие Info требует ограниченной последовательности свежих подтверждений состояния;
- закрытие Info требует устойчивой Formation boundary до выбора следующего флота;
- scanner-layer exception сохраняет физическую stage/type диагностику;
- структурный `complete == False` остаётся отдельным результатом распознавания и не превращается в physical failure;
- recoverable continuation разрешён только после доказанного восстановления детерминированного UI состояния;
- при неизвестном UI состоянии batch останавливается fail-closed;
- `failed_fleet_index` и итоговые `PARTIAL`/`FAILED` не допускают false success после физического сбоя.

Если production-изменение затрагивает сам переход между флотами, после automated gates нужен один контролируемый реальный device acceptance полного диапазона Surface Fleet 1..6. Не повторять несколько эквивалентных ручных прогонов без нового evidence или существенного code diff.

Реальные device/OCR acceptance и benchmarks выполняются локальными инструментами из `tools/acceptance/` и `tools/benchmarks/`. Они не становятся required checks каждого PR без отдельного устойчивого обоснования.

### Runtime localization integrity

Общий pytest suite запускает `tests/test_runtime_russianization_audit.py`. Тест выполняет permanent semantic audit текущих production consumer sites и Global/EN identity, а self-tests обязаны доказывать обе стороны контракта:

- FAIL: CJK operator prose, обычное untranslated English предложение, foreign locale/server/package/assets/OCR alias;
- PASS: русский контекст, ADB/OCR/API/URL/path/package/game identifiers, deferred exception text и feature structure вне display sink.

Для explicit translation PR этот guard дополняет, но не заменяет dynamic base→head structural gate. Для feature/bugfix/refactor structural parity не применяется, permanent integrity остаётся обязательной частью обычных product tests.

### PowerShell

- Parser через фактический `pwsh` для tracked затронутых `.ps1`/`.psm1` и для полного набора, если этого требует CI;
- PSScriptAnalyzer зафиксированной версии как обязательный gate;
- статический аудит правил;
- disposable smoke для изменённой Git-логики;
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
- Start/Update/Repair/Build ownership и PowerShell gates;
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

## Definition of Done

- правильная ветка и base SHA;
- минимальный связный diff;
- архитектурные границы соблюдены;
- generated-файлы согласованы;
- все **релевантные** локальные gates выполнены;
- required checks `Python`, `Windows`, `Security` зелёные на exact head;
- на exact head отсутствуют старые параллельные Stage/evidence workflow;
- упавшие проверки исправлены и повторены в затронутой области;
- полный suite не повторялся без существенного изменения или диагностической причины;
- secret scan выполнен на финальном relevant diff;
- Codex adversarial self-review завершён;
- внешний reviewer прошёл необходимые milestone/final checkpoints;
- security review завершён в требуемом объёме;
- открытые blocking review threads отсутствуют;
- документация обновлена;
- post-merge verification завершён для слитой задачи;
- ограничения перечислены;
- от пользователя не требуется рутинных технических действий.
