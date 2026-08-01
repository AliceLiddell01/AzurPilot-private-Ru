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

- compile/import затронутого модуля;
- существующий ruff-профиль;
- точечные pytest;
- полный связанный набор;
- generator check;
- dependency sync при изменении зависимостей.

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

### PowerShell

- Parser через фактический `pwsh`;
- PSScriptAnalyzer как обязательный gate для релевантного изменения;
- статический аудит правил;
- disposable smoke для Git refs/branches/files;
- идемпотентный повторный запуск.

### WebUI

- импорт/создание app;
- endpoint/unit tests;
- lifecycle smoke;
- Windows process semantics;
- автоматизированная visual acceptance через browser/Windows runner, если она обязательна; отсутствие безопасной среды блокирует merge.

## Secret scan

Перед commit и PR:

- staged/final diff;
- новые архивы и binaries;
- `.env`, config, logs, dumps, backups;
- API tokens, webhooks, credentials, cookies;
- device/user identifiers.

Перед commit и merge обязателен фактически запущенный secret scanner. Ручной паттерн-аудит может быть только дополнительной проверкой и не заменяет обязательный scanner. Если scanner недоступен, задача блокируется.

## Definition of Done

- правильная ветка и базовый SHA;
- минимальный связный diff;
- архитектурные границы соблюдены;
- generated-файлы согласованы;
- все обязательные проверки выполнены, а недоступный обязательный gate оформлен как `blocked`;
- упавшие проверки исправлены и повторены в пределах установленного бюджета;
- secret scan выполнен;
- независимый reviewer pass завершён;
- документация обновлена;
- post-merge verification завершён для слитой задачи;
- ограничения перечислены;
- от пользователя не требуется рутинных технических действий.
