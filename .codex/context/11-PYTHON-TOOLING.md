# Единый Python tooling: текущее состояние и контракт миграции

## Назначение и границы

Этот документ фиксирует постоянную архитектурную границу объединения
инструментов запуска, обслуживания, диагностики и доставки AzurPilot в единый
Python tooling. Реализованные части описаны как текущий контракт, а не как
исторический отчёт о ручном запуске.

Документ основан на фактическом коде текущей ветки, тестах, CI, `.codex/context/`,
репозиторных skills и plugin package. При расхождении с кодом первичным остаётся
код и исполняемый контракт.

В текущем increment выполнены:

- package installation, `project.scripts.azur` и `python -m azurpilot`;
- Python services `doctor`/`start`/`stop`/`build`/`repair`/`update`;
- shared typed result/error DTO, root resolver, process/filesystem/coordination primitives;
- cross-platform core contract tests и отдельный macOS CI job.

В рамках этой фиксации по-прежнему не выполняются:

- изменение поведения `gui.py`, `alas.py`, WebUI, Dev MCP или Game MCP;
- удаление PowerShell, Bash, BAT, native PostgreSQL hooks или бинарных launcher/installer artifacts;
- миграция MCP/plugin и отдельный CodeRabbit adapter;
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

### 1.1 Реализованный Python package и CLI

`pyproject.toml` является installable package через явный setuptools build
backend, `tool.uv.package = true` и `project.scripts.azur =
"azurpilot.cli:main"`. Source package `azurpilot/` содержит `argparse + Rich`
presentation adapter и сервисы в `azurpilot.tooling`. `python -m azurpilot`
использует тот же `main()` и не имеет отдельного поведения.

Реализованные команды: `doctor`, `start`, `stop`, `build`, `repair` и `update`.
Команды `mcp` и `coderabbit` намеренно не добавлены: MCP/plugin transport и
CodeRabbit остаются отдельными поверхностями текущего проекта. Existing
`module.*` entrypoints сохраняются без изменения.

Root resolver использует только `--repository-root`, validated user/machine
configuration или installation identity. Explicit/configured invalid root
завершается без fallback на CWD. Machine result использует закрытые Pydantic
DTO с bounded strings/arrays, `extra="forbid"`, operation-specific details и
evidence. Human rendering отделён от JSON stdout.

`StructuredProcessRunner` запускает только `shell=False` argv, ограничивает
stdout/stderr, использует monotonic deadline и сохраняет PID/start time/exact
executable/argv/cwd. На Windows project `.venv` Python redirector разрешается
через bounded `pyvenv.cfg` к canonical base runtime с `__PYVENV_LAUNCHER__`,
чтобы ownership и child-tree stop проверяли реальный процесс, а не transient
launcher. `ScopedPath`, atomic writes, SHA-256, external
`StateLayout`, `FileLock` и typed transaction journal дают общую safety boundary
для Build/Repair/Update/Lifecycle.

`BuildService` сохраняет здоровую `.venv`, создаёт config только из template и
использует существующую границу `deploy.uv` для подготовки. На Windows он также
проверяет закреплённый ADB и пользовательский ярлык, если не указан
`--no-shortcut`; POSIX возвращает `unsupported` для возможности только Windows.
`RepairService` сначала диагностирует, затем перемещает среду во внешнюю
резервную копию и при ошибке возвращает подтверждённый откат либо `unknown`.
`UpdateService` выполняет fetch и только `merge --ff-only`, до изменения
подтверждает каноническую идентичность remote и внешнюю логическую резервную
копию PostgreSQL, отклоняет dirty/local-ahead/diverged state и оставляет журнал
ошибки зависимостей для восстановления только для чтения.

### 1.2 Полный inventory legacy entrypoints

Ниже перечислены все tracked `.ps1`, `.psm1`, `.sh` и BAT-wrapper, найденные в
текущем checkout. Кандидат на будущий Python adapter не равен кандидату на
удаление: сначала должен быть доказан новый owner, parity и отсутствие call site.
Project-owned PowerShell/Shell wrapper может оставаться только временным
compatibility artifact на период parity/cutover. После миграции callers и
подтверждения parity его retirement/removal обязателен; бессрочный wrapper или
неявный fallback не является допустимым target state. Исключение —
runtime-native contract внешней среды, например PostgreSQL image initialization
hook, когда shell является естественной частью runtime и перенос на Python не
даёт архитектурной пользы.

| Артефакт | Назначение и entrypoint | Caller и внешние зависимости | Mutations и safety contract | Failure/recovery, tests и future owner |
| --- | --- | --- | --- | --- |
| `scripts/Start-AzurPilot.ps1` | Владелец запуска подготовленного Windows checkout; запускает `gui.py`, ждёт WebUI readiness и при необходимости открывает браузер | Ярлык из `AzurPilot.Shortcut.psm1`, README и ручной `pwsh`; PowerShell 7.6, project Python, `gui.py`, `config/deploy.yaml`, Docker Compose | Repository-scoped mutex и stop event; preflight `infrastructure/observability/compose.yaml`, PostgreSQL, bootstrap и опциональный Caddy; не делает Git update и не синхронизирует `.venv`; exact process/port ownership | Bounded timeout, captured/redacted output, foreign-port fail-closed, owned process tree stop; стабильные exit categories в самом скрипте; `tests/platform/powershell/test_powershell_contracts.py`, lifecycle acceptance и README. Будущий owner: `tooling.lifecycle` + Windows platform adapter |
| `scripts/Stop-AzurPilot.ps1` | Штатно останавливает backend текущего checkout | README, оператор и Start; PowerShell, project Python, `gui.py`, `scripts/lib` lifecycle contract | Stop event владельцу Start; ждёт порт, mutex и exact process; fallback допускается только после PID/executable/command/cwd/creation evidence; PostgreSQL не трогает | Чужой listener не останавливается, generic `Stop-Process` не используется; bounded wait и exit categories; lifecycle tests и Windows CI. Будущий owner: тот же lifecycle service, отдельный stop adapter |
| `scripts/Update-AzurPilot.ps1` | Единственный владелец обычного пользовательского обновления | README, operator workflow и Repair precondition; Git, `uv`, `robocopy`, `tar`, Docker/PostgreSQL | Проверяет root, branch, remote, disabled upstream push, clean tree и отсутствие active operation; `fetch` + `merge --ff-only`; при изменении dependency files создаёт внешний candidate/backup/journal, синхронизирует `.venv`, делает PostgreSQL logical backup | Failpoints и recovery для `AfterBackup`, `AfterSync`, `AfterMerge`; ambiguous/corrupt journal блокирует автоматическое продолжение; `tools/acceptance/powershell/Test-Update-AzurPilot.ps1`, PowerShell contracts, CI. `tooling.delivery` теперь владеет отдельной Git publication boundary; Update migration остаётся отдельной задачей |
| `scripts/Repair-AzurPilot.ps1` | Диагностирует окружение и транзакционно восстанавливает существующую `.venv`; отдельно чинит shortcut | Operator/README; PowerShell, Python, `uv`, Docker/PostgreSQL, `robocopy`, external transaction root | Не меняет branch/remote/user data; не обходит незавершённый Update; move/backup `.venv`, rebuild через `deploy.uv`, restore auxiliary tools, hash/config validation, rollback; optional COM shortcut repair | Journal phases, multiple/missing backup fail-closed, rollback result различается; exit categories включают diagnostic/shortcut/elevation; PowerShell parser/PSScriptAnalyzer, tests и Windows CI. Будущий owner: `tooling.repair` поверх тех же delivery/process primitives |
| `scripts/Build-AzurPilot.ps1` | Подготавливает уже полученный checkout и локальный shortcut | README/installer workflow; PowerShell, pinned uv/ADB bootstrap archives, SHA-256, Python, `deploy.uv`, COM | Не клонирует и не обновляет Git; создаёт `config/deploy.yaml` из template, при необходимости строит `.venv`, проверяет imports/ADB/frozen lock, удаляет только созданное partial state при отказе | Bootstrap cache, hashes и test failpoints; сохранение существующего здорового окружения; PowerShell/Windows gates. Будущий owner: `tooling.bootstrap` + artifact/ADB/platform adapters |
| `scripts/lib/AzurPilot.Lifecycle.psm1` | Общий Windows ownership contract для Start/Stop | Импортируется обоими скриптами; CIM/NetTCP, process tree, named mutex/event | Сравнивает exact repository, project Python, `gui.py`, command line и parent chain; `taskkill.exe /PID /T /F` только после подтверждённого ownership и creation date | `Free`/`Foreign`/`AzurPilot`, safe fallback и no generic kill; lifecycle acceptance. Будущий owner: `platform.windows.process` и `platform.windows.coordination` |
| `scripts/lib/AzurPilot.Shortcut.psm1` | Создаёт и проверяет `.lnk` на Start-команду | Repair/Build и Windows COM `WScript.Shell` | Формирует `pwsh -File scripts\Start-AzurPilot.ps1 -FromShortcut`, проверяет target/cwd/icon, пишет temp и делает atomic replace с backup | Restore on failure, local/all-users modes и admin boundary; shortcut checks в Repair/Build. Будущий owner: `platform.windows.shortcut`; COM остаётся platform adapter |
| `scripts/Start-Codex-Local-Mcp.ps1` | Тонкий Windows wrapper для `module.mcp_shared.local_http_supervisor` | Ручной Desktop setup; project `.venv` Python, user environment tokens | Запускает supervisor hidden, пишет ignored state/logs, ждёт `127.0.0.1:8775/8776` и `LOCAL_MCP_READY`; не управляет игровым lifecycle | Bounded readiness и failure при отсутствии token/process; contract/runtime MCP tests. Будущий owner: `tooling.mcp.local_http`; `.ps1` допускается только временно для parity/cutover и затем подлежит обязательному retirement/removal |
| `tools/acceptance/powershell/Test-AzurPilotLifecycle.ps1` | Изолированный Windows smoke ownership/mutex/event/foreign-port/fallback | Запускается вручную и из Windows CI; временный fixture checkout | Проверяет только synthetic processes и cleanup fixture, не production game/device | Нет device side effects; результат заменяет не parser/PSScriptAnalyzer, а дополняет их. Будущий owner: Python process/coordination acceptance при сохранении Windows compatibility gate |
| `tools/acceptance/powershell/Test-Update-AzurPilot.ps1` | Изолированный harness для Update transaction/recovery | Ручной и CI Windows run; local bare producer/client, fake venv and failpoints | Проверяет no-op, fast-forward, dirty/local-ahead/diverged, dependency transaction, journal corruption/orphan/conflict, network failure | Cleanup fixture и explicit `KeepFixtures`; это source of parity, а не runtime command. Будущий owner: Python delivery integration suite |
| `deploy/docker/deploy-image.sh` | Полноценный Linux Docker deployment: checkout, image, container, WebUI readiness и URL | Linux Bash, `git`, `curl`, Docker, apt/yum/dnf/systemd при установке; отдельные Docker volumes/config | Может установить host tools, clone/update `personal/stable` через `merge --ff-only`, build image, remove old container, run bind-mounted checkout/venv volume; optional public/private URL output | `set -euo pipefail`, interactive prompts, bounded curl/log readiness; нет Python parity; `tests/contracts/repository/test_no_upstream_project_network_defaults.py` проверяет часть routing. Будущий owner: `tooling.deploy` + Linux/Docker adapters; shell wrapper — только временный parity/cutover слой с обязательным retirement/removal после миграции callers |
| `deploy/docker/Docker-run.sh` | Старый Linux wrapper с update/build/run path | Bash, `git`, Docker, host ADB; использует string `eval`, stash/pull origin master и XDG lock | Может менять checkout через stash/pull и убивать container; не соответствует current `personal/stable` lifecycle safety | Нет основания считать его эквивалентом `deploy-image.sh`; сначала provenance/call-site audit, затем migration или removal decision. Будущий owner: не механический port, а отдельный redesign |
| `infrastructure/observability/postgres/bootstrap/01-bootstrap.sh` | Image-native PostgreSQL roles/schema/default privileges bootstrap | Docker official PostgreSQL image, `/run/secrets`, `psql`, `.pgpass` mode 600 | Создаёт/изменяет DB roles, passwords, ownership и grants внутри container; secrets не печатает | Strict Bash, cleanup temporary `.pgpass`, `ON_ERROR_STOP`; покрывается Compose/PostgreSQL gates. Будущий owner: container hook или эквивалентный image-native contract, не общий host tooling |
| `infrastructure/observability/postgres/init/01-bootstrap.sh` | Image-native `pg_hba.conf` local auth normalization | PostgreSQL init container, `psql`, `awk`, `mv`, file mode | Пишет temporary HBA, atomic move и reload; действует только внутри DB image | Strict Bash и trap cleanup; перенос в Python допустим лишь вместе с эквивалентом init image contract и отдельной DB acceptance |
| `deploy/launcher/Alas.bat` | Старый Windows wrapper: добавляет `.venv`/embedded Git в PATH, вызывает `python -m deploy.installer`, затем `gui.py --electron` | BAT, project `.venv`, Python; `deploy.installer` в текущем checkout отсутствует | Передаёт управление отсутствующему legacy installer; не является текущим Start owner | `deploy/Readme.md` всё ещё ссылается на этот путь; бинарный launcher и installer artifacts требуют отдельного provenance audit. Допускается только как временный compatibility path до cutover; после миграции callers и parity — обязательное retirement/removal |
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

### 1.3 Python runtime, WebUI и deploy seams

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

### 1.4 Call sites, documentation, shortcuts и installer references

Найденные пользовательские и проектные references распределены между несколькими
границами:

- `README.md` документирует Start/Stop/Update/Repair/Build и ручной запуск
  `azur`; legacy `pwsh` команды остаются временным Windows parity/cutover path.
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
    delivery.py                # allowlist -> commit -> remote verify pipeline
    pull_request.py             # typed draft PR body/provider boundary
    mcp_status.py              # collector/model, без human rendering
    coderabbit.py              # reserved external review boundary, not adapter
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

#### 2.3.1 Deterministic repository-root resolution

Установленный CLI может быть вызван не из repository cwd. Для project-bound
операций root разрешается строго в таком порядке:

```text
explicit --repository-root
  → validated user/machine configuration
  → project-local installation identity / safe discovery
  → stable failure with a machine-readable reason code
```

Каждый кандидат сначала приводится к canonical absolute path и проверяется как
обычный non-bare Git worktree: каталог существует, `.git` и
`git rev-parse --show-toplevel` согласованы, root не выходит за допустимый
containment boundary, а набор AzurPilot markers (`pyproject.toml` с project
name, `uv.lock`, `module/` и `deploy/`) подтверждает именно этот repository.
Кандидаты оцениваются по уровням приоритета. Как только текущий уровень даёт
ровно один validated root, кандидаты lower-priority уровней не сравниваются с
ним и не могут его обесценить. Конфликт или несколько разных validated
кандидатов блокируют выбор только внутри одного уровня; это также относится к
повторенным или неоднозначным explicit значениям. Невалидный explicit root не
заменяется cwd или следующим fallback: операция завершается стабильным code
вроде `TOOLING_REPOSITORY_NOT_FOUND` после его schema/ownership validation.

`azur --help`, version и другие project-independent команды могут работать без
root. `azur doctor`, `start`, `stop`, `build`, `repair` и `update` сначала
получают validated root; вызов вне checkout корректен,
если root найден через указанную цепочку, и fail-closed, если нет. Текущий cwd
не считается доверенным только по факту нахождения процесса в этом каталоге.

Project-local installation identity означает проверяемую связь executable,
Python environment/package metadata и repository markers. Safe discovery может
подняться от такой доверенной installation boundary к root, но не должен
угадывать root по произвольному `sys.path`, имени процесса или случайному cwd.
В результат включаются provenance (`explicit`, `configured` или
`installation`) и bounded identity evidence; секреты и личные пути не
публикуются.

Package получает доступ к существующим `module/`, `deploy/` и другим seams через
один явно объявленный source/package boundary: setuptools discovery перечисляет
`azurpilot` и переходные namespace packages, а project-specific config/data
берётся из validated root. Динамическое копирование дерева, добавление
непроверенного cwd в import path и вторая реализация запрещены. `deploy.uv`,
`deploy.atomic`, `module.dev_runtime` и `module.mcp_shared` остаются canonical
seams: новый adapter импортирует их из того же source tree.

Текущий `tool.uv.package = true` и editable/wheel build проверены локально.
`uv.lock` отражает только смену source проекта на editable; новых runtime
dependencies не добавлено. `azur --help`, `python -m azurpilot --help` и
`--json doctor` имеют общий CLI contract, а legacy `python -m module.*` пути
сохраняются без миграции MCP/plugin. `doctor` также показывает наличие
project console script и его user-level регистрацию. Успешный `azur build`
добавляет project console script в user-level Windows `PATH`, а `doctor`
остаётся read-only и только проверяет регистрацию и доступность в текущем
shell. Уже открытый shell не изменяется дочерним процессом; после первой
регистрации требуется новый shell.

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

Все transport adapters преобразуют service result в один небольшой transport-
neutral envelope. Концептуальная форма допускает generic или отдельные DTO:

```text
ToolingResult[TDetails, TEvidence]
```

Например, `StartResult`, `DoctorResult`, `UpdateResult` и
`CodeRabbitReviewResult` могут иметь собственные `TDetails`/`TEvidence`, но не
собственный несогласованный envelope:

```jsonc
{
  "ok": true,
  "code": "TOOLING_OPERATION_READY",
  "state": "ready",
  "message": "Операция подтверждена",
  "operation_id": null,
  "details": {
    "schema": "StartDetails.v1",
    "repository_root_source": "configured",
    "readiness": "ready"
  },
  "warnings": [{ "code": "WARNING_CODE", "message": "Короткое пояснение" }],
  "evidence": {
    "schema": "LifecycleEvidence.v1",
    "ownership": "confirmed"
  }
}
```

`details` и `evidence` не являются `dict[str, Any]` и не превращаются в
произвольные словари. Каждый operation-specific payload обязан иметь
версионированную typed schema с явными типами, bounded количеством/размером
полей и запретом неизвестных свойств (`additionalProperties: false` или
эквивалентный closed-schema режим). Расширения допустимы только через явно
определённую версию/namespace; произвольные extra properties по умолчанию
отклоняются. `warnings` также является bounded списком typed warning DTO, а не
неограниченным логом.

Обязательные свойства контракта:

- `code` — стабильный machine-readable reason code в `SCREAMING_SNAKE_CASE`;
- `state` — конечное состояние (`ready`, `running`, `failed`, `unknown`,
  `rolled_back`, `conflict`, `unavailable` и ограниченный согласованный набор);
- `message` — русская operator-facing фраза без секретов;
- `details` — конкретный operation-specific DTO, bounded и redacted; не raw
  stdout/stderr, не полный config и не arbitrary map;
- `operation_id` — только для долгой операции, которую можно безопасно читать и
  завершать по immutable request;
- `evidence` — конкретный operation-specific evidence DTO с exact
  head/identity/hash только там, где публикация разрешена; raw logs и secrets
  запрещены.

Одна model/schema используется service, CLI `--json`, MCP tool schema и
contract tests. JSON schema должна быть нормальной machine-readable схемой с
bounded enums/strings/arrays и стабильной версией, а не документацией поверх
свободного JSON. Service result не зависит от Rich, ANSI, TTY или MCP SDK;
transport adapter отвечает только за сериализацию, rendering и exit mapping.

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
`>=3.14.6,<3.15`, явный setuptools build backend, `tool.uv.package = true` и
`project.scripts.azur = "azurpilot.cli:main"`. Distribution включает
`azurpilot` и явно перечисленные переходные packages; project config/data
разрешаются через validated repository root. Новых runtime dependencies не
добавлено.

Постоянные operational entrypoints:

```text
azur doctor
azur start
azur stop
azur build
azur repair
azur update

python -m azurpilot <same command>
```

`mcp`/`coderabbit` не являются частью этого increment: существующие MCP/plugin
и review surfaces остаются отдельными compatibility paths. Во время перехода
`python -m module.*` сохраняется для MCP, generator и CI. `azur` не вызывает
legacy `.ps1`/`.sh`/`.bat` wrappers; они остаются Windows parity/cutover слоем.

### 3.2 Выбор CLI framework

| Вариант | Плюсы | Ограничения для проекта | Решение |
| --- | --- | --- | --- |
| `argparse` | Стандартная библиотека, нет новой зависимости, зрелые nested subparsers, простой тест через `argv` | Completion и rich command registry придётся оформить самостоятельно | Предпочтительный baseline для первого implementation vertical slice |
| Click | Хорошие groups/options/completion и CLI testing helpers | Новая runtime dependency, decorator-heavy boundary, нужно отдельно зафиксировать JSON/error semantics | Рассмотреть на vertical slice при доказанном выигрыше |
| Typer | Type hints, удобные nested commands и completion | Дополнительная dependency поверх Click, version/typing coupling | Рассмотреть только по результатам vertical slice, не выбирать автоматически |

`argparse + Rich` остаётся текущей рекомендацией, а не необратимым решением.
Окончательный parser/framework можно подтвердить на первом implementation
vertical slice. Оценка должна покрывать nested commands, современный `--help`,
shell completion, discoverability, typing, testability, интеграцию с Rich,
JSON/error semantics, startup cost и долгосрочную поддержку. Следующий
implementation increment может доказанно выбрать Click, Typer или другой
вариант, если он улучшает постоянный CLI contract без новой деградации
machine/API boundaries и с обоснованной
dependency policy.

Независимо от framework обязательны: deterministic command registry, `--help`,
`--json`, `--no-color`/`NO_COLOR`, stdin/stdout/stderr separation, injectable
service dependencies, отсутствие side effect при импорте и строгая граница
между presentation layer и service layer. Ни один parser не получает право
владеть policy или подменять typed service result.

## 4. CLI UX contract

### 4.1 Единый стиль

Human CLI — полноценный пользовательский интерфейс, а не набор раскрашенных
`print()` вызовов. Rich остаётся естественным presentation layer для этого
интерфейса, но не определяет service policy или machine contract.

- Human output предназначен для terminal; machine output — для `--json`.
- При `--json` stdout содержит только один bounded JSON report на операцию;
  progress, heartbeat и debug идут в stderr. В watch-режиме каждый JSON report
  завершается переводом строки.
- Цвет включается только для TTY, отключается при `NO_COLOR`, `--no-color` и
  redirected output. Нельзя кодировать состояние только цветом.
- Presentation использует ясную визуальную hierarchy, compact panels/tables
  там, где они действительно помогают, consistent status markers и actionable
  next actions. Не нужны огромные banners, повторяющиеся заголовки и visual
  noise.
- Layout учитывает terminal width и должен оставаться читаемым в Windows
  Terminal и распространённых Linux/macOS terminals. Rich-компоненты не
  должны разъезжаться при узком окне.
- Progress/spinner разрешены только для реально длительной операции и только в
  TTY; в non-TTY/JSON они отключаются либо заменяются bounded status events в
  stderr. Rich не является обязательной service dependency и не источником
  machine contract.
- Ошибка всегда содержит code, краткую причину и конкретное действие. Секреты,
  bearer values, cookies, full environment, absolute user paths и raw external
  payload не печатаются.
- Ctrl+C переводит операцию в `cancel_requested`; результатом должен быть
  подтверждённый `cancelled`, `rolled_back` или `unknown`, а не молчаливый
  успех. После unknown повтор запрещён без read-only recovery.
- stdout/stderr кодируются UTF-8 с явной политикой replacement для внешнего
  вывода; исходный raw stdout/stderr можно сохранить только в защищённом
  bounded diagnostic artifact.

`--json` полностью независим от Rich, ANSI, TTY, terminal width и human layout:
его schema, поля, ordering policy и machine-readable codes не меняются из-за
способа отображения. Presentation snapshots допустимы только как отдельный
human-UX test и не являются частью API contract.

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
mockup не является API contract или committed evidence.

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
| `Start-Codex-Local-Mcp.ps1` | `tooling.mcp.local_http` | `.codex/config.toml`, supervisor, MCP runtime tests | Только временный parity/cutover adapter; после миграции callers и подтверждения parity — обязательное retirement/removal | two exact services, token source, `/ready`, owner identity, cleanup и отсутствие duplicate process |
| `dev_tools/mcp_status.py` | `tooling.mcp_status` service + CLI renderer | MCP tests, `docs/dev-runtime.md`, observability docs, plugin routing | Mostly mechanical extraction of collector/model; renderer remains transport-specific | same bounded probe order, reason codes, redaction, `source_config` vs `effective` distinction, strict/watch/metrics semantics |
| `deploy/docker/deploy-image.sh` | `tooling.deploy` + Linux/Docker adapters | deployment docs and repository contract tests | Redesign: interactive deployment and host package installation are not generic lifecycle; shell wrapper — только временный parity/cutover слой | Linux parity, safe checkout/update, Docker ownership, network/volume behavior, readiness and rollback/diagnostic path; после миграции callers wrapper retirement/removal обязателен |
| `deploy/docker/Docker-run.sh` | no automatic target; separate legacy decision | Possible external operator shortcuts; repository search | Redesign or retirement, not port | provenance, zero supported callers, replacement acceptance and explicit removal decision |
| PostgreSQL init/bootstrap `.sh` | container-native hook or equivalent image contract | Compose, PostgreSQL/Alembic/backup gates | Not a host tooling migration | equivalent roles, HBA, secrets, atomicity, image startup order and DB acceptance |
| `Alas.bat` + installer binaries/docs | временный compatibility path до cutover или explicit removal; постоянный project-owned wrapper запрещён | `deploy/Readme.md`, root binaries/icon, external shortcuts | Documentation/provenance decision | valid replacement, user-facing migration notice, installer ownership, no hidden caller, hygiene/security pass; после parity wrapper retirement/removal обязателен |
| `dev_tools/alas2.bat` | retired tombstone; no automatic migration target | внешние shortcuts и repository provenance | Не включать в общий parity gate; отдельное решение о removal | zero supported callers, replacement/provenance evidence и explicit removal decision |
| PowerShell acceptance harnesses | Python parity harness plus retained Windows gates | `.github/workflows/ci.yml`, PSScriptAnalyzer/parser | Tests migrate last; current tests stay during parity | deterministic fixture equivalence, failure/recovery matrix and Windows execution evidence |

Механические части: вызов existing `deploy.uv`, bounded filesystem/hash helpers,
JSON result serialization и перенос pure validation. Redesign требуют process
ownership, Git update, transaction recovery, Docker deployment, shortcut/COM,
native PostgreSQL hooks и CodeRabbit/WSL boundary. Эти области имеют риск
регрессии, который нельзя закрыть только unit test или совпадением exit code.

## 6. Git Delivery и PR Publication: текущая безопасная граница

`azurpilot.tooling.delivery` — отдельный package/service, а не побочный режим
`azur start` и не скрытая Git-команда MCP. `azurpilot.tooling.pull_request`
отвечает только за typed draft PR provider boundary. Канонический pipeline:

```text
delivery validate
  → explicit allowlist stage
  → staged Gitleaks
  → commit
  → exact committed-range Gitleaks
  → ordinary push
  → exact remote verify
  → journal status/recover при ambiguity

pr prepare
  → structured body render
  → external body-file
  → explicit gh --repo/--base/--head
  → read-back identity/body digest
```

### 6.1 Реализованные проверки

| Boundary | Обязательная проверка | Fail-closed поведение |
| --- | --- | --- |
| `delivery validate` | canonical root, hosted repository identity publication и base remotes, branch/ref, exact local HEAD, exact base/remote SHA, ancestry, active operation, preimage/postimage и staged allowlist | invalid manifest, unrelated staged path, traversal, symlink или mismatch блокируют operation |
| `delivery publish` | explicit target paths, manifest-bound raw/Git-clean staged postimage, scoped Gitleaks index, typed commit parent/diff, exact committed range, ordinary push и `ls-remote` SHA | scanner finding, commit mismatch, remote conflict или unknown push сохраняются в journal; blind retry запрещён |
| `delivery status/recover` | external typed journal и read-only remote ref check | in-flight/unknown не мутируются повторным push; требуется новый immutable request после recovery |
| `pr prepare` | exact local/remote base/head, hosted remote identity и все обязательные body sections | body/spec/provider boundary с неверным exact identity отклоняется |
| `pr publish/verify` | explicit `gh pr` repository, draft flag, candidate ambiguity check, temporary `--body-file`, provider read-back и body digest | cross-repository, wrong SHA, non-draft, duplicate или unknown provider result блокируют публикацию |

`pull_request.py` строит не короткое summary, а полный русскоязычный PR report.
В нём должны быть конкретные сведения о цели, scope и границах, подсистемах и
файлах, реализации, локальных/live-проверках, exact-head CI,
security/secret scan, CodeRabbit disposition, rollback/migration и ограничениях.
Основные секции требуют маркированные факты; английский текст разрешён только
для technical identifiers, имён инструментов/API, protocol tokens и CI contexts.
Минимальная содержательность проверяется до записи временного `body-file`.

Human `delivery validate` использует тот же typed result, что и JSON adapter, но
рендерит его через Rich как bounded `Delivery Package`: repository, branch,
сокращённые human SHA, base/remote ref, target count и изменения `A`/`M`/`D`.
Он явно сообщает `Изменения не применены.`. Agent `--json` не выводит Rich/ANSI и
сохраняет полный exact SHA, target set и typed evidence. После `pr edit` сервис
всегда читает PR обратно; timeout/unknown без подтверждённого body остаётся
`UNKNOWN/IN_FLIGHT` и не запускает повторную mutation.

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

Core Python tooling/CLI и external/product capabilities — разные контракты.
Целевой core contract: **Windows + Linux + macOS**. Это относится к
`azur --help`, `azur doctor`, configuration, filesystem primitives, structured
process layer, Git integration, JSON/machine output и CLI rendering. Core
семантика, DTO, reason codes, deadlines и отсутствие side effect при импорте
должны быть одинаковыми на всех трёх ОС, с отдельными native adapters там, где
различается системный механизм.

### 10.1 Core CLI/tooling

| Surface | Windows | Linux | macOS | Общая гарантия |
| --- | --- | --- | --- | --- |
| Core Python/CLI | Python 3.14, project `.venv`, `Scripts/python.exe`, hidden child windows | Python 3.14, `.venv/bin/python`, POSIX signals/locks | Python 3.14, `spawn` semantics и macOS process restrictions | одни request/result schemas, reason codes, deadlines и no side effect on import |
| Process ownership | PID + creation time + executable + exact argv/cwd + parent chain; `taskkill` only after evidence | PID + start time + executable/argv/cwd + process group | PID + start time + executable/argv/cwd + process group | PID/port/name alone не дают права на stop |
| Files/locks | reparse/junction, ACL, atomic replace | symlink, mount/path containment, POSIX lock, atomic replace | symlink/path containment, POSIX lock, atomic replace | scoped path, bounded IO, journal и fail-closed ambiguity |
| Git/uv integration | structured Git/uv; PowerShell остаётся только compatibility adapter на parity | structured Git/uv; native Docker — отдельная capability | structured Git/uv; no WSL assumption | no force/reset/clean/destructive fallback; exact SHA verification |
| Human/machine output | Windows Terminal и redirected UTF-8 streams | распространённые Linux terminals и redirected streams | распространённые macOS terminals и redirected streams | Rich presentation отделён от stable JSON/machine contract |

Core test strategy должна включать platform-neutral contract tests и
соответствующие Windows/Linux/macOS runners для core surface, а native adapter
tests — только для реально заявленных integrations. Текущие required contexts
репозитория (`Python`, `Windows`, `Security`) сохраняются; расширение CI на
macOS для core tooling является отдельной implementation/release work package,
но отсутствие такого runner сейчас не превращает core contract в
platform-specific.

### 10.2 External/product capabilities

Внешние и продуктовые возможности остаются capability-dependent. ADB,
конкретный emulator/device backend, Docker backend, WSL, Windows shortcut/COM,
PostgreSQL deployment hooks и CodeRabbit могут быть доступны только на части
ОС или при отдельной конфигурации. Таблица фиксирует границу, а не обещает
наличие конкретного backend:

| Capability | Windows | Linux | macOS | Если capability отсутствует |
| --- | --- | --- | --- | --- |
| Docker/PostgreSQL | Docker Desktop/Compose optional | native Docker/image hooks optional | Docker Desktop optional | typed `unsupported`/`unavailable` с постоянным reason code; required mutation не пропускается молча |
| ADB/device/emulator | текущий product acceptance для Windows/MuMu | только если отдельный adapter действительно настроен | только если отдельный adapter действительно настроен | generic `doctor` не выполняет game/device action; отсутствие backend не маскируется как success |
| WSL/shortcut integration | WSL и COM shortcut — Windows-specific adapters | WSL shortcut path не предполагается; native adapter возможен отдельно | unsupported, если отдельная integration не заявлена | capability result остаётся `unsupported`/`unavailable`, без попытки использовать чужую OS boundary |
| CodeRabbit/review backend | explicit WSL boundary или configured external backend | native review adapter, если configured | configured external backend, если поддержан | bounded `CODERABBIT_UNAVAILABLE` или другой owned code; core CLI продолжает честно работать |

Нельзя выводить поддержку emulator/device backend из того, что core CLI
запускается на этой ОС. Capability probe обязан различать `unsupported`,
`unavailable`, `not_configured` и подтверждённую готовность, если эти состояния
нужны конкретной operation schema.

PowerShell gates нельзя удалить в момент появления Python command. Каждая команда
должна пройти dual-run/parity на Windows, затем отдельное решение может изменить
required gate. Native Docker/PostgreSQL hooks и Linux deployment проверяются своим
runtime, не Windows-only unit tests.

## 11. Verification и Definition of Done

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

Для Git Delivery/PR capability дополнительно требуются disposable bare-remote
integration, negative allowlist/preimage/postimage checks, ordinary push с
exact remote SHA, journal status/recovery и provider body read-back. Финальный
live acceptance выполняется двумя способами: human `azur` output и один
закрытый JSON envelope для agent-oriented CLI.

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
- core CLI/tooling проектируется для Windows/Linux/macOS, а external/product
  capabilities остаются capability-dependent;
- repository root для project-bound CLI разрешается deterministic chain с
  explicit/configured/installation provenance и fail-closed failure;
- общий result envelope мал и стабилен, а `details`/`evidence` — только typed,
  versioned, bounded и closed-schema operation payload;
- project-owned PowerShell/Shell wrappers — временный parity/cutover слой с
  обязательным retirement/removal после миграции callers; runtime-native hooks
  внешней среды остаются отдельным исключением;
- `argparse + Rich` — предпочтительная рекомендация для первого vertical slice,
  но parser/framework не закреплён необратимо;
- human CLI является полноценным terminal UI, а presentation layer остаётся
  отдельно от service и machine/JSON contract;
- `mcp_status` переиспользуется как collector/model, а render/exit остаются
  transport-aware;
- CodeRabbit и WSL — внешние configured integrations с exact committed review,
  не repository hardcode;
- Windows/PowerShell и native container contracts остаются до доказанной parity;
- ambiguous result всегда fail-closed и требует read-only recovery.

### 12.1 Разрешённые follow-up неоднозначности

В этом follow-up разрешены шесть пунктов (несмотря на ошибочное упоминание
«пяти» в исходном prompt):

1. **Cross-platform:** core CLI/tooling имеет целевой baseline Windows + Linux +
   macOS; ADB, emulator/device, Docker, WSL, shortcut/COM и другие
   external/product capabilities не обещаются без фактического adapter и
   возвращают typed `unsupported`/`unavailable` при отсутствии.
2. **Repository discovery:** `--repository-root` имеет высший приоритет, затем
   validated user/machine configuration, затем проверенная installation identity
   и safe discovery; первый уровень с ровно одним validated root побеждает,
   конфликты проверяются только внутри одного уровня, а недоказанный explicit
   root завершается стабильным reason code без молчаливого fallback на cwd.
3. **Typed results:** `details` и `evidence` не являются свободными
   `dict[str, Any]`; используются generic/operation-specific DTO, versioned
   closed schemas, bounded fields, stable codes и общая model для service, CLI
   JSON, MCP schema и tests.
4. **Legacy wrappers:** project-owned `.ps1`/`.psm1`/`.sh` compatibility
   wrappers живут только до parity/cutover, миграции callers и проверки
   postcondition; после этого retirement/removal обязателен. PostgreSQL
   image-native initialization hooks не являются таким host wrapper.
5. **Framework:** `argparse + Rich` остаётся baseline recommendation без новой
   dependency; окончательный parser можно выбрать на первом vertical slice по
   nested commands, help, completion, discoverability, typing, testability,
   Rich, JSON/error semantics, startup cost и долгосрочной поддержке.
6. **UX:** human CLI проектируется как современный terminal UI с hierarchy,
   compact layout, width awareness, status/action guidance и TTY-only progress;
   Rich/ANSI/layout никогда не меняют независимый `--json` contract.

### 12.2 Намеренно отложено до следующих increments

- caller migration, dual-run parity и обязательное удаление временных
  project-owned wrappers;
- MCP/plugin transport migration и отдельный CodeRabbit/WSL adapter;
- database backup/upgrade adapter, Docker-native deployment и device/game
  acceptance;
- Windows shortcut/COM adapter и другие platform-specific capabilities;
- расширение operation schemas, JSON Schema publication и long-running
  asynchronous transport, если их потребует следующий consumer.

Не входит в этот increment: удаление legacy файлов, изменение `gui.py`,
`alas.py`, MCP/plugin behavior, device/game acceptance, PostgreSQL mutations и
merge опубликованной ветки.
