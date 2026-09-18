# Инструкции Codex для AzurPilot Private RU

## 1. Роль

Этот файл — короткий обязательный контракт для работы в
`AliceLiddell01/AzurPilot-private-Ru`. Он задаёт глобальные инварианты,
маршрутизацию контекста и границы безопасности, но не заменяет чтение кода.

Отвечай пользователю, пиши документацию, operator-facing текст и сообщения
коммитов на русском языке. Имена кода, внешних API, protocol tokens, package
names, paths и другие точные технические идентификаторы сохраняй как есть.

## 2. Источники истины

Используй источники в таком порядке:

1. фактический код и конфигурация **целевой ветки**;
2. исполняемые tests, generated contracts и подтверждённое runtime behavior;
3. эксплуатационные контракты и Wiki персонального форка;
4. релевантные документы `.codex/context/`;
5. upstream `wess09/AzurPilot` и его diff;
6. DeepWiki как архитектурную карту унаследованного слоя;
7. исторические отчёты, обсуждения и одноразовые заметки.

При расхождении документации с кодом сначала установи фактическое поведение.
Исторический документ или старый PR не имеет права переопределять текущий код.

## 3. Progressive disclosure

Перед инженерной задачей открой `.codex/context/INDEX.md` и загрузи **только**
документы, относящиеся к реальному scope. Не читай всю папку «на всякий случай»
и не перечитывай уже подтверждённые документы после каждого малого fix.

Canonical owners правил перечислены в `INDEX.md`. В частности:

- Git/branch/PR/merge/rollback — `GIT-WORKFLOW.md`;
- verification и Definition of Done — `08-VERIFICATION.md`;
- Python tooling/external integrations — `11-PYTHON-TOOLING.md`;
- Git внутри PowerShell — `POWERSHELL-GIT-RULES.md`.

`MIGRATION-MAP.md` — историческая справка и не входит в обычный рабочий
контекст.

## 4. Preflight и ветки

До изменения репозитория зафиксируй:

- repository identity и целевую ветку;
- exact base/head SHA;
- staged/unstaged/untracked состояние, если доступен checkout;
- затронутые подсистемы;
- доступные проверки и ограничения среды.

Не stash/drop/reset пользовательские изменения и не включай unrelated diff.

`master` — зеркало upstream; `personal/stable` — стабильная пользовательская
ветка. Новая обычная работа использует явный task base и ветку
`<domain>/<unique-capability-name>`. Уже опубликованную ветку/PR продолжай
только после проверки exact identity и head. Полный lifecycle — только в
`.codex/context/GIT-WORKFLOW.md`.

## 5. Долговременные инженерные инварианты

- Task-specific hardcode запрещён. Изменяемые task/event/map/OCR/PR/migration
  значения должны идти через существующий config, registry, model, schema,
  abstraction, extension point или generated source.
- Permanent tests и CI проверяют текущее продуктовое поведение, а не номер
  этапа, historical SHA, конкретный PR или одноразовый evidence snapshot.
- Если файл попал в diff, проверь весь его человеческий текст. Комментарии,
  операторские логи и diagnostics должны быть литературным русским, кроме
  точных технических идентификаторов.
- Перед ручным редактированием выясни, является ли файл source или generated
  output. Меняй источник и запускай существующий generator; не лечи generated
  JSON/metadata вручную.
- Не создавай параллельную архитектуру, compatibility shim или новый owner,
  пока существующий owner можно безопасно расширить.

## 6. Продуктовая граница

Текущий AzurPilot Private RU — **Global/EN-only runtime**:

- server: `en`;
- package: `com.YoStarEN.AzurLane`;
- runtime WebUI: `ru-RU`;
- canonical assets: `assets/en`.

Унаследованные CN/JP/TW branches, assets и OCR abstractions могут существовать в
upstream-коде, но это не означает product support. Не расширяй, не тестируй и не
выдавай foreign variants за поддерживаемый runtime без явной задачи на изменение
этой границы.

Игровое взаимодействие сохраняет цикл:

```text
снимок экрана → распознавание состояния → одно допустимое действие → новый снимок
```

Не заменяй его blind sequence вида «клик → sleep → следующий клик». После
изменившего состояние действия нужен новый screenshot и повторная проверка.

## 7. Python, tooling и integrations

Проект использует Python `>=3.14.6,<3.15`, `uv`, `uv.lock` и локальную
`.venv`. Не вводи `requirements*.txt` или системный `pip` как второй
dependency path без отдельной подтверждённой миграции.

Repository-owned Python tooling находится в `azurpilot/`. `azurpilot.tooling`
владеет typed contracts и CLI/service boundaries, а
`azurpilot.integrations` — прямыми адаптерами внешних интеграций. Не возвращай
Docker MCP Gateway/Toolkit или другой общий proxy в critical path, если текущий
typed adapter уже является владельцем.

PowerShell Start/Stop/Update/Repair/Build и их compatibility gates не считаются
удалёнными только потому, что Python CLI содержит соответствующую команду.
Граница и критерии parity описаны в `11-PYTHON-TOOLING.md`.

## 8. Git и безопасность

В пользовательском checkout, `master`, `personal/stable` и опубликованных
ветках destructive Git запрещён, включая `reset --hard`, `clean -fd[x]`,
`checkout -f`, force-push, `branch -D`, destructive reflog/gc. Разрешение
пользователя само по себе не превращает такой checkout в disposable.
Исключения для доказанно disposable среды определяет только
`GIT-WORKFLOW.md`.

Перед публикацией проверяй diff на secrets, tokens, passwords, cookies,
credentials в URL, `.env`, local config, logs/dumps/backups, device identifiers
и случайные binary/archive files. Найденный секрет полностью не выводи.

## 9. Проверки

Проверки выбираются по **фактическому diff и изменённой границе**, от дешёвых к
дорогим: static/diff audit → syntax/lint → targeted tests → связанный suite →
необходимый generator/build/live gate → secret scan.

Не запускай device/game/browser/CLI acceptance только потому, что такой gate
существует в проекте. Он обязателен лишь когда diff затрагивает соответствующую
surface или её контракт. Human + `--json` live acceptance обязателен для
изменений CLI/tooling/delivery/PR capability, а не для несвязанного combat/OCR/
docs fix.

Не повторяй полный suite после каждого малого исправления. Повторяй затронутые
checks и полный связанный набор только после существенного relevant diff или для
диагностики.

Недоступный обязательный product/security gate даёт `blocked`, а не
воображаемый success. CodeRabbit rate limit/cooldown до финального
пользовательского ревью — отдельное review limitation: не ждать его и не делать
blind retry, но остальные обязательные gates сохраняются.

## 10. PR lifecycle

Нормальная pre-merge конечная точка — `READY_FOR_CHATGPT_REVIEW`:

- связный diff реализован;
- релевантные проверки выполнены;
- exact-head required CI проверен;
- security/secret scan выполнен;
- blocking findings разобраны;
- draft PR создан или обновлён.

После этого остановись для **финального пользовательского ревью**. Не привязывай
долговременный контракт к названию конкретной модели или версии reviewer.

Merge разрешает только отдельное **текущее** сообщение пользователя,
однозначно относящееся к этому PR. Перед merge нужен exact-head revalidation.
После подтверждённого merge выполняются post-merge verification и cleanup.

## 11. Итоговый отчёт

Кратко укажи:

- что изменено и почему;
- base/head branch и SHA;
- ключевые файлы;
- фактически выполненные checks и exact-head CI;
- secret/security result;
- ограничения среды и review;
- итоговый lifecycle status.

Не утверждай, что тест, CI, Parser, GUI, device/game сценарий или внешний review
были выполнены, если фактического evidence нет.
