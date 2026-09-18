# Python tooling и внешние интеграции

## Назначение

Документ описывает **текущее** repository-owned Python tooling AzurPilot и его
устойчивые границы. Это не roadmap миграции и не журнал предыдущих задач.

При споре сначала проверять текущий код, ближайшие tests и generated contracts.
Current PR/head SHA, machine paths, review-cycle state и планы будущей реализации
сюда не записываются.

## 1. Текущие owners

| Surface | Текущий owner | Инвариант |
|---|---|---|
| CLI | `azurpilot.cli` | parsing/rendering отделены от service logic; import не запускает operations |
| Typed result model | `azurpilot.tooling.contracts` | closed Pydantic models, stable result/state/reason codes, bounded evidence |
| Repository/process primitives | `azurpilot.tooling` | exact identity, path/process ownership, bounded operations, fail-closed ambiguity |
| Lifecycle/build/repair/update | соответствующие services в `azurpilot.tooling` | CLI является adapter; platform-specific behavior не размножается в renderer |
| Git delivery | `azurpilot.tooling.delivery` | typed manifest, exact refs, allowlist, journal, ordinary push/read-back |
| Pull request publication | `azurpilot.tooling.pull_request` | typed PR spec/body, explicit provider identity, draft/read-back contract |
| External integrations | `azurpilot.integrations` | direct typed adapters, bounded credential/evidence boundary |
| MCP status/compatibility | tooling + существующие MCP contract gates | source state не выдаётся за effective client registration |
| Windows compatibility/operator paths | project PowerShell scripts/modules | не удаляются без доказанной parity и caller migration |

Source package `azurpilot/` является tooling/application package и не
становится gameplay owner. `alas.py`, `gui.py`, `module/application`,
`module/device`, combat/map/campaign и другие product layers сохраняют свои
границы.

## 2. CLI contract

Установленный entrypoint `azur` ведёт в `azurpilot.cli:main`. Текущая
реализация использует `argparse` и Rich; это факт реализации, а не предложение
для будущего выбора framework.

CLI имеет два presentation режима:

- human output — краткий terminal UI;
- `--json` — ровно один закрытый machine-readable result envelope.

Rich/ANSI/progress не меняют JSON schema. Service layer не зависит от terminal
renderer, а tests/MCP не должны scrape human output.

Project-bound command не угадывает repository identity по случайному cwd, если
identity не доказана. Explicit/configured/installation provenance валидируется,
а неоднозначность завершается typed failure.

## 3. Typed results и evidence

`ToolingResult[TDetails, TEvidence]` и operation-specific DTO — canonical model
между service, CLI JSON и tests.

Постоянные правила:

- schema закрытая; arbitrary `dict[str, Any]` не используется как публичный
  result contract;
- details/evidence bounded по размеру;
- raw provider payload, full logs, tokens, cookies, credential URLs и личные
  paths не сериализуются;
- success/failure/unsupported/unavailable/not-configured различаются typed
  state/reason codes, когда operation contract требует такого различия;
- timeout/unknown external mutation не превращается в success по предположению;
- diagnostic/read-only command не получает скрытый mutating fallback.

## 4. Git delivery

Delivery boundary публикует только доказанный Git state.

Typed manifest/spec содержит необходимую identity:

- repository;
- expected branch/local head;
- base SHA;
- remote/ref;
- intended paths/changes;
- preimage/postimage и operation intent там, где они нужны.

Правила publication:

- staged allowlist обязателен; `git add .` как скрытый fallback запрещён;
- force/force-with-lease и destructive cleanup не используются;
- push обычный и проверяется exact remote SHA;
- unknown/timeout mutation сохраняется в external journal как неоднозначное
  состояние, recovery сначала делает read-only verification;
- Gitleaks evidence относится к staged/committed scope, определённому operation,
  а не заменяется случайным regex search.

Git lifecycle, ветки и разрешение merge принадлежат
`.codex/context/GIT-WORKFLOW.md`, а не этому документу.

## 5. Pull request publication

`PullRequestService` работает через typed publication spec и renderer body.

PR contract:

- repository/base/head задаются явно;
- provider read-back подтверждает repository identity, refs/SHAs и draft state;
- duplicate, cross-repository, wrong-head и provider-unknown состояния
  fail-closed;
- body строится из structured model, а не shell string;
- operator-facing body — содержательный русскоязычный report; technical
  identifiers сохраняются без перевода;
- CodeRabbit evidence в body должно соответствовать exact base/head, если оно
  заявлено.

Создание draft PR не даёт разрешение на merge. Lifecycle после publication
определяет `GIT-WORKFLOW.md`.

## 6. External integrations

`IntegrationRegistry` содержит текущие шесть семейств:

1. CodeRabbit;
2. Semgrep;
3. Grafana;
4. Context7;
5. Docker Docs;
6. Docker Hub.

Это direct typed adapters. Docker MCP Toolkit/Gateway и generic MCP proxy не
являются critical path или fallback registration source для этих integrations.

Общие правила:

- user/machine credentials приходят из разрешённой внешней конфигурации или
  environment и не попадают в tracked source;
- status/doctor/read operations остаются bounded;
- provider mutation разрешена только если конкретный adapter и operation contract
  явно её поддерживают;
- provider output нормализуется до bounded typed evidence;
- отсутствие external capability не маскируется synthetic success.

### Semgrep

Semgrep запускается только с явным scoped input: staged/changed/explicit paths
по контракту команды. Scan всего repository не используется как скрытый default.
Findings нормализуются, path обязан оставаться внутри validated root.

### Grafana

Grafana route принимается только из подтверждённой текущей topology/config.
Нельзя возвращать unconditional `host.docker.internal` или угадывать endpoint.
Tool allowlist остаётся read-only; mutating Grafana tools блокируются.

### Context7 и Docker Docs

Используют прямой HTTP/MCP adapter с bounded discovery/calls. Наличие anonymous
или credentialed режима определяется adapter/config, а не hardcoded секретом.

### Docker Hub

Docker Hub surface остаётся read-only через allowlist/denylist. Mutation tools
не разрешаются как fallback ради удобства диагностики.

### CodeRabbit

CodeRabbit — advisory reviewer, не источник истины.

Current boundary:

- adapter выбирает доказанный WSL2 Linux review environment;
- используется отдельный persistent review clone canonical repository;
- review scope — exact committed head/base; implementation checkout не
  подменяется review clone;
- review clone во время active review не используется для product fixes;
- finding triage: confirmed / partially confirmed / false positive /
  insufficient evidence;
- bounded review cycle ограничивает substantive iterations и сохраняет typed
  state;
- provider rate limit/cooldown не расходует substantive iteration и не запускает
  blind retry;
- provider text/location/title нормализуются в bounded evidence;
- machine path, username, auth/config directory и executable path не
  хардкодятся в tracked source.

Подробный human workflow хранится в
`.agents/skills/azurpilot-coderabbit-review/`, а Git lifecycle — в
`GIT-WORKFLOW.md`.

## 7. MCP boundary

Dev MCP и Game MCP остаются отдельными products:

- `module.dev_mcp` — development runtime/smoke/evidence/control boundary;
- `module.game_mcp` — game read/control boundary;
- `module.mcp_shared` — нейтральный authenticated transport/shared protocol
  code, но не новый product owner.

Canonical version/compatibility sources находятся в project metadata и generated
plugin compatibility files. Не копировать mutable version/hash tables в context.

MCP diagnostics должны различать:

- tracked/source configuration;
- локально наблюдаемый runtime;
- plugin/source snapshot;
- effective client/session registration, если для него реально есть
  authoritative evidence.

Нельзя объявлять effective registration «готовой» только потому, что
`.codex/config.toml` корректен.

## 8. Core cross-platform contract

Core Python tooling/CLI сохраняет одинаковую semantic model для Windows, Linux и
macOS: DTO, reason codes, JSON contract, repository identity, structured process
invocation и bounded filesystem/Git primitives.

External/product capabilities capability-dependent:

- WSL/COM shortcut — Windows-specific;
- конкретный emulator/device backend требует отдельного подтверждения;
- Docker/PostgreSQL availability зависит от configured runtime;
- CodeRabbit review environment зависит от доказанного adapter/runtime.

Запуск core CLI на ОС сам по себе не доказывает поддержку device/emulator или
external provider на этой ОС.

## 9. PowerShell compatibility boundary

Наличие Python command не означает автоматическое удаление существующего
PowerShell owner или gate.

Start/Stop/Update/Repair/Build и project modules могут оставаться operator/
compatibility paths, пока callers не переведены и parity не доказана. Removal
требует одновременно:

- эквивалентного success/failure/recovery behavior;
- exact ownership/path/process/Git evidence;
- миграции callers/docs/shortcuts/installers;
- актуальных tests/CI на затронутых ОС;
- отдельного решения об удалении, а не вывода «Python уже умеет похожую команду».

Image-native/container hooks PostgreSQL не считаются legacy host wrapper только
из-за того, что написаны на shell.

## 10. Security boundary

Tooling не должен:

- печатать секреты или полные credential URLs;
- сериализовать arbitrary provider logs в JSON evidence;
- читать произвольные файлы вне validated root;
- останавливать процесс по PID/port/name без ownership evidence;
- выполнять generic shell/eval ради обхода typed adapter;
- автоматически чинить ambiguous state mutating operation;
- переносить пользовательские secrets/config в review/disposable checkout.

## 11. Verification routing

Проверки выбираются по изменённой surface.

Для core CLI/tooling contract:

- syntax/compile/lint;
- targeted contract/integration tests;
- human invocation;
- соответствующий `--json` invocation с одним closed result envelope;
- platform gate, если затронут native adapter.

Для external integrations:

- tests соответствующего adapter/service;
- `dev_tools/integration_contract_gate.py`;
- scoped live/provider check только когда prerequisites доступны и операция
  действительно входит в scope.

Для MCP source/compatibility изменений дополнительно применяется существующий
MCP compatibility gate и generated metadata verification.

Для PowerShell change остаются Parser/PSScriptAnalyzer и требуемый Windows
acceptance. Они не запускаются для несвязанного Python/domain diff.

Точный общий Definition of Done находится в `08-VERIFICATION.md`.

## 12. Что не хранить здесь

Не добавлять обратно:

- roadmap/stage/increment/follow-up state;
- исходный prompt и историю выбора framework;
- «будущую» directory tree рядом с уже реализованной tree;
- current PR/branch/SHA/CI run;
- literal user/machine paths;
- version tables, которые уже генерируются из canonical source;
- обещание capability, которого ещё нет в коде;
- reviewer model/version как permanent requirement.

Если появляется новая устойчивая owner/boundary — обновить соответствующий раздел
и удалить старую формулировку, а не накапливать рядом временные слои.
