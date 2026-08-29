# Dev Runtime: Task Sandbox

Stage 1 запускает только штатный `gui.py --run ap` с фиксированными loopback
параметрами и exact ownership процесса. Stage 2 добавляет task-aware API поверх
этого lifecycle, не меняя transport MCP и обычный production scheduler.

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
