---
name: azurpilot-coderabbit-review
description: "CodeRabbit code review PR, branch or commit в AzurPilot, triage findings, повторный CodeRabbit review, WSL2 Arch review или rate limit. Используй при явном CodeRabbit/code-review intent либо при делегации canonical CodeRabbit checkpoint от azurpilot-repository-development; не используй для generic PR preparation или обычной разработки вне такого checkpoint."
---

# Независимое CodeRabbit review

Применяй этот skill, когда пользователь явно запрашивает CodeRabbit/code review,
разбор CodeRabbit findings, повторный review, WSL2 Arch CodeRabbit review или
работу после rate limit CodeRabbit, а также когда
`azurpilot-repository-development` делегирует canonical CodeRabbit review
checkpoint. Такая делегация является достаточным internal trigger: отдельный
пользовательский CodeRabbit-запрос не требуется. Для generic PR preparation,
финального ChatGPT review и обычной разработки вне такого checkpoint основным
остаётся `azurpilot-repository-development`.

Перед запуском прочитай [review-workflow.md](references/review-workflow.md).
CodeRabbit — независимый reviewer, а не источник истины: каждый finding сверяй
с текущим кодом, call sites, tests и архитектурой. Autofix или совет reviewer не
применяй вслепую.

## Предварительные условия CLI и remote

До первого вызова review в WSL2 Arch явно разреши исполняемый файл CodeRabbit.
Не ищи и не создавай shell alias, не используй Windows `.cmd`-обёртку и не
вызывай голое имя `coderabbit`: alias может существовать только в
интерактивном shell и не является prerequisite. Сначала проверь явный
исполняемый путь `$HOME/.local/bin/coderabbit`, а если его нет — разреши
реальный executable через `type -P coderabbit` с проверкой `-x`, а не через
вывод, который может обозначать alias или function. Сохрани разрешённый путь в
переменной и используй его для `--version`, `auth status`, `review --help` и
самого review. Если executable не найден, остановись с явным prerequisite
blocker; не подменяй эту проверку GitHub status или free allowance.

Перед запуском CLI review-клон обязан иметь hosted Git remote того же
репозитория, который проверяется. Сначала получи canonical URL из основного
checkout и PR, нормализуй его к hosted Git URL репозитория без credentials,
query/fragment и лишних path components и сравни owner/repository с PR. До
любого `git remote set-url` или копирования remote в clone отвергни
отсутствующий remote, локальный путь (`/mnt/c/...`), `file://`, UNC, URL с
credentials и любой не-hosted URL. Установи только уже проверенный URL в
отдельном review-клоне и затем проверь `git remote get-url origin`; повторно
убедись, что он совпадает с canonical URL PR. Если CodeRabbit сообщает, что
repository не распознан или review уходит в free allowance из-за remote, такой
запуск не считай review: исправь remote, перепроверь его и повтори не более
одного раза.

## Обязательные границы

- Получай live base/head и проверяй exact commit; если PR существует,
  дополнительно получай и проверяй его state.
- Выполняй CodeRabbit CLI в отдельном обычном WSL2 Arch review checkout. Это не
  implementation worktree: product fixes вносятся только в основной checkout.
- Не копируй в review checkout secrets, cookies, локальные config, dumps или
  пользовательские identifiers без доказанной необходимости.
- Разбирай каждый finding как `confirmed`, `partially confirmed`, `false
  positive` или `insufficient evidence`. Для `partially confirmed` отдельно
  фиксируй: проблема или риск подтверждены, но root cause либо suggested fix
  не принимаются автоматически; исправляй по текущей архитектуре.
  Подтверждённые findings исправляй минимальным diff и повторяй релевантные
  checks; false positive не закрывай фиктивным кодом.
- Парси фактический результат CLI, отделяя findings от status-сообщений. Не
  называй skipped, disabled или rate-limited status substantive review.

## Rate limit и результат

Rate limit/cooldown CodeRabbit не является product blocker. Не жди таймер и не
устраивай retry-loop: зафиксируй последний exact head, выполни остальные
доступные проверки, создай или обнови draft PR и передай состояние
`READY_FOR_CHATGPT_REVIEW` с явной пометкой, что дополнительный review не
выполнен из-за rate limit.

После существенного подтверждённого fix запусти повторный review, если CLI
доступен. Заверши отчёт количеством issues по severity, путями и влиянием либо
сообщением `CodeRabbit raised 0 issues.`; не приписывай CodeRabbit результаты
собственного ручного self-review.
