# Индекс контекста Codex

Папка содержит короткие **долговременные** архитектурные карты и рабочие
контракты AzurPilot Private RU. Она не является snapshot текущего PR.

## Как пользоваться

1. Прочитать корневой `AGENTS.md`.
2. Определить фактический diff/scope.
3. Открыть только документы из таблицы, которые владеют затронутой границей.
4. Проверить вывод по коду, tests и generated contracts целевой ветки.
5. При расширении scope догрузить новый документ; не перечитывать всё дерево.
6. Внешнюю документацию открывать только для конкретного спорного API/контракта.

## Canonical owners

| Файл | Владеет вопросом |
|---|---|
| `01-PROJECT-MAP.md` | где находится текущий owner поведения и основные слои |
| `02-RUNTIME-ARCHITECTURE.md` | entrypoints, process/runtime composition и MCP runtime |
| `03-CONFIG-I18N.md` | config sources/generation, migrations, RU/EN product boundary |
| `04-DEVICE-UI-OCR.md` | ADB, screenshot/control, Page/Button/Template/OCR |
| `05-COMBAT-MAP-CAMPAIGN.md` | combat, map, campaign и map detection |
| `06-OPERATION-SIREN.md` | Operation Siren и `module/os*` |
| `07-WEBUI-INFRASTRUCTURE.md` | WebUI, product MCP, persistence projections, notifications |
| `08-VERIFICATION.md` | scope-derived gates, CI, review checkpoints, Definition of Done |
| `09-SOURCES-MAINTENANCE.md` | правила качества и обновления этой папки |
| `10-GLOSSARY.md` | краткие термины |
| `11-PYTHON-TOOLING.md` | текущие `azurpilot.tooling`, integrations, CLI/delivery boundaries |
| `GIT-WORKFLOW.md` | Git/branch/PR/upstream/merge/rollback/cleanup lifecycle |
| `POWERSHELL-GIT-RULES.md` | Git внутри `.ps1`/`.psm1` |
| `MIGRATION-MAP.md` | историческая справка о старом AI-контексте; **не читать в обычной задаче** |

Постоянный CI contract дополнительно описан в `docs/ci.md`.

## Разрешение конфликтов

- Код/tests/generated contracts выше контекстных документов.
- Для workflow-вопроса побеждает соответствующий canonical owner из таблицы,
  а не более общий повтор в соседнем документе.
- Если два canonical документа реально расходятся, не пытайся «усреднить»
  правило: установи фактическое состояние, исправь owner и убери дубликат.
- Более свежий PR/issue не становится permanent rule автоматически.

## Экономия контекста

- `GIT-WORKFLOW.md` читать по релевантным разделам, а не целиком.
- `POWERSHELL-GIT-RULES.md` нужен только для PowerShell/Git scope.
- `11-PYTHON-TOOLING.md` нужен только для Python tooling, CLI, delivery, PR,
  MCP status или внешних integrations.
- Domain docs не загружаются для несвязанной documentation/Git-only задачи.
- Уже подтверждённый инвариант не перечитывается после каждого малого fix.

## Что здесь запрещено хранить

- current PR/branch/head SHA и одноразовый evidence;
- номера roadmap/stage и состояние конкретного increment/follow-up;
- остатки исходного prompt или обсуждения выбора решения;
- привязку обязательного reviewer к конкретной модели/версии;
- абсолютные пользовательские paths, secrets и machine-specific config;
- точные номера строк, размеры файлов и количество методов/tools;
- длинные построчные обзоры существующей реализации.

Такие данные принадлежат текущему task/PR evidence или Git history, а не
долговременному агентскому контексту.
