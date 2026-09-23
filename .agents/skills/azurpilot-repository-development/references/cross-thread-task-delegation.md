# Optional Codex registration check

Этот reference — единственный repository-level контракт для проверки effective
Codex registration через отдельную Codex task/thread, когда изменение затрагивает
Codex/plugin registration, client-visible schema или routing. Он не является
обязательным MCP acceptance gate, не создаёт новую feature, ветку или PR и не
передаёт другой task ownership разработки.

Обязательный продуктовый gate `fresh_mcp_client_acceptance` закрывается
`azur mcp accept`: команда сама запускает отдельный процесс с новым клиентом MCP,
выполняет `initialize()`, согласованный `tools/list`, проверки контракта, каталога и версии,
а также обязательные вызовы только для чтения. Не создавай `FreshMcpClientPlan` и не
импортируй внутренний `mcp_client` в обход этой штатной команды. Задача Codex
не заменяет проверку нового клиента и не используется как её транспорт.

## Когда контракт применяется

Сначала coordinator task штатными project-owned действиями подтверждает source и
runtime:

1. `azur mcp reconcile --source --bump auto` подтверждает только
   `source_reconciled`.
2. `azur mcp status` читает текущее source/runtime состояние; если live gate
   входит в scope, итоговый status должен подтвердить `runtime_ready=true`.
3. При `runtime_state=stale` или `runtime_state=stopped` coordinator выполняет
   единственный typed runtime repair path `azur mcp reconcile` без `--source`,
   затем повторно читает status. Same-repository stale marker допускает только
   recorded exact-identity cleanup с unchanged marker и STOPPED/no-conflict
   postcondition; unknown/foreign ownership, invalid marker/liveness, чужой
   port owner и failure stop/start остаются typed `BLOCKED_PRECONDITION`.
   `LOCAL_MCP_SUPERVISOR_STOPPED` — диагностический сигнал для этого typed
   recovery path, а не самостоятельный shortcut.
4. Если MCP impact `REQUIRED`, coordinator отдельно выполняет обязательный
   `fresh_mcp_client_acceptance`; source snapshot, status и unit tests его не
   заменяют.

Если изменение не затрагивает Codex/plugin registration, этот reference не
требуется. `runtime_ready=true` вместе с `session_state=not_observable` не
является runtime failure и не делает Codex registration check обязательной.
Если check нужна, она фиксирует только `effective_codex_registration` (effective
Codex registration); её
отсутствие или недоступность является отдельным внешним ограничением Codex.

## Роли и границы

- `Coordinator task` остаётся владельцем work item, diff, Git/PR lifecycle и
  итогового решения по MCP readiness.
- `Fresh independent task/thread` — отдельная project-scoped Codex task,
  созданная штатной orchestration surface coordinator task, только для bounded
  проверки Codex registration. Она возвращает evidence coordinator и не закрывает
  `fresh_mcp_client_acceptance`.
- `Subagent`, fork текущей agent session, same-directory child worker и
  Connected App/remote fallback не считаются fresh independent task/thread.

Создание выполняется через доступную Codex task orchestration surface, а
coordinator ждёт terminal outcome через штатное ожидание/чтение task. Не просите
пользователя вручную создавать новый чат и не используйте shell, browser или
remote app как замену task orchestration.

## Codex Desktop orchestration boundary

В Codex Desktop coordinator создаёт именно новый stored task через
`mcp__codex_app__create_thread`, а не через `fork_thread`, subagent или
same-directory child. Сначала coordinator разрешает project через
`list_projects` и выбирает проект AzurPilot с `isGitRepository=true`, затем
определяет текущую coordinator branch и exact committed HEAD. До создания task
он обязан проверить, что branch tip равен ожидаемому exact HEAD. Если branch не
определяется однозначно или tip уже расходится с expected HEAD, task не
создаётся, а optional check получает typed `BLOCKED_PRECONDITION` с точной
причиной.

### Канонический branch-based запуск

Для optional check используется `target.type=project` с worktree и
`startingState.type=branch`, где `branchName` — фактическая branch coordinator
work item. Перед созданием coordinator уже проверил branch tip, поэтому это
канонический способ передать task тот же committed exact HEAD; setup worktree
остаётся автоматическим и не требует ручного переключения состояния. В prompt
передаются repository identity, branch, exact expected HEAD, bounded registration
scope и запрет на изменение code, branch, PR и lifecycle.

### Неиспользуемый working-tree маршрут

`startingState.type=working-tree` не является exact-head continuation: этот режим
может создать task от другого состояния и потому не заменяет проверку branch tip.
Нельзя компенсировать mismatch ручным `git switch` или `git checkout` после
создания task.

`create_thread` асинхронен: готовый результат содержит настоящий `threadId` и
`hostId`, а промежуточный `clientThreadId` нельзя передавать в ожидание, чтение
или follow-up tools. Coordinator ждёт ready task через `wait_threads` с
настоящим `threadId`, затем читает её terminal turn через `read_thread` с
outputs; создание task, промежуточный progress или отсутствие ошибки не
являются registration evidence. Если setup вернул только `clientThreadId`,
coordinator сначала наблюдает появление ready `threadId` и только после этого
начинает bounded wait.

Перед optional registration check task сначала независимо проверяет repository
identity, фактический exact HEAD и branch state. Фактический HEAD обязан
совпасть с переданным expected HEAD; detached HEAD допустим для verification
worker. Fresh task не выполняет post-create `git switch` или post-create
`git checkout` для исправления mismatch: при несовпадении возвращается typed
`BLOCKED_PRECONDITION`, а registration calls не выполняются.

Если check продолжается, task использует фактически callable Codex route и
возвращает negotiated registration, client-visible contract/catalog и runtime
evidence только в пределах этой optional проверки. Она не запускает smoke или
mutation без отдельного разрешения scope; shell/HTTP/ADB, прямой внутренний
module и source snapshot не заменяют negotiated MCP evidence.

## Каноническая последовательность

1. Coordinator фиксирует bounded context: repository identity, coordinator
   branch, exact expected HEAD, затронутую Codex registration surface, route,
   source/plugin/compatibility state и ожидаемый runtime state.
2. До создания task coordinator проверяет, что branch tip совпадает с exact
   expected HEAD. При неоднозначной branch или mismatch optional check получает
   typed `BLOCKED_PRECONDITION` и task не создаётся.
3. Coordinator создаёт fresh independent task/thread с
   `startingState.type=branch` и передаёт context вместе с запретом изменять
   production code, branch, PR или lifecycle.
4. Fresh task до registration calls заново проверяет repository identity,
   фактический exact HEAD и branch state. HEAD должен быть равен expected HEAD;
   detached HEAD допустим. Она не принимает source config, старый snapshot или
   сам факт создания task за evidence и не выполняет post-create switch/checkout.
5. После этого fresh task проверяет только effective Codex registration и
   negotiated client-visible contract/catalog в обозначенном scope. Ошибка этой
   проверки классифицируется как результат `codex_registration_check`, а не как
   MCP product gate failure.
6. Fresh task возвращает machine-readable или иным образом проверяемый terminal
   result с evidence: exact identity/HEAD, route, registration, contract/catalog,
   runtime, result и ограничениями без секретов.
7. Coordinator дожидается и читает terminal result delegated task, сохраняет
   evidence как отдельную `IntegrationCheck` и не смешивает её с обязательным
   `fresh_mcp_client_acceptance`.
8. Если orchestration surface недоступна, branch tip не совпал с expected HEAD
   или fresh task не подтверждает registration, optional check получает typed
   `BLOCKED_PRECONDITION` с точной причиной. Это не переводит уже успешный fresh
   MCP client gate в `FAIL`/`BLOCKED_PRECONDITION`.

Fresh task не исправляет обнаруженный defect и не становится вторым владельцем
разработки. Если она находит проблему, она возвращает evidence coordinator;
изменение кода выполняется coordinator по обычному owner contract.

## Запрещённые сокращения

Нельзя выдавать source snapshot, `azur mcp status` или факт создания task за
mandatory fresh MCP client acceptance; нельзя передавать Codex registration
check расплывчатое «проверь MCP» без exact expected state; нельзя подменять
новую task/thread subagent-ом, fork-ом, same-session retry или Connected App;
нельзя исправлять HEAD mismatch post-create `git switch`/`git checkout`; нельзя
скрывать external Codex limitation под product MCP failure.
