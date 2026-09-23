# Регламент автономной работы с Git для AzurPilot Private RU

Репозиторий: `AliceLiddell01/AzurPilot-private-Ru`  
Upstream: `wess09/AzurPilot`

Этот файл — **единственный владелец** Git/branch/PR/upstream/merge/rollback/cleanup
lifecycle. Он описывает текущее правило, а не changelog его эволюции.

Модель ответственности: агент выполняет доступную техническую работу до
готового draft PR; пользователь выполняет финальное ревью, а merge разрешается
только отдельной текущей явной командой.

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
→ для новой задачи: fetch origin → personal/stable → FF-only → <domain>/<unique-capability-name>
→ реализация логическими слоями
→ targeted checks
→ Codex adversarial self-review
→ финальные релевантные gates
→ внешний review только по явному запросу или обязательному project policy
→ commit / draft PR
→ required CI exact head
→ `READY_FOR_CHATGPT_REVIEW`
→ STOP до финального ревью пользователя
→ [явная текущая команда merge]
→ post-merge verification
→ cleanup
→ короткий доказательный отчёт
```

Codex не просит пользователя запускать команды, тесты, Git, CI, создавать PR или проверять промежуточные файлы, если это технически доступно самому Codex. Merge не является частью автоматического финала: он выполняется только после отдельной текущей команды пользователя и финального пользовательского review.

**Subagents не обязательны.** По умолчанию один основной Codex выполняет все внутренние passes последовательно. Независимость обеспечивается внешним reviewer/tool, когда он предусмотрен task contract.

## 3. Контекст и источники

Использовать progressive disclosure.

Приоритет:

1. фактический код/конфигурация целевой ветки;
2. ближайшие исполняемые тесты и runtime-поведение;
3. корневой `AGENTS.md` и релевантные файлы `.codex/context/`;
4. README/Wiki форка;
5. upstream diff/issues/PR;
6. DeepWiki как архитектурная карта;
7. официальная документация конкретного API/инструмента.

Правила:

- сначала `.codex/context/INDEX.md`, затем только нужные документы;
- `GIT-WORKFLOW.md` читать по релевантным разделам;
- не перечитывать большие документы после каждого небольшого fix;
- не выполнять общий web/docs survey без конкретного вопроса;
- не расширять область проблемы без evidence из call graph, tests, diff или runtime-поведения;
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
| `azurpilot.tooling` | Git, `.venv`, update, rollback |
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
- проверки Windows Python tooling и native integration;
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

Специфичные для провайдера правила retry/rate limit внешнего review принадлежат
соответствующему review skill. CodeRabbit запускается только по явному запросу
пользователя или обязательному policy; отсутствие запроса фиксируется как
`NOT_RUN`, а не как ограничение провайдера.

Не создавать инфраструктурный issue автоматически из-за одной transient-ошибки; делать это только при устойчивой проблеме или если task contract требует tracking.

Перед любой сменой branch в основном checkout Codex самостоятельно подтверждает:

- ожидаемый путь репозитория, `git rev-parse --show-toplevel` и `origin`;
- текущая ветка и tracking/upstream;
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
7. commit/draft PR;
8. required checks и достаточный review;
9. `READY_FOR_CHATGPT_REVIEW` → STOP до финального пользовательского review и отдельной
   текущей команды пользователя.

Fast-track не даёт разрешения на merge. После финального пользовательского review и
отдельной текущей команды пользователя запускается обычный merge gate, включая
exact-head revalidation и короткий relevant post-merge smoke.

### Стандартный

Обычный bugfix/feature в известной подсистеме:

- релевантная архитектурная разведка;
- code/tests;
- targeted checks;
- сквозная проверка в разумной границе;
- adversarial self-review;
- внешний review только по явному запросу или обязательному project policy.

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

### Capability branches

Новая capability использует уникальное имя без roadmap/stage номера. Для новой
обычной работы default — ветка вида `<domain>/<unique-capability-name>`, заданная
task contract. Одна задача — одна рабочая ветка. Ошибка теста или fix
реализации не создаёт новую ветку.

`codex/*` остаётся compatibility/legacy namespace для уже опубликованных
веток. Существующую `codex/*` branch можно продолжить только после проверки
exact repository identity, task ownership и head; новые обычные задачи этот
namespace не используют.

### `chatgpt/*`

Legacy. Существующую ветку можно закончить, если она однозначно относится к задаче и exact identity подтверждена; новые задачи используют формат
`<domain>/<unique-capability-name>`.

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

Для последовательной разработки основной Windows checkout проекта является обычной рабочей копией Codex. Перед началом новой задачи:

```text
fetch origin
→ switch personal/stable
→ fast-forward only до origin/personal/stable
→ создать branch из task contract в формате <domain>/<unique-capability-name>
→ работать в основном checkout
```

Если в checkout уже открыта однозначно относящаяся к незавершённой задаче `codex/*` или другая capability branch, продолжать её после проверки exact repository identity и head. После публикации feature-ветки оставлять checkout на ней, пока PR ожидает review; автоматически возвращаться на `personal/stable` не нужно.

Disposable clone/worktree допустим только при реальной необходимости: параллельная разработка, опасный reproduction/experiment, несовместимое состояние зависимостей/runtime, destructive recovery testing или явный запрос пользователя. Он не является default и не должен использоваться для переноса обычного diff.

Для CodeRabbit review используется host-native executable текущей host OS в том
же canonical implementation checkout: Windows использует `coderabbit.exe`, а
POSIX host использует `coderabbit`. Adapter обязан доказать exact
repository/root/base/head, clean index/worktree и postcondition того же
candidate; отдельные clone, worktree, UNC route, WSL bridge и wrapper не
являются допустимой заменой.

Stacked publication использует только реальную опубликованную parent branch.
Запрещены `codex/base-*`, temporary/scratch/transport/helper remote ref и
вспомогательная remote publication. Если parent local HEAD ещё не совпадает с
parent remote HEAD, canonical delivery/PR workflow возвращает typed
`TOOLING_STACKED_PARENT_UNPUBLISHED` и не создаёт обходной ref.

В любой дополнительной среде base SHA фиксируется до изменений, пользовательские config/secrets не копируются без необходимости, временные artifacts отделяются, а после завершения удаляются только ресурсы текущей задачи. Destructive Git внутри disposable среды регулируется разделом 22.

## 12. Рабочий цикл

### Разведка

- live branch/PR state;
- base SHA;
- класс задачи;
- релевантный код/tests/history/context;
- затронутые границы;
- risks/checks/rollback.

### План

Зафиксировать outcome, scope, ожидаемый diff, gates, review checkpoints, критерии остановки до `READY_FOR_CHATGPT_REVIEW`, а также отдельный post-merge/rollback план. Не создавать отдельный plan-файл без необходимости.

### Реализация

До публикации candidate или запуска review effective diff относительно exact
base проходит `azur mcp impact --base <exact-base-sha>`. При `REQUIRED`
штатный `azur mcp reconcile --source --bump auto` и последующие integrity и
base-to-head compatibility checks обязательны; изменение source set после
reconciliation делает предыдущий результат stale. Generated MCP artifacts
являются производным scope той же задачи. Source reconciliation имеет только
`source_reconciled=true`; если live MCP входит в обязательный gate, напрямую
вызови через PATH `azur mcp status`. При `runtime_state=stale` или
`runtime_state=stopped` выполни единственный typed runtime repair path
`azur mcp reconcile` без `--source`, затем повторный status с
`runtime_ready=true`. Исходный stale/stopped status до этой попытки не
является финальным blocker-ом; same-repository stale marker допустимо
восстанавливать только typed recorded-identity cleanup с unchanged marker и
STOPPED/no-conflict postcondition. Unknown/foreign ownership, port conflict,
failure stop/start или mismatch postcondition остаются fail-closed. Source-only result и
`MCP_RUNTIME_UNAVAILABLE` не закрывают live acceptance. Внутренние
`module.*_mcp`, supervisor scripts и Python module launchers напрямую не
используются.

Для любой project-owned operator capability, уже представленной через `azur`,
каноничен только literal invocation `azur ...` из PATH текущей shell. `uv run`,
`python -m azurpilot`, `.venv/.../azur`, absolute executable path и shell
wrapper — запрещённые обходы, а не эквивалентные формы. `uv` разрешён для
dependency/bootstrap/test/build задач, где он является владельцем операции.

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
5. внешний review только по явному запросу или обязательному policy;
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

Для чувствительных/расширенных изменений тот же основной Codex отдельно проверяет trust-границы, findings, validation/severity, проверку исправления и secrets/privacy. Внешний scanner/reviewer остаётся независимым gate, если предусмотрен проектом.

## 14. Контракт Start/Update/Repair/Build

```text
azur start
azur update
azur repair
azur build
```

- **Start:** запускает подготовленную установку; не владеет Git update.
- **Update:** единственный владелец update path; безопасная схема `fetch → history check → merge --ff-only`; без reset/rebase и уничтожения local changes.
- **Repair:** диагностирует и транзакционно восстанавливает `.venv`, сохраняя rollback state до успешной проверки.
- **Build:** подготавливает уже полученный checkout; не клонирует repo, не подменяет Update, не уничтожает config, проверяет hashes загружаемых artifacts.

Изменение одной команды не должно захватывать обязанности другой. Windows
acceptance проверяет эти typed Python services, а PowerShell остаётся только
runner glue.

## 15. Python и зависимости

Формальный контракт задаётся `pyproject.toml`/`uv.lock`; текущий проверяемый Windows runtime — Python 3.14.6.

Не выдумывать команды: сначала читать фактический `pyproject.toml`, `uv.lock` и `docs/ci.md`.

При dependency change обязательны согласованность lock, clean locked sync, релевантные tests/rollback, source/vulnerability check и license review для новой зависимости.

## 16. Secrets

Не записывать/печатать secrets в repo/logs/artifacts и не переносить пользовательскую конфигурацию в disposable worktree без необходимости.

Secret scanner обязателен перед публикацией relevant diff и перед merge, если после прошлого scan relevant diff менялся.

Проверять как минимум source diff, новые archives/binaries, `.env*`, configs/logs/dumps/backups, keys/tokens/cookies/auth headers и персональные identifiers.

При finding: блокировать публикацию/merge, удалить secret из рабочего дерева, проверить историю текущей ветки и при remote exposure использовать доступный revoke/rotate workflow без публикации значения.

## 17. GUI, emulator и игровая проверка

Запускать только когда изменение реально требует этого acceptance.

- тестовая конфигурация изолирована;
- irreversible gameplay/purchases/value-consuming actions запрещены;
- для OCR сохраняются только безопасные artifacts/metrics;
- проверять timeout/retry/exit conditions;
- после теста очищать принадлежащие задаче процессы/sessions/profiles.

Если обязательный безопасный acceptance невозможен, sensitive merge блокируется.

## 18. Коммиты

Commit должен быть логически цельным. Не дробить задачу ради формального числа commits и не создавать новый commit только из-за каждого review fix, если squash/amend безопасен и политика ветки это допускает.

Перед commit:

- final relevant diff;
- required format/syntax/targeted checks;
- secret scan;
- отсутствие случайных файлов.

Сообщение описывает смысл изменения (`fix(update): ...`, `feat(build): ...`), а не `fix/final/test`.

## 19. PR, review и merge

PR обязателен для `master`, `personal/stable`, standard/extended задач, dependency/security-sensitive изменений и Start/Update/Repair/Build.

PR body должен быть создан из typed structured model через временный внешний
Markdown-файл и `--body-file`, а затем прочитан обратно. Обязательны разделы
`Цель`, `Scope`, `Реализация`, `Проверки`, `CI`, `Security / secret scan`,
`CodeRabbit review и disposition`, `Readiness`, `Migration / rollback`,
`Ограничения`.
В body фиксируются repository/base/head identity, base SHA, подсистемы,
фактически выполненные gates, security result, migration/rollback,
ограничения и предполагаемый merge method. Inline shell body и implicit
repository context запрещены.
Body является полноценным русскоязычным отчётом для человека, а не коротким
автоматическим summary: в каждой секции должны быть конкретные факты, а в
scope, реализации, проверках, CI, security, rollback и ограничениях —
маркированные пункты. English допускается только для technical identifiers,
названий API/инструментов, protocol tokens, CI contexts и других специальных
слов, которые нельзя безопасно переводить. Renderer обязан отклонять
полупустой body до provider call.

Для delivery допустим только manifest с закрытой схемой, exact repository,
branch/base/head, preimage/postimage и allowlist paths. В index добавляются
только declared paths; Gitleaks запускается по staged scope и exact committed
range. Push — обычный explicit refspec без force/force-with-lease с
последующей проверкой exact remote SHA. Неизвестный результат push переводится
в read-only recovery без blind retry.

GitHub PR проверяется с явными `--repo`, `--base`, `--head`, draft mode и
read-back exact identity. PR body сохраняет секцию CodeRabbit для typed evidence;
если ревью не запрошено, в ней указывается `NOT_RUN` и «не запрошено». Только
явная команда пользователя запускает CodeRabbit; его provider lifecycle и triage
описаны в [отдельном skill](../../.agents/skills/azurpilot-coderabbit-review/SKILL.md).

### Внешнее ревью

Внешнее ревью не является обязательным milestone для каждой задачи. Запускай его
только по явной команде пользователя или обязательному project policy. Для
CodeRabbit незапрошенный review остаётся `NOT_RUN` и не является ограничением;
если пользователь запросил review, применяй provider skill, фиксируй exact-head
результат и разбирай actionable findings по его правилам. Результат ревью не
заменяет tests, CI, security gates или явное разрешение merge.

Если CodeRabbit skill вернул `rate_limited`, Git lifecycle может достичь
`READY_FOR_CHATGPT_REVIEW`, когда внешний review не является обязательным
policy gate и нет actionable finding или blocking review thread. Это не
отменяет required CI, security/secret scan, mandatory product/live acceptance
или blocking review threads. Правила ожидания, retry и triage провайдера
принадлежат coderabbit skill/reference.

Readiness фиксируется typed state: обязательный gate имеет `PASS`, `FAIL`,
`BLOCKED_PRECONDITION` или `NOT_REQUIRED`; если MCP impact равен `REQUIRED`,
`fresh_mcp_client_acceptance` является обязательным gate, не может иметь
`NOT_REQUIRED` и принимает `PASS` только по evidence независимой свежей MCP
client/session с initialize, negotiated catalog, contract и read-only calls.
Codex registration check хранится отдельно и не является product gate.
`FAIL`/`BLOCKED_PRECONDITION`
требует `overall_outcome=BLOCKED` и запрещает `READY_FOR_CHATGPT_REVIEW` и
merge-ready, даже если implementation complete. Provider rate limit является
review limitation и не меняет mandatory product/live gate.

### Merge

Обязательная последовательность одна:

1. пользователь завершил финальное пользовательское ревью текущего head PR;
2. **после этого** пользователь отправил отдельное текущее сообщение, однозначно
   разрешающее merge именно этого PR;
3. только затем выполняются повторная проверка точного head и разрешённый merge.

Старое разрешение, разрешение для другого PR и общая фраза вроде «доведи до
конца» недостаточны. Зелёные CI, CodeRabbit и self-review не являются
разрешением на merge и не заменяют ни финальное пользовательское ревью, ни
отдельную текущую команду.

До такой команды draft PR остаётся в `READY_FOR_CHATGPT_REVIEW`. После команды
состояние переходит в `merge-authorized` и выполняется exact-head
revalidation. Для `personal/stable` по умолчанию используется squash merge для
небольших/средних PR; merge commit допустим, когда самостоятельная история
коммитов важна. Rebase merge требует отдельного обоснования.

Если после финального пользовательского ревью изменился относящийся к задаче
diff, прежнее `merge-authorized` состояние недействительно: повтори
затронутые проверки и ревью и получи новое актуальное разрешение по тому же
pre-merge контракту.

`master` синхронизируется только процедурой раздела 9.

## 20. GitHub Actions

Предпочитать существующие reusable workflows и runners. Новый workflow создавать только для устойчивой повторяемой ценности, а не для разового запуска, компенсации временно отсутствующего инструмента или дублирования существующей проверки.

Workflow должен иметь ограниченные permissions, безопасно работать с недоверенным PR и использовать проектную политику pinning actions.

## 21. Опасные Git-операции

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

## 22. Ошибки и retry budget

Для ошибки:

1. сохранить достаточный output/evidence;
2. определить root cause;
3. отличить product defect от устойчивого setup/runner defect;
4. исправить текущую ветку/bootstrap;
5. повторить relevant checks;
6. выполнить self-review изменённой области.

Запрошенный внешний reviewer после fix повторяется только если нужен re-check
его finding или он явно требует повтор. Без запроса пользователя или обязательного
policy новые diff сами по себе не запускают CodeRabbit.

Бюджет:

- transient infrastructure: до 2 быстрых повторов, если нет explicit cooldown;
- retry budget внешнего reviewer определяется его специализированным skill/contract;
- flaky test: до 2 повторов с evidence;
- одна code root cause: до 3 fix/targeted-check циклов;
- security finding: до 2 fix/validation циклов.

После исчерпания бюджета retry для обязательного product/security gate merge
блокируется, полезное состояние сохраняется, временные ресурсы безопасно
очищаются.

Для незапрошенного CodeRabbit `NOT_RUN` не является limitation и не блокирует
Draft PR или readiness. Provider-specific retry/triage нужны только после явного
review request и описаны в sibling skill.

## 23. Post-merge и rollback

После merge:

1. получить merged SHA;
2. проверить required checks/merged state;
3. выполнить короткий relevant smoke;
4. проверить Update/Build/Repair только если затронут эксплуатационный контур;
5. убедиться в ожидаемом diff/state;
6. удалить task branch/worktree/artifacts, если безопасно.

При regression destructive rollback не выполнять автоматически. Использовать controlled revert/hotfix branch и ускоренный relevant pipeline.

## 24. Branch protection

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

### Capability branches

Capability branches, включая explicit domain-prefixed branches, должны:

- не использовать force push после публикации;
- до merge сохранять draft PR и ветку для финального ревью;
- после успешного merge удалять ветку согласно cleanup;
- полезную незавершённую ветку сохранять при blocker.

## 25. Definition of Done

### Pre-merge `READY_FOR_CHATGPT_REVIEW`

Задача готова к передаче на финальное ревью, когда:

- base branch/SHA и scope зафиксированы;
- работа выполнена в основном checkout либо в явно обоснованной дополнительной среде;
- diff минимален и без scope creep;
- effective candidate diff прошёл MCP impact classification; при затронутом
  source set canonical bundle и derived artifacts согласованы повторно;
- релевантные local gates выполнены;
- tests обновлены там, где менялось поведение;
- полный suite выполнен в установленном checkpoint и не повторялся без причины;
- Codex adversarial self-review завершён;
- явно запрошенные или обязательные внешние review checkpoints обработаны;
- незапрошенный CodeRabbit остаётся `NOT_RUN`, а не limitation;
- security/secret gates выполнены в требуемом объёме;
- required CI зелёный на exact head;
- blocking review threads отсутствуют;
- draft PR содержит актуальные scope, base SHA, gates и ограничения;
- typed readiness не содержит противоречия между mandatory gate и overall outcome;
- PR ожидает финального пользовательского ревью;
- последовательность разрешения merge соответствует разделу `### Merge`.

### Post-merge completion

После явной команды пользователя дополнительно обязательны:

- revalidation exact head, required CI и review blockers;
- merge разрешённой стратегией;
- post-merge verification зелёный;
- docs/rollback обновлены там, где нужно;
- принадлежащие задаче временные ресурсы очищены;
- основной checkout и ветки приведены к согласованному состоянию.

Task-specific capability не входит в DoD, если соответствующий gate не относится к фактическому scope.

## 26. Progress updates и итоговый отчёт

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

## 27. Живое состояние

Активные branches, PR, SHAs, CI status и upstream state не фиксируются здесь как постоянные факты. Получать их заново при соответствующей операции.

## 28. Итоговая политика

Штатный pre-merge результат:

```text
проверенный commit/draft PR
+ требуемые review/CI gates
+ передача на финальное пользовательское review
+ остановка без merge
+ короткий отчёт
```

После отдельной текущей команды пользователя к этому результату добавляются разрешённый merge, post-merge verification и cleanup. Если обязательный product gate недоступен или не пройден, корректный результат — сохранённое полезное состояние и `blocked`, а не непроверенный merge. Rate limit фиксируется как reviewer limitation только для CodeRabbit review, который был явно запрошен.
