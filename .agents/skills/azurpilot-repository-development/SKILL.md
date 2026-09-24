---
name: azurpilot-repository-development
description: "Разработка, исправление ошибок и рефакторинг AzurPilot: инфраструктура, CI/тесты, адаптация upstream, подготовка PR, слияние и очистка. Не используй для объяснений без изменения файлов."
---

# Разработка AzurPilot

Навык направляет к владельцам правил и помогает закончить изменение в
репозитории, а не заменяет Git lifecycle или verification matrix.

## Вход и границы

1. Прочитай корневой `AGENTS.md` и `.codex/context/INDEX.md`.
2. Подтверди checkout, ветку, base SHA и чистоту пользовательского состояния.
3. Открой только owner-документы фактического diff.
4. Найди owner поведения, call path, ближайшие contract tests и root cause.
   После этого прекращай повторное чтение тех же участков без нового вопроса.
5. Меняй минимальный связный scope; source-set или generated-файл сначала
   проверь на владельца и генератор.

Git, branches, PR, merge и cleanup принадлежат `.codex/context/GIT-WORKFLOW.md`.
Выбор gates, test loop и freeze point принадлежат
`.codex/context/08-VERIFICATION.md`. Python CLI, typed services, MCP и delivery
contracts принадлежат `.codex/context/11-PYTHON-TOOLING.md`.
Для GUI/browser или live проверки открывай только
[browser and live testing](references/browser-and-live-testing.md).

## Канонический developer path

- Изменение MCP синхронизируй после freeze candidate одним вызовом
  `azur mcp sync --base <exact-base-sha>`. `NO_CHANGES` и `SYNCED` — terminal
  results. При `SYNCED` generated metadata, owned runtime readiness и
  fresh-client acceptance уже обработаны. После изменения MCP source-set
  повтори sync, который рассчитывает версию от exact base и финального candidate.
- Публикуй задачу одним `azur delivery publish --message
  "type(scope): краткое описание"`. По умолчанию команда включает все changed
  candidate paths, в том числе generated MCP metadata; `--path` нужен только
  для намеренно ограниченного scope. Не создавай внешний delivery manifest и
  не запускай `validate MANIFEST` перед normal publish.
- Для обычного Smoke используй `dev_run_smoke`: bounded сценарий возвращает
  terminal result после cleanup. Автоматические checkpoint'ы выполняются по
  declarative triggers; ручной checkpoint tool не является частью catalog.
  Длинный/interactive/visual сценарий запускай через отдельный публичный
  `dev_start_smoke`; он возвращает `DEV_SMOKE_STARTED`, ход выполнения читай через
  `dev_get_smoke`, а визуальную оценку проводи отдельными evaluation tools.
- `impact`, `status`, `reconcile`, `start`, `stop`, `restart`, `accept`,
  `delivery validate`, `dev_get_smoke` и screenshot/evaluation tools оставлены
  для диагностики или соответствующего async/evaluation path, а не как
  обязательная choreography normal workflow.

Project-owned operator actions запускай только буквальной командой `azur ...`
через PATH текущей shell. `uv` используй для dependency/bootstrap/test/build.
Не подменяй typed result разбором human CLI output или вызовом внутренних
Python modules.

## Проверка и публикация

Повторяй конкретный failing node, после fix — только затронутую дешёвую
проверку. Subsystem suite запускай после стабилизации области, полный relevant
suite — один раз на frozen candidate. Существенный code/config diff после gate
инвалидирует его; report-only правка — нет. Используй canonical lint/test
команды из owner-документов; не запускай noisy bare `ruff check .` без отдельной
цели.

Проверку CodeRabbit запускай только по явному запросу пользователя или
обязательному правилу проекта; явно передай её соседнему навыку
`azurpilot-coderabbit-review`, которому принадлежат ограничения частоты,
повторов и разбора результатов. Draft PR, exact-head CI, передачу на финальное
пользовательское ревью и запрет на merge без отдельной текущей команды
пользователя определяет `GIT-WORKFLOW.md`. Если задача явно требует optional
Codex registration check, используй
[cross-thread contract](references/cross-thread-task-delegation.md).
