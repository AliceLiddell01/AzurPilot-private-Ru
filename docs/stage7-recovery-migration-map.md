# Stage 7: карта восстановления из `personal/test`

## Зафиксированная база

- base branch: `personal/stable`
- base SHA: `ceff650194afcfd7c7f9f61e155c3af4918193b1`
- donor branch: `personal/test`
- donor SHA: `9f8b4aded625eaa1d85b1e2a96e3496b8270c56c`
- merge base: `89fae04e3ac62acb73b71eec9c14f49ee27e9bc1`
- рабочая ветка: `codex/stage7-log-russianization-recovery`

`personal/test` используется только как read-only донор. Прямой merge, rebase и wholesale cherry-pick не применяются.

## Решения по файлам WIP

| Решение | Файлы | Обоснование и проверка |
|---|---|---|
| `REIMPLEMENT_FROM_INTENT` | `.github/workflows/lint.yml`; `dev_tools/stage7_log_audit.py`; `dev_tools/verify_stage7.py`; `tests/test_stage7_log_inventory.py` | Текущий CI уже разбит на независимые jobs. Stage 7 должен проверять семантические инварианты и публиковать generated diagnostics, а не сравнивать tracked snapshots побайтно. Проверка: Stage 7 unit tests, semantic verifier, artifacts и Job Summary. |
| `PORT_WITH_ADAPTATION` | `dev_tools/russianization_audit.py`; `module/config/config.py`; `module/logger.py`; `module/webui/app.py`; `module/webui/app_dependencies.py`; `module/webui/app_developer_tools.py`; `module/webui/app_helpers.py`; `module/webui/app_lifecycle.py`; `module/webui/fastapi.py`; `module/webui/patch.py`; `module/webui/process_manager.py`; `module/webui/remote_access.py`; `module/webui/utils.py`; `assets/gui/css/advanced-material-alas.css`; `assets/gui/css/alas.css`; `tests/test_stage7_process_lifecycle_logs.py`; `tests/test_stage7_webui_traceback_rendering.py`; `tests/run_stage7_webui_traceback_visual.py`; `tests/fixtures/stage7_webui_traceback/fixture-dark.html`; `tests/fixtures/stage7_webui_traceback/fixture-light.html` | Сохраняется намерение WIP, но перенос выполняется минимальными hunks поверх stable. Для `module/config/config.py` сохраняется новый контракт Operation Siren Data Logger. Для WebUI сохраняются no-reload orphan recovery, ownership registry и shutdown из stable; traceback должен проходить escaping/redaction/layout/lifecycle tests. |
| `PORT_AS_IS` | `alas.py`; `mcp_server_sse.py`; `deploy/AidLux/requirements_generator.py`; `deploy/Windows/adb.py`; `deploy/Windows/alas.py`; `deploy/Windows/app.py`; `deploy/Windows/config.py`; `deploy/Windows/emulator.py`; `deploy/Windows/logger.py`; `deploy/Windows/patch.py`; `deploy/Windows/pip.py`; `deploy/Windows/utils.py`; `deploy/adb.py`; `deploy/alas.py`; `deploy/app.py`; `deploy/config.py`; `deploy/docker/Docker-run.sh`; `deploy/docker/deploy-image.sh`; `deploy/docker/requirements_generator.py`; `deploy/emulator.py`; `deploy/headless/requirements_generator.py`; `deploy/launcher/Alas.bat`; `deploy/patch.py`; `deploy/pip.py`; `deploy/set.py`; `deploy/utils.py`; `deploy/uv.py`; `module/config/config_updater.py`; `module/config/server.py`; `module/config/time_source.py`; `module/config/utils.py`; `module/config/watcher.py`; `scripts/Build-AzurPilot.ps1`; `scripts/Repair-AzurPilot.ps1`; `scripts/Start-AzurPilot.ps1`; `scripts/Update-AzurPilot.ps1`; `scripts/lib/AzurPilot.Shortcut.psm1`; `tests/test_stage7_deploy_logs.py`; `tests/test_stage7_powershell_logs.py` | Эти файлы не менялись в stable после точки ответвления. Переносится только donor diff; control flow, placeholders, severity, exit codes, команды, пути и raw stdout/stderr должны остаться неизменными. PowerShell дополнительно проходит Parser, PSScriptAnalyzer и Windows smoke. |
| `SUPERSEDED_BY_STABLE` | `dev_tools/russianization/results/stage5_9_test_matrix.md` | Старая отметка WIP не является источником истины и не переносится без нового фактического результата проверок. |
| `REJECT_GENERATED_OUTPUT` | `dev_tools/russianization/results/stage7_log_scope.json`; `dev_tools/russianization/results/stage7_metrics.json`; `dev_tools/russianization/results/stage7_report.md`; `tests/fixtures/stage7_webui_traceback/1366x768-dark.png`; `tests/fixtures/stage7_webui_traceback/1366x768-light.png`; `tests/fixtures/stage7_webui_traceback/fingerprint.json` | Scope, metrics, report, screenshots и browser diagnostics генерируются во время проверки и публикуются как artifacts/summary. Они не являются tracked byte-for-byte blockers. Pixel/fingerprint equality между runner-окружениями запрещена. |
| `REIMPLEMENT_FROM_INTENT` | `dev_tools/russianization/results/stage7_log_exceptions.json` | Donor-файл из 1665 строк не переносится вслепую. Допустимы только точечные policy entries с устойчивым идентификатором, конкретной категорией и evidence; broad directory allowlist запрещён. |

## Инварианты адаптации

1. First-party Stage 7 сообщения должны быть русскими, а raw external payload, команды, пути, exception types и machine identifiers — неизменными.
2. Placeholder signature, severity/call kind и порядок событий сравниваются с фактической stable-базой.
3. `stage7_unresolved`, unknown classification, semantic mismatches, mojibake, invalid Stage 8 transfer и traceback security/layout findings должны быть равны нулю.
4. Generated outputs записываются только в явно заданный output-каталог и не сравниваются с tracked copies.
5. WebUI traceback проходит HTML escaping и redaction до callback и существует в единственном экземпляре. Единственный владелец вертикальной прокрутки — document/page; modal и traceback container не имеют собственного vertical scroll range, а внутри traceback сохраняется только локальная horizontal scroll.
6. Stage 7 не меняет Operation Siren Data Logger state machine, retry/evidence contracts или canonical runtime name.
7. CI сохраняет текущие независимые jobs, Gitleaks, secret-pattern audit, generator idempotence и failure diagnostics.


## Repair `gui.py` после ложного PASS

Первоначальная приёмка Stage 7 была неполной: политика `stage7_gui_stable_policy.py` помечала активные журналы актуального WebUI supervisor как `superseded_by_stable`. Это сохраняло важную post-divergence реализацию `gui.py`, но ошибочно освобождало её first-party сообщения от русификации.

Repair выполнен поверх актуального `gui.py` из `personal/stable` @ `ceff650194afcfd7c7f9f61e155c3af4918193b1`:

- donor-версия `gui.py` не переносилась;
- изменены только first-party literals вызовов `logger.*`, поля `title`/`reason`/`impact`/`action` подтверждённых exception contexts и три динамические подписи, попадающие в журнал;
- `WebUI`, `Electron`, `SSL`, `IPv4`, `IPv6`, `PID`, `taskkill`, `psutil`, `uv`, команды, пути, имена процессов и raw exception/output сохранены как технический контекст;
- broad policy `superseded_by_stable` удалена;
- неприкосновенность supervisor доказана нормализованным AST и отдельными signatures logger calls; строки нормализуются только в разрешённых message/context positions;
- function set, control flow, retry/timeouts, socket binding, process operations, worker registry, PID reuse, orphan recovery и dependency-sync protocol не изменились.

GUI-specific метрики repair head:

- `stage7_gui_candidates`: **127**;
- `stage7_gui_translated`: **113**;
- `stage7_gui_reviewed_technical`: **2**;
- `stage7_gui_raw_external`: **25**;
- `stage7_gui_unresolved`: **0**;
- CJK/English first-party remaining: **0/0**;
- control-flow/placeholder/severity/sequence mismatches: **0/0/0/0**.

Machine-readable inventory, approved delta и contract report генерируются в CI artifacts. Они не добавляются в tracked repository.
