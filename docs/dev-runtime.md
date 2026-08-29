# Dev Runtime: Task Sandbox

Stage 1 запускает только штатный `gui.py --run ap` с фиксированными loopback
параметрами и exact ownership процесса. Stage 2 добавляет task-aware API поверх
этого lifecycle, не меняя transport MCP и обычный production scheduler.

## Dev MCP для Codex

Stage 3 добавляет отдельный dev-only adapter без собственного runtime:

```text
Codex
  → local stdio Dev MCP
  → DevSessionManager
  → фиксированный профиль ap
```

Dev MCP использует только локальный stdio transport. Production MCP в
`mcp_server_sse.py` остаётся отдельным и не импортируется adapter-ом; MCP SDK и
production transport не изменяются. Server bootstrap не создаёт
`DevSessionManager`, не читает `config/ap.json`, не запускает WebUI и не требует
PostgreSQL, эмулятор или ADB. Manager создаётся лениво при первом вызове tool.

Project-scoped регистрация находится в `.codex/config.toml` и использует
подготовленное окружение проекта:

```toml
[mcp_servers.azurpilot-dev]
command = "uv"
args = ["run", "--locked", "--no-sync", "python", "-m", "module.dev_mcp"]
cwd = "."
enabled = true
required = false
startup_timeout_sec = 5
tool_timeout_sec = 180
```

Публичные tools: `dev_preflight`, `dev_doctor`, `dev_list_tasks`,
`dev_plan_session`, `dev_start_session`, `dev_status`, `dev_stop_session`,
`dev_cleanup` и `dev_recover`. Read-only являются preflight, doctor, каталог,
plan и status. Start всегда task-aware и требует `root_tasks`; stop по умолчанию
очищает scheduler-state, а `preserve_task_state=true` является явным
диагностическим исключением и требует последующего `dev_cleanup`.

Каждый tool использует только фиксированный `ap`: MCP не принимает profile,
instance, path, config setter или shell command. Входные схемы строгие и
запрещают неизвестные свойства. Ответ проходит отдельную allowlist-сериализацию
полей `DevResult`; пути, команды, окружение, secrets и traceback не выдаются.

Публичный output contract сохраняет top-level `DevResult` (`ok`, `code`,
`message`, `state`, `session_id`, `details`) и известные machine-readable
вложенные структуры. В частности, `preflight.checks` сохраняет `name`, `ok`,
`code`, `message`; nested `DevResult` в `doctor` сохраняет свои поля; `status`
сохраняет `task_lifecycle` (`mode`, `phase`, `cleanup_required`,
`policy_expected`) и безопасный `task_policy` snapshot (`present`, `valid`,
`state`, `session_id`, `profile`, selectors и provenance dependencies).
Каждый контекст использует собственный allowlist: неизвестные поля удаляются
на любой вложенности, а значения проходят bounded depth/items/text и redaction.

Для stdio stdout зарезервирован JSON-RPC протоколом и не содержит operator logs,
banner или debug output. Диагностические сообщения идут только в stderr.

Stage 3 не добавляет screenshots, log/evidence collector, timeline,
`run_task_smoke`, автоматическую оценку игрового PASS/FAIL, arbitrary shell или
редактор конфигурации. Handshake и `tools/list` не зависят от наличия профиля,
эмулятора и PostgreSQL; проверка runtime lifecycle выполняется отдельными
контролируемыми вызовами Dev Runtime.

## Каталог и план

`DevSessionManager.list_tasks()` читает только `config/ap.json` и обнаруживает
секции, содержащие корректный `Scheduler` с `Command`, совпадающим с именем
top-level section. `manager.plan(root_tasks=[...], excluded_tasks=[...])`
возвращает machine-readable план. Unknown, unsafe и конфликтующие selectors
отклоняются без записи в конфигурацию.

## Policy и provenance

Task-aware запуск сначала сбрасывает scheduler runtime fields всех schedulable
tasks `ap`, затем включает только root tasks и атомарно создаёт
`config/state/dev-runtime-task-policy.json`. Worker получает session context
через наследуемое process environment, но policy считается активной только при
совпадении профиля, session marker, repository root и exact policy path.

Scheduler selection разрешает только roots и задачи, добавленные через
доказанный `task_call()` от уже разрешённой задачи. Для excluded dependency
хранится `reason=dependency_override`, `required_by`, `root`, timestamp и
monotonic sequence. Необъяснимая ручная активация не получает privilege.

## Cleanup

Обычный `stop()`, stale/orphan recovery, failed launch/readiness и следующий
safe start очищают scheduler-state всех текущих schedulable tasks профиля `ap`:
`Scheduler.Enable=False`, `Scheduler.NextRun` получает canonical legacy reset
значение. Остальные поля профиля не изменяются. Cleanup атомарен, проверяется
повторным чтением и идемпотентен.

Task-aware `DevSession` записывает durable lifecycle marker до первой мутации
`config/ap.json`. Маркер различает подготовку, активную сессию, явный preserve,
ожидающий cleanup и подтверждённое чистое состояние. Если отдельный policy-файл
потерян или повреждён, recovery не считает cleanup ненужным: каталог текущего
профиля перечитывается, scheduler-state сбрасывается и результат проверяется.
`status`, `preflight` и `doctor` fail closed для неподтверждённого policy state.

`dev_start_session` запускает выбранные tasks обычным scheduler-путём. Поэтому
task-aware acceptance может выполнить наследуемую из `ap` политику ожидания,
включая остановку локального эмулятора при длительном ожидании следующего task.
Для smoke следует выбирать низкорисковую существующую task и заранее убедиться,
что окружение контролируемое; `Restart` не является безопасным выбором для
пользовательского эмулятора.

`stop(preserve_task_state=True)` — явный диагностический opt-out. Он оставляет
policy в состоянии `preserved`, сообщает об этом в result и предупреждении, а
последующий `cleanup()` или новый safe start возвращает обычное чистое состояние.

## CLI

```text
uv run --locked python dev_tools/dev_runtime.py list
uv run --locked python dev_tools/dev_runtime.py plan --task <TaskCommand>
uv run --locked python dev_tools/dev_runtime.py task-smoke --task <TaskCommand>
uv run --locked python dev_tools/dev_runtime.py cleanup
```

`cleanup` сбрасывает task-aware scheduler-state после явно сохранённого
`preserve_task_state` и не останавливает живой процесс. Команды печатают UTF-8 JSON и не выводят полный `ap` config. MCP transport,
generic profile management и PowerShell launcher в Stage 2 не добавляются.
