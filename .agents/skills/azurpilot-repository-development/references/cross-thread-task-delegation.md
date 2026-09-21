# Cross-thread continuation для свежей MCP-сессии

Этот reference — единственный repository-level контракт для continuation через
отдельную Codex task/thread, когда обязательная проверка требует свежей
client/session registration. Он не создаёт новую feature, ветку или PR и не
передаёт другой task ownership разработки.

## Когда контракт применяется

Сначала coordinator task обязана штатными project-owned действиями доказать
нужное состояние source и runtime:

1. `azur mcp reconcile --source --bump auto` подтверждает только
   `source_reconciled`.
2. `azur mcp status` читает текущее source/runtime состояние; итоговый status
   после возможного repair должен подтвердить `runtime_ready=true`, если live
   gate входит в scope.
3. При `runtime_state=stale` или `runtime_state=stopped` coordinator сначала
   вызывает единственный typed runtime repair path `azur mcp reconcile` без
   `--source`, после чего status нужно прочитать повторно. Этот path использует
   существующий `McpService.reconcile`: останавливает только доказанного exact
   owner, запускает нужные owned services и проверяет `runtime_ready=true`.
   Само stale/stopped состояние не является причиной создавать новую task.
   `LOCAL_MCP_SUPERVISOR_STOPPED` — один из typed сигналов такого состояния,
   а не отдельный shortcut для обхода `azur mcp reconcile`.
   `LOCAL_MCP_SUPERVISOR_OWNERSHIP_MISMATCH`, неизвестная identity/liveness,
   чужой port owner, failure stop/start или нарушенный postcondition остаются
   typed `BLOCKED_PRECONDITION`; эвристическая остановка запрещена.

Если после доказанного `runtime_ready=true` текущая Codex task не может доказать свежую
`effective_codex_registration` из-за task/session-scoped registration cache, это
не конечный blocker, пока текущая Codex surface умеет создать и наблюдать
independent task/thread. Источником истины остаются фактически callable MCP
surface, contract и catalog, а не source config или сам факт создания task.

## Роли и границы

- `Coordinator task` остаётся владельцем work item, diff, Git/PR lifecycle и
  итогового решения.
- `Fresh independent task/thread` — отдельная project-scoped Codex task,
  созданная штатной orchestration surface coordinator task. Она является
  bounded verification/live-acceptance worker и возвращает evidence coordinator.
- `Subagent`, fork текущей agent session, same-directory child worker и
  Connected App/remote fallback не считаются fresh independent task/thread.

Создание выполняется через доступную Codex task orchestration surface, а
coordinator ждёт terminal outcome через штатное ожидание/чтение task. Не
просите пользователя вручную создавать новый чат и не используйте shell,
browser или remote app как замену task orchestration.

## Codex Desktop orchestration boundary

В Codex Desktop coordinator создаёт именно новый stored task через
`mcp__codex_app__create_thread`, а не через `fork_thread`, subagent или
same-directory child. Сначала coordinator разрешает project через
`list_projects` и выбирает проект AzurPilot с `isGitRepository=true`, затем
определяет текущую coordinator branch и exact committed HEAD. До создания task
он обязан проверить, что branch tip равен ожидаемому exact HEAD. Если branch не
определяется однозначно или её tip уже расходится с ожидаемым HEAD, task не
создаётся, а результатом остаётся typed `BLOCKED_PRECONDITION` с точной
причиной.

### Канонический branch-based запуск

Для независимой проверки используется `target.type=project` с worktree и
`startingState.type=branch`, где `branchName` — фактическая branch coordinator
work item. Перед созданием coordinator уже проверил branch tip, поэтому это
канонический способ передать свежей task тот же committed exact HEAD; setup
worktree остаётся автоматическим и не требует ручного
переключения состояния. В prompt передаются repository identity, branch, exact
expected HEAD, bounded acceptance scope и запрет на изменение code, branch, PR и
lifecycle.

### Неиспользуемый working-tree маршрут

`startingState.type=working-tree` не является exact-head continuation: этот
режим может создать task от другого состояния и потому не заменяет проверку
branch tip. Нельзя компенсировать такой mismatch ручным `git switch` или
`git checkout` после создания task.

`create_thread` асинхронен: готовый результат содержит настоящий `threadId`
и `hostId`, а промежуточный `clientThreadId` нельзя передавать в ожидание,
чтение или follow-up tools. Coordinator ждёт ready task через
`wait_threads` с настоящим `threadId`, затем читает её terminal turn через
`read_thread` с outputs; создание task, промежуточный progress или отсутствие
ошибки не являются acceptance evidence. Если setup вернул только
`clientThreadId`, coordinator сначала наблюдает появление ready `threadId` и
только после этого начинает bounded wait.

Перед любым MCP или live acceptance fresh task сначала независимо проверяет
repository identity, фактический exact HEAD и branch state. Фактический HEAD
обязан совпасть с переданным expected HEAD; detached HEAD допустим для
verification worker. Fresh task не выполняет post-create `git switch` или
post-create `git checkout` для исправления mismatch: при несовпадении
возвращается typed
`BLOCKED_PRECONDITION`, а до MCP/live acceptance дело не доходит.

Fresh task обязана использовать фактически callable `azurpilot-dev` MCP
surface: первым read-only вызовом выполнить `dev_get_contract`, затем
проверить `dev_list_smoke_capabilities` и `dev_validate_smoke`, запустить
разрешённый `dev_start_smoke`, дождаться immutable terminal outcome через
`dev_get_smoke` и вернуть проверяемое end-to-end evidence. Shell/HTTP/ADB,
прямой внутренний module и source snapshot не заменяют MCP acceptance.

## Каноническая последовательность

1. Coordinator фиксирует bounded context: repository identity, coordinator
   branch, exact expected HEAD, требуемые MCP family/route,
   source/plugin/compatibility state, ожидаемый runtime state, обязательные
   capability/contract checks и точный разрешённый live acceptance scope.
2. До создания task coordinator проверяет, что branch tip совпадает с exact
   expected HEAD. При неоднозначной branch или mismatch он возвращает typed
   `BLOCKED_PRECONDITION` и не создаёт task.
3. Coordinator создаёт fresh independent task/thread с
   `startingState.type=branch` и передаёт этот context вместе с запретом
   изменять production code, branch, PR или lifecycle.
4. Fresh task до любого MCP/live acceptance заново проверяет repository
   identity, фактический exact HEAD и branch state. Фактический HEAD должен
   быть равен expected HEAD; detached HEAD допустим. Она не принимает source
   config, старый snapshot или факт создания task за доказательство freshness и
   не выполняет post-create switch/checkout.
5. После этого fresh task проверяет effective MCP registration, negotiated
   callable surface/contract/catalog и runtime readiness. Только после
   подтверждения свежей registration и `runtime_ready=true` она выполняет
   разрешённый live smoke/acceptance в указанном scope.
6. Fresh task возвращает machine-readable или иным образом проверяемый
   terminal result с evidence: exact identity/HEAD, route, registration,
   contract/catalog, runtime, acceptance result и ограничениями без секретов.
7. Coordinator дожидается и читает terminal result delegated task, сохраняет
   его evidence в основном lifecycle и только после этого классифицирует gate.
8. Если orchestration surface недоступна, branch tip не совпал с expected HEAD
   или fresh task не подтверждает required state, результатом остаётся typed
   `BLOCKED_PRECONDITION` с точной причиной. Создание task без terminal evidence
   не закрывает gate.

Fresh task не исправляет обнаруженный defect и не становится вторым владельцем
разработки. Если она находит проблему, она возвращает evidence coordinator;
изменение кода выполняется coordinator по обычному owner contract.

## Запрещённые сокращения

Нельзя завершать workflow blocker-ом только из-за stale registration текущей
task, если independent orchestration доступна; нельзя выполнять live acceptance
в старой task после срабатывания fresh-session trigger; нельзя подменять новый
task/thread subagent-ом, fork-ом, same-session retry или Connected App; нельзя
передавать delegated task расплывчатое «проверь MCP» без exact expected state.
