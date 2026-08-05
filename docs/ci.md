# Continuous Integration

## Назначение

Постоянный CI AzurPilot Private RU проверяет текущее продуктовое поведение, а не историю этапов разработки. Единственный обязательный pull-request workflow находится в `.github/workflows/ci.yml` и запускается без `paths`-фильтров для каждого PR в `personal/stable`.

CI не использует исторические SHA, committed evidence, stage-specific baselines или временные migration gates как источник истины. Источниками истины являются текущий код, исполняемые тесты и фактическое состояние ветки.

## Required checks

Ruleset ветки `personal/stable` требует три отдельных status checks:

- `Python`;
- `Windows`;
- `Security`.

Имена являются публичным контрактом ruleset. Переименование job требует предварительного согласованного изменения ruleset; добавление новой required job допускается только при наличии отдельного устойчивого класса риска, который нельзя корректно включить в существующие три проверки.

## Python

Job выполняется на `ubuntu-24.04` с Python `3.14.6` и проверяет:

- `uv lock --check` и `uv sync --locked`;
- Ruff для ошибок выполнения и импорта;
- компиляцию основных Python entry points и каталогов;
- продуктовые regression-тесты WebUI, конфигурации, устройства, OCR и локальных инструментов;
- генераторы конфигурации и assets;
- отсутствие generated diff и незакоммиченных файлов.

Локальный эквивалент среды:

```bash
uv lock --check
uv sync --locked
uv run --locked ruff check . --select E9,F63,F7,F82 --ignore F821,F722
```

Точный набор продуктовых тестов зафиксирован в job `Python`. Он намеренно не включает реальное устройство, эмулятор или игровой аккаунт.

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
- security/privacy regressions устройства, OCR RPC, debug output и traceback rendering;
- DOM-безопасность WebUI через закреплённый browser runner;
- отсутствие изменений рабочего дерева после проверок.

Секреты в диагностике должны редактироваться; найденное значение нельзя публиковать полностью.

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
