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
2. `azur mcp status` должен подтвердить `runtime_ready=true`, если live gate
   входит в scope.
3. При `LOCAL_MCP_SUPERVISOR_STOPPED` сначала разрешены только доказанный owned
   `azur mcp start` или `azur mcp restart`, после чего status нужно прочитать
   повторно. Само это состояние не является причиной создавать новую task.

Если после этого текущая Codex task не может доказать свежую
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
same-directory child. Перед созданием он разрешает project через
`list_projects` и выбирает проект AzurPilot с `isGitRepository=true`; для
независимой проверки используется `target.type=project` с worktree и
`startingState.type=working-tree`, чтобы task получила тот же committed exact
HEAD без права менять coordinator checkout. В prompt передаются repository
identity, exact HEAD, bounded acceptance scope и запрет на изменение code,
branch, PR и lifecycle.

`create_thread` асинхронен: готовый результат содержит настоящий `threadId`
и `hostId`, а промежуточный `clientThreadId` нельзя передавать в ожидание,
чтение или follow-up tools. Coordinator ждёт ready task через
`wait_threads` с настоящим `threadId`, затем читает её terminal turn через
`read_thread` с outputs; создание task, промежуточный progress или отсутствие
ошибки не являются acceptance evidence. Если setup вернул только
`clientThreadId`, coordinator сначала наблюдает появление ready `threadId` и
только после этого начинает bounded wait.

Fresh task обязана использовать фактически callable `azurpilot-dev` MCP
surface: первым read-only вызовом выполнить `dev_get_contract`, затем
проверить `dev_list_smoke_capabilities` и `dev_validate_smoke`, запустить
разрешённый `dev_start_smoke`, дождаться immutable terminal outcome через
`dev_get_smoke` и вернуть проверяемое end-to-end evidence. Shell/HTTP/ADB,
прямой внутренний module и source snapshot не заменяют MCP acceptance.

## Каноническая последовательность

1. Coordinator фиксирует bounded context: repository identity, exact expected
   HEAD, требуемые MCP family/route, source/plugin/compatibility state,
   ожидаемый runtime state, обязательные capability/contract checks и точный
   разрешённый live acceptance scope.
2. Coordinator создаёт fresh independent task/thread и передаёт этот context
   вместе с запретом изменять production code, branch, PR или lifecycle.
3. Fresh task заново проверяет repository identity и exact HEAD, effective MCP
   registration, negotiated callable surface/contract/catalog и runtime
   readiness. Она не принимает source config, старый snapshot или факт
   создания task за доказательство freshness.
4. Только после подтверждения свежей registration и `runtime_ready=true`
   fresh task выполняет разрешённый live smoke/acceptance в указанном scope.
5. Fresh task возвращает machine-readable или иным образом проверяемый
   terminal result с evidence: exact identity/HEAD, route, registration,
   contract/catalog, runtime, acceptance result и ограничениями без секретов.
6. Coordinator дожидается и читает terminal result delegated task, сохраняет
   его evidence в основном lifecycle и только после этого классифицирует gate.
7. Если orchestration surface недоступна или fresh task не подтверждает
   required state, результатом остаётся typed `BLOCKED_PRECONDITION` с точной
   причиной. Создание task без terminal evidence не закрывает gate.

Fresh task не исправляет обнаруженный defect и не становится вторым владельцем
разработки. Если она находит проблему, она возвращает evidence coordinator;
изменение кода выполняется coordinator по обычному owner contract.

## Запрещённые сокращения

Нельзя завершать workflow blocker-ом только из-за stale registration текущей
task, если independent orchestration доступна; нельзя выполнять live acceptance
в старой task после срабатывания fresh-session trigger; нельзя подменять новый
task/thread subagent-ом, fork-ом, same-session retry или Connected App; нельзя
передавать delegated task расплывчатое «проверь MCP» без exact expected state.
