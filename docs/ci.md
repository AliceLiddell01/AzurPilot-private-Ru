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

- для PR из веток `codex/translate-*` — fail-closed structural parity фактического
  `base→head` через `dev_tools/translation_structural_gate.py`, загруженный из
  точного PR base;
- `uv lock --check` и `uv sync --locked --group ci`;
- Ruff для ошибок выполнения и импорта;
- компиляцию основных Python entry points и каталогов;
- автоматическое обнаружение всего каталога `tests/` через `pytest 9.1.1`, зафиксированный в `uv.lock`;
- генераторы конфигурации и assets;
- отсутствие generated diff и незакоммиченных файлов.

Локальный эквивалент среды:

```bash
uv lock --check
uv sync --locked --group ci
uv run --locked ruff check . --select E9,F63,F7,F82 --ignore F821,F722
```

Job `Python` не содержит ручного реестра модулей: `pytest` автоматически собирает весь каталог `tests/`. Тесты, которым требуется реальное устройство, эмулятор или игровой аккаунт, должны проверять только локальный контракт либо оставаться в `tools/acceptance/`.

Translation structural step получает SHA из `pull_request.base.sha` и
`pull_request.head.sha`, сравнивает changed production Python через локальный
`git diff` и запрещает translation PR менять workflow, verifier или его tests.
В production scope входят точки входа, `module/**/*.py` и `campaign/**/*.py`.
Проверка разрешает изменение строк только в статически однозначных
operator-facing logger/exception positions (включая `logger.exception`),
безопасные строковые
конкатенации, `%`-подстановки и вызовы `strip`; строковые позиции сверяются в
UTF-8 byte coordinates, используемых Python AST. Все неизвестные string
contexts и любые структурные изменения блокируются. Верхнеуровневый required
context при этом остаётся `Python` — отдельный status context не создаётся.

## Windows

Job выполняется на `windows-latest` с PowerShell и Python `3.14.6`:

- парсит каждый tracked `.ps1` и `.psm1` через PowerShell Parser;
- запускает PSScriptAnalyzer `1.25.0` с уровнями `Error` и `Warning`;
- выполняет Windows-регрессии WebUI, device acceptance contract и эксплуатационных PowerShell-скриптов;
- требует чистое рабочее дерево.

Локальные проверки должны выполняться через `pwsh`, а не через Windows PowerShell 5.1. Правила написания Git-команд находятся в `.codex/context/POWERSHELL-GIT-RULES.md`.

## Security

Job выполняется на `ubuntu-24.04` и проверяет:

- текущие исходники и диапазон коммитов PR через Gitleaks `8.30.1` с проверкой SHA256 загружаемого архива;
- новые файлы, архивы, бинарные данные и repository hygiene;
- security/privacy regressions устройства, OCR RPC, debug output и traceback rendering, включая loopback-only RPC, безопасный wire format без pickle, bounded payload, model/batch/candidate limits, debug opt-in, retention, cleanup и symlink/reparse protection;
- DOM-безопасность WebUI через закреплённый browser runner;
- отсутствие изменений рабочего дерева после проверок.

Секреты в диагностике должны редактироваться; найденное значение нельзя публиковать полностью.

## Dependency lock

Обязательные Python-инструменты CI объявлены в `pyproject.toml` и разрешаются в `uv.lock`: `ruff` находится в group `dev`, а `pytest==9.1.1` и `playwright==1.55.0` — в group `ci`. Required jobs не используют `uv pip install` или `uv run --with` для обязательных инструментов.

## Action pins и checkout

Все сторонние GitHub Actions закрепляются полным commit SHA. Обычные jobs получают exact head PR. Security job использует ограниченную историю, достаточную для проверки диапазона PR, и при необходимости точечно догружает базовый commit.

Полная история репозитория не загружается без отдельной необходимости. `paths`-фильтры для required workflow не применяются, чтобы required checks не оставались в состоянии `Expected`.

## Диагностика

Диагностические artifacts создаются и загружаются только при падении соответствующей job. Они содержат безопасные сведения о среде, `git status` и рабочем diff, хранятся ограниченное время и не являются обязательными committed evidence.

Успешный CI не публикует постоянные отчёты и не изменяет репозиторий.

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
