# Проверки и Definition of Done

## Нулевая проверка

До обещания результата зафиксировать доступность:

- чтения/записи репозитория;
- веток, commits и PR;
- Python и `uv`;
- `pwsh` и PowerShell Parser;
- PSScriptAnalyzer;
- tests/lint/build;
- Windows/GUI/emulator/game smoke.

Недоступность одного инструмента не отменяет остальные проверки. Если недоступен обязательный для задачи gate, результат получает статус `blocked`: Codex не выдаёт непроверенный артефакт как готовый и не передаёт запуск пользователю.

## Постоянный CI

Единственный постоянный pull-request workflow — `.github/workflows/ci.yml`. Он должен запускаться для каждого PR в `personal/stable` без `paths`-фильтров и публиковать три устойчивых context, которые ruleset обязан сделать required:

- `Python`;
- `Windows`;
- `Security`.

Исторические номера этапов, committed evidence, stage-specific baselines и временные migration gates не являются постоянными quality gates. Подробный фактический контракт, локальные эквиваленты и правила изменения CI находятся в `docs/ci.md`.

## Режимы

### Fast-track

Для документации, опечатки и очевидного локального изменения без control flow.

Минимум:

- проверить целевой файл и контекст;
- минимальный diff;
- format/syntax;
- итоговый diff;
- secret scan.

### Стандартный

Для обычного fix/feature в одной известной подсистеме.

Дополнительно:

- проследить вызовы;
- проверить аналогичную реализацию;
- запустить точечные tests;
- проверить сквозное поведение в разумной границе.

### Расширенный

Для:

- upstream/master/personal stable;
- Start/Update/Repair/Build;
- Python/dependencies/uv.lock;
- device/input/OCR/combat/Operation Siren;
- MCP/security/privacy;
- нескольких подсистем.

Требует архитектурного анализа, полного релевантного набора проверок и явных рисков.

## Типовая матрица

### Python

- `uv lock --check` и `uv sync --locked --group ci` для постоянного CI;
- compile/import затронутого модуля;
- существующий ruff-профиль;
- точечные tests;
- полный связанный набор;
- generator check;
- чистое рабочее дерево после генераторов.

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

Реальные device/OCR acceptance и benchmarks выполняются локальными инструментами из `tools/acceptance/` и `tools/benchmarks/`. Они не становятся required checks каждого PR без отдельного устойчивого обоснования.

### Runtime localization integrity

Общий pytest suite запускает `tests/test_runtime_russianization_audit.py`. Тест выполняет permanent semantic audit текущих production consumer sites и Global/EN identity, а self-tests обязаны доказывать обе стороны контракта:

- FAIL: CJK operator prose, обычное untranslated English предложение, foreign locale/server/package/assets/OCR alias;
- PASS: русский контекст, ADB/OCR/API/URL/path/package/game identifiers, deferred exception text и feature structure вне display sink.

Для explicit translation PR этот guard дополняет, но не заменяет dynamic base→head structural gate. Для feature/bugfix/refactor structural parity не применяется, permanent integrity остаётся обязательной частью обычных product tests.

### PowerShell

- Parser через фактический `pwsh` для всех tracked `.ps1` и `.psm1`;
- PSScriptAnalyzer зафиксированной версии как обязательный gate;
- статический аудит правил;
- disposable smoke для Git refs/branches/files;
- идемпотентный повторный запуск.

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
- create-only migration валидного marker в `config/state/`, rejection
  повреждённого legacy marker и отсутствие runtime-state JSON в profile discovery;
- `.env`/passfile contract, distinct app/migrator secrets и old-credential
  negative auth;
- app DML положительно, DDL/role/database отрицательно;
- Start/Update/Repair/Build ownership и PowerShell gates;
- final import, repeat zero-delta, dump/list, scratch restore и reconciliation;
- после canary legacy `.db` и canonical CSV не создаются повторно.

## Secret scan

Перед commit и PR:

- staged/final diff;
- новые архивы и binaries;
- `.env`, config, logs, dumps, backups;
- API tokens, webhooks, credentials, cookies;
- device/user identifiers.

Перед commit и merge обязателен фактически запущенный secret scanner. Ручной паттерн-аудит может быть только дополнительной проверкой и не заменяет обязательный scanner. Если scanner недоступен, задача блокируется.

Постоянный job `Security` проверяет текущие исходники и релевантный диапазон коммитов PR. Диагностика должна редактировать секреты и загружаться только при падении.

## Definition of Done

- правильная ветка и базовый SHA;
- минимальный связный diff;
- архитектурные границы соблюдены;
- generated-файлы согласованы;
- required checks `Python`, `Windows`, `Security` зелёные на exact head;
- на exact head отсутствуют старые параллельные Stage/evidence workflow;
- упавшие проверки исправлены и повторены в пределах установленного бюджета;
- secret scan выполнен;
- независимый reviewer pass завершён;
- security review завершён;
- открытые review threads отсутствуют;
- документация обновлена;
- post-merge verification завершён для слитой задачи;
- ограничения перечислены;
- от пользователя не требуется рутинных технических действий.
