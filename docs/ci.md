# Continuous Integration

## Назначение

Постоянный CI AzurPilot Private RU проверяет текущее продуктовое поведение, а не историю этапов разработки. Единственный обязательный pull-request workflow находится в `.github/workflows/ci.yml` и запускается без `paths`-фильтров для каждого PR в `personal/stable`.

CI не использует исторические SHA, committed evidence, stage-specific baselines или временные migration gates как источник истины. Источниками истины являются текущий код, исполняемые тесты и фактическое состояние ветки.

## Публичные status contexts

Workflow публикует три стабильных status contexts:

- `Python`;
- `Windows`;
- `Security`.

Активный repository ruleset `Protect personal/stable` (ID `20179789`) применяется к `refs/heads/personal/stable` и требует именно эти три context со strict-проверкой актуальности ветки. Старые Stage-зависимые required contexts отсутствуют. Ruleset также запрещает удаление ветки и non-fast-forward updates, требует pull request и разрешения review threads. Имена jobs являются публичным контрактом; переименование требует согласованного изменения ruleset.

## Python

Job выполняется на `ubuntu-24.04` с Python `3.14.6` и проверяет:

- disposable PostgreSQL 18 service container: создание synthetic
  least-privilege owner, Alembic `base → head → base → head`, single-head и
  autogenerate check;
- PostgreSQL repository integration, multiprocessing counters, transactions,
  idempotency, conflict, health и redaction contracts;
- для PR из веток `codex/translate-*` — fail-closed structural parity фактического
  `base→head` через `dev_tools/translation_structural_gate.py`, загруженный из
  точного PR base;
- `uv lock --check` и `uv sync --locked --group ci`;
- Ruff для ошибок выполнения и импорта;
- компиляцию основных Python entry points и каталогов;
- автоматическое обнаружение всего каталога `tests/` через `pytest 9.1.1`, зафиксированный в `uv.lock`;
- permanent semantic runtime-localization audit: доказанные operator-facing sinks должны оставаться русскими, а runtime identity — только RU/Global/EN;
- генераторы конфигурации и assets;
- отсутствие generated diff и незакоммиченных файлов.

Локальный эквивалент среды:

```bash
uv lock --check
uv sync --locked --group ci
uv run --locked ruff check . --select E9,F63,F7,F82 --ignore F821,F722
```

Локальный эквивалент PostgreSQL/Alembic-цикла описан в
[`postgresql-storage-foundation.md`](postgresql-storage-foundation.md#alembic).

Job `Python` не содержит ручного реестра модулей: `pytest` автоматически собирает весь каталог `tests/`. Тесты, которым требуется реальное устройство, эмулятор или игровой аккаунт, должны проверять только локальный контракт либо оставаться в `tools/acceptance/`.

Translation structural step получает SHA из `pull_request.base.sha` и
`pull_request.head.sha`, сравнивает changed production Python через локальный
`git diff` и запрещает translation PR менять workflow, verifier или его tests.
В production scope входят точки входа, `module/**/*.py` и `campaign/**/*.py`.

`tests/test_runtime_russianization_audit.py` проверяет текущее дерево на каждом PR и не зависит от historical SHA или base snapshot. Он запрещает CJK и неклассифицированные English-only предложения в deterministic display sinks и защищает `ru-RU`, `en`, Global package, `assets/en`, EN metadata и OCR namespace `azur_lane`. Узкие semantic allowances относятся к техническим, machine и game значениям; broad file/directory ignores отсутствуют.

Structural parity ниже применяется только к explicit translation PR. Feature, bugfix и refactor меняют functionality согласно Declared Scope и обычным product tests, но не освобождаются от permanent runtime-localization audit.

Все строковые значения verifier считает exact-by-default. Изменение допускается
только для статически однозначных operator-facing prose-позиций конкретных
sinks: первого message argument поддерживаемых прямых `logger.*` и точных
method-call `self.logger.*` вызовов, обеих позиционных prose-позиций точного
`logger.attr(name, text)`/`self.logger.attr(name, text)`, первого позиционного
label точного `logger.attr_align(name, text, ...)`/`self.logger.attr_align(name, text, ...)`, а также keyword values
`title=`/`content=` прямого `handle_notify(...)`.

Для Operation Siren отдельно разрешён только keyword `content=` точного
`self.notify_push(...)` в доказанных task-consumers
`module/os/tasks/scheduling.py`, `module/os/tasks/fleet_auto_change.py` и
`module/os/tasks/hazard_leveling.py`. `notify_push.title` остаётся exact, потому
что `_format_launcher_notification()` анализирует title и использует его для
ветвления. Позиционный content, произвольные `obj.notify_push` и другие файлы
не входят в allowlist.

Локальная строковая переменная может считаться частью `notify_push.content`
только для явно доказанного path/function/variable contract, если verifier
подтверждает единственное чтение этой переменной внутри exact keyword
`content=` и fail-closed reaching-definition связь с этим sink. Любое другое
чтение, неподдерживаемая запись или control-flow с отслеживаемой переменной
снимает такое разрешение. Это покрывает как локальный `content` в
`check_and_notify_action_point_threshold()`, так и `coin_status` в
`_handle_smart_scheduling_no_task()`, не создавая generic-разрешения локальных
assignments.

Для доказанного display-builder
`OpsiHazard1Leveling._format_check_report()` разрешены только непустые безопасные
строковые templates в exact `lines.append(...)`. Локальные `status` и
`time_str` разрешаются лишь когда все их чтения принадлежат этим append-display
expressions, а записи являются простыми assignments. Разделитель
`"\n".join(lines)`, вычисления, ключи/идентификаторы и любые другие строки
helper остаются exact.

Остальные соседние аргументы и неизвестные keywords, строки в
`raise`/exception constructors, неизвестных calls/keywords и machine-sensitive
контекстах остаются exact. Call target, call shape, dynamic expressions,
placeholders, conversion и format specification должны совпадать. Для
`logger.attr_align`/`self.logger.attr_align` второй positional argument, `front`
и `align` также exact.

Для обычных `STRING` сохраняются prefix и точный вид quote delimiter. Для
f-string verifier использует token contract текущего Python runtime:
`FSTRING_START` и `FSTRING_END` exact, replacement-field tokens exact, а
`FSTRING_MIDDLE` нормализуется только когда его byte range принадлежит
одобренному outer prose literal. `FSTRING_MIDDLE` внутри
`FormattedValue.format_spec` остаётся exact. Template-string tokens `TSTRING_*`
не входят в translation allowlist и exact-by-default. Строковые диапазоны
сверяются в UTF-8 byte coordinates, используемых Python AST.

CI загружает implementation verifier командой вида
`git show "${PR_BASE_SHA}:dev_tools/translation_structural_gate.py"` только из
динамического текущего PR base. Этот `git show` разрешён исключительно для
получения trusted verifier implementation. Его нельзя использовать для
historical production baseline, whole-file freeze, committed before-tree
snapshot, approved-delta history или permanent assertion против старой
production revision. Проверяемая модель — dynamic current base + verifier из
этой базы + exact current head как source data.

Верхнеуровневый required context при этом остаётся `Python` — отдельный status
context не создаётся.

## Windows

Job выполняется на `windows-latest` с PowerShell и Python `3.14.6`:

- парсит каждый tracked `.ps1` и `.psm1` через PowerShell Parser;
- запускает PSScriptAnalyzer `1.25.0` с уровнями `Error` и `Warning`;
- выполняет Windows-регрессии WebUI, device acceptance contract и эксплуатационных PowerShell-скриптов;
- импортирует SQLAlchemy/Psycopg/Alembic wheels на Python 3.14 и проверяет
  lazy engine, PID/spawn, pool, config и redaction без сетевого подключения;
- требует чистое рабочее дерево.

Локальные проверки должны выполняться через `pwsh`, а не через Windows PowerShell 5.1. Правила написания Git-команд находятся в `.codex/context/POWERSHELL-GIT-RULES.md`.

## Security

Job выполняется на `ubuntu-24.04` и проверяет:

- текущие исходники и диапазон коммитов PR через Gitleaks `8.30.1` с проверкой SHA256 загружаемого архива;
- новые файлы, архивы, бинарные данные и repository hygiene;
- storage architecture, отсутствие SQLite fallback/production wiring,
  bounded schema metadata и безопасное преобразование DB errors;
- security/privacy regressions устройства, OCR RPC, debug output и traceback rendering, включая loopback-only RPC, безопасный wire format без pickle, bounded payload, model/batch/candidate limits, debug opt-in, retention, cleanup и symlink/reparse protection;
- DOM-безопасность WebUI через закреплённый browser runner;
- отсутствие изменений рабочего дерева после проверок.

Секреты в диагностике должны редактироваться; найденное значение нельзя публиковать полностью.

## Dependency lock

Обязательные Python-инструменты CI объявлены в `pyproject.toml` и разрешаются в `uv.lock`: `ruff` находится в group `dev`, а `pytest==9.1.1` и `playwright==1.55.0` — в group `ci`. Required jobs не используют `uv pip install` или `uv run --with` для обязательных инструментов.

`astral-sh/setup-uv` в required jobs устанавливает `uv 0.11.32`, совпадающий с закреплённой проектной зависимостью. Это исключает плавающую версию bootstrap-инструмента между CI-запусками.

## Action pins и checkout

Все сторонние GitHub Actions закрепляются полным commit SHA. Обычные jobs получают exact head PR. Security job использует ограниченную историю, достаточную для проверки диапазона PR, и при необходимости точечно догружает базовый commit.

Полная история репозитория не загружается без отдельной необходимости. `paths`-фильтры для required workflow не применяются, чтобы required checks не оставались в состоянии `Expected`.

## Heartbeat и watchdog

Потенциально длительные или тихие команды запускаются через `tools/ci_heartbeat.py`. Runner транслирует stdout/stderr дочернего процесса в Actions log и одновременно сохраняет полный текстовый лог этапа. Если команда не выводит новых строк 60 секунд, runner пишет heartbeat с названием операции, общим временем выполнения и длительностью текущей тишины.

Heartbeat не изменяет exit code дочерней команды и не заменяет watchdog. Для длительных этапов сохраняется отдельный `timeout-minutes`, поэтому живой heartbeat не позволяет реально зависшей проверке выполняться бесконечно. Короткие проверки могут использовать тот же runner с отключённым heartbeat только для унифицированного логирования.

Для браузерной DOM-проверки, чей stdout является машинно-читаемым JSON-отчётом, heartbeat выводится отдельным служебным циклом и не смешивается с report payload.

## Диагностика

Диагностические artifacts создаются и загружаются только при падении соответствующей job. Помимо безопасных сведений о среде, `git status` и рабочего diff, они содержат полные текстовые логи выполненных этапов. `Python` также сохраняет JUnit XML полного `pytest`, а `Security` — redacted Gitleaks-логи и browser evidence. Значения секретов в диагностике не публикуются.

Каждая job всегда пишет краткий `$GITHUB_STEP_SUMMARY` с exact commit и именем failure artifact. Основные проверки разделены на отдельные steps внутри прежних трёх required jobs, поэтому GitHub UI показывает конкретную упавшую фазу без размножения status contexts.

Успешный CI не загружает диагностические artifacts и не изменяет репозиторий.

## Acceptance и benchmarks

Реальные acceptance-прогоны и benchmarks находятся вне required CI:

- `tools/acceptance/` — проверки реального устройства, OCR и WebUI;
- `tools/benchmarks/` — OCR- и screenshot benchmarks.

Их unit/regression-контракты входят в CI там, где защищают production-поведение без реального устройства. Сам реальный прогон выполняется только в подходящей контролируемой среде и не должен превращаться в обязательную проверку каждого PR.

## Не-CI автоматизации

Workflow публикации Docker-образа, синхронизации зеркала и обработки Issues не являются required pull-request checks. Они имеют отдельные события запуска и не должны дублировать `Python`, `Windows` или `Security`.

## Изменение CI

При изменении `.github/workflows/ci.yml` необходимо:

1. сохранить точные публичные имена required jobs;
2. не вводить исторические Stage/evidence-зависимости;
3. проверить full-SHA pins и минимальные permissions;
4. выполнить exact-head CI;
5. убедиться, что на head запустился только один обязательный workflow с тремя required jobs;
6. обновить этот документ, если изменился фактический контракт.
