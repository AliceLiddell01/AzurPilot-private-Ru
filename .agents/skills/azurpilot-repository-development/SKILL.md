---
name: azurpilot-repository-development
description: "Feature, bugfix, refactor, infrastructure, CI/test, documentation, upstream adaptation, подготовка PR, продолжение ветки/PR и явная команда merge/cleanup в AzurPilot. Веди изменение через repository workflow до draft PR и остановись перед финальным ревью; не применяй для read-only объяснения кода, перевода текста без изменения репозитория или общего вопроса без инженерного действия."
---

# Рабочий процесс разработки AzurPilot

Применяй этот skill, когда пользователь просит изменить репозиторий AzurPilot,
продолжить существующую ветку или PR, подготовить PR либо выполнить явно
разрешённый merge/cleanup. Для простого объяснения кода, перевода или другой
read-only задачи без изменения репозитория этот skill не нужен.

Перед началом прочитай `AGENTS.md`, `.codex/context/INDEX.md` и только
относящиеся к задаче canonical docs. При Git/PR lifecycle обязательно прочитай
`.codex/context/GIT-WORKFLOW.md` и `.codex/context/08-VERIFICATION.md`.
Подробности загружай по мере необходимости:

- [engineering-contract.md](references/engineering-contract.md) — постоянные
  границы реализации и языка;
- [ci-and-verification.md](references/ci-and-verification.md) — выбор gates,
  exact-head CI и secret scan;
- [browser-and-live-testing.md](references/browser-and-live-testing.md) —
  Browser/Computer Use, WebUI, device и live acceptance;
- [pr-merge-cleanup.md](references/pr-merge-cleanup.md) — draft PR, финальное
  ревью, явное разрешение merge и cleanup.

## Рабочий цикл

1. Выполни preflight: подтверди repository root, remotes, текущую ветку,
   tracking/upstream, base branch/SHA и staged/unstaged/untracked состояние.
   Пользовательские изменения не stash/drop/reset и не включай в свой diff.
2. Для новой задачи обнови `origin/personal/stable` разрешённым способом и
   создай `codex/<task>` в текущем основном checkout. Однозначно относящуюся к
   задаче опубликованную ветку/PR продолжай после проверки exact head. Не создавай
   implementation worktree или второй clone без специальной причины.
3. Проследи call sites, тесты, конфигурацию и ближайшие архитектурные границы.
   Не зашивай task-specific данные в production, CI или permanent tests.
   Текущие продуктовые тесты и CI остаются stage-agnostic: они проверяют
   поведение, а не номер этапа.
4. Реализуй минимальный связный diff. Вместе с поведением обнови релевантные
   tests и документацию. В каждом реально затронутом файле проверь все
   operator-facing комментарии, логи и диагностические сообщения: человеческий
   текст должен быть литературным русским, а идентификаторы и machine tokens —
   сохранены по контракту.
5. Выполни релевантные проверки от дешёвых к дорогим: static/diff audit,
   syntax, lint, targeted tests, полный связанный набор, browser/live acceptance
   по необходимости и фактический secret scanner перед публикацией. Для точных
   правил используй указанные references и `docs/ci.md`.
6. Проведи adversarial self-review base→head. Для CodeRabbit используй sibling
   skill `azurpilot-coderabbit-review` и отдельный WSL2 Arch review checkout; не
   считай status check или автоматический review источником истины.
7. После завершения проверок создай содержательный commit, push и **только draft
   PR**. В PR body укажи цель, scope, base SHA, подсистемы, реализацию, фактически
   выполненные проверки, security/secret result, rollback/migration и ограничения.
   Required CI должен быть проверен на exact PR head.
8. Нормальная конечная точка — `READY_FOR_CHATGPT_REVIEW`. Сообщи, что draft PR
   готов к финальному ревью ChatGPT 5.6 Sol, и остановись. CI, self-review и
   CodeRabbit не заменяют это финальное ревью.

## После явной команды merge

Старое разрешение не переносится на новый PR. Только отдельное текущее сообщение
пользователя, однозначно относящееся к этому PR, разрешает merge. Перед ним заново
проверь PR head, required CI, blocking review threads, итоговый diff и secret
scan; убедись, что после финального ChatGPT review relevant diff перепроверен.
После разрешённого merge выполни post-merge verification, безопасный возврат
основного checkout на `personal/stable`, удаление task branch и только
принадлежащих задаче временных ресурсов. CodeRabbit rate limit не является
product blocker: не жди cooldown, зафиксируй последний exact head и завершай
сессию в `READY_FOR_CHATGPT_REVIEW` после остальных доступных gates.
