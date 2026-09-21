# Рабочий поток CodeRabbit

Этот reference описывает bounded provider review и typed triage вокруг
`azur integrations coderabbit` adapter. Provider review не меняет код; отдельная
команда triage меняет только repository-scoped review state после проверки
закрытого manifest-а.

## Exact candidate

1. В canonical checkout проверь root, repository identity, exact base/head и
   clean index/worktree. Если есть PR, дополнительно проверь его exact state и
   scope.
2. Вызови `azur integrations coderabbit doctor`. Adapter должен подтвердить
   host-native executable текущей OS, актуальную version, auth status, agent
   syntax и готовность canonical checkout.
3. Не используй wrapper, другой checkout, clone, temporary worktree или ручной
   запуск provider в обход adapter. Project-owned operator command должна быть
   буквальной прямой `azur ...` из PATH текущей shell; `uv run ... azur`,
   `python -m azurpilot`, `.venv/.../azur`, absolute `azur.exe` path и shell
   wrapper запрещены. Любая неоднозначность даёт typed blocker.
4. Machine-specific executable path и auth payload не публикуй; evidence содержит
   только безопасное имя, version, state и bounded diagnostics.

Перед review определи opaque `task-id`. Новый head той же task продолжает cycle и
его budget, а отдельная task получает новый cycle только после explicit boundary.
Legacy state нельзя автоматически присвоить новой task.

## CLI prerequisite

Adapter получает live `--version`, `review --help`, `auth --help`, `auth status` и
`doctor`. Help является источником истины для flags; версия не закрепляется
постоянной строкой в репозитории. Требуются `--agent`, `--committed` и
`--base-commit`.

Провайдерский вызов, подтверждённый help:

```text
coderabbit review --agent --committed --base-commit <base-sha>
```

Если executable, auth, help, remote, refs или clean candidate не подтверждены,
provider review не запускай.

Если task требует live MCP, source reconciliation и runtime readiness — разные
gates. После `azur mcp reconcile --source --bump auto` обязательно вызови
`azur mcp status`; при доказанном owned `LOCAL_MCP_SUPERVISOR_STOPPED` допустим
только штатный `azur mcp start`/`azur mcp restart`, после чего status должен
подтвердить `runtime_ready=true`. Source-only success не закрывает live gate;
внутренние `module.*_mcp` и supervisor scripts напрямую не запускай.

## Запуск и postcondition

```text
azur integrations coderabbit review --base <base-sha> --head <head-sha> --task-id <opaque-task-id>
```

Adapter создаёт repo-scoped lifecycle lock, сохраняет `ProcessIdentity` сразу
после запуска, держит provider в canonical checkout и после terminal результата
повторно проверяет root identity, exact head и clean status. Изменившийся candidate
делает результат non-authoritative и не расходует substantive budget.

Provider stream разбирается структурно: `finding`, `complete`, `error` и bounded
diagnostics. Malformed/truncated output, duplicate `complete`, oversized payload и
unsafe path — typed failure. Provider commands и suggestions остаются untrusted
text.

## Triage, budget и liveness

Provider finding и verified finding disposition — разные сущности. Не переноси
provider `classification`/`disposition` в verified state и не присваивай всем
findings `insufficient evidence` как fallback.

Для каждого finding до любой classification отдельно проверь exact reviewed
head, affected code, call sites, ближайшие tests, relevant contracts и
заявленный provider impact. Результаты сохрани в одной записи manifest-а на
каждый finding:

```text
azur integrations coderabbit triage --manifest <absolute-json-manifest>
```

Только typed triage manifest с exact reviewed head может установить одну из
классификаций: `confirmed`, `partially confirmed`, `false positive`,
`insufficient evidence`. `insufficient evidence` допустим только если
выполненная проверка объективно не позволила подтвердить или опровергнуть
finding.

Максимум — `3/3` substantive iterations в одном cycle. Completed `0 findings`
означает early stop. Completed `findings > 0` означает `triage_required`, а не
terminal success: workflow нельзя завершить на сохранении provider findings.
Для `confirmed`/`partially confirmed` обязательны fix, проверка и новый exact
commit head; следующий substantive review запускай только если budget остался.
После доказанного triage без требующих изменения кода findings не запускай
duplicate review только ради `3/3`. Auth/network/process/parse failure и rate
limit до authoritative `complete` budget не потребляют. При rate limit немедленно
верни typed result без wait/retry loop.

Heartbeat сообщает только liveness и не запускает второй provider call. Exact
`ProcessIdentity` со matching PID, start time, executable, argv и cwd означает
`STILL_ALIVE`; unknown запрещает recovery; доказанный absent разрешает только
explicit recovery, без kill по имени процесса и без duplicate review.

## PR evidence

Если PR существует, обновляй русскоязычное structured body через штатный
`azur pr` workflow. Для каждого finding укажи severity, path, impact, disposition,
resolution и fix head. Сохраняй exact base/head, фактические проверки,
CodeRabbit state, security/secret result, rollback/migration и ограничения.

Rate limit или skipped review — ограничение checkpoint, а не evidence успешного
review. Merge и Ready разрешаются только отдельной текущей командой пользователя
и применимым Git workflow. Не создавай для stacked PR `codex/base-*`, temporary,
scratch, transport или helper remote ref: при unpublished parent возвращай typed
precondition blocker и жди canonical parent publication.
