# Регламент автономной работы Codex с Git для AzurPilot Private RU

Версия: **2.3**
Репозиторий: `AliceLiddell01/AzurPilot-private-Ru`
Upstream: `wess09/AzurPilot`
Модель ответственности: **Codex выполняет 100% технической работы до готового draft PR; пользователь выполняет финальное ревью, а merge разрешается только отдельной явной командой.**

## Журнал изменений

### 2.1

- capability preflight сокращён до минимального; task-specific capabilities проверяются перед первым использованием;
- один основной Codex выполняет implementation, adversarial self-review, security и release passes последовательно, без обязательных subagents;
- внешнее ревью запускается на существенных milestone checkpoints: не только в конце, но и не после каждого мелкого fix;
- rate limit/cooldown обязательного reviewer завершает текущий прогон с сохранением состояния вместо ожидания;
- дорогие suites/gates повторяются только после существенного relevant diff или для диагностики;
- контекст загружается по необходимости; routine tool-call narration запрещён.

### 2.2

- основной Windows checkout `C:\AzurPilot` закреплён как обычная рабочая копия для последовательной разработки;
- добавлена безопасная preflight-проверка состояния main checkout перед сменой ветки;
- disposable clone/worktree оставлены только для обоснованной параллельной, опасной или несовместимой работы;
- WSL2 review clone отделён от implementation checkout и используется только для независимого CodeRabbit review;
- после публикации feature-ветки checkout остаётся на ней до финального review/merge.

### 2.3

- pre-merge lifecycle завершается состоянием `READY_FOR_CHATGPT_REVIEW` на draft PR;
- финальное ревью выполняет пользователь через ChatGPT 5.6 Sol;
- merge требует отдельной текущей явной команды пользователя, после которой Codex выполняет post-merge verification и cleanup;
- CodeRabbit rate limit/cooldown не блокирует draft PR и не требует ожидания.

### 2.0

- введён полностью автономный lifecycle;
- рабочие ветки переведены на `codex/*`;
- закреплены CI/security/secret/review/post-merge gates;
- PowerShell Parser, PSScriptAnalyzer и Windows smoke стали обязательными для релевантных изменений;
- описаны merge, rollback, cleanup и fail-closed поведение.

## 1. Область действия

Документ применяется только к `AliceLiddell01/AzurPilot-private-Ru` и регулирует:

- `master`, `personal/stable`, `codex/*`, legacy `chatgpt/*`, `sync/*`;
- upstream sync и перенос upstream-изменений в персональный контур;
- Git/PR/CI/review/merge/rollback/cleanup;
- Python, PowerShell, документацию, security и production-sensitive изменения.

## 2. Главный принцип

Штатный lifecycle:

```text
задача
→ безопасный preflight main checkout + base SHA
→ релевантный контекст
→ план
→ для новой задачи: fetch origin → personal/stable → FF-only → codex/<task>
→ реализация логическими слоями
→ targeted checks
→ Codex adversarial self-review
→ внешний review checkpoint на существенном/рискованном milestone
→ финальные релевантные gates
→ commit / draft PR
→ required CI exact head
→ `READY_FOR_CHATGPT_REVIEW`
→ STOP до финального ревью пользователя
→ [явная текущая команда merge]
→ post-merge verification
→ cleanup
→ короткий доказательный отчёт
```

Codex не просит пользователя запускать команды, тесты, Git, CI, создавать PR или проверять промежуточные файлы, если это технически доступно самому Codex. Merge не является частью автоматического финала: он выполняется только после отдельной текущей команды пользователя и финального ChatGPT review.

**Subagents не обязательны.** По умолчанию один основной Codex выполняет все внутренние passes последовательно. Независимость обеспечивается внешним reviewer/tool, когда он предусмотрен task contract.

## 3. Контекст и источники

Использовать progressive disclosure.

Приоритет:

1. фактический код/конфигурация целевой ветки;
2. ближайшие executable tests и runtime behavior;
3. корневой `AGENTS.md` и релевантные файлы `.codex/context/`;
4. README/Wiki форка;
5. upstream diff/issues/PR;
6. DeepWiki как архитектурная карта;
7. официальная документация конкретного API/инструмента.

Правила:

- сначала `.codex/context/INDEX.md`, затем только нужные документы;
- `GIT-WORKFLOW.md` читать по релевантным разделам;
- `POWERSHELL-GIT-RULES.md` читать только при PowerShell/Git scope;
- не перечитывать большие документы после каждого небольшого fix;
- не выполнять общий web/docs survey без конкретного вопроса;
- не расширять problem surface без evidence из call graph, tests, diff или runtime behavior;
- при расхождении документации и кода сначала установить фактическое поведение.

## 4. Архитектурные границы форка

AzurPilot Private RU наследует upstream, но содержит отдельный персональный эксплуатационный контур.

| Область | Типичные риски |
|---|---|
| WebUI/runtime | процессы, порт, lifecycle |
| config/scheduler | состояние, migration, повторный запуск |
| device/ADB | reconnect, timeout, platform differences |
| screenshot/input/OCR | координаты, thresholds, localization |
| combat/campaign/Operation Siren | state machine, retries, exit conditions |
| integrations/MCP | secrets, privacy, network errors |
| `scripts/*.ps1` | Git, `.venv`, update, rollback |
| production data | migration, credentials, recovery, rollback |

Оценивать сквозной поток только в пределах фактически затронутых границ.

## 5. Capabilities

### Базовые

Проверяются в начале:

- repo/worktree и чтение base branch;
- Git;
- runtime/package manager, без которого нельзя начать конкретную задачу.

### Task-specific

Проверяются перед первым соответствующим gate:

- GitHub push/PR/review/checks/artifacts/merge;
- Windows/PowerShell Parser/PSScriptAnalyzer;
- secret/security scanners;
- browser/WebUI;
- ADB/emulator/game;
- production database/network/runtime;
- внешняя документация.

Не доказывать в нулевую минуту доступность capability, которая может не понадобиться.

Повторяющийся дефект среды исправлять в setup/bootstrap или canonical project runner, а не новым ad-hoc workaround в каждой задаче.

## 6. Минимальный preflight и блокеры

До изменения файлов:

1. определить repo/worktree;
2. убедиться, что пользовательские изменения не затрагиваются;
3. получить base branch/base SHA;
4. проверить только инструменты, необходимые для начала текущего scope.

Если обязательный product gate недоступен в момент фактической необходимости:

- не выдавать результат как готовый;
- не обходить защиту;
- сохранить безопасную диагностику и полезные commits/PR;
- очистить только принадлежащие текущему прогону временные ресурсы;
- завершить прогон как `blocked`.

Rate limit/cooldown CodeRabbit обрабатывается специальным правилом внешнего review: это не product blocker и не причина ждать. После фиксации последнего exact head продолжи остальные gates и создай/обнови draft PR.

Не создавать инфраструктурный issue автоматически из-за одной transient-ошибки; делать это только при устойчивой проблеме или если task contract требует tracking.

Перед любой сменой branch в основном checkout Codex самостоятельно подтверждает:

- ожидаемый путь репозитория, `git rev-parse --show-toplevel` и `origin`;
- current branch и tracking/upstream;
- `git status`, staged, unstaged и untracked files;
- локальные commits, отсутствующие на upstream, и ahead/behind/divergence;
- существование целевой remote branch.

Если обнаружены локальные изменения, unpublished commits или divergence, происхождение состояния устанавливается до смены branch. Нельзя автоматически stash/drop, reset, rebase, merge, force-push или удалять неизвестные файлы.

## 7. Классы задач

### Fast-track

Только опечатки, формулировки, комментарии, ссылки и очевидный локальный diff без изменения control flow/state/security/architecture.

Минимум:

1. preflight/base SHA;
2. целевой файл;
3. минимальный diff;
4. format/syntax;
5. relevant check;
6. final diff + secret scan;
7. commit/PR;
8. required checks и достаточный review;
9. merge + короткий post-merge smoke.

### Стандартный

Обычный bugfix/feature в известной подсистеме:

- релевантная архитектурная разведка;
- code/tests;
- targeted checks;
- сквозная проверка в разумной границе;
- adversarial self-review;
- внешний review checkpoint согласно разделу 20.

### Расширенный

Используется для:

- `master`, upstream sync, `personal/stable` update path;
- Start/Update/Repair/Build;
- Python/dependencies/`uv.lock`;
- device/input/OCR/combat/Operation Siren;
- MCP/security/privacy;
- production/data migration/recovery;
- нескольких подсистем или риска потери данных.

Если класс неочевиден, выбрать более строгий.

## 8. Модель веток

### `master`

Чистое зеркало `wess09/AzurPilot:master`. Fork-only changes запрещены. Обновление — только по разделу 9.

### `personal/stable`

Стабильная пользовательская версия и источник автоматического обновления:

```text
origin/personal/stable
```

Не используется как рабочая ветка. Изменения попадают только через PR и required gates.

### `codex/*`

Новые задачи:

```text
codex/<task>
```

Одна задача — одна рабочая ветка. Ошибка теста или fix реализации не создаёт новую ветку.

### `chatgpt/*`

Legacy. Существующую ветку можно закончить, если она однозначно относится к задаче; новые задачи используют `codex/*`.

### `sync/*`

Только upstream sync:

```text
sync/upstream-master-YYYYMMDD-<short-sha>
```

## 9. Upstream sync `master`

### Pre-check

Перед sync:

1. получить `origin/master` и `upstream/master`;
2. подтвердить remotes;
3. доказать ancestry/fast-forward;
4. просмотреть переносимый commit range/diff;
5. выполнить релевантные security/secret checks.

При divergence запрещены reset/rebase/force push/обычный merge. Синхронизация блокируется и оформляется controlled conflict/divergence workflow.

### Sync PR

Создать `sync/upstream-master-...` точно на `upstream/master`, открыть PR в `master`, указать old/new SHA, range, существенные подсистемы, checks/risks и пройти review/CI.

### Применение

После gates выполнить только non-force fast-forward и подтвердить:

```text
origin/master == sync branch == upstream/master
```

Fork-only diff должен отсутствовать. Merge/squash/rebase commit для зеркального sync не использовать.

## 10. Перенос upstream в `personal/stable`

Не переносить upstream механически.

Использовать отдельную `codex/port-upstream-<topic>` и:

1. изучить upstream diff;
2. сохранить намеренные отличия форка;
3. адаптировать персональный runtime/PowerShell-контур;
4. обновить tests/docs;
5. проверить migration/rollback;
6. пройти расширенный pipeline отдельным PR.

## 11. Основной checkout и дополнительная изоляция

Для последовательной разработки основной Windows checkout проекта `C:\AzurPilot` является обычной рабочей копией Codex. Перед началом новой задачи:

```text
fetch origin
→ switch personal/stable
→ fast-forward only до origin/personal/stable
→ создать codex/<task>
→ работать в C:\AzurPilot
```

Если в checkout уже открыта однозначно относящаяся к незавершённой задаче `codex/*` branch, продолжать её после проверки exact head. После публикации feature-ветки оставлять checkout на ней, пока PR ожидает review; автоматически возвращаться на `personal/stable` не нужно.

Disposable clone/worktree допустим только при реальной необходимости: параллельная разработка, опасный reproduction/experiment, несовместимое состояние зависимостей/runtime, destructive recovery testing или явный запрос пользователя. Он не является default и не должен использоваться для переноса обычного diff.

Для review разрешён отдельный WSL2 Arch clone, но он не является implementation checkout: он получает exact branch/head, выполняет независимый CodeRabbit review, а все подтверждённые fixes вносятся в `C:\AzurPilot`.

В любой дополнительной среде base SHA фиксируется до изменений, пользовательские config/secrets не копируются без необходимости, временные artifacts отделяются, а после завершения удаляются только ресурсы текущей задачи. Destructive Git внутри disposable среды регулируется разделом 22.

## 12. Рабочий цикл

### Разведка

- live branch/PR state;
- base SHA;
- класс задачи;
- релевантный код/tests/history/context;
- затронутые boundaries;
- risks/checks/rollback.

### План

Зафиксировать outcome, scope, ожидаемый diff, gates, review checkpoints, критерии остановки до `READY_FOR_CHATGPT_REVIEW`, а также отдельный post-merge/rollback план. Не создавать отдельный plan-файл без необходимости.

### Реализация

- минимальный связный diff;
- не форматировать посторонние файлы;
- dependencies/network sources менять только с обоснованием;
- не создавать `_v2/_final/_fixed` вместо исправления текущего файла;
- tests/docs обновлять вместе с поведением.

### Проверка

Порядок:

1. static/syntax/parser;
2. lint/static analysis;
3. targeted tests;
4. adversarial self-review base→head;
5. внешний review checkpoint на существенном/рискованном milestone;
6. полный релевантный test set перед PR/final checkpoint;
7. dependency/build/security/secret gates;
8. controlled smoke;
9. GUI/browser/emulator/game acceptance только для relevant scope;
10. final diff.

Полный suite не повторять после каждого мелкого fix. После fix сначала повторять затронутые checks. Полный повтор нужен после существенного code diff, изменения общего контракта/зависимостей или для диагностики.

После PR exact-head required CI является авторитетным повтором постоянных gates; не дублировать локально тот же полный CI без причины.

## 13. Последовательные passes одного Codex

### Implementer pass

Разведка, implementation, tests, docs, первичная диагностика.

### Adversarial self-review

Перечитать фактический base→head diff как незнакомое изменение и искать:

- несоответствие задаче/scope creep;
- пропущенные call sites;
- regressions/error handling;
- fail-open/fail-closed;
- idempotency/concurrency, если релевантны;
- недостаточные tests/docs;
- Git/workflow violations.

Собственные прежние объяснения не считаются доказательством корректности.

### Security pass

Для чувствительных/расширенных изменений тот же основной Codex отдельно проверяет trust boundaries, findings, validation/severity, fix verification, secrets/privacy. Внешний scanner/reviewer остаётся независимым gate, если предусмотрен проектом.

## 14. PowerShell

При изменении `.ps1`/`.psm1` с Git-командами применяется `POWERSHELL-GIT-RULES.md`.

Обязательные релевантные gates:

- фактический Parser через `pwsh`;
- PSScriptAnalyzer закреплённой версии;
- disposable Git smoke для изменённой Git-логики;
- Windows integration smoke для Start/Update/Repair/Build и другого затронутого Windows flow.

Статический аудит не заменяет обязательный runtime gate.

## 15. Контракт Start/Update/Repair/Build

```text
scripts/Start-AzurPilot.ps1
scripts/Update-AzurPilot.ps1
scripts/Repair-AzurPilot.ps1
scripts/Build-AzurPilot.ps1
```

- **Start:** запускает подготовленную установку; не владеет Git update.
- **Update:** единственный владелец update path; безопасная схема `fetch → history check → merge --ff-only`; без reset/rebase и уничтожения local changes.
- **Repair:** диагностирует и транзакционно восстанавливает `.venv`, сохраняя rollback state до успешной проверки.
- **Build:** подготавливает уже полученный checkout; не клонирует repo, не подменяет Update, не уничтожает config, проверяет hashes загружаемых artifacts.

Изменение одной команды не должно захватывать обязанности другой.

## 16. Python и зависимости

Формальный контракт задаётся `pyproject.toml`/`uv.lock`; текущий проверяемый Windows runtime — Python 3.14.6.

Не выдумывать команды: сначала читать фактический `pyproject.toml`, `uv.lock` и `docs/ci.md`.

При dependency change обязательны согласованность lock, clean locked sync, релевантные tests/rollback, source/vulnerability check и license review для новой зависимости.

## 17. Secrets

Не записывать/печатать secrets в repo/logs/artifacts и не переносить пользовательскую конфигурацию в disposable worktree без необходимости.

Secret scanner обязателен перед публикацией relevant diff и перед merge, если после прошлого scan relevant diff менялся.

Проверять как минимум source diff, новые archives/binaries, `.env*`, configs/logs/dumps/backups, keys/tokens/cookies/auth headers и персональные identifiers.

При finding: блокировать публикацию/merge, удалить secret из рабочего дерева, проверить историю текущей ветки и при remote exposure использовать доступный revoke/rotate workflow без публикации значения.

## 18. GUI, emulator и игровая проверка

Запускать только когда изменение реально требует этого acceptance.

- тестовая конфигурация изолирована;
- irreversible gameplay/purchases/value-consuming actions запрещены;
- для OCR сохраняются только безопасные artifacts/metrics;
- проверять timeout/retry/exit conditions;
- после теста очищать принадлежащие задаче процессы/sessions/profiles.

Если обязательный безопасный acceptance невозможен, sensitive merge блокируется.

## 19. Коммиты

Commit должен быть логически цельным. Не дробить задачу ради формального числа commits и не создавать новый commit только из-за каждого review fix, если squash/amend безопасен и политика ветки это допускает.

Перед commit:

- final relevant diff;
- required format/syntax/targeted checks;
- secret scan;
- отсутствие случайных файлов.

Сообщение описывает смысл изменения (`fix(update): ...`, `feat(build): ...`), а не `fix/final/test`.

## 20. PR, review и merge

PR обязателен для `master`, `personal/stable`, standard/extended задач, dependency/security-sensitive изменений и Start/Update/Repair/Build.

PR body должен содержать только существенное: цель/scope, base SHA, ключевой diff, выполненные gates, migration/rollback и ограничения.

### Внешнее ревью

Внешний reviewer — **milestone gate**.

Схема:

1. завершить логически цельный слой;
2. targeted checks;
3. Codex adversarial self-review;
4. внешний review, если слой существенный/рискованный;
5. исправить findings, повторить targeted checks + self-review;
6. следующий внешний checkpoint — только после существенного нового code diff, изменения architecture/security/data contract или если reviewer требует re-check;
7. required CI на exact PR head.

Большая задача может иметь несколько review checkpoints, чтобы не накапливать десятки findings до конца. Небольшой standard diff обычно требует одного checkpoint.

Не запускать полный внешний review заново из-за typo/format/docs или узкой test-only правки без изменения production contract.

**Rate limit/cooldown:** не ждать таймер и не polling-loop внутри активного прогона. Для CodeRabbit сохранить branch/commit/PR, зафиксировать последний проверенный exact head и завершить текущий прогон в состоянии `READY_FOR_CHATGPT_REVIEW`; новый review возможен только в отдельном будущем запуске после доступности reviewer.

### Merge

Merge выполняется только после финального ревью пользователя через ChatGPT 5.6 Sol и отдельной текущей команды, однозначно относящейся к этому PR. До такой команды draft PR остаётся на `READY_FOR_CHATGPT_REVIEW`, даже если CI и CodeRabbit зелёные. Для `personal/stable` по умолчанию используется squash merge для небольших/средних PR; merge commit — только если самостоятельная история commits важна. Rebase merge — только с отдельным обоснованием.

Не считай CodeRabbit rate limit/cooldown product blocker: не жди его и не повторяй запрос в цикле. Зафиксируй последний exact head, выполни остальные доступные gates и передай draft PR с пометкой `READY_FOR_CHATGPT_REVIEW`.

`master` синхронизируется только процедурой раздела 9.

## 21. GitHub Actions

Предпочитать существующие reusable workflows и runners. Новый workflow создавать только для устойчивой повторяемой ценности, а не для разового запуска, компенсации временно отсутствующего инструмента или дублирования существующей проверки.

Workflow должен иметь ограниченные permissions, безопасно работать с недоверенным PR и использовать проектную политику pinning actions.

## 22. Опасные Git-операции

В пользовательском checkout, `master`, `personal/stable` и опубликованных ветках запрещены:

```text
git push --force
git push --force-with-lease
git reset --hard
git clean -fd
git clean -fdx
git checkout -f
git branch -D
git reflog expire
git gc --prune=now
```

Не обходить branch protection, не переписывать опубликованную историю и не уничтожать пользовательские uncommitted data.

В disposable clone/worktree destructive cleanup допустим только после проверки, что среда создана Codex для текущей задачи, не содержит пользовательских данных/secrets и полезный результат уже сохранён. Предпочтительно удалить весь worktree.

## 23. Ошибки и retry budget

Для ошибки:

1. сохранить достаточный output/evidence;
2. определить root cause;
3. отличить product defect от устойчивого setup/runner defect;
4. исправить текущую ветку/bootstrap;
5. повторить relevant checks;
6. выполнить self-review изменённой области.

Внешний reviewer после fix повторяется, если finding пришёл от него и нужен re-check, появился существенный production diff, изменился architecture/security/data contract или reviewer явно требует повтор.

Бюджет:

- transient infrastructure: до 2 быстрых повторов, если нет explicit cooldown;
- explicit reviewer rate limit/cooldown: 0 ожидания/polling, сохранить состояние и завершить run;
- flaky test: до 2 повторов с evidence;
- одна code root cause: до 3 fix/targeted-check циклов;
- security finding: до 2 fix/validation циклов.

После исчерпания бюджета merge блокируется, полезное состояние сохраняется, временные ресурсы безопасно очищаются.

## 24. Post-merge и rollback

После merge:

1. получить merged SHA;
2. проверить required checks/merged state;
3. выполнить короткий relevant smoke;
4. проверить Update/Build/Repair только если затронут эксплуатационный контур;
5. убедиться в ожидаемом diff/state;
6. удалить task branch/worktree/artifacts, если безопасно.

При regression destructive rollback не выполнять автоматически. Использовать controlled revert/hotfix branch и ускоренный relevant pipeline.

## 25. Branch protection

### `master`

- force push/delete запрещены;
- fork-only commits запрещены;
- required checks обязательны;
- возможен только узкий automation bypass для post-review fast-forward sync, если он уже предусмотрен ruleset.

### `personal/stable`

- force push/delete запрещены;
- PR + required checks обязательны;
- required checks не являются разрешением на merge;
- human final review и отдельная текущая команда пользователя обязательны перед merge;
- auto-merge допустим только после такой команды и при соблюдении остальных правил проекта.

### `codex/*`

- не использовать force push после публикации;
- до merge сохранять draft PR и ветку для финального ревью;
- после успешного merge удалять ветку согласно cleanup;
- полезную незавершённую ветку сохранять при blocker.

## 26. Definition of Done

### Pre-merge `READY_FOR_CHATGPT_REVIEW`

Задача готова к передаче на финальное ревью, когда:

- base branch/SHA и scope зафиксированы;
- работа выполнена в основном checkout либо в явно обоснованной дополнительной среде;
- diff минимален и без scope creep;
- релевантные local gates выполнены;
- tests обновлены там, где менялось поведение;
- полный suite выполнен в установленном checkpoint и не повторялся без причины;
- Codex adversarial self-review завершён;
- необходимые доступные внешние review checkpoints обработаны; ограничения CodeRabbit явно зафиксированы;
- security/secret gates выполнены в требуемом объёме;
- required CI зелёный на exact head;
- blocking review threads отсутствуют;
- draft PR содержит актуальные scope, base SHA, gates и ограничения;
- финальное ревью ChatGPT 5.6 Sol ожидает пользователя;
- merge не выполнялся без отдельной текущей команды пользователя.

### Post-merge completion

После явной команды пользователя дополнительно обязательны:

- revalidation exact head, required CI и review blockers;
- merge разрешённой стратегией;
- post-merge verification зелёный;
- docs/rollback обновлены там, где нужно;
- принадлежащие задаче временные ресурсы очищены;
- основной checkout и ветки приведены к согласованному состоянию.

Task-specific capability не входит в DoD, если соответствующий gate не относится к фактическому scope.

## 27. Progress updates и итоговый отчёт

Во время работы писать progress update только при:

- начале новой крупной фазы;
- факте, который меняет план;
- существенном checkpoint;
- blocker.

Update — 1–2 предложения с конкретным результатом. Не narrate routine calls вроде «читаю файл», «запускаю тест», «проверяю Git».

Финал краткий и доказательный. До merge используй `READY_FOR_CHATGPT_REVIEW`, после merge — `merged`, а при реальном невозможном gate — `blocked`:

```text
Статус: READY_FOR_CHATGPT_REVIEW / merged / blocked / reverted
Git: base SHA, branch, PR, merge SHA
Изменено: ключевые файлы/подсистемы
Проверено: фактически выполненные relevant gates + exact-head CI
Review: self-review, external checkpoints, blocking findings
Post-merge: relevant smoke/verification или `не применимо до merge`
Ограничения: только реальные
От пользователя требуется: ничего / неизбежный внешний шаг
```

Не дублировать в финале полные изменённые файлы, длинные test logs и историю каждого tool call, если пользователь прямо этого не просил.

## 28. Живое состояние

Активные branches, PR, SHAs, CI status и upstream state не фиксируются здесь как постоянные факты. Получать их заново при соответствующей операции.

## 29. Итоговая политика

Штатный pre-merge результат:

```text
проверенный commit/draft PR
+ требуемые review/CI gates
+ передача на финальное ChatGPT review
+ остановка без merge
+ короткий отчёт
```

После отдельной текущей команды пользователя к этому результату добавляются разрешённый merge, post-merge verification и cleanup. Если обязательный product gate недоступен или не пройден, корректный результат — сохранённое полезное состояние и `blocked`, а не непроверенный merge и не просьба пользователю вручную закончить технический цикл. Исключение — CodeRabbit rate limit: он не блокирует draft PR и фиксируется как ограничение review.
