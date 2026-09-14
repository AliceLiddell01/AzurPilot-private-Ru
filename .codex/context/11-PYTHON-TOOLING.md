# Единый Python tooling: текущее состояние и контракт будущей миграции

## Назначение и границы

Этот документ фиксирует постоянную архитектурную границу для будущего объединения
инструментов запуска, обслуживания, диагностики, доставки и внешних интеграций
AzurPilot в единый Python tooling. Он является контрактом проектирования, а не
отчётом о разовом запуске и не описывает исторический этап разработки.

Документ основан на фактическом коде текущей ветки, тестах, CI, `.codex/context/`,
репозиторных skills и plugin package. При расхождении с кодом первичным остаётся
код и исполняемый контракт.

В рамках этой фиксации не выполняются:

- перенос `Start`/`Stop`/`Build`/`Repair`/`Update` в Python;
- включение package installation или console entrypoint в `pyproject.toml`;
- изменение поведения `gui.py`, `alas.py`, WebUI, Dev MCP или Game MCP;
- удаление PowerShell, Bash, BAT, native PostgreSQL hooks или бинарных launcher/installer artifacts;
- добавление runtime dependencies;
- замена обязательных CI jobs или device/game acceptance.

Нативные `.sh` внутри образа PostgreSQL остаются container-owned initialization
contract. Само расширение файла не является основанием для удаления. То же
относится к Linux Docker deployment, пока его ownership и parity не подтверждены.

## Источники конфигурации и текущие ограничения

| Область | Текущий источник истины | Граница будущего tooling |
| --- | --- | --- |
| Версия Python и зависимости | `pyproject.toml`, `uv.lock`, локальная `.venv`, `uv` | Python API не должен подменять `uv` произвольным `pip`; lock и `uv` остаются явными интеграциями |
| WebUI и планировщик | `gui.py`, `alas.py`, `module/webui/`, `module/application/` | tooling управляет lifecycle через typed service/adapter, не импортирует игровые handlers для запуска |
| Development target | `module/dev_runtime/target.py`, `module/dev_runtime/target_policy.json`, marker в `config/state/` | target разрешается registry и explicit consent; произвольный профиль в команде или MCP не принимается |
| MCP identity | `config/mcp-versions.toml`, `module/*_mcp/contract.py`, `plugins/azurpilot/compatibility.json` | CLI, MCP и tests используют одну model; значения версий не дублируются литералами |
| Project MCP registration | `.codex/config.toml` | plugin package не становится вторым registration source |
| User/machine state | environment, user config, ignored `config/state/`, внешние transaction roots | секреты, cookies, process caches и личные пути не попадают в tracked source |
| CI и release gates | `.github/workflows/ci.yml`, `docs/ci.md`, `.codex/context/08-VERIFICATION.md` | Python/Windows/Security сохраняются до доказанной parity; green CI не разрешает merge |

## 1. Текущее состояние

### 1.1 Полный inventory legacy entrypoints

Ниже перечислены все tracked `.ps1`, `.psm1`, `.sh` и BAT-wrapper, найденные в
текущем checkout. Кандидат на будущий Python adapter не равен кандидату на
удаление: сначала должен быть доказан новый owner, parity и отсутствие call site.

| Артефакт | Назначение и entrypoint | Caller и внешние зависимости | Mutations и safety contract | Failure/recovery, tests и future owner |
| --- | --- | --- | --- | --- |
| `scripts/Start-AzurPilot.ps1` | Владелец запуска подготовленного Windows checkout; запускает `gui.py`, ждёт WebUI readiness и при необходимости открывает браузер | Ярлык из `AzurPilot.Shortcut.psm1`, README и ручной `pwsh`; PowerShell 7.6, project Python, `gui.py`, `config/deploy.yaml`, Docker Compose | Repository-scoped mutex и stop event; preflight `infrastructure/observability/compose.yaml`, PostgreSQL, bootstrap и опциональный Caddy; не делает Git update и не синхронизирует `.venv`; exact process/port ownership | Bounded timeout, captured/redacted output, foreign-port fail-closed, owned process tree stop; стабильные exit categories в самом скрипте; `tests/platform/powershell/test_powershell_contracts.py`, lifecycle acceptance и README. Будущий owner: `tooling.lifecycle` + Windows platform adapter |
| `scripts/Stop-AzurPilot.ps1` | Штатно останавливает backend текущего checkout | README, оператор и Start; PowerShell, project Python, `gui.py`, `scripts/lib` lifecycle contract | Stop event владельцу Start; ждёт порт, mutex и exact process; fallback допускается только после PID/executable/command/cwd/creation evidence; PostgreSQL не трогает | Чужой listener не останавливается, generic `Stop-Process` не используется; bounded wait и exit categories; lifecycle tests и Windows CI. Будущий owner: тот же lifecycle service, отдельный stop adapter |
| `scripts/Update-AzurPilot.ps1` | Единственный владелец обычного пользовательского обновления | README, operator workflow и Repair precondition; Git, `uv`, `robocopy`, `tar`, Docker/PostgreSQL | Проверяет root, branch, remote, disabled upstream push, clean tree и отсутствие active operation; `fetch` + `merge --ff-only`; при изменении dependency files создаёт внешний candidate/backup/journal, синхронизирует `.venv`, делает PostgreSQL logical backup | Failpoints и recovery для `AfterBackup`, `AfterSync`, `AfterMerge`; ambiguous/corrupt journal блокирует автоматическое продолжение; `tools/acceptance/powershell/Test-Update-AzurPilot.ps1`, PowerShell contracts, CI. Будущий owner: `tooling.delivery` + Git/dependency/database adapters |
| `scripts/Repair-AzurPilot.ps1` | Диагностирует окружение и транзакционно восстанавливает существующую `.venv`; отдельно чинит shortcut | Operator/README; PowerShell, Python, `uv`, Docker/PostgreSQL, `robocopy`, external transaction root | Не меняет branch/remote/user data; не обходит незавершённый Update; move/backup `.venv`, rebuild через `deploy.uv`, restore auxiliary tools, hash/config validation, rollback; optional COM shortcut repair | Journal phases, multiple/missing backup fail-closed, rollback result различается; exit categories включают diagnostic/shortcut/elevation; PowerShell parser/PSScriptAnalyzer, tests и Windows CI. Будущий owner: `tooling.repair` поверх тех же delivery/process primitives |
| `scripts/Build-AzurPilot.ps1` | Подготавливает уже полученный checkout и локальный shortcut | README/installer workflow; PowerShell, pinned uv/ADB bootstrap archives, SHA-256, Python, `deploy.uv`, COM | Не клонирует и не обновляет Git; создаёт `config/deploy.yaml` из template, при необходимости строит `.venv`, проверяет imports/ADB/frozen lock, удаляет только созданное partial state при отказе | Bootstrap cache, hashes и test failpoints; сохранение существующего здорового окружения; PowerShell/Windows gates. Будущий owner: `tooling.bootstrap` + artifact/ADB/platform adapters |
| `scripts/lib/AzurPilot.Lifecycle.psm1` | Общий Windows ownership contract для Start/Stop | Импортируется обоими скриптами; CIM/NetTCP, process tree, named mutex/event | Сравнивает exact repository, project Python, `gui.py`, command line и parent chain; `taskkill.exe /PID /T /F` только после подтверждённого ownership и creation date | `Free`/`Foreign`/`AzurPilot`, safe fallback и no generic kill; lifecycle acceptance. Будущий owner: `platform.windows.process` и `platform.windows.coordination` |
| `scripts/lib/AzurPilot.Shortcut.psm1` | Создаёт и проверяет `.lnk` на Start-команду | Repair/Build и Windows COM `WScript.Shell` | Формирует `pwsh -File scripts\Start-AzurPilot.ps1 -FromShortcut`, проверяет target/cwd/icon, пишет temp и делает atomic replace с backup | Restore on failure, local/all-users modes и admin boundary; shortcut checks в Repair/Build. Будущий owner: `platform.windows.shortcut`; COM остаётся platform adapter |
| `scripts/Start-Codex-Local-Mcp.ps1` | Тонкий Windows wrapper для `module.mcp_shared.local_http_supervisor` | Ручной Desktop setup; project `.venv` Python, user environment tokens | Запускает supervisor hidden, пишет ignored state/logs, ждёт `127.0.0.1:8775/8776` и `LOCAL_MCP_READY`; не управляет игровым lifecycle | Bounded readiness и failure при отсутствии token/process; contract/runtime MCP tests. Будущий owner: `tooling.mcp.local_http` или сохранённый compatibility wrapper |
| `tools/acceptance/powershell/Test-AzurPilotLifecycle.ps1` | Изолированный Windows smoke ownership/mutex/event/foreign-port/fallback | Запускается вручную и из Windows CI; временный fixture checkout | Проверяет только synthetic processes и cleanup fixture, не production game/device | Нет device side effects; результат заменяет не parser/PSScriptAnalyzer, а дополняет их. Будущий owner: Python process/coordination acceptance при сохранении Windows compatibility gate |
| `tools/acceptance/powershell/Test-Update-AzurPilot.ps1` | Изолированный harness для Update transaction/recovery | Ручной и CI Windows run; local bare producer/client, fake venv and failpoints | Проверяет no-op, fast-forward, dirty/local-ahead/diverged, dependency transaction, journal corruption/orphan/conflict, network failure | Cleanup fixture и explicit `KeepFixtures`; это source of parity, а не runtime command. Будущий owner: Python delivery integration suite |
| `deploy/docker/deploy-image.sh` | Полноценный Linux Docker deployment: checkout, image, container, WebUI readiness и URL | Linux Bash, `git`, `curl`, Docker, apt/yum/dnf/systemd при установке; отдельные Docker volumes/config | Может установить host tools, clone/update `personal/stable` через `merge --ff-only`, build image, remove old container, run bind-mounted checkout/venv volume; optional public/private URL output | `set -euo pipefail`, interactive prompts, bounded curl/log readiness; нет Python parity; `tests/contracts/repository/test_no_upstream_project_network_defaults.py` проверяет часть routing. Будущий owner: `tooling.deploy` + Linux/Docker adapters, только после отдельной Linux parity |
| `deploy/docker/Docker-run.sh` | Старый Linux wrapper с update/build/run path | Bash, `git`, Docker, host ADB; использует string `eval`, stash/pull origin master и XDG lock | Может менять checkout через stash/pull и убивать container; не соответствует current `personal/stable` lifecycle safety | Нет основания считать его эквивалентом `deploy-image.sh`; сначала provenance/call-site audit, затем migration или removal decision. Будущий owner: не механический port, а отдельный redesign |
| `infrastructure/observability/postgres/bootstrap/01-bootstrap.sh` | Image-native PostgreSQL roles/schema/default privileges bootstrap | Docker official PostgreSQL image, `/run/secrets`, `psql`, `.pgpass` mode 600 | Создаёт/изменяет DB roles, passwords, ownership и grants внутри container; secrets не печатает | Strict Bash, cleanup temporary `.pgpass`, `ON_ERROR_STOP`; покрывается Compose/PostgreSQL gates. Будущий owner: container hook или эквивалентный image-native contract, не общий host tooling |
| `infrastructure/observability/postgres/init/01-bootstrap.sh` | Image-native `pg_hba.conf` local auth normalization | PostgreSQL init container, `psql`, `awk`, `mv`, file mode | Пишет temporary HBA, atomic move и reload; действует только внутри DB image | Strict Bash и trap cleanup; перенос в Python допустим лишь вместе с эквивалентом init image contract и отдельной DB acceptance |
| `deploy/launcher/Alas.bat` | Старый Windows wrapper: добавляет `.venv`/embedded Git в PATH, вызывает `python -m deploy.installer`, затем `gui.py --electron` | BAT, project `.venv`, Python; `deploy.installer` в текущем checkout отсутствует | Передаёт управление отсутствующему legacy installer; не является текущим Start owner | `deploy/Readme.md` всё ещё ссылается на этот путь; бинарный launcher и installer artifacts требуют отдельного provenance audit. Не удалять в этом изменении |
| `dev_tools/alas2.bat` | Retired tombstone | BAT; сообщает о переходе на `.venv`/uv или Rust launcher | Ничего не запускает и завершается с ненулевым кодом | Удаление или оставление — отдельное compatibility decision после проверки внешних shortcuts, не часть tooling migration |

Полный список файлов этой группы: `scripts/Build-AzurPilot.ps1`,
`scripts/Repair-AzurPilot.ps1`, `scripts/Start-AzurPilot.ps1`,
`scripts/Stop-AzurPilot.ps1`, `scripts/Update-AzurPilot.ps1`,
`scripts/Start-Codex-Local-Mcp.ps1`, `scripts/lib/AzurPilot.Lifecycle.psm1`,
`scripts/lib/AzurPilot.Shortcut.psm1`,
`tools/acceptance/powershell/Test-AzurPilotLifecycle.ps1`,
`tools/acceptance/powershell/Test-Update-AzurPilot.ps1`,
`deploy/docker/deploy-image.sh`, `deploy/docker/Docker-run.sh`,
`infrastructure/observability/postgres/bootstrap/01-bootstrap.sh`,
`infrastructure/observability/postgres/init/01-bootstrap.sh`,
`deploy/launcher/Alas.bat` и `dev_tools/alas2.bat`.

### 1.2 Python runtime, WebUI и deploy seams

| Компонент | Фактический ownership | Что важно для будущего owner |
| --- | --- | --- |
| `gui.py` | Запускает Uvicorn/WebUI, создаёт dual-stack sockets, использует `multiprocessing` и `spawn` на macOS, держит dependency-sync service и worker recovery | Нельзя считать HTTP port достаточным доказательством ownership; нужно сохранить readiness event, child cleanup, restart semantics и Windows venv redirector behavior |
| `alas.py` | Scheduler, task dispatch, application/game runtime и отдельные subprocess paths | Общий tooling не должен проникать в gameplay loop или превращать состояние игры в CLI exit code |
| `module/webui/` | `ProcessManager`, worker registry, application pages, deploy settings и WebUI lifecycle | Existing owner остаётся единственным WebUI owner; registry хранит PID и creation time, read-only paths не создают lock/migration side effects |
| `module/application/` | Нейтральные DTO, ports, `InstanceQueryService`, `TaskCatalogService`, Game read/control services и typed errors | Это domain application layer, а не место для Git/PowerShell/CodeRabbit; tooling вызывает его только через узкий adapter, когда нужен product status |
| `module/dev_runtime/` | DevSession, target registry, process identity, evidence, SmokeRun, runtime control, persistent operation и shared WebUI facade | Уже содержит образец typed result, bounded state, target identity, coordination lock, exact ownership и fail-closed recovery; новые tooling primitives должны быть совместимы по принципам |
| `deploy/uv.py` | Обёртка над `uv`, project `.venv`, managed Python, index policy и dependency sync service | Это существующий reusable seam; его поведение нельзя тихо размножать в новом CLI. Сначала выделить protocol/adapter и оставить compatibility import |
| `deploy/atomic.py` | Atomic file/dir write/replace/read/remove и Windows retry behavior | Кандидат на общий filesystem adapter, но symlink/junction/reparse и transaction-root policy должны быть проверены отдельным контрактом |
| `deploy/config.py`, `deploy/utils.py`, `deploy/set.py` | Legacy deploy config model, YAML patch/write and command-line configuration | Не создавать новую схему поверх generated config; source/config generation остаётся в `module/config/` и существующих generators |
| `dev_tools/infrastructure_doctor.py`, `dev_tools/postgresql_runtime.py`, `dev_tools/observability_compose_migration.py` | Existing diagnostics and infrastructure operations | Их result/error contracts нужно переиспользовать через adapter; tooling не должен обходить PostgreSQL/Compose ownership |

### 1.3 Call sites, documentation, shortcuts и installer references

Найденные пользовательские и проектные references распределены между несколькими
границами:

- `README.md` документирует Start/Stop/Update/Repair/Build, shortcut и ручные
  `pwsh` команды; эти команды пока остаются каноническим Windows operator path.
- `deploy/Readme.md` содержит устаревшую ссылку на отсутствующий
  `deploy.installer` и на `deploy/launcher/Alas.bat`; это подтверждённый
  documentation drift, но его исправление не входит в архитектурную фиксацию.
- `tools/acceptance/README.md` и `.github/workflows/ci.yml` владеют Windows
  parser, PSScriptAnalyzer и изолированными lifecycle/update runs.
- `.codex/context/02-RUNTIME-ARCHITECTURE.md`,
  `07-WEBUI-INFRASTRUCTURE.md`, `08-VERIFICATION.md`, `GIT-WORKFLOW.md` и
  `POWERSHELL-GIT-RULES.md` описывают текущие ownership и gates.
- `docs/dev-runtime.md`, `docs/game-mcp.md`, `docs/ci.md`,
  `docs/postgresql-production-cutover.md`, `docs/runtime-russianization-release.md`
  фиксируют MCP, WebUI, PostgreSQL и lifecycle boundaries.
- `plugins/azurpilot/README.md`, `references/mcp-routing.md`, три plugin skills и
  `.agents/plugins/marketplace.json` описывают Codex routing; plugin не является
  installer и не создаёт второй MCP implementation.
- `.codex/config.toml` содержит project-scoped direct stdio entries, loopback
  aliases, Docker Gateway и direct diagnostic routes. Это tracked registration
  source, но не доказательство effective registration в живой Codex session.
- В корне присутствуют `alas-launcher.exe`, `unins000.exe`, `unins000.dat` и
  `deploy/launcher/icon.ico`. Бинарные artifacts нельзя объявлять legacy или
  удалять без отдельной проверки происхождения, installer ownership и внешних
  shortcuts.

## 2. Целевая архитектура

### 2.1 Разделение слоёв

Целевой поток должен быть единым по смыслу для CLI, MCP и tests:

```text
CLI adapter       MCP adapter       test adapter
      \               |                 /
       \              |                /
        common tooling service layer
          |       |         |
      config   process   delivery/state
          |       |         |
       platform adapters и integration adapters
       Windows/Linux/macOS, Git, ADB, Docker, WSL, CodeRabbit
```

Границы слоёв:

1. `contracts` — immutable request/result/error DTO, schema version и capability;
2. `services` — policy и orchestration без `print`, MCP SDK, shell string и
   platform-specific process calls;
3. `ports` — узкие Protocol для clock, filesystem, process, lock, Git, Docker,
   ADB, WSL и review backend;
4. `platform adapters` — Windows process/COM/ACL/shortcut и POSIX
   process/signal/lock/path behavior;
5. `integration adapters` — Git, `uv`, Docker Compose, PostgreSQL, ADB,
   CodeRabbit и WSL;
6. `CLI/MCP adapters` — parsing, rendering, transport authorization и exit
   mapping. Они не владеют бизнес-политикой.

`module/application/` остаётся отдельным нейтральным product application layer.
`tooling` может вызывать его через typed ports для статуса WebUI/profile, но не
получает `State`, raw config dictionaries, `Device`, gameplay handlers или
transport-specific response objects.

### 2.2 Рекомендуемый package/module layout

Переезд не выполняется сейчас. Для последующих increments рекомендуется сначала
сформировать стабильное source API, не перемещая весь legacy product tree:

```text
azurpilot/
  __main__.py                 # python -m azurpilot
  cli.py                      # main() и top-level command registry
  tooling/
    contracts.py              # request/result/error/capability DTO
    config.py                 # project и user configuration boundary
    process.py                 # structured process invocation
    filesystem.py             # scoped paths, hashes, atomic operations
    coordination.py           # mutex/lock/ownership abstractions
    lifecycle.py              # Start/Stop service policy
    bootstrap.py               # Build/uv/ADB preparation policy
    repair.py                  # diagnosis and transactional repair
    update.py                  # safe update orchestration
    delivery.py                # validate -> remote verify pipeline
    mcp_status.py              # collector/model, без human rendering
    coderabbit.py              # review backend boundary
    ports.py                   # platform/integration Protocols
    adapters/
      windows.py
      posix.py
      git.py
      uv.py
      docker.py
      postgresql.py
      adb.py
      wsl.py
      coderabbit.py
  commands/
    doctor.py
    start.py
    stop.py
    build.py
    repair.py
    update.py
    mcp.py
    coderabbit.py
```

На первом шаге реализации допустим совместимый source location внутри текущего
`module/` при сохранении публичного будущего namespace `azurpilot`. В частности,
`deploy.uv`, `deploy.atomic`, `module.dev_runtime` и `module.mcp_shared` должны
стать адаптируемыми источниками, а не копироваться в два независимых дерева.
Существующие `module.dev_mcp` и `module.game_mcp` остаются compatibility
entrypoints до подтверждения нового package contract.

### 2.3 Configuration boundary

Tracked configuration содержит только продуктовую политику и безопасные ссылки:

- `pyproject.toml` и `uv.lock` — dependency contract;
- `config/mcp-versions.toml` и plugin `compatibility.json` — MCP compatibility;
- `module/dev_runtime/target_policy.json` — default target policy;
- `config/argument/` и generated config — продуктовые параметры;
- `.codex/config.toml` — project-scoped route declarations без literal tokens;
- Compose/Caddy templates — container topology без secret values.

User/machine configuration остаётся вне tracked source: repository root, user
environment tokens, OAuth/CodeRabbit auth, WSL home, Docker credential store,
local browser state, ACL and external transaction roots. Adapter принимает такие
значения через validated request/config object, а не через hardcoded developer
path. В durable документации используются роли (`<repository-root>`, permanent
WSL review clone, user config), а не личные usernames или рабочие абсолютные
пути.

### 2.4 Structured process invocation

Общий process port должен принимать:

```text
executable: validated path or allowlisted command
argv: tuple[str, ...]          # каждый аргумент отдельный
cwd: validated repository path
environment: explicit allowlist + bounded inherited values
timeout: monotonic deadline
capture: bounded stdout/stderr with UTF-8 replacement policy
ownership: pid, created_at, executable, argv, cwd
```

Запрещены `shell=True`, `eval`, строковая конкатенация команды, поиск процесса
по имени без identity и неограниченный вывод. На Windows сохраняются hidden
window/UTF-8/`CREATE_NO_WINDOW` semantics, а остановка дерева допускается только
после exact ownership. На POSIX используется отдельный process-group/signal
adapter с тем же требованием ownership.

Результат внешнего вызова не должен автоматически означать изменение состояния:
`returncode`, readiness, postcondition, remote SHA и journal phase проверяются
раздельно.

### 2.5 Stable result/error contract

Все transport adapters преобразуют service result в одну bounded модель:

```json
{
  "ok": true,
  "code": "TOOLING_OPERATION_READY",
  "state": "ready",
  "message": "Операция подтверждена",
  "operation_id": null,
  "details": {},
  "warnings": [],
  "evidence": {}
}
```

Обязательные свойства контракта:

- `code` — стабильный machine-readable reason code в `SCREAMING_SNAKE_CASE`;
- `state` — конечное состояние (`ready`, `running`, `failed`, `unknown`,
  `rolled_back`, `conflict`, `unavailable` и ограниченный согласованный набор);
- `message` — русская operator-facing фраза без секретов;
- `details` — bounded redacted metadata, не raw stdout/stderr и не полный config;
- `operation_id` — только для долгой операции, которую можно безопасно читать и
  завершать по immutable request;
- `evidence` — exact head/identity/hash только там, где публикация разрешена.

Будущий CLI сохраняет compatibility mapping старых exit codes, но внутри
использует semantic error codes. Предлагаемая общая numeric категория: `0` —
успех, `2` — ошибка invocation, `20` — precondition, `21` — ownership/conflict,
`22` — timeout или in-flight ambiguity, `23` — environment/dependency unavailable,
`24` — apply failed и rollback подтверждён, `25` — rollback/verification не
подтверждены, `30` — unexpected. Специфические diagnostic/shortcut/elevation
состояния должны оставаться machine-readable subcodes, а не расширять каждый
transport собственными несогласованными числами. Это proposal до отдельного
implementation contract.

Read-only и bounded checks выполняются синхронно. Start/Stop, dependency sync,
Repair, Update, Delivery и CodeRabbit review, если они дольше допустимого
request timeout, возвращают `operation_id` и читаемое состояние; MCP не удерживает
request во время долгой работы.

## 3. Packaging и entrypoints

### 3.1 Фактическая отправная точка

В `pyproject.toml` проект имеет `name = "azurpilot"`, ограничение Python
`>=3.14.6,<3.15` и `tool.uv.package = false`. Поэтому сейчас нет установленного
package console script, а рабочими являются project-local команды вида
`uv run --locked --no-sync python -m module.dev_mcp` и существующие Python
modules/scripts. Переключение `package = true` само по себе не является
миграцией: сначала нужны package layout, import audit, lock update и CI parity.

Целевые compatibility entrypoints:

```text
azur doctor
azur start
azur stop
azur build
azur repair
azur update
azur mcp status
azur coderabbit status|doctor|review

python -m azurpilot <same command>
```

В `pyproject.toml` это потребует явного `project.scripts.azur =
"azurpilot.cli:main"`, но добавлять запись до готовности package нельзя. Во время
перехода `python -m module.*` сохраняется как compatibility path для MCP,
generator и CI.

### 3.2 Выбор CLI framework

| Вариант | Плюсы | Ограничения для проекта | Решение |
| --- | --- | --- | --- |
| `argparse` | Стандартная библиотека, нет новой зависимости, зрелые nested subparsers, простой тест через `argv` | Completion и rich command registry придётся оформить самостоятельно | Рекомендуется для первой реализации |
| Click | Хорошие groups/options/completion и CLI testing helpers | Новая runtime dependency, decorator-heavy boundary, нужно отдельно зафиксировать JSON/error semantics | Не вводить автоматически; рассмотреть при доказанной потребности |
| Typer | Type hints, удобные nested commands и completion | Дополнительная dependency поверх Click, version/typing coupling, migration не оправдана для текущего scope | Не использовать на этапе contract discovery |

Независимо от framework обязательны: deterministic command registry, `--help`,
`--json`, `--no-color`/`NO_COLOR`, stdin/stdout/stderr separation, injectable
service dependencies и отсутствие side effect при импорте.

## 4. CLI UX contract

### 4.1 Единый стиль

- Human output предназначен для terminal; machine output — для `--json`.
- При `--json` stdout содержит только один bounded JSON report на операцию;
  progress, heartbeat и debug идут в stderr. В watch-режиме каждый JSON report
  завершается переводом строки.
- Цвет включается только для TTY, отключается при `NO_COLOR`, `--no-color` и
  redirected output. Нельзя кодировать состояние только цветом.
- Rich остаётся допустимым renderer, но не service dependency и не источником
  machine contract. Table, progress и spinner отключаются в non-TTY/JSON.
- Ошибка всегда содержит code, краткую причину и конкретное действие. Секреты,
  bearer values, cookies, full environment, absolute user paths и raw external
  payload не печатаются.
- Ctrl+C переводит операцию в `cancel_requested`; результатом должен быть
  подтверждённый `cancelled`, `rolled_back` или `unknown`, а не молчаливый
  успех. После unknown повтор запрещён без read-only recovery.
- stdout/stderr кодируются UTF-8 с явной политикой replacement для внешнего
  вывода; исходный raw stdout/stderr можно сохранить только в защищённом
  bounded diagnostic artifact.

### 4.2 Mockups

`azur doctor`:

```text
$ azur doctor
AzurPilot doctor
  Репозиторий       OK       проверен текущий checkout
  Project Python    OK       `.venv` отвечает
  Git                OK       рабочее дерево чистое
  WebUI owner       UNKNOWN  процесс не зарегистрирован
  MCP contract      OK       source и локальный contract совместимы
Итог: PARTIAL (DOCTOR_RUNTIME_NOT_RUNNING)
Действие: запустите `azur start`, если нужен WebUI.
```

`azur start`:

```text
$ azur start --no-browser
Проверка ownership checkout... OK
Проверка WebUI owner... отсутствует
Запуск `gui.py`... PID подтверждён
Ожидание readiness... OK
Готово: WebUI запущен (START_READY)
Остановка: `azur stop`
```

`azur mcp status`:

```text
$ azur mcp status
AzurPilot MCP Status
SERVER          LOCAL/DIRECT   ИСТОЧНИК CODEX   РЕГИСТРАЦИЯ CODEX   STATUS
azurpilot-dev   3.x OK          OK               UNKNOWN              PARTIAL
azurpilot-game  1.x OK          OK               UNKNOWN              PARTIAL
Docker Gateway  configured     ready            —                    OK
Итог: PARTIAL (CODEX_EFFECTIVE_REGISTRATION_NOT_OBSERVABLE)
```

Фактические версии, route identity и reason codes берутся из текущего report;
mockup не является baseline или committed evidence.

`azur coderabbit review`:

```text
$ azur coderabbit review --base <base-ref> --committed-only
Проверка repository root и clean review checkout... OK
Проверка auth в WSL2 review backend... OK
Проверка exact base/head... OK
CodeRabbit: committed-only review запущен
Итог: findings=0 (CODERABBIT_REVIEW_READY)
```

Prerequisite error:

```text
ОШИБКА [TOOLING_PRECONDITION_FAILED]
Project Python не найден или не подтверждён в текущем checkout.
Причина: ожидается project-local `.venv` и согласованный `uv.lock`.
Действие: выполните подготовку checkout через штатный Build path; повторный
запуск не выполняется автоматически.
```

## 5. Migration map и preconditions удаления

| Текущий owner | Будущий target | Существующие callers и tests | Характер изменения | Что должно быть доказано до удаления/переключения |
| --- | --- | --- | --- | --- |
| Start/Stop PS + Lifecycle module | `tooling.lifecycle` и Windows process/coordination adapters | shortcut, README, WebUI owner, PowerShell contracts, lifecycle acceptance, Windows CI | Redesign общей policy; не перевод строка-в-строку | exact owner, mutex/event, foreign port, readiness, Ctrl+C, child cleanup, timeout и exit compatibility на Windows |
| Build PS + `deploy.uv`/ADB bootstrap | `tooling.bootstrap` | Build/Repair, `deploy.uv`, `.venv`, generated deploy config, Windows CI | Выделение существующей policy и artifact verifier | pinned archive hash, no-network healthy path, frozen sync, partial cleanup, shortcut parity, Python 3.14 |
| Update PS | `tooling.update` + `delivery` + Git/uv/PostgreSQL adapters | README, Repair guard, Update harness, Git rules, PostgreSQL CI | High-risk redesign; journal/backup semantics нельзя упрощать | fast-forward only, branch/remote/ref checks, clean tree, dependency transaction failpoints, DB backup, crash recovery, exact postcondition и no blind retry |
| Repair PS | `tooling.repair` | README, Update transaction guard, Repair diagnostics/Windows CI | Service extraction с rollback, не generic reinstall | external transaction root, ACL/path safety, backup hash, restore/rollback states, diagnostic-only no writes, shortcut parity |
| Shortcut PS module | `platform.windows.shortcut` | Build/Repair, `.lnk` and README | Platform-specific adapter | COM target/cwd/icon/arguments, atomic replace, restore, local/all-users permissions |
| `Start-Codex-Local-Mcp.ps1` | `tooling.mcp.local_http` | `.codex/config.toml`, supervisor, MCP runtime tests | Thin wrapper can become CLI adapter | two exact services, token source, `/ready`, owner identity, cleanup and no duplicate process |
| `dev_tools/mcp_status.py` | `tooling.mcp_status` service + CLI renderer | MCP tests, `docs/dev-runtime.md`, observability docs, plugin routing | Mostly mechanical extraction of collector/model; renderer remains transport-specific | same bounded probe order, reason codes, redaction, `source_config` vs `effective` distinction, strict/watch/metrics semantics |
| `deploy/docker/deploy-image.sh` | `tooling.deploy` + Linux/Docker adapters | deployment docs and repository contract tests | Redesign: interactive deployment and host package installation are not generic lifecycle | Linux parity, safe checkout/update, Docker ownership, network/volume behavior, readiness and rollback/diagnostic path |
| `deploy/docker/Docker-run.sh` | no automatic target; separate legacy decision | Possible external operator shortcuts; repository search | Redesign or retirement, not port | provenance, zero supported callers, replacement acceptance and explicit removal decision |
| PostgreSQL init/bootstrap `.sh` | container-native hook or equivalent image contract | Compose, PostgreSQL/Alembic/backup gates | Not a host tooling migration | equivalent roles, HBA, secrets, atomicity, image startup order and DB acceptance |
| `Alas.bat`, `alas2.bat`, installer binaries/docs | compatibility wrapper or explicit removal | `deploy/Readme.md`, root binaries/icon, external shortcuts | Documentation/provenance decision | valid replacement, user-facing migration notice, installer ownership, no hidden caller, hygiene/security pass |
| PowerShell acceptance harnesses | Python parity harness plus retained Windows gates | `.github/workflows/ci.yml`, PSScriptAnalyzer/parser | Tests migrate last; current tests stay during parity | deterministic fixture equivalence, failure/recovery matrix and Windows execution evidence |

Механические части: вызов existing `deploy.uv`, bounded filesystem/hash helpers,
JSON result serialization и перенос pure validation. Redesign требуют process
ownership, Git update, transaction recovery, Docker deployment, shortcut/COM,
native PostgreSQL hooks и CodeRabbit/WSL boundary. Эти области имеют риск
регрессии, который нельзя закрыть только unit test или совпадением exit code.

## 6. Delivery Package: будущая безопасная граница

Delivery — отдельный package/service, а не побочный режим `azur start` и не
скрытая Git-команда MCP. Канонический pipeline:

```text
validate
  → prepare
  → apply
  → verify
  → stage
  → commit
  → push
  → remote verify
```

### 6.1 Фазы

| Фаза | Обязательная проверка | Запрещённое упрощение |
| --- | --- | --- |
| `validate` | canonical repository root, trusted path, branch/ref, exact local HEAD, requested base/head, clean/in-flight state, user intent и allowlist paths | нельзя выводить root из текущего cwd без проверки и нельзя считать branch name доказательством HEAD |
| `prepare` | внешний transaction root, unique operation id, snapshot/hash, ownership lock, candidate from exact ref, journal before mutation | нельзя хранить backup только внутри изменяемого checkout или перезаписывать ambiguous journal |
| `apply` | только разрешённые paths/operations, path traversal check, symlink/junction/reparse rejection, atomic file replacement, bounded payload | нельзя `eval`, arbitrary shell, recursive delete по вычисленному пути или массовый copy без allowlist |
| `verify` | exact files/hash, Git status, expected local HEAD, generated outputs, environment/readiness и product postcondition | `returncode == 0` недостаточен; verification failure не превращать в success |
| `stage` | явный список staged paths, отсутствие secrets/binaries/logs/state, diff review | нельзя stage всего checkout или ignored/local state |
| `commit` | explicit commit intent, declared scope, current HEAD unchanged, commit message contract | нельзя коммитить автоматически в результате read-only check или после in-flight ambiguity |
| `push` | configured remote, exact branch, no force, no lease bypass, current commit equals verified local head | нельзя force push, silent remote change или blind retry после неизвестного результата |
| `remote verify` | `ls-remote`/provider result подтверждает exact pushed SHA на exact ref; при mismatch — stop | нельзя считать HTTP acknowledgement или accepted queue delivered/pushed |

### 6.2 Reusable primitives

Первый implementation должен выделить и протестировать:

- `ScopedPath`: canonical root, containment, path traversal и
  symlink/junction/reparse rejection;
- `ProcessIdentity`/`OwnershipEvidence`: PID, creation time, executable,
  argv, cwd и process tree;
- `BoundedCommand`: structured argv, environment allowlist, deadline,
  UTF-8 capture/redaction и return classification;
- `AtomicFile`/`TransactionJournal`: temp path, fsync/replace, phase schema,
  backup hash, rollback state и recovery classification;
- `GitSnapshot`: repository root, branch, exact HEAD, remote/ref and dirty state;
- `ArtifactVerifier`: size, SHA-256, expected source/host and extraction scope;
- `OperationStore`: immutable request, idempotency key, expiry, owner and
  postcondition;
- `RemoteRefVerifier`: exact branch/ref/SHA after push, без вывода credentials.

При crash, timeout, missing result, process exit race, journal corruption или
remote mismatch service возвращает `unknown`/`in_flight` и блокирует повтор. Новый
action допускается только после read-only recovery и нового immutable request.

## 7. MCP, skills и plugin impact

### 7.1 Existing MCP topology

```text
project Codex direct stdio
  azurpilot-dev  → uv run ... python -m module.dev_mcp
  azurpilot-game → uv run ... python -m module.game_mcp

Windows Desktop compatibility aliases
  azurpilot_dev  → authenticated loopback 127.0.0.1:8775
  azurpilot_game → authenticated loopback 127.0.0.1:8776
  оба процесса владеются module.mcp_shared.local_http_supervisor

ChatGPT/public
  отдельный authenticated HTTPS remote surface → module.*_mcp.remote
```

`azurpilot-dev` и `azurpilot-game` — разные protocol identities и разные
catalogs. Alias с underscore — registration key local HTTP, а не новая identity.

`module/dev_mcp/` содержит low-level MCP server, typed schemas, `DevMcpAdapter`,
`DevSessionManager` facade, Dev contract, Smoke/Evidence/runtime-control tools и
developer-only game/database capabilities. Запуск stdio не создаёт runtime и не
читает target до вызова инструмента. `module/game_mcp/` — отдельная stateless
read/control surface с profile, read/control scopes и lazy `GameMcpBackend`,
который использует `module.application` и не импортирует Dev MCP/Dev Runtime.
`module/mcp_shared/` владеет только общим authenticated Streamable HTTP/auth
transport и local supervisor.

MCP adapter должен вызывать common service напрямую. MCP не запускает
`azur` через subprocess, не scrape-ит human CLI output и не использует Connected
App/remote surface как fallback direct Codex route. Long-running service operation
возвращает persistent `operation_id`, а read tool читает authoritative state.

### 7.2 Skills, plugin и compatibility

| Артефакт | Роль | Правило при миграции |
| --- | --- | --- |
| `plugins/azurpilot/.codex-plugin/plugin.json` | Plugin Creator manifest, metadata и skills | не добавлять CLI/MCP implementation и новый registration source |
| `plugins/azurpilot/compatibility.json` | bounded MCP/API/Smoke compatibility | читать через model; не копировать версии и flags в CLI help или tests |
| `plugins/azurpilot/README.md`, `references/mcp-routing.md` | routing/trust/contract documentation | обновлять при изменении route, сохраняя direct/remote separation |
| `plugins/azurpilot/skills/azurpilot-development` | Development Runtime/Smoke workflow | `dev_get_contract` → capabilities → validate → start/poll/evidence; migration не должна обходить contract |
| `plugins/azurpilot/skills/azurpilot-game-control` | обычный Game read/control workflow | только `azurpilot-game`, profile и postcondition; Dev skill не является fallback |
| `plugins/azurpilot/skills/azurpilot-troubleshooting` | evidence-first диагностика catalog/auth/runtime/postcondition | timeout/unknown → STOP WRITES → read-only recovery → no blind retry |
| `.agents/skills/azurpilot-repository-development` | repository development, CI, PR lifecycle | новый tooling code проходит обычный workflow; не менять merge boundary |
| `.agents/skills/azurpilot-coderabbit-review` | canonical CodeRabbit review checkpoint | review выполняется в отдельном permanent WSL2 review checkout, не в implementation checkout |
| `.agents/plugins/marketplace.json` | source-controlled local marketplace | package path остаётся source-only; plugin install не регистрирует MCP |
| `.codex/config.toml` | repository-level project route | CLI/tooling может проверять source config, но не выдавать его за effective registration |
| `config/mcp-versions.toml` | canonical server SemVer identity | один reader и bounded range validation |

Диагностическая matrix plugin закрепляет отдельные слои: intent → skill routing
→ project config/trust → local process → MCP handshake/catalog → plugin snapshot
→ remote/auth only for explicitly chosen remote route → backend/postcondition.
Не смешивать эти слои при проектировании `azur doctor`.

## 8. Reuse `dev_tools/mcp_status.py`

### 8.1 Текущее устройство

`dev_tools/mcp_status.py` уже разделён на четыре логические части:

1. collector/probes: local stdio `initialize`/`tools/list`/contract call,
   direct remote metadata, Semgrep local MCP, Docker executable/profile/gateway
   и third-party read-only probes;
2. model: bounded report, status/reason code, source revision/working tree,
   expected/observed version, local/direct surface, Codex source config,
   effective registration, remote backend/public edge, plugin и Docker state;
3. renderer: human tables, notes, redacted protocol/version cells и separate
   Docker/secrets/metrics blocks;
4. CLI adapter: `--json`, `--strict`, `--emit-metrics`, bounded `--watch`,
   `--interval-seconds` и `--repository-root`.

Коллектор не выполняет mutating MCP tools, ограничивает time/size/tool count,
redacts sensitive data и использует machine-readable reason codes. Важное
разделение нельзя терять: `source_config` означает проверенный tracked
`.codex/config.toml`, а `effective_codex_registration` остаётся
`not_observable`, пока нет authoritative evidence текущей trusted Codex session.
CLI scrape и synthetic `ready` запрещены.

### 8.2 Target service

Для `azur mcp status` и общего `azur doctor` рекомендуется:

```text
McpStatusService.collect(request) -> McpStatusReport
McpStatusService.strict_exit(report) -> ExitDecision
HumanStatusRenderer.render(report, stream)
JsonStatusRenderer.render(report, stream)
```

`McpStatusReport` должен быть transport-neutral и не содержать raw payload,
tokens, full URLs with credentials, arbitrary logs или личные paths. Docker
Gateway, Semgrep, metrics и user-scoped Context7 остаются независимыми optional
surfaces; auxiliary metrics failure не должен маскировать canonical status.
`--strict` возвращает ненулевой код при drift/unavailable, `--watch` соблюдает
bounded interval и завершается по Ctrl+C с последним подтверждённым состоянием.

Новый service может использовать existing collector через import adapter. Нельзя
создавать параллельный `mcp_status`, копировать reason-code table в CLI или
вводить mutating `doctor`.

## 9. CodeRabbit boundary

### 9.1 Текущий контракт

CodeRabbit — внешний review integration, не часть runtime AzurPilot и не часть
MCP server. Repository skill уже требует:

- canonical WSL2 Arch review clone, постоянный между запусками;
- Linux-native `coderabbit` executable и user-level auth/config вне tracked source;
- обязательную проверку `auth status --agent` и `review --help`;
- committed-only review exact committed HEAD против literal exact base SHA;
- hosted remote, branch и checkout identity, совпадающие с PR;
- triage каждого finding как confirmed, partially confirmed, false positive или
  insufficient evidence;
- исправление только подтверждённых/частично подтверждённых findings в основном
  implementation checkout, затем повтор exact-head review;
- отсутствие retry loop при rate limit/cooldown.

Machine path, username, local config directory и executable path должны приходить
из user/machine configuration или environment. Их нельзя зашивать в репозиторный
Python, CLI help, plugin metadata, tests или durable context. В документации
фиксируется только роль: Windows implementation checkout, permanent WSL2 Arch
review clone, Linux CodeRabbit binary и user config.

### 9.2 Будущий CLI adapter

```text
azur coderabbit status   # наличие и безопасное чтение внешней конфигурации
azur coderabbit doctor   # auth/backend/clone/remote/HEAD preflight без review
azur coderabbit review  # committed-only review с explicit base/head evidence
```

Adapter обязан использовать structured argv через WSL integration, не PowerShell
alias и не shell string. `review` получает base/head из проверенного Git/PR
request, не угадывает их по default branch, не меняет implementation checkout и
не публикует полные secrets. Отсутствие PR само по себе не заменяет review
evidence: если review scope можно определить по exact commit range, это отдельная
локальная проверка; если внешняя auth/CLI недоступна, возвращается bounded
`CODERABBIT_UNAVAILABLE`.

Rate limit — operational limitation review integration, не product/security
failure. Остальные gates, secret scan и draft PR не пропускаются из-за него.

## 10. Cross-platform contract

| Surface | Windows | Linux | macOS | Общая гарантия |
| --- | --- | --- | --- | --- |
| Core Python | Python 3.14 project `.venv`, `Scripts/python.exe`, hidden child windows | Python 3.14 `.venv/bin/python`, POSIX signals/locks | Python 3.14, `spawn` semantics и Mach-port ограничения | одинаковые DTO, reason codes, deadline и no side effect on import |
| Process ownership | PID + creation time + executable + exact argv/cwd + parent chain; `taskkill` only after evidence | PID + start time + executable/argv/cwd + process group | PID + start time + executable/argv/cwd; no Windows APIs | PID/port/name alone не дают права на stop |
| Files/locks | reparse/junction, ACL, atomic replace, COM shortcut | symlink, mount/path containment, POSIX lock, atomic replace | symlink/path containment, POSIX lock, `spawn` cleanup | scoped path, bounded IO, journal and fail-closed ambiguity |
| Git/uv | PowerShell compatibility remains during parity; `uv` project policy | structured Git/uv commands; native Docker deployment | structured Git/uv; no WSL assumption | no force/reset/clean/destructive fallback; exact SHA verification |
| Docker/PostgreSQL | Docker Desktop/Compose and loopback ports are optional capabilities | native Docker deployment and image hooks | Docker Desktop optional | capability probe reports unavailable, never silently skips required mutation |
| ADB/device | Windows/MuMu path is current product acceptance | adapter may be unsupported/explicitly unavailable | adapter may be unsupported/explicitly unavailable | no game/device action from generic `doctor` or delivery |
| WSL/CodeRabbit | Windows calls Linux-native review adapter only through explicit WSL boundary | native review adapter if configured | unsupported unless separately configured | no machine path hardcode, no review in implementation checkout |

Будущая CI matrix сохраняет `Python`, `Windows`, `Security` как required contexts.
Python проверяет full suite, compile, generators, lock and Ruff; Windows — Parser,
PSScriptAnalyzer, lifecycle/update compatibility and Windows regressions; Security
— Gitleaks tracked/history scope, risky files, security/privacy/browser tests.
macOS добавляется только при заявленном product support, а не как побочный эффект
переноса CLI.

PowerShell gates нельзя удалить в момент появления Python command. Каждая команда
должна пройти dual-run/parity на Windows, затем отдельное решение может изменить
required gate. Native Docker/PostgreSQL hooks и Linux deployment проверяются своим
runtime, не Windows-only unit tests.

## 11. Verification и Definition of Done для будущих increments

### 11.1 Проверки по уровням

1. diff/static audit: scope, Russian operator prose, отсутствие secret/personal
  path и случайных commit SHA;
2. Python syntax/import/compile и `ruff` только для изменённого Python;
3. pure contract tests: DTO, result/error mapping, path/process/hash/journal;
4. integration tests: fake process, PID reuse, foreign port, lock conflict,
   timeout, crash recovery, exact Git ref, uv candidate и remote SHA;
5. Windows parser/PSScriptAnalyzer и существующие PowerShell harnesses, пока
   parity не подтверждена;
6. Linux Docker/PostgreSQL acceptance для deployment/native hooks, если их scope
   затронут;
7. MCP contract/catalog and `mcp_status` tests; no mutating MCP in diagnostics;
8. required `Python`/`Windows`/`Security` CI на exact PR head;
9. CodeRabbit canonical review на exact committed HEAD/base, когда это входит в
   текущий repository development workflow;
10. Gitleaks по tracked/staged content и history согласно текущему CI, без
    подмены ручным regex scan.

Doc-only change не требует device/game smoke и не должен создавать synthetic
runtime evidence. Physical device, MuMu, ADB, gameplay и visual acceptance
запускаются только при явно затронутом соответствующем scope.

### 11.2 Removal checklist

Перед удалением legacy owner или переключением caller должны одновременно быть:

- production implementation и compatibility adapter в одной ветке;
- parity matrix с успешными и failure/recovery сценариями;
- exact process/checkout/path/remote evidence;
- обновлённые README/docs/context/skills/plugin refs и shortcuts/installers;
- tests и CI для всех затронутых OS;
- secret/risky-file scan без новых исключений;
- rollback plan, in-flight journal policy и отсутствие blind retry;
- явная проверка, что native container hook не был ошибочно заменён host tool;
- отдельное решение о merge/removal lifecycle, не вытекающее из зелёного CI.

## 12. Зафиксированные решения и не реализовано

Зафиксировано:

- common service layer является будущим owner policy; CLI, MCP и tests — только
  adapters;
- `module/application` и existing runtime owners переиспользуются через ports,
  но не дублируются и не смешиваются с Git/WSL/CodeRabbit;
- `argparse` — default для первой CLI implementation при отсутствии новой
  dependency;
- `mcp_status` переиспользуется как collector/model, а render/exit остаются
  transport-aware;
- CodeRabbit и WSL — внешние configured integrations с exact committed review,
  не repository hardcode;
- Windows/PowerShell и native container contracts остаются до доказанной parity;
- ambiguous result всегда fail-closed и требует read-only recovery.

Не реализовано этим документом: пакет `azurpilot`, команда `azur`, изменения
`pyproject.toml`, миграция любого legacy script, удаление файлов, изменение
MCP/plugin/CI, изменение runtime behavior, новые dependencies, device/game
acceptance и запуск следующей migration work package.
