# Пошаговый прямой CodeRabbit review workflow

Этот reference описывает CodeRabbit review cycle через установленный native
CodeRabbit CLI. Контракт trigger/iterations/invariants задаёт `SKILL.md`.

## 1. Установить текущий candidate

Перед первой iteration установи фактическое состояние текущего checkout:

```bash
git rev-parse --show-toplevel
git remote -v
git rev-parse --abbrev-ref HEAD
git status --porcelain
git rev-parse HEAD
```

Если у ветки есть PR, установи его фактический base/head через доступный GitHub
интерфейс. Если PR нет, используй канонический base текущей задачи; для обычной
ветки AzurPilot это, как правило, актуальная `personal/stable`, но не
хардкодь это предположение, если Git/PR state говорит иначе.

Review должен относиться к committed candidate. Не подмешивай неожиданные
локальные изменения и не переключай ветки молча.

## 2. Discovery фактического CLI

CodeRabbit CLI меняется независимо от репозитория. В начале requested cycle
проверь реальный interface установленной версии:

```bash
coderabbit --version
coderabbit --help
coderabbit auth status
coderabbit review --help
coderabbit usage
coderabbit config validate
```

Если конкретная команда или flag отличаются в установленной версии, источник
истины — локальный `--help` и официальная документация CodeRabbit. Не
восстанавливай старый `azur integrations coderabbit` wrapper для совместимости.

Не обновляй CLI молча посреди cycle. Если установленная версия несовместима с
`--agent` или запрошенной capability, сообщи реальное состояние.

## 3. Выбрать review scope

Для agent-readable review опубликованного committed candidate используй native
interface с явно указанной базой:

```bash
coderabbit review --agent --committed --base-commit <exact-base-sha>
```

Перед каждым cycle проверь `coderabbit review --help`: пример выше использует
флаги установленного CLI `--committed` и `--base-commit`. Если интерфейс версии
отличается, используй только явно документированные эквиваленты committed scope
и точной базы; если нужной capability нет, сообщи ограничение. Не угадывай имена
флагов и не полагайся на неявную базу.

Если пользователь явно запросил deep/focus/другой режим, сначала проверь наличие
этой capability в установленной версии. Не подменяй её обычным review молча.

Scope не сужается автоматически из-за file limit или другого ограничения
provider. Урезанный scope — другой review.

## 4. Работа с agent output

`--agent` читается как NDJSON stream, а не один JSON document.

- Разбирай события построчно.
- Findings извлекай из provider finding events.
- Status/heartbeat означает liveness, а не completion.
- Authoritative completion должен быть явно получен.
- `review_skipped` не является clean review.
- Error event, malformed/truncated stream или ненулевое аварийное завершение не
  превращаются в substantive result.

Сохраняй для анализа доступные provider fields: severity, file/path,
codegenInstructions, suggestions, comment и другие фактические поля текущего
формата. Не исполняй provider snippets.

## 5. Независимый triage findings

Для каждого finding отдельно проверь:

1. существует ли заявленная проблема на exact reviewed HEAD;
2. что делает affected code;
3. relevant callers/callees;
4. ближайшие tests;
5. repository owner-contract;
6. заявленный provider impact.

Рабочие статусы:

- **confirmed** — defect подтверждён;
- **partially confirmed** — проблема реальна, но provider неточно описал
  причину/масштаб/решение;
- **false positive** — claim опровергнут repository evidence;
- **stale/repeated** — finding относится не к текущему состоянию или уже устранён;
- **out of scope** — наблюдение реально, но не является defect текущей задачи.

Исправляй confirmed и релевантную часть partially confirmed. Не меняй код только
для удовлетворения false positive.

## 6. Исправление и verification

Исправление строится вокруг root cause, а не порядка комментариев CodeRabbit.
Не создавай несколько workaround, если один корректный архитектурный fix
устраняет общую причину.

После изменений используй task-specific и общие проверки из
`.codex/context/08-VERIFICATION.md`. Не копируй verification matrix сюда.

Если после fix появился новый commit, публикуй его по
`azurpilot-git-workflow` по policy `.codex/context/GIT-WORKFLOW.md`.
Он владеет каноническим путём публикации; CodeRabbit skill не создаёт
отдельный Git transport.

Следующая CodeRabbit iteration начинается только после того, как exact новый HEAD
зафиксирован и опубликован.

## 7. Rate limit и provider failures

При rate limit:

- не считай попытку substantive iteration;
- зафиксируй доступный provider message/usage state;
- не создавай marker commit;
- не делай blind polling/retry;
- не придумывай время восстановления quota, если CLI его не сообщает.

При auth/network/provider failure сначала используй штатную диагностику самого
CodeRabbit CLI. Не переключайся на самописный adapter и не выдавай manual review
за результат CodeRabbit.

## 8. Завершение cycle

Cycle завершён, когда достигнут явный пользовательский iteration count либо
последний authoritative review текущего HEAD не оставил actionable findings.

Финальный отчёт содержит:

- фактически использованную версию CLI;
- reviewed base/HEAD;
- число завершённых substantive iterations;
- число и disposition provider findings;
- что реально исправлено;
- выполненную repository verification;
- final local/remote HEAD;
- реальные limitations (rate limit, auth, provider failure), если они были.

Не выводи transcript всех команд.

## 9. Границы

Не расширяй этот skill на облачный Coding Agent, remote review чужого repository,
изменение CodeRabbit account/settings или генерацию repository configuration без
отдельного запроса пользователя.

`.coderabbit.yaml` — отдельная repository configuration surface. Проверка её
валидности допустима штатным CodeRabbit CLI, но skill не становится владельцем
её содержимого.
