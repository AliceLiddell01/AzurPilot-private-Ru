# Источники и поддержка контекста

## Роль

`.codex/context/` — навигационная база долговременных архитектурных границ.
Она должна помогать быстро ответить:

- где находится текущий owner поведения;
- какие инварианты нельзя ломать;
- какие соседние surfaces действительно связаны с diff;
- какой canonical документ владеет workflow-вопросом;
- какие проверки следуют из изменённой boundary.

Она не должна пересказывать весь репозиторий, текущий PR или историю обсуждения.

## Приоритет источников

1. Код и конфигурация целевой ветки.
2. Исполняемые tests, generated contracts и подтверждённое behavior.
3. Эксплуатационные контракты и Wiki форка.
4. Релевантный canonical context.
5. Upstream diff/source.
6. DeepWiki для унаследованной архитектуры.
7. Исторические отчёты и discussion.

## Один вопрос — один canonical owner

Workflow policy не копируется целиком между несколькими файлами.

- Git/PR/merge/rollback — `GIT-WORKFLOW.md`.
- Verification/DoD — `08-VERIFICATION.md`.
- Python tooling/external integrations — `11-PYTHON-TOOLING.md`.
- PowerShell Git mechanics — `POWERSHELL-GIT-RULES.md`.
- Domain behavior — соответствующий numbered document.

Другой документ может кратко сослаться на правило, но не должен поддерживать
вторую независимую формулировку с другой строгостью.

## Когда обновлять

Обновить context, если изменилось хотя бы одно:

- owner подсистемы или composition root;
- entrypoint или направление data/control flow;
- source/generated boundary;
- product/server/security/privacy boundary;
- эксплуатационный contract;
- обязательный scope-derived gate;
- branch/PR lifecycle;
- public typed contract, которым должны руководствоваться агенты.

Не обновлять ради:

- номера строки или размера файла;
- одного локального helper;
- точного количества methods/tools/assets;
- current PR/head SHA;
- текущего review cycle;
- косметического rename без изменения boundary.

## Hygiene permanent context

В durable documents запрещены:

- `Stage N`, номер roadmap или migration phase как текущее правило;
- «в этом increment/follow-up», «исходный prompt» и подобный task residue;
- отложенный implementation plan, смешанный с current architecture;
- привязка обязательного reviewer к конкретному названию/версии модели;
- machine-local absolute paths, usernames, tokens и credentials;
- historical SHA/evidence baseline, существующий только ради старого PR.

Если важна история решения, она остаётся в Git history, issue/PR или design
record. В context переносится только актуальный долговременный результат.

## Как проверять актуальность

Перед правкой context:

1. открыть код целевой ветки;
2. найти основные call sites/composition roots;
3. проверить ближайшие tests и generated contracts;
4. при необходимости сравнить Wiki/upstream;
5. сформулировать только подтверждённый устойчивый вывод;
6. заменить stale формулировку, а не дописать рядом новое исключение;
7. проверить, что соседний canonical owner не дублирует то же правило.

Если документ превратился в inventory, roadmap или несколько временных слоёв
одновременно, его нужно разделить или сократить. Большой размер сам по себе не
ошибка, но permanent context должен оправдывать каждую секцию повторным
использованием в будущих задачах.

## Проверки качества

Repository contract tests должны ловить как минимум:

- stage/increment/follow-up/prompt residue в durable context;
- literal reviewer model version в canonical workflow;
- возврат stale product boundary вроде multi-server runtime;
- восстановление заведомо устаревшего architecture statement;
- превращение scope-specific live gate в глобальный обязательный gate.

`MIGRATION-MAP.md` является исторической справкой и исключается из этих
semantic assertions, если не участвует в обычном routing.

## Запрет на AI-слепки

Не добавлять «полный анализ N строк на дату X». Такой snapshot быстро устаревает,
конкурирует с кодом и заставляет агента реконструировать временную шкалу.

Для временного исследования использовать issue/PR notes или локальный report.
В permanent context переносить только короткий устойчивый итог.
