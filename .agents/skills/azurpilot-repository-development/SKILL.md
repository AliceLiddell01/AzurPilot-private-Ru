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
5. Для GUI/WebUI/device/game acceptance при необходимости открой
   [browser-and-live-testing.md](references/browser-and-live-testing.md).

Не загружай Git workflow, verification или live-testing reference, если
фактическая задача их не затрагивает.

## Рабочий цикл

1. Восстанови фактическую область задачи и текущее состояние затронутых файлов.
   Если задача продолжает существующий PR/ветку, сначала сравни live-state с
   предыдущей подтверждённой точкой и не повторяй уже выполненную работу.
2. Проследи владельца поведения, call sites, ближайшие тесты, конфигурацию и
   generated/source границы. Не вводи данные конкретной задачи в production,
   CI или постоянные tests.
3. Для effective candidate diff относительно exact base выполни read-only
   `azur mcp impact --base <exact-base-sha>`. Если результат `REQUIRED`,
   выполни штатный `azur mcp reconcile --source --bump auto`, затем повтори
   current-tree integrity и base-to-head compatibility checks. Это доказывает
   только `source_reconciled`; оно не доказывает `runtime_ready`. Если текущая
   verification/acceptance требует live MCP, после source reconciliation
   буквально вызови через PATH текущей shell `azur mcp status`. При
   `runtime_state=stopped` и доказанном owned supervisor выполни канонический
   `azur mcp start` (или `azur mcp restart` для stale runtime), затем снова
   вызови `azur mcp status` и требуй `runtime_ready=true`. При
   `LOCAL_MCP_SUPERVISOR_STOPPED`, unknown ownership, port conflict или
   readiness failure обязательный live gate остаётся typed blocked/failed; его
   нельзя выдать за завершённый. Не запускай `module.*_mcp`, внутренние
   supervisor scripts или Python module entrypoints напрямую. Любое новое
   изменение затронутого source set после reconciliation делает прежний
   результат stale и требует повторной reconciliation.
4. Перед CodeRabbit review создай или привяжи opaque logical task identity и
   передай её в canonical review flow через `--task-id`; новый head той же
   task продолжает её cycle, а другая task получает новый cycle.
5. Реализуй минимальный связный diff. Обнови относящиеся к изменению тесты и
   документацию. Во всех затронутых файлах с текстом для человека проверь русский язык.
   Project-owned operator action с доступным в PATH `azur` запускай только как
   буквальную команду `azur ...`. Запрещены `uv run ... azur`, `python -m
   azurpilot`, `.venv/.../azur`, абсолютный путь к `azur.exe` и shell wrapper;
   `uv` разрешён для dependency/bootstrap/test/build задач, но не как launcher
   operator command. Если `azur` отсутствует, не используй fallback.
6. Для репозиторных evidence при необходимости используй существующие прямые
   адаптеры `azurpilot.integrations`. Не меняй user config, OAuth/grants,
   dashboards/alerts или game/runtime state только ради получения evidence.
7. Проверки выбирай **только** по `08-VERIFICATION.md`. Этот skill не
   поддерживает собственную копию списка обязательных gates.
8. Если canonical workflow требует CodeRabbit review checkpoint, явно делегируй
   sibling skill `azurpilot-coderabbit-review`. Такая внутренняя делегация не
   требует повторного пользовательского CodeRabbit-запроса. Специфичные для
   провайдера правила triage, provider rate limit и retry принадлежат этому
   sibling skill.
9. Все правила commit/push/draft PR, состояния перед финальным пользовательским
   ревью, merge authorization, rollback и cleanup бери **только** из
   `GIT-WORKFLOW.md`. Этот skill не переопределяет их.
   Не создавай temporary/scratch/transport/helper remote ref, `codex/base-*`
   branch или вспомогательную remote publication. Typed delivery/PR workflow
   использует только реальную parent/feature branch; если local parent HEAD
   отличается от parent remote HEAD, результат — typed precondition blocker,
   пока parent не опубликован штатным lifecycle. Workaround ref запрещён.

## Завершение

Сообщай фактический статус и evidence из документа-владельца. Не объявляй тест,
CI, secret scan, live acceptance или внешнее ревью выполненными без реального
результата.
