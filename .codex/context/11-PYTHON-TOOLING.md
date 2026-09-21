# Python tooling и внешние интеграции

## Назначение

Документ описывает **текущее** Python tooling AzurPilot, принадлежащее репозиторию, и его
устойчивые границы. Это не дорожная карта миграции и не журнал предыдущих задач.

При споре сначала проверять текущий код, ближайшие тесты и сгенерированные
контракты. Текущие PR/head SHA, локальные пути конкретной машины, состояние цикла ревью
и планы будущей реализации сюда не записываются.

## 1. Текущие владельцы

| Область | Текущий владелец | Инвариант |
|---|---|---|
| CLI | `azurpilot.cli` | parsing/rendering отделены от service logic; import не запускает operations |
| Типизированная модель результата | `azurpilot.tooling.contracts` | закрытые модели Pydantic, стабильные result/state/reason codes, ограниченные evidence |
| Примитивы репозитория/процессов | `azurpilot.tooling` | точная identity, подтверждённое владение path/process, ограниченные операции, fail-closed при неоднозначности |
| Lifecycle/build/repair/update | соответствующие сервисы в `azurpilot.tooling` | CLI является adapter; платформенно-зависимое поведение не размножается в renderer |
| Git delivery | `azurpilot.tooling.delivery` | типизированный manifest, exact refs, allowlist, journal, обычный push/read-back |
| Публикация pull request | `azurpilot.tooling.pull_request` | типизированный PR spec/body, явная identity провайдера, draft/read-back contract |
| Внешние интеграции | `azurpilot.integrations` | прямые типизированные adapters, ограниченная граница credentials/evidence |
| MCP status/compatibility | tooling + существующие MCP contract gates | состояние source не выдаётся за фактическую регистрацию клиента |
| Пути совместимости Windows/оператора | проектные PowerShell scripts/modules | не удаляются без доказанной эквивалентности и миграции вызывающих компонентов |

Пакет `azurpilot/` является tooling/application package и не
становится владельцем игрового поведения. `alas.py`, `gui.py`, `module/application`,
`module/device`, combat/map/campaign и другие продуктовые слои сохраняют свои
границы.

## 2. Контракт CLI

Установленный entrypoint `azur` ведёт в `azurpilot.cli:main`. Текущая
реализация использует `argparse` и Rich; это факт реализации, а не предложение
для будущего выбора framework.

CLI имеет два режима представления:

- человекочитаемый вывод — краткий terminal UI;
- `--json` — ровно один закрытый машиночитаемый результирующий конверт.

Rich/ANSI/progress не меняют JSON schema. Сервисный слой не зависит от terminal renderer, а tests/MCP не должны
разбирать человекочитаемый вывод как машинный контракт.

Команда, привязанная к проекту, не угадывает repository identity по случайному cwd, если
identity не доказана. Explicit/configured/installation provenance валидируется,
а неоднозначность завершается типизированной ошибкой.

### Literal operator boundary

Если project-owned capability уже представлена через установленный `azur` и
PATH текущей shell её подтверждает, operator action вызывается только
буквальной командой `azur ...`. `uv run ... azur`, `uv run python -m
azurpilot`, `python -m azurpilot`, `.venv/.../azur`, absolute `azur.exe` path и
PowerShell/cmd wrapper являются обходом operator path и запрещены. Отсутствие
`azur` — typed unavailable/precondition, а не разрешение на fallback. `uv`
остаётся допустимым для dependency/bootstrap/test/build задач, не представленных
через project-owned operator capability.

## 3. Типизированные результаты и evidence

`ToolingResult[TDetails, TEvidence]` и DTO конкретных операций — каноническая модель
между service, CLI JSON и tests.

Постоянные правила:

- schema закрытая; arbitrary `dict[str, Any]` не используется как публичный
  result contract;
- details/evidence ограничены по размеру;
- сырой payload провайдера, полные логи, tokens, cookies, credential URLs и личные
  paths не сериализуются;
- success/failure/unsupported/unavailable/not-configured различаются типизированными
  state/reason codes, когда operation contract требует такого различия;
- timeout/unknown external mutation не превращается в success по предположению;
- диагностическая/read-only команда не получает скрытый mutating fallback.

## 4. Git delivery

Граница delivery публикует только доказанное состояние Git.

Типизированный manifest/spec содержит необходимую identity:

- repository;
- expected branch/local head;
- base SHA;
- remote/ref;
- intended paths/changes;
- preimage/postimage и operation intent там, где они нужны.

Правила публикации:

- staged allowlist обязателен; `git add .` как скрытый fallback запрещён;
- force/force-with-lease и destructive cleanup не используются;
- push обычный и проверяется exact remote SHA;
- unknown/timeout mutation сохраняется во внешнем journal как неоднозначное
  состояние, recovery сначала делает read-only verification;
- Gitleaks evidence относится к staged/committed scope, определённому operation,
  а не заменяется случайным regex search.
- remote mutation публикует только реальную `expected_branch` через typed
  lifecycle; temporary/scratch/transport/helper refs и `codex/base-*` запрещены;
- если stacked parent local HEAD отличается от parent remote HEAD, typed delivery
  возвращает `TOOLING_STACKED_PARENT_UNPUBLISHED` и ждёт canonical publication
  parent branch; вспомогательный remote ref не создаётся.

Git lifecycle, ветки и разрешение merge принадлежат
`.codex/context/GIT-WORKFLOW.md`, а не этому документу.

## 5. Публикация pull request

`PullRequestService` работает через типизированный publication spec и renderer body.

PR contract:

- repository/base/head задаются явно;
- read-back провайдера подтверждает identity репозитория, refs/SHAs и draft state;
- duplicate, cross-repository, wrong-head и provider-unknown состояния
  fail-closed;
- body строится из structured model, а не shell string;
- операторский body — содержательный русскоязычный отчёт; technical
  identifiers сохраняются без перевода;
- CodeRabbit evidence в body должно соответствовать exact base/head, если оно
  заявлено.

Создание draft PR не даёт разрешение на merge. Lifecycle после публикации
определяет `GIT-WORKFLOW.md`.

## 6. Внешние интеграции

Точный каталог интеграций не дублируется в документации: канонические имена
берутся из `IntegrationName`, а порядок — из `ADAPTER_ORDER`.
`IntegrationRegistry` обязан соответствовать этим источникам.

Интеграции реализованы прямыми типизированными адаптерами. Docker MCP
Toolkit/Gateway и generic MCP proxy не являются критическим путём или резервным
источником регистрации.

Общие правила:

- user/machine credentials приходят из разрешённой внешней конфигурации или
  environment и не попадают в tracked source;
- status/doctor/read operations остаются bounded;
- мутация через провайдера разрешена только если конкретный adapter и operation contract
  явно её поддерживают;
- вывод провайдера нормализуется до ограниченного типизированного evidence;
- отсутствие внешней capability не маскируется synthetic success.

### Semgrep

Semgrep запускается только с явным scoped input: staged/changed/explicit paths
по контракту команды. Scan всего repository не используется как скрытый default.
Findings нормализуются, path обязан оставаться внутри validated root.

### Grafana

Маршрут Grafana принимается только из подтверждённой текущей topology/config.
Нельзя возвращать unconditional `host.docker.internal` или угадывать endpoint.
Tool allowlist остаётся read-only; mutating Grafana tools блокируются.

### Context7 и Docker Docs

Используют прямой HTTP/MCP adapter с bounded discovery/calls. Наличие anonymous
или credentialed режима определяется adapter/config, а не hardcoded секретом.

### Docker Hub

Поверхность Docker Hub остаётся read-only через allowlist/denylist. Mutation tools
не разрешаются как fallback ради удобства диагностики.

### CodeRabbit

CodeRabbit — консультативный reviewer, а не источник истины.

Текущая граница:

- adapter выбирает доказанный host-native executable текущей OS (`coderabbit.exe`
  на Windows и `coderabbit` на POSIX);
- provider запускается в том же canonical checkout, что прошёл exact
  repository/root/head и clean-candidate preflight;
- review scope — exact committed head/base; implementation checkout не
  подменяется другим checkout;
- pre/post candidate fingerprint должен совпасть; mutation в checkout во время
  active review не допускается;
- provider finding не равен verified finding disposition;
- до classification каждый finding проходит individual exact-head triage по
  affected code, call sites, ближайшим tests, relevant contracts и заявленному
  impact;
- `insufficient evidence` не является default/fallback и допустим только при
  доказанной невозможности подтвердить или опровергнуть finding;
- triage фиксируется typed manifest-ом через прямой `azur integrations
  coderabbit triage`; review с findings остаётся `triage_required`;
- после confirmed / partially confirmed обязательны fix, проверка, новый exact
  commit head и следующий review только при оставшемся budget; без code change
  duplicate review ради `3/3` запрещён;
- ограниченный цикл review ограничивает substantive iterations и сохраняет типизированное
  state;
- pre-spawn reservation не расходует budget: доказанный `not_spawned`/`absent_after_cleanup`
  очищает state для retry, а `alive`/`unknown` сохраняет точное ownership для recovery;
- provider rate limit/cooldown не расходует substantive iteration и не запускает
  blind retry;
- text/location/title провайдера нормализуются в ограниченное evidence;
- machine path, username, auth/config directory и executable path не
  хардкодятся в tracked source.

Подробный workflow для человека хранится в
`.agents/skills/azurpilot-coderabbit-review/`, а Git lifecycle — в
`GIT-WORKFLOW.md`.

## 7. Граница MCP

Dev MCP и Game MCP остаются отдельными продуктами:

- `module.dev_mcp` — граница development runtime/smoke/evidence/control;
- `module.game_mcp` — граница game read/control;
- `module.mcp_shared` — нейтральный authenticated transport/shared protocol
  code, но не новый владелец продукта.

Канонические источники версий/совместимости находятся в project metadata и generated
plugin compatibility files. Не копировать mutable version/hash tables в context.

MCP diagnostics должны различать:

- tracked/source configuration;
- локально наблюдаемый runtime;
- снимок plugin/source;
- effective client/session registration, если для него реально есть
  достоверное evidence.

Нельзя объявлять effective registration «готовой» только потому, что
`.codex/config.toml` корректен.

Для MCP lifecycle различай минимум два typed результата:

- `source_reconciled`: tracked source и generated metadata согласованы после
  `azur mcp reconcile --source --bump auto`;
- `runtime_ready`: owned runtime реально запущен, exact contract/catalog
  подтверждены через `azur mcp status`.

Source reconciliation не закрывает live gate. Если live MCP обязателен, после
source reconcile напрямую через PATH вызови `azur mcp status`; при
`LOCAL_MCP_SUPERVISOR_STOPPED` и подтверждённом owned supervisor допустим
`azur mcp start`/`azur mcp restart`, затем повторный status. `MCP_RUNTIME_UNAVAILABLE`,
`runtime_state=stopped`, `session_state=not_observable` или unknown ownership
остаются blocker/limitation. Не запускай `module.*_mcp`, supervisor modules или
внутренние Python scripts напрямую.

Если source/runtime уже приведены в требуемое состояние, а текущая task не
может доказать свежую effective registration из-за session-scoped cache,
используй [единый контракт cross-thread continuation](../../.agents/skills/azurpilot-repository-development/references/cross-thread-task-delegation.md).
Независимая task/thread должна заново подтвердить фактически вызываемую surface,
contract, catalog и `runtime_ready`; создание task без terminal evidence не
закрывает mandatory gate.

## 8. Базовый кроссплатформенный контракт

Базовый Python tooling/CLI сохраняет одинаковую semantic model для Windows, Linux и
macOS: DTO, reason codes, JSON contract, repository identity, structured process
invocation и bounded filesystem/Git primitives.

Внешние и продуктовые capabilities зависят от среды:

- WSL/COM shortcut — Windows-specific;
- конкретный emulator/device backend требует отдельного подтверждения;
- Docker/PostgreSQL availability зависит от configured runtime;
- CodeRabbit review environment зависит от доказанного native adapter/runtime и
  exact process identity.

Запуск core CLI на ОС сам по себе не доказывает поддержку device/emulator или
внешнего провайдера на этой ОС.

## 9. Граница совместимости PowerShell

В поддерживаемой product scope владельцем Start/Stop/Update/Repair/Build является
Python tooling. PowerShell остаётся только runner glue; legacy shell допустим
лишь для external native hooks и CI glue, а не как второй project-owned operator
path. Удаление legacy-пути требует одновременно:

- эквивалентного поведения success/failure/recovery;
- exact ownership/path/process/Git evidence;
- миграции вызывающих компонентов/docs/shortcuts/installers;
- актуальных tests/CI на затронутых ОС;
- отдельного решения об удалении, а не вывода «Python уже умеет похожую команду».

Image-native/container hooks PostgreSQL не считаются legacy host wrapper только
из-за того, что написаны на shell.

## 10. Граница безопасности

Tooling не должен:

- печатать секреты или полные credential URLs;
- сериализовать произвольные логи провайдера в JSON evidence;
- читать произвольные файлы вне validated root;
- останавливать процесс по PID/port/name без ownership evidence;
- выполнять generic shell/eval ради обхода typed adapter;
- автоматически чинить ambiguous state mutating operation;
- переносить пользовательские secrets/config в review/disposable checkout.

## 11. Маршрутизация проверок

Проверки выбираются по изменённой области.

Для core CLI/tooling contract:

- syntax/compile/lint;
- targeted contract/integration tests;
- human invocation;
- соответствующий `--json` invocation с одним closed result envelope;
- platform gate, если затронут native adapter.

Для внешних интеграций:

- tests соответствующего adapter/service;
- `dev_tools/integration_contract_gate.py`;
- scoped live/provider check только когда prerequisites доступны и операция
  действительно входит в scope.

Для MCP source/compatibility изменений дополнительно применяется существующий
MCP compatibility gate и generated metadata verification.

Для Windows tooling change запускаются Python CLI/lifecycle/shortcut/update/
repair/build checks. PowerShell остаётся только runner glue и не является
production operator implementation.

Общие критерии готовности находятся в `08-VERIFICATION.md`.

## 12. Что не хранить здесь

Не добавлять обратно:

- номер временного этапа или состояние конкретной итерации задачи;
- исходное пользовательское задание и историю выбора framework;
- предлагаемую будущую структуру каталогов рядом с уже действующей;
- текущие PR/branch/SHA/CI run;
- буквальные пользовательские пути и пути конкретной машины;
- таблицы версий, которые уже генерируются из канонического источника;
- обещание capability, которого ещё нет в коде;
- привязку обязательного финального ревью к конкретной модели/версии.

Если появляется новый устойчивый владелец или граница, обновить соответствующий
раздел и удалить старую формулировку, а не накапливать рядом временные слои.
