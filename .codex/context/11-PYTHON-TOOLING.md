# Python tooling и внешние интеграции

## Назначение

Документ описывает **текущее** repository-owned Python tooling AzurPilot и его
устойчивые границы. Это не дорожная карта миграции и не журнал предыдущих задач.

При споре сначала проверять текущий код, ближайшие тесты и сгенерированные
контракты. Текущие PR/head SHA, локальные machine paths, состояние review-цикла
и планы будущей реализации сюда не записываются.

## 1. Текущие владельцы

| Область | Текущий владелец | Инвариант |
|---|---|---|
| CLI | `azurpilot.cli` | parsing/rendering отделены от service logic; import не запускает operations |
| Typed result model | `azurpilot.tooling.contracts` | closed Pydantic models, stable result/state/reason codes, bounded evidence |
| Repository/process primitives | `azurpilot.tooling` | exact identity, path/process ownership, bounded operations, fail-closed ambiguity |
| Lifecycle/build/repair/update | соответствующие services в `azurpilot.tooling` | CLI является adapter; platform-specific поведение не размножается в renderer |
| Git delivery | `azurpilot.tooling.delivery` | typed manifest, exact refs, allowlist, journal, ordinary push/read-back |
| Pull request publication | `azurpilot.tooling.pull_request` | typed PR spec/body, explicit provider identity, draft/read-back contract |
| Внешние интеграции | `azurpilot.integrations` | прямые типизированные adapters, ограниченная credential/evidence граница |
| MCP status/compatibility | tooling + существующие MCP contract gates | source state не выдаётся за effective client registration |
| Windows compatibility/operator paths | project PowerShell scripts/modules | не удаляются без доказанной parity и caller migration |

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
а неоднозначность завершается typed failure.

## 3. Типизированные результаты и evidence

`ToolingResult[TDetails, TEvidence]` и DTO конкретных операций — каноническая модель
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

Граница delivery публикует только доказанное состояние Git.

Typed manifest/spec содержит необходимую identity:

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
- unknown/timeout mutation сохраняется в external journal как неоднозначное
  состояние, recovery сначала делает read-only verification;
- Gitleaks evidence относится к staged/committed scope, определённому operation,
  а не заменяется случайным regex search.

Git lifecycle, ветки и разрешение merge принадлежат
`.codex/context/GIT-WORKFLOW.md`, а не этому документу.

## 5. Публикация pull request

`PullRequestService` работает через typed publication spec и renderer body.

PR contract:

- repository/base/head задаются явно;
- provider read-back подтверждает repository identity, refs/SHAs и draft state;
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
- provider mutation разрешена только если конкретный adapter и operation contract
  явно её поддерживают;
- provider output нормализуется до bounded typed evidence;
- отсутствие внешней capability не маскируется synthetic success.

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

Поверхность Docker Hub остаётся read-only через allowlist/denylist. Mutation tools
не разрешаются как fallback ради удобства диагностики.

### CodeRabbit

CodeRabbit — advisory reviewer, не источник истины.

Текущая граница:

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
- plugin/source snapshot;
- effective client/session registration, если для него реально есть
  authoritative evidence.

Нельзя объявлять effective registration «готовой» только потому, что
`.codex/config.toml` корректен.

## 8. Базовый кроссплатформенный контракт

Базовый Python tooling/CLI сохраняет одинаковую semantic model для Windows, Linux и
macOS: DTO, reason codes, JSON contract, repository identity, structured process
invocation и bounded filesystem/Git primitives.

Внешние и продуктовые capabilities зависят от среды:

- WSL/COM shortcut — Windows-specific;
- конкретный emulator/device backend требует отдельного подтверждения;
- Docker/PostgreSQL availability зависит от configured runtime;
- CodeRabbit review environment зависит от доказанного adapter/runtime.

Запуск core CLI на ОС сам по себе не доказывает поддержку device/emulator или
внешнего провайдера на этой ОС.

## 9. Граница совместимости PowerShell

Наличие Python command не означает автоматическое удаление существующего
владельца PowerShell или gate.

Start/Stop/Update/Repair/Build и project modules могут оставаться operator/
compatibility paths, пока callers не переведены и parity не доказана. Removal
требует одновременно:

- эквивалентного поведения success/failure/recovery;
- exact ownership/path/process/Git evidence;
- миграции callers/docs/shortcuts/installers;
- актуальных tests/CI на затронутых ОС;
- отдельного решения об удалении, а не вывода «Python уже умеет похожую команду».

Image-native/container hooks PostgreSQL не считаются legacy host wrapper только
из-за того, что написаны на shell.

## 10. Граница безопасности

Tooling не должен:

- печатать секреты или полные credential URLs;
- сериализовать arbitrary provider logs в JSON evidence;
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

Для PowerShell change остаются Parser/PSScriptAnalyzer и требуемый Windows
acceptance. Они не запускаются для несвязанного Python/domain diff.

Общие критерии готовности находятся в `08-VERIFICATION.md`.

## 12. Что не хранить здесь

Не добавлять обратно:

- номер временного этапа или состояние конкретной итерации задачи;
- исходное пользовательское задание и историю выбора framework;
- предлагаемую будущую структуру каталогов рядом с уже действующей;
- текущие PR/branch/SHA/CI run;
- буквальные пользовательские и machine paths;
- таблицы версий, которые уже генерируются из канонического источника;
- обещание capability, которого ещё нет в коде;
- привязку обязательного финального ревью к конкретной модели/версии.

Если появляется новый устойчивый владелец или граница, обновить соответствующий
раздел и удалить старую формулировку, а не накапливать рядом временные слои.
