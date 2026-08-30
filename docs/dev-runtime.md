# Dev Runtime: Task Sandbox

Dev Runtime запускает только штатный `gui.py --run ap` с фиксированными локальными
параметрами и точным владением процессом. Task Sandbox добавляет API с учётом задач поверх
этого жизненного цикла, не меняя обычный рабочий планировщик. Для Codex и ChatGPT
предусмотрены разные transport boundaries поверх одного adapter.

## Dev MCP для Codex

Dev MCP добавляет отдельный адаптер только для разработки без собственного runtime:

```text
Codex
  → локальный stdio Dev MCP
  → DevSessionManager
  → фиксированный профиль ap
```

Dev MCP для Codex использует локальный транспорт stdio. Для ChatGPT существует
отдельный `module.dev_mcp.remote` с authenticated HTTPS Streamable HTTP на `/mcp`;
он не переиспользует `mcp_server_sse.py`. Production MCP остаётся отдельным и
не импортируется адаптером. Запуск обоих Dev MCP entrypoint-ов не создаёт
`DevSessionManager`, не читает `config/ap.json`, не запускает WebUI и не требует
PostgreSQL, эмулятор или ADB. Менеджер создаётся лениво при первом вызове инструмента.

Регистрация в пределах проекта находится в `.codex/config.toml` и использует
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

Базовые инструменты Dev Runtime (без Smoke Harness): `dev_preflight`, `dev_doctor`, `dev_get_contract`, `dev_list_tasks`,
`dev_plan_session`, `dev_start_session`, `dev_status`, `dev_stop_session`,
`dev_cleanup`, `dev_recover`, `dev_get_evidence`, `dev_get_timeline`,
`dev_get_logs` и `dev_get_screenshot`. Только для чтения работают `preflight`,
`doctor`, каталог, `plan`, `status` и `timeline`. `evidence`, `logs` и `screenshot`
могут дополнять состояние и сохранять локальные артефакты, но не изменяют жизненный цикл
и не являются разрушительными. `dev_start_session` всегда
работает в режиме с учётом задач и требует `root_tasks`; `stop` по умолчанию очищает
состояние планировщика, а
`preserve_task_state=true` является явным диагностическим исключением и требует
последующего `dev_cleanup`.

Инструменты Universal Smoke Harness описаны в отдельном разделе ниже.

Каждый инструмент использует только фиксированный `ap`: MCP не принимает
`profile`, `instance`, `path`, установщик конфигурации или команду оболочки. Входные схемы строгие и
запрещают неизвестные свойства. Ответ проходит отдельную сериализацию по разрешённому списку
полей `DevResult`; пути, команды, окружение, учётные данные и стек вызовов не выдаются.

Публичный контракт вывода сохраняет верхнеуровневый `DevResult` (`ok`, `code`,
`message`, `state`, `session_id`, `details`) и известные машиночитаемые
вложенные структуры. В частности, `preflight.checks` сохраняет `name`, `ok`,
`code`, `message`; вложенный `DevResult` в `doctor` сохраняет свои поля; `status`
сохраняет `task_lifecycle` (`mode`, `phase`, `cleanup_required`,
`policy_expected`) и безопасный снимок `task_policy` (`present`, `valid`,
`state`, `session_id`, `profile`, селекторы и происхождение зависимостей).
Каждый контекст использует собственный разрешённый список: неизвестные поля удаляются
на любой вложенности, а значения проходят ограничение глубины, числа элементов и длины текста,
после чего очищаются от чувствительных данных.

`dev_get_contract` — read-only граница совместимости для canonical-пакета
`AzurPilot`. Она возвращает только `contract_schema_version`, семейство продукта,
версии Dev MCP/Smoke schemas, фиксированный профиль `ap`, feature flags и
capability families. В контракте нет путей, секретов или сведений об окружении;
плагин сравнивает его с `plugins/azurpilot/compatibility.json` и при любом
несовпадении останавливается с `PLUGIN_RUNTIME_INCOMPATIBLE` до mutating calls.

Для stdio stdout зарезервирован JSON-RPC протоколом и не содержит журналов оператора,
баннеров или отладочного вывода. Диагностические сообщения идут только в stderr.

## Диагностика и подтверждающие данные

Сессия с учётом задач создаёт отдельный игнорируемый каталог
`config/state/dev-runtime-runs/<session-id>/`. В нём хранятся только ограниченные
`manifest.json`, атомарный `timeline.json`, метаданные границы общего журнала и
локальные PNG/метаданные явных запросов снимка экрана. Диагностика привязана к текущей
рабочей копии, точному `session_id` и фиксированному профилю `ap`; MCP не принимает
пути, `profile`, `instance` или произвольные имена файлов. Хранение ограничено
числом сессий, возрастом и общим размером и не удаляет активную сессию.

Манифест сохраняет временные метки жизненного цикла в UTC, корневые и исключённые
задачи, локальный снимок Git только для чтения (`HEAD`, `branch`/`detached`, изменённые отслеживаемые пути),
состояние с машинными причинами, сводку хронологии, доступность журнала, метаданные
снимка экрана, последнюю структурированную ошибку и результат очистки. Снимок Git использует только
фиксированные локальные команды без сети, удалённых репозиториев, данных пользователя и содержимого
неотслеживаемых файлов. Ошибка Git переводит диагностику в `degraded`, но не блокирует
обычный жизненный цикл.

Хронология записывается только на канонических границах выполнения: создание и
готовность `session`, подготовка `policy`, запуск процесса, начало/возврат `task`,
данные о зависимостях из Task Sandbox, предупреждение/ошибка выполнения, `stop` и очистка. Каждое
событие имеет возрастающий `sequence`, временную метку UTC и ограниченные поля. Текущее
задание сообщается только для активной сессии с подтверждённым владением; после `stop` оно равно
`none`, а последняя задача остаётся в хронологии.

`dev_get_logs` читает только диапазон общего
`config/state/dev-runtime-gui.log`, зафиксированный при старте сессии с учётом задач
`session`, а при подтверждённом завершении — также по конечную границу завершения.
Предыдущие сессии не выдаются; замена, усечение, отсутствие файла, некорректный UTF-8,
повреждённая физическая строка и повреждённый `cursor` превращаются в ограниченный
результат диагностики с причиной состояния. Страница журнала использует ограниченные
`limit`, `cursor`, `more` и `truncated`; длинная физическая строка читается ограниченным
префиксом целиком, без выдачи её продолжения отдельной строкой. Пути и учётные данные
проходят общий слой очистки только для dev-контура.

`dev_get_screenshot` — только явное наблюдение активной сессии с подтверждённым
владением. Рабочий процесс обслуживает запрос текущим кадром из уже существующего пути
`Device.screenshot()`; отдельные `Device`, ввод, навигация, обрезка и OCR не
создаются. PNG проверяется по размеру и декодированию, сохраняется локально с
`screenshot_id`, временной меткой UTC, MIME, размерами, размером в байтах и SHA-256. MCP
возвращает метаданные в структурированном содержимом и само изображение через официальный
`ImageContent`, без base64 в обычном JSON.

Обработчик MCP остаётся тонким: он валидирует строгую схему и вызывает единый
API `DevSessionManager`. Чтение артефактов, Git, журнала, владения и снимка экрана
делается внутри слоя выполнения и диагностики. В обычном рабочем процессе перехватчики —
лёгкая пустая операция; рабочий `mcp_server_sse.py`, транспорт и база данных не меняются.

Evidence API не добавляет `run_task_smoke`, автоматическую оценку игрового PASS/FAIL,
координацию повторов и ожидания, периодические снимки, произвольную оболочку,
универсальный читатель файлов, OCR или редактор конфигурации. `Handshake` и `tools/list` не зависят от
наличия профиля, эмулятора и PostgreSQL; проверка жизненного цикла runtime выполняется
отдельными контролируемыми вызовами Dev Runtime.

## Каталог и план

`DevSessionManager.list_tasks()` читает только `config/ap.json` и обнаруживает
секции, содержащие корректный `Scheduler` с `Command`, совпадающим с именем
верхнеуровневой секции. `manager.plan(root_tasks=[...], excluded_tasks=[...])`
возвращает машиночитаемый план. Неизвестные, небезопасные и конфликтующие селекторы
отклоняются без записи в конфигурацию.

## Политика и происхождение

Запуск с учётом задач сначала сбрасывает поля состояния планировщика всех доступных
планировщику задач `ap`, затем включает только корневые задачи и атомарно создаёт
`config/state/dev-runtime-task-policy.json`. Рабочий процесс получает контекст сессии
через наследуемое окружение процесса, но политика считается активной только при
совпадении профиля, маркера сессии, корня рабочей копии и точного пути политики.

Выбор планировщика разрешает только корневые задачи и задачи, добавленные через
доказанный `task_call()` от уже разрешённой задачи. Для зависимости, исключённой
из списка, хранятся `reason=dependency_override`, `required_by`, `root`, временная метка и
монотонный порядковый номер. Необъяснимая ручная активация не получает привилегий.

## Очистка

Обычный `stop()`, восстановление устаревшего/осиротевшего состояния, неудачный запуск/переход к готовности и следующий
безопасный запуск очищают состояние планировщика всех текущих доступных ему задач профиля `ap`:
`Scheduler.Enable=False`, `Scheduler.NextRun` получает каноническое значение сброса.
Остальные поля профиля не изменяются. Очистка атомарна, проверяется
повторным чтением и идемпотентна.

`DevSession` с учётом задач записывает постоянный маркер жизненного цикла до первой мутации
`config/ap.json`. Маркер различает подготовку, активную сессию, явное сохранение,
ожидающую очистку и подтверждённое чистое состояние. Если отдельный файл политики
потерян или повреждён, восстановление не считает очистку ненужной: каталог текущего
профиля перечитывается, состояние планировщика сбрасывается и результат проверяется.
`status`, `preflight` и `doctor` безопасно отказывают при неподтверждённом состоянии политики.

`dev_start_session` запускает выбранные задачи обычным путём планировщика. Поэтому
проверка с учётом задач может выполнить унаследованную из `ap` политику ожидания,
включая остановку локального эмулятора при длительном ожидании следующей задачи.
Для проверки следует выбирать низкорисковую существующую задачу и заранее убедиться,
что окружение контролируемое; `Restart` не является безопасным выбором для
пользовательского эмулятора.

`stop(preserve_task_state=True)` — явное диагностическое исключение. Оно оставляет
политику в состоянии `preserved`, сообщает об этом в результате и предупреждении, а
последующий `cleanup()` или новый безопасный запуск возвращает обычное чистое состояние.

## CLI

```text
uv run --locked python dev_tools/dev_runtime.py list
uv run --locked python dev_tools/dev_runtime.py plan --task <TaskCommand>
uv run --locked python dev_tools/dev_runtime.py task-smoke --task <TaskCommand>
uv run --locked python dev_tools/dev_runtime.py cleanup
```

`cleanup` сбрасывает состояние планировщика после явно сохранённого
`preserve_task_state` и не останавливает живой процесс. Команды печатают UTF-8 JSON и не выводят полную конфигурацию `ap`. Транспорт MCP,
общее управление профилями и PowerShell-запускатель в Task Sandbox не добавляются.

## Universal Smoke Harness

Smoke Harness предоставляет декларативные `SmokeSpec` и
`SmokeRun`. Спецификация описывает только наблюдаемые условия: фиксированную
область задач `ap`, ограниченные переопределения конфигурации и типизированные
утверждения. Это не DSL: запрещены shell, Python/eval, произвольные пути,
HTTP/SQL, ADB, ввод, `sleep`, повторные попытки и patch. Неизвестные поля
запрещены, значения и массивы ограничены; после нормализации сохраняются
`spec.json` и SHA-256 `spec_hash`. После создания API не позволяет менять spec,
timeout, область задач или override.

Перед созданием запуска проверяются доступность политики Task Sandbox, чистота
отслеживаемого дерева source и точный снимок Git (`HEAD`, branch/detached,
fingerprint). Во время выполнения тот же снимок Evidence API проверяется на переходах
состояния и heartbeat; drift переводит запуск в `INVALIDATED` и запрещает PASS.
Игнорируемое runtime state не считается изменением source.

`SmokeRun` хранится в игнорируемом каталоге
`config/state/dev-runtime-smoke/<smoke-id>/` в отдельных ограниченных JSON-файлах
`spec.json`, `state.json`, `result.json` и `control.json`. Записи защищены
межпроцессной блокировкой, атомарной записью, проверкой схемы и защитой от
symlink/junction. Состояния выполнения (`created`, `preparing`, `running`,
`evaluating`, `cleaning_up`, `awaiting_external_evaluation`, `finished`)
отделены от итогов `PASS`, `PRODUCT_FAILED`, `PRECONDITION_FAILED`,
`HARNESS_FAILED`, `EVIDENCE_INCOMPLETE`, `TIMEOUT`, `INVALIDATED` и
`CANCELLED`. Одновременно разрешён только один активный запуск.

Длительная часть запускается отдельным Python проекта через
`module.dev_runtime.smoke_supervisor`; команда, рабочий каталог и личность
исполняемого файла проверяются точно. Supervisor вызывает обычный
`DevSessionManager.start()` с Task Sandbox и читает runtime только через
публичные API Evidence API `evidence`, `timeline`, `logs`, `status` и снимка экрана.
Он не вызывает gameplay handlers, `Device`, production MCP или raw scheduler.
После ошибки сначала сохраняется первичная ошибка продукта, затем выполняются
stop, очистка Task Sandbox, сброс scheduler, восстановление только объявленных
overrides и проверки orphan/source.

Встроенный `SmokeCapabilityRegistry` предоставляет типизированные условия:
наличие/отсутствие события, запуск/отсутствие task, зависимость с provenance,
ошибка выполнения и ожидаемая безопасная ошибка, полнота evidence, состояние
runtime/port, значение и восстановление config, длительность и ограниченный
фрагмент журнала сессии. Каждый результат содержит `PASS`/`FAIL`/`PENDING`/
`UNAVAILABLE` и явные ссылки на Evidence API. Negative assertions не
проходят до закрытия окна наблюдения; необъявленная structured runtime error и
неполная evidence health блокируют PASS.

Переопределения config разрешены только для существующих обычных листовых
параметров, которым canonical `argument.yaml` явно присваивает
`smoke_override: true`; generator переносит этот capability в `args.json`.
GUI type сам по себе разрешением не является. До apply сохраняются только
объявленные исходные значения; после run выполняются read-back, restore и
semantic mutation guard. Scheduler, runtime state/policy/evidence, secrets,
credentials, executable/path и arbitrary config paths запрещены, включая
защитную проверку имён как второй слой. Harness не выполняет auto-repair и
auto-retry.

Для UI допускается одно замороженное утверждение `external_visual` за run.
Evidence API сохраняет точный PNG по `screenshot_id` и SHA-256 после объявленного
события или task trigger, затем run полностью очищает runtime и переходит в
`awaiting_external_evaluation`. `dev_get_smoke_evaluation` возвращает
замороженные rubric, hashes и metadata вместе с PNG через MCP `ImageContent`;
только один `dev_submit_smoke_evaluation` может добавить неизменяемый внешний
verdict с provenance.

Smoke Harness расширяет локальный stdio Dev MCP ровно следующими инструментами:
`dev_list_smoke_capabilities`, `dev_validate_smoke`, `dev_start_smoke`,
`dev_get_smoke`, `dev_cancel_smoke`, `dev_get_smoke_evaluation` и
`dev_submit_smoke_evaluation`. `dev_start_smoke` быстро возвращает `smoke_id`,
не удерживая MCP request; результат читается через polling `dev_get_smoke`.
Сервер остаётся без побочных действий при startup и сохраняет stdout только для
MCP protocol. Production `mcp_server_sse.py`, MCP SDK и обычный gameplay path
не изменяются. Remote entrypoint использует зафиксированный в проекте `mcp==1.23.0`
и его `StreamableHTTPSessionManager` в stateless-режиме без event store; каждый
HTTP request повторно проходит auth и не оставляет серверных session records.

## Public HTTPS для ChatGPT

Публичный путь заменяет недоступный для этой personal organization Secure MCP
Tunnel:

```text
ChatGPT app AzurPilot Development
  → HTTPS :443 /mcp
  → Caddy с автоматическим сертификатом
  → 127.0.0.1:8765 remote Dev MCP
  → тот же DevMcpAdapter и профиль ap
```

Backend намеренно принимает только `127.0.0.1` и не должен публиковаться через
firewall/router. Наружу разрешаются только TCP `443` и, при необходимости для
ACME/redirect Caddy, TCP `80`. Порты `8765`, `2019`, `5432`, ADB/emulator,
production WebUI и `mcp_server_sse.py` наружу не пробрасываются.

Внешний OAuth/OIDC provider является authorization server; AzurPilot не
реализует собственный auth server. Production запуск fail-closed и требует все
параметры:

```text
AZURPILOT_DEV_MCP_PUBLIC_URL=https://<public-host>/mcp
AZURPILOT_DEV_MCP_OAUTH_ISSUER=https://<oauth-issuer>
AZURPILOT_DEV_MCP_OAUTH_AUDIENCE=<resource-audience>
AZURPILOT_DEV_MCP_OAUTH_JWKS_URL=https://<oauth-issuer>/<jwks-path>
AZURPILOT_DEV_MCP_OAUTH_SUBJECT=<single-operator-subject>
AZURPILOT_DEV_MCP_OAUTH_SCOPE=azurpilot:dev
AZURPILOT_DEV_MCP_PORT=8765
```

Issuer должен публиковать OAuth/OIDC discovery, authorization-code flow с PKCE
S256 и выдавать короткоживущий подписанный access token с `iss`, `aud` или
`resource`, `exp`, при необходимости `nbf`, `sub` и scope `azurpilot:dev`.
Resource server проверяет RS256 signature через зафиксированный JWKS, issuer,
audience/resource, subject, expiry и scope на каждом `/mcp` request. Query-token,
wildcard Host/Origin и `ALLOW_NO_AUTH` не поддерживаются. Well-known protected
resource metadata публикуется без auth и указывает на внешний issuer; сам
`/mcp` всегда требует auth.

Для remote HTTP transport request deadline остаётся bounded даже для blocking
adapter call: при истечении timeout HTTP-клиент получает `504`, а abandoned
worker может завершить уже начатую mutating operation. Такой timeout считается
неопределённым результатом: клиент не повторяет mutating request автоматически,
а перед следующей мутацией сначала читает `dev_status` или соответствующий
smoke/evidence state.

Подготовь Caddy по шаблону
[`docs/dev-mcp/Caddyfile.example`](dev-mcp/Caddyfile.example), замени только
placeholder host на собственное DNS-имя, сохрани локальную копию как
`docs/dev-mcp/Caddyfile` и направь его A/AAAA record на машину. Эта локальная
копия явно исключена из Git правилом `.gitignore`; не добавляй её через `git
add -f`.
Запусти backend отдельно:

```text
uv run --locked --no-sync python -m module.dev_mcp.remote doctor
uv run --locked --no-sync python -m module.dev_mcp.remote
caddy validate --config docs/dev-mcp/Caddyfile
caddy run --config docs/dev-mcp/Caddyfile
```

Первые команды выполняются с OAuth-переменными в защищённом окружении; значения
не записываются в Git. Перед подключением проверь без секрета: `GET
https://<public-host>/.well-known/oauth-protected-resource/mcp` возвращает
metadata, а `POST https://<public-host>/mcp` без auth возвращает `401` с
`WWW-Authenticate` и ссылкой на metadata. В приложении `AzurPilot Development`
используй URL mode `https://<public-host>/mcp` и OAuth, затем обнови app после
изменения tool descriptors. Сначала выполняются только read-only
`dev_get_contract`, `dev_preflight` и `dev_list_smoke_capabilities`.

Безопасный rollback: остановить remote backend, остановить Caddy, удалить только
созданное для этого endpoint правило firewall/router, отозвать или ротировать
OAuth credentials и отключить app в ChatGPT. Системный network reset не нужен.

## Canonical Plugin AzurPilot

Canonical Plugin Creator package находится в `plugins/azurpilot/`; его
machine-readable ID — `azurpilot`, а отображаемое имя — `AzurPilot`. Пакет
содержит ровно один Development skill и не регистрирует `.app.json` или
`.mcp.json`: Codex использует этот stdio Dev MCP напрямую, а ChatGPT подключается
к тому же adapter через authenticated public HTTPS `/mcp`. OAuth/OIDC provider,
Caddy config и credentials хранятся вне Git; второй MCP implementation и
production `mcp_server_sse.py` не используются.

Основной workflow skill: `dev_get_contract` →
`dev_list_smoke_capabilities` → строгий `SmokeSpec` → `dev_validate_smoke` →
exact source snapshot → `dev_start_smoke` → polling `dev_get_smoke` → при
необходимости замороженная внешняя visual evaluation. PASS допустим только при
PASS-result, exact source, подтверждённой очистке и полной evidence. Результаты
`PRODUCT_FAILED`, `HARNESS_FAILED`, `EVIDENCE_INCOMPLETE`, `TIMEOUT`,
`INVALIDATED`, `CANCELLED` и `PRECONDITION_FAILED` не превращаются в auto-retry
или успех.

На Stage 6 capability `Game`, игровые tools/app/skill и production-интеграция
не добавляются. Ограничения ChatGPT Developer Mode или текущего плана на write
tools фиксируются как `CHATGPT_WRITE_UNAVAILABLE_PRODUCT_LIMITATION`, а не
обходятся новым transport или auth server.
