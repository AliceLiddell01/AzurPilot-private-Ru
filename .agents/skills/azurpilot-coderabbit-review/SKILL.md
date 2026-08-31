---
name: azurpilot-coderabbit-review
description: "CodeRabbit review PR, branch or commit в AzurPilot, triage review findings, повторный review после fixes, WSL2 Arch review checkout и подготовка PR к финальному ревью. Применяй также при rate limit CodeRabbit; не используй для обычного read-only объяснения кода без review-запроса."
---

# Независимое CodeRabbit review

Применяй этот skill, когда пользователь просит CodeRabbit review, review PR,
разбор findings, повторную проверку после fixes, review в WSL2 Arch или доведение
PR до точки внешнего финального ревью. Для общей разработки без review-запроса
основным остаётся `azurpilot-repository-development`.

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
- Разбирай каждый finding как `confirmed`, `false positive` или `needs evidence`.
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
