---
name: azurpilot-repository-development
description: "Разработка, исправление ошибок, рефакторинг, инфраструктура, CI/тесты, документация, адаптация upstream, подготовка или продолжение PR и явно разрешённый merge/cleanup в AzurPilot. Используй для изменения репозитория; не применяй для read-only объяснений и других задач без изменения файлов."
---

# Разработка AzurPilot

Этот skill — маршрутизатор инженерной задачи. Он не является вторым владельцем
Git lifecycle или общей матрицы проверок.

## Что читать

1. Всегда: `AGENTS.md` и `.codex/context/INDEX.md`.
2. Только относящиеся к фактическому diff доменные документы из INDEX.
3. При Git/ветке/PR/публикации/merge/rollback/cleanup:
   `.codex/context/GIT-WORKFLOW.md`.
4. Перед выбором и итоговой оценкой проверок:
   `.codex/context/08-VERIFICATION.md`.
5. Дополнительные skill-specific references:
   - [engineering-contract.md](references/engineering-contract.md) — границы
     реализации, языка и пользовательского checkout;
   - [browser-and-live-testing.md](references/browser-and-live-testing.md) —
     только для GUI/WebUI/device/game acceptance.

Не загружай Git workflow, verification или live-testing reference, если
фактическая задача их не затрагивает.

## Рабочий цикл

1. Восстанови фактическую область задачи и текущее состояние затронутых файлов.
   Если задача продолжает существующий PR/ветку, сначала сравни live-state с
   предыдущей подтверждённой точкой и не повторяй уже выполненную работу.
2. Проследи владельца поведения, call sites, ближайшие тесты, конфигурацию и
   generated/source границы. Не вводи данные конкретной задачи в production,
   CI или постоянные tests.
3. Реализуй минимальный связный diff. Обнови относящиеся к изменению тесты и
   документацию. Во всех затронутых human-facing файлах проверь русский текст.
4. Для repository evidence при необходимости используй существующие прямые
   адаптеры `azurpilot.integrations`. Не меняй user config, OAuth/grants,
   dashboards/alerts или game/runtime state только ради получения evidence.
5. Проверки выбирай **только** по `08-VERIFICATION.md`. Этот skill не
   поддерживает собственную копию списка обязательных gates.
6. Если canonical workflow требует CodeRabbit review checkpoint, явно делегируй
   sibling skill `azurpilot-coderabbit-review`. Такая внутренняя делегация не
   требует повторного пользовательского CodeRabbit-запроса. Каждый finding
   независимо проверяй по фактическому коду; не применяй autofix вслепую и не
   делай polling-loop при rate limit.
7. Все правила commit/push/draft PR, состояния перед финальным пользовательским
   ревью, merge authorization, rollback и cleanup бери **только** из
   `GIT-WORKFLOW.md`. Этот skill не переопределяет их.

## Завершение

Сообщай фактический статус и evidence из документа-владельца. Не объявляй тест,
CI, secret scan, live acceptance или внешнее ревью выполненными без реального
результата.
