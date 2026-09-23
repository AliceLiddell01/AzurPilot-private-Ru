---
name: azurpilot-repository-development
description: "Разработка, исправления и рефакторинг AzurPilot: инфраструктура, CI/тесты, upstream, PR, merge и cleanup. Не используй для read-only объяснений и задач без изменения файлов."
---

# Разработка AzurPilot

Этот skill направляет к владельцам правил; он не дублирует Git lifecycle и
матрицу проверок.

## Контекст

1. Прочитай корневой `AGENTS.md` и `.codex/context/INDEX.md`.
2. Открой документы, владельцы которых совпадают с фактическим diff.
3. Для веток, публикации, PR и merge используй только
   `.codex/context/GIT-WORKFLOW.md`.
4. Проверки выбирай только по `.codex/context/08-VERIFICATION.md`.
5. Для GUI/device/live testing используй только при необходимости
   [browser-and-live-testing.md](references/browser-and-live-testing.md).

## Рабочий цикл

1. Подтверди repository, текущую ветку, base SHA и пользовательские изменения.
2. Найди owner поведения, реальные call sites, contract и ближайшие проверки.
3. Внеси минимальное связное изменение и обнови только относящиеся tests/docs.
4. Выполни targeted checks, затем только требуемые diff-derived integration gates.
5. Проверь итоговый diff и сообщи фактические результаты.

Используй canonical typed service/CLI вместо ручного повторения его работы.
Если обычная операция владеет lifecycle, она сама выполняет запуск, ожидание,
evidence и cleanup; дополнительная диагностика нужна после конкретного typed
failure.

## Canonical developer operations

- Для MCP impact вызови `azur mcp impact --base <exact-base-sha>`. Только при
  `REQUIRED` выполни `azur mcp reconcile --source --bump auto` (либо явно
  указанный typed minimum bump); когда нужен fresh-client acceptance, вызови
  `azur mcp accept`.
- Не собирай `FreshMcpClientPlan` и не запускай внутренние Python snippets,
  когда доступна canonical `azur mcp accept`.
- `source_reconciled` не означает runtime readiness. Если runtime сообщает
  `stale`/`stopped`, одна typed `azur mcp reconcile` без `--source` должна
  подтвердить `runtime_ready=true`. Same-repository stale recovery требует
  recorded exact identities, unchanged marker и `STOPPED/no-conflict`; unknown
  или foreign ownership остаётся fail-closed. `session_state=not_observable` сам
  по себе не является runtime failure.
- Обязательный gate `fresh_mcp_client_acceptance` использует отдельный fresh MCP client/process через `azur mcp accept`. Effective Codex registration —
  отдельная optional check только при изменении registration/schema/routing;
  тогда следуй [cross-thread-task-delegation.md](references/cross-thread-task-delegation.md).
- Для обычного Smoke используй `dev_start_smoke`: bounded сценарий возвращает
  terminal typed result и автоматически фиксирует объявленные checkpoints.
  Долгий/интерактивный сценарий возвращает operator run id; `dev_get_smoke` и
  ручной capture оставлены для диагностики.
- Не делай предварительный runtime/game/WebUI/store запрос, если этот факт
  принадлежит owner operation. Один факт имеет один authoritative source.

Project-owned operator actions запускай буквальной командой `azur ...` через
PATH текущей shell. `uv` используй для dependency, test и build задач.

## Review и delivery

CodeRabbit review checkpoint запускай только по явному запросу пользователя или
обязательному project policy; незапрошенный review остаётся `NOT_RUN`. Если
review требуется, явно делегируй sibling skill
`azurpilot-coderabbit-review`. Provider lifecycle, provider rate limit, retry и
triage принадлежат этому sibling skill. Самоотчёт `NOT_RUN` без запроса не
является limitation.
Для PR, Draft, exact-head gates, merge authorization и cleanup следуй
`GIT-WORKFLOW.md`; не переводить PR в Ready и не выполнять merge без отдельного
текущего разрешения пользователя.
