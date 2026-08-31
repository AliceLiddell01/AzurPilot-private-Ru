---
name: azurpilot-coderabbit-review
description: "CodeRabbit code review PR, branch or commit в AzurPilot, triage findings, повторный CodeRabbit review, WSL2 Arch review или rate limit. Применяй только при явном review-запросе; не используй для общей подготовки, обычной разработки или read-only объяснения кода."
---

# Независимое CodeRabbit review

Применяй этот skill, когда пользователь явно запрашивает CodeRabbit/code review,
разбор CodeRabbit findings, повторный review, WSL2 Arch CodeRabbit review или
работу после rate limit CodeRabbit. Для generic PR preparation, финального
ChatGPT review и общей разработки без явного CodeRabbit review основным остаётся
`azurpilot-repository-development`.

Перед запуском прочитай [review-workflow.md](references/review-workflow.md).
CodeRabbit — независимый reviewer, а не источник истины: каждый finding сверяй
с текущим кодом, call sites, tests и архитектурой. Autofix или совет reviewer не
применяй вслепую.

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
