# Notification Platform

## Цель и границы

Этот документ является долговечным архитектурным контрактом платформы уведомлений AzurPilot. Он фиксирует фактическое состояние `personal/stable` на момент Stage 1 и целевую модель, к которой последующие этапы должны прийти без двух параллельных notification stacks.

Аудит Stage 1 выполнен относительно `personal/stable` `c92c7b7ece6994a5987ce37442734cd7dcd590c4`.

Целевой поток:

```text
Notification Event
        ↓
Notification Policy
        ↓
Durable Delivery State
        ↓
Notification Channel
        ↓
Delivery
```

Stage 1 не меняет production behavior. Здесь нет новых таблиц, dispatcher, channel adapters, новых WebUI endpoints или миграции producers.

Обозначения в документе:

- **Факт** — подтверждено текущим кодом или read-only runtime-интерфейсом.
- **Решение** — целевой архитектурный контракт.
- **Reference** — внешний источник design context, не описание текущего AzurPilot.
- **Decision gate** — конкретный выбор, который нельзя безопасно сделать без данных, отсутствующих на Stage 1.

## Текущее фактическое состояние

### Общая картина

**Факт.** В текущем коде нет единого notification domain/application boundary. Существуют как минимум четыре пересекающихся механизма:

1. синхронный внешний push через `module.notify.handle_notify()` и OnePush;
2. локальный `notify_webui()`, который синхронно делает HTTP POST на `127.0.0.1`;
3. producer-specific `CoinTaskMixin.notify_push()` в Operation Siren, самостоятельно выбирающий WebUI и OnePush;
4. in-memory `asyncio.Queue` + SSE `/api/notify_stream` для launcher-facing доставки.

**Факт.** У этих путей нет общего event identity, единой taxonomy, durable history, общей retry state, общей deduplication state или единого policy engine.

**Факт.** `module/notify/__init__.py` является только lazy-import wrapper для `handle_notify` и `notify_webui`; отдельной абстракции он не добавляет.

### `handle_notify()`

**Факт.** `module/notify/notify.py::handle_notify()`:

- принимает YAML-строку с конфигурацией провайдера;
- объединяет YAML documents;
- выбирает provider через `onepush.core.get_notifier()`;
- передаёт provider-specific параметры прямо в OnePush;
- содержит специальные ветки для `Custom` и `gocqhttp`;
- выполняет `notifier.notify()` синхронно в producer thread;
- для `requests.Response` считает успешным только HTTP 200;
- дополнительно проверяет `status == "failed"` у `gocqhttp`;
- ловит `OnePushException` и произвольные исключения и возвращает `False`;
- не имеет собственной retry/backoff/dedup/persistence модели;
- не задаёт собственный единый timeout contract для provider вызова.

Следствие: ошибка доставки обычно не выбрасывается в producer, но синхронная отправка всё равно занимает его execution path до возврата underlying provider.

Отдельный технический риск текущей реализации: специальная обработка `Custom` имеет ветку, где обращение к `config["data"]` возможно при отсутствующем ключе. Исключение перехватывается общим обработчиком, поэтому это не Stage 1 fix, а finding для удаления вместе с legacy wrapper либо отдельного bugfix, если этот provider потребуется до cutover.

### `notify_webui()`

**Факт.** `module/notify/notify.py::notify_webui()`:

- получает `WebuiPort`, с fallback на `25548`;
- всегда обращается к `http://127.0.0.1:<port>/api/notify`;
- передаёт `instance`, `title`, `content` и дополнительные поля JSON;
- задаёт timeout 2 секунды;
- возвращает `False` только при exception;
- **не проверяет HTTP status code и response body**.

Следствие: HTTP 4xx/5xx без transport exception сейчас считается успешным `notify_webui()`.

### WebUI API и SSE

**Факт.** `module/webui/api.py` содержит одну process-local очередь:

```text
_notification_queue = asyncio.Queue()
```

`POST /api/notify` читает JSON и делает `await _notification_queue.put(data)`, после чего отвечает `{"success": true}`.

`GET /api/notify_stream` создаёт SSE generator. Он:

- ожидает `_notification_queue.get()`;
- выдаёт только `data: <json>`;
- раз в 30 секунд при отсутствии сообщения выдаёт keepalive comment;
- не выдаёт SSE `id`;
- не реализует durable cursor/resume;
- не имеет history storage.

**Факт.** `asyncio.Queue.get()` удаляет item из общей очереди. Поэтому несколько одновременно подключённых consumers **конкурируют за сообщения**: текущее поведение является single-consumer/load-sharing, а не broadcast/fanout. Один payload получает один consumer.

**Факт.** ACK `/api/notify` означает только помещение payload в process-local queue. Он не доказывает, что launcher/SSE consumer существовал или получил уведомление.

**Факт.** Перезапуск WebUI уничтожает очередь и все невыданные сообщения.

### Authentication и network boundary текущих endpoints

**Факт.** `module/webui/fastapi.py` добавляет API routes в тот же Starlette app, что и PyWebIO routes. Middleware в этом слое включает gzip/header behavior, но не общий authentication middleware для raw API routes.

**Факт.** WebUI password flow реализован внутри PyWebIO session login. Notification routes `/api/notify` и `/api/notify_stream` не имеют собственного `is_local_request` gate и не проходят отдельную route-level authentication проверку.

**Факт.** Нельзя утверждать, что эти endpoints всегда глобально доступны: фактическая reachability зависит от bind address и внешней сетевой топологии. Generic deploy templates допускают wildcard host, тогда как персонализированный Windows build нормализует WebUI host к loopback. Поэтому security contract должен исправляться на уровне API/auth boundary, а не исходить из предположения о конкретном bind.

## Producer inventory

В таблице `External` означает OnePush через `handle_notify`, `Local` — `notify_webui` → WebUI queue/SSE.

| Current producer / callsite | Предметное событие и условие | Current scope / transport / config | Current reliability | Покрытие | Canonical event type |
| --- | --- | --- | --- | --- | --- |
| `alas.py::_check_sensitive_exit()` | Чувствительная задача завершилась с ошибкой; AzurPilot останавливается | profile=`config_name`; External + Local; `Error.OnePushConfig` | синхронно; без retry/persistence; Local max 2 s | `tests/test_scheduler_core_runtime_messages.py`, `tests/test_alas_error_handling.py` | `task.failed` + `sensitive=true` |
| `alas.py::run()` — `GameNotRunningError` | Игра не запущена, планируется `Restart` | External + Local; `Error.OnePushConfig` | best-effort, блокирует producer на время calls | error-handling tests | `runtime.game.unavailable` |
| `alas.py::run()` — предел repeated game recovery | Достигнут предел повторных восстановлений Azur Lane | External + Local | best-effort, без durable attempt state уведомления | recovery tests | `runtime.recovery.failed` |
| `alas.py::run()` — `GameStuckError`/`GameTooManyClickError`, старт recovery | Игра зависла/клик-цикл; запускается проверяемый restart | External; при успешном game-only recovery Local success; при дальнейших исходах External + Local | notification send не является частью recovery transaction | recovery tests | `runtime.game.stuck` и `runtime.recovery.succeeded/failed` |
| `alas.py::run()` — успешная Stage 2 emulator recovery | MuMu восстановлен штатно или hard-kill path, финальная UI-проверка прошла | External + Local | best-effort | emulator recovery tests | `runtime.emulator.recovered` |
| `alas.py::run()` — game recovery полностью неуспешна | Game restart и разрешённая emulator escalation не дали healthy game | External + Local | best-effort | emulator recovery tests | `runtime.recovery.failed` |
| `alas.py::run()` — обрабатываемая ошибка игрового клиента | Ошибка клиента; планируется автоматический Restart | External + Local | best-effort | scheduler/error tests | `runtime.game.error` |
| `alas.py::run()` — `GamePageUnknownError` | Состояние страницы не определено, аварийное завершение | External + Local | best-effort | structural/runtime tests | `runtime.game.page_unknown` |
| `alas.py::run()` — `ScriptError` | Ошибка сценария, текущую задачу продолжить нельзя | External + Local | best-effort | structural/runtime tests | `task.failed` с cause=`script_error` |
| `alas.py::run()` — `EmulatorNotRunningError`, recovery success | Эмулятор был недоступен и восстановлен, scheduler продолжает recovery path | External + Local | best-effort | emulator recovery tests | `runtime.emulator.recovered` |
| `alas.py::run()` — `EmulatorNotRunningError`, recovery failure | Эмулятор недоступен и восстановить его нельзя | External + Local | best-effort | emulator recovery tests | `runtime.emulator.unavailable` |
| `alas.py::run()` — `RequestHumanTakeover` | Автоматизация не может безопасно продолжить | External + Local | best-effort | scheduler/runtime tests | `task.failed` + cause=`human_takeover_required` |
| `alas.py::run()` — unhandled exception | Необработанная ошибка задачи/runtime | External + Local | best-effort | scheduler/runtime tests | `task.failed` + cause=`unhandled_exception` |
| `alas.py::loop()` — per-task result | После `run`: success / recoverable / failure, только если `Scheduler.PushNotification=true` | External only; `Error.OnePushConfig` используется как transport config | call обёрнут в `try/except`; failure notification пропускается; без Local | scheduler continuation/core tests | `task.completed`, `task.recovered`, `task.failed` |
| `alas.py::loop()` — repeated task failure limit | Одна задача достигла предела последовательных ошибок, scheduler останавливается | External + Local | best-effort | scheduler core tests | `task.failure_limit.reached` |
| `module/campaign/run.py::CampaignRun.triggered_stop_condition()` | Run-count limit достигнут | External; `Error.OnePushConfig` | синхронный best-effort wrapper; нет retry | прямого notification-specific test не найдено | `campaign.stop_condition.reached`, kind=`run_count` |
| тот же метод | Reach-level limit достигнут | External | то же | то же | `campaign.stop_condition.reached`, kind=`level` |
| тот же метод | Получен новый корабль при включённом stop condition | External | то же | то же | `campaign.stop_condition.reached`, kind=`new_ship` |
| `module/campaign/campaign_event.py::coin_limit_triggered()` | Coin limit достигнут; campaign откладывается | External | то же | прямого notification-specific test не найдено | `campaign.stop_condition.reached`, kind=`coin` |
| `module/handler/fast_forward.py` — GemsFarming auto-search setup failure | Не удалось применить ожидаемые настройки auto-search/fleet order | External; `Error.OnePushConfig`; failure `handle_notify` дополнительно логируется | synchronous best-effort | structural translation gate | `campaign.auto_search.configuration_failed` |
| `module/commission/commission.py::commission_receive()` | Получена награда комиссии и `CommissionNotifyReward=true`; title повышается при крупной gem-награде | External + Local; `Error.OnePushConfig` | best-effort; без retry/history | отдельного delivery test не найдено | `commission.reward.received` |
| `module/os/tasks/scheduling.py::CoinTaskMixin.check_and_notify_action_point_threshold()` | Изменилась категория/порог action points | через `notify_push`: Local и/или External | producer-local cooldown state, no durable generic dedup | direct wrapper behavior не покрыт единым channel contract test | `opsi.action_point.changed` |
| smart scheduling overflow branch | Для предотвращения overflow выполнен Meowfficer farming | `notify_push` | то же | нет общей notification contract проверки | `opsi.scheduler.coin_task.executed` |
| smart scheduling insufficient resources branch | Одновременно недостаточно yellow coins и AP | `notify_push` + producer-local cooldown | то же | то же | `opsi.resources.insufficient` |
| smart scheduling low AP branch | AP ниже minimum reserve | `notify_push` + producer-local cooldown | то же | то же | `opsi.action_point.low` |
| smart scheduling no coin task branch | Не включена ни одна задача пополнения yellow coins | `notify_push` | то же | то же | `opsi.scheduler.configuration.invalid` |
| smart scheduling delegated coin-task branch | Scheduler прокси-выполнил задачу пополнения yellow coins | `notify_push` + producer-local attempt state | то же | то же | `opsi.scheduler.coin_task.executed` |
| `module/os/tasks/hazard_leveling.py::_cl1_ap_check()` | AP ниже резерва; первая такая проверка уведомляет, следующие подавляются до recovery | `notify_push`; `OpsiHazard1_PreviousApInsufficient` | producer-specific dedup flag | нет общего notification contract test | `opsi.action_point.low` |
| `module/os/tasks/hazard_leveling.py::os_check_leveling()` | Сбор ship-exp data неуспешен | `notify_push` | best-effort | нет общего notification contract test | `opsi.ship_exp.check_failed` |
| `module/os/tasks/fleet_auto_change.py::_notify_auto_change_complete()` | Автоподбор флота завершён | `notify_push` | исключение notification call подавляется producer | нет общего notification contract test | `opsi.fleet.auto_change.completed` |
| `module/os/tasks/fleet_auto_change.py::_handle_auto_change_error()` | Автоподбор не выполнен, функция отключена, назначается Restart | `notify_push` | исключение notification call подавляется producer | нет общего notification contract test | `opsi.fleet.auto_change.failed` |
| `module/webui/app_developer_tools.py::_test_notify_error()` | Ручной developer test Error.OnePushConfig | External only | напрямую тестирует legacy wrapper | UI developer utility | `notification.test.requested` в будущей dev-only service |
| `dev_tools/cyclic_notify.py` | Бесконечный тестовый loop каждые 0.5 s с локально редактируемым OnePush YAML | External only | без stop/retry policy; ad-hoc utility | нет | удалить; заменить bounded dev test harness |

### Producer-level dedup/suppression, существующие отдельно от notification layer

**Факт.** Operation Siren уже содержит локальные механизмы подавления повторов: cooldown timestamps, `OpsiHazard1_PreviousApInsufficient`, last-notified/last-attempt runtime attributes и минимальный интервал для части smart-scheduling уведомлений. Эти механизмы не являются общей delivery state и не переживают все типы restart одинаково.

**Решение.** Семантика «это новое предметное событие или тот же incident» остаётся у producer/event schema. Семантика delivery cooldown, suppression и duplicate delivery переходит в `NotificationPolicy` + durable delivery state. Producer-specific flags удаляются после подтверждения эквивалентного поведения.

## Config inventory

Source of truth для перечисленных пользовательских полей — `module/config/argument/argument.yaml`; `module/config/config_generated.py`, `config/template.json` и i18n являются generated/representation слоями.

| Current key | Default / type | Sensitive | Current readers / semantics | Target | Removal condition |
| --- | --- | --- | --- | --- | --- |
| `Scheduler.PushNotification` | `false`, checkbox/bool | нет | `alas.py::loop()`; generic task-result OnePush. Для EventShop тот же ключ переопределён и трактуется только как разрешение error push | policy rule для `task.completed/recovered/failed` с task/profile subject | все scheduler producers переведены на events; EventShop больше не переиспользует ключ |
| `Error.OnePushConfig` | `provider: null`, YAML textarea/string | **да**: может содержать token/key/url/password | почти все legacy external producers, Opsi fallback, developer test | channel-owned secret/config reference; routing отдельно | legacy OnePush paths удалены и first-class adapters покрывают нужные providers |
| `OpsiGeneral.NotifyOpsiMail` | `true`, bool | нет | включает внешний OnePush в `CoinTaskMixin.notify_push()` | policy rule `opsi.*` → external channel set | все `notify_push` producers migrated |
| `OpsiGeneral.LauncherPush` | `true`, bool | нет | включает Local/WebUI push для `notify_push()` | policy rule `opsi.*` → Desktop channel | Desktop channel cutover завершён |
| `OpsiGeneral.IndependentPush` | `false`, bool | нет | выбирает между `OpsiOnePushConfig` и `Error.OnePushConfig` | удаляется; отдельный named channel instance выбирается policy rule, если действительно нужен отдельный provider/account | routing умеет ссылаться на named channel instances |
| `OpsiGeneral.OpsiOnePushConfig` | `provider: null`, YAML textarea/string | **да** | отдельный OnePush config при `IndependentPush=true` | channel-owned named adapter config/secret reference | provider/account перенесён и legacy key больше не читается |
| `Commission.CommissionNotifyReward` | `false`, bool | нет | producer gate для reward notification | policy rule для `commission.reward.received`; сам event публикуется независимо от delivery preference | commission producer emits canonical event |
| `Commission.CommissionNotifyRewardStatistics` | `true`, bool | нет | решает, добавлять ли cumulative gem statistics в legacy rendered text | presentation rule/template option; event payload хранит только разрешённый typed snapshot доступных reward facts | commission renderer/template migrated |
| `EventShop.Scheduler.PushNotification` | schema default `false`, bool | нет | специальная `notification_policy.py` трактует этот task field **только как error notification permission**, принудительно выключая generic completion push | явная policy rule для EventShop failure events | удалён semantic override/hack |

**Факт.** `ActionPointNotifyLevels` встречается в комментарии/описании `module/os/tasks/scheduling.py`, но актуального generated user setting с таким canonical key аудит не обнаружил. Он не должен переноситься в новую конфигурацию как будто существует.

**Факт runtime.** Read-only Game MCP для профиля `ap` возвращает notification credential fields redacted. На момент аудита `OpsiGeneral.NotifyOpsiMail=true`, `LauncherPush=true`, `IndependentPush=false`; `Scheduler.PushNotification` у просмотренных task configurations выключен. Это observation текущего профиля, а не schema default и не основание хардкодить target policy.

**Решение.** Новая пользовательская модель имеет один корень `Notifications`, но не является механическим переименованием legacy keys. Она разделяет:

```text
Notifications
├── Global
│   ├── enabled
│   └── history_retention
├── Channels
│   └── <named channel instance> → enabled + adapter config + secret refs
├── Policies
│   └── ordered rules: matcher → channel set / suppress / cooldown
└── Presentation
    └── renderer/template options, если они действительно нужны пользователю
```

Секреты не являются частью `NotificationEvent`, policy rule, history API или MCP payload.

## Current transport topology

### External OnePush

```text
producer
  ↓
handle_notify(Error.OnePushConfig или OpsiOnePushConfig)
  ↓
YAML parse + provider selection
  ↓
onepush.get_notifier(provider)
  ↓
provider.notify(...)
  ↓
внешний HTTP/API provider
```

OnePush 1.2.0 закреплён в `pyproject.toml`. Текущий wrapper использует provider abstraction библиотеки, включая `Custom` и `gocqhttp` special cases.

### Local WebUI / launcher

```text
producer
  ↓
notify_webui(instance, title, content)
  ↓ HTTP POST, hardwired 127.0.0.1
/api/notify
  ↓
process-local asyncio.Queue
  ↓ destructive get()
/api/notify_stream (SSE)
  ↓
один из подключённых consumers / launcher
```

**Факт.** In-repo consumer `/api/notify_stream` не найден; endpoint является launcher-facing contract, а реализация consumer находится вне рассмотренного Python call graph.

## Architectural problems

1. **Transport-driven producers.** Producers знают про OnePush, YAML provider config и/или localhost WebUI.
2. **Нет canonical identity.** Одно domain occurrence, внешний push и локальный push не связаны общим event/delivery ID.
3. **Нет durable state.** Restart теряет local queue и попытки доставки.
4. **Нет общей retry model.** Retry/backoff отсутствует в notification layer.
5. **Неопределённая idempotency.** Повтор producer path может создать повторный внешний push без общей защиты.
6. **Local queue не broadcast.** Несколько SSE consumers делят сообщения.
7. **False-positive local success.** `notify_webui()` не проверяет HTTP status/body.
8. **Loopback coupling.** `127.0.0.1` предполагает co-location backend и launcher.
9. **Config semantic overload.** `Scheduler.PushNotification` имеет особую EventShop semantics; Opsi routing кодирует transport selection через четыре взаимосвязанных поля.
10. **Secrets смешаны с transport YAML.** Один string содержит provider selection и credentials.
11. **Rendered text рождается у producer.** Локализация и channel-specific presentation невозможно централизованно контролировать.
12. **Producer-specific suppression.** Opsi cooldown/dedup state размазан по runtime коду.
13. **Developer tooling обходит application boundary.** Dev button и `cyclic_notify.py` вызывают OnePush wrapper напрямую.

## Target architecture

### Boundary

**Решение.** Notification domain/application boundary живёт в `module.application`, потому что это существующий нейтральный слой DTO/ports/services. Он не импортирует WebUI, OnePush, requests или provider SDK.

Целевое логическое разделение:

```text
module.application.notifications
    typed events / schemas
    event-type registry
    policy model + resolver
    publish use case
    channel port contracts
    history/read DTOs

module.persistence
    PostgreSQL notification event/delivery/attempt repositories

infrastructure adapters
    Desktop/Launcher
    Telegram
    Webhook
    temporary OnePush adapter during cutover only

module.webui
    authenticated history/read API
    authenticated live projection stream
```

Имена конкретных Python packages/classes подтверждаются реализацией Stage 2 по существующим conventions, но dependency direction выше является закрытым решением.

## Notification Event contract

### Canonical model

**Решение.** Canonical event — immutable typed application DTO. Он не является `dict[str, Any]` и не содержит channel configuration.

Минимальный контракт:

| Field | Contract |
| --- | --- |
| `id` | уникальный UUID event occurrence; повторная публикация того же occurrence должна сохранять identity |
| `type` | stable dotted semantic name, например `runtime.game.stuck` |
| `schema_version` | положительное целое; меняется при несовместимой эволюции typed event data |
| `source` | stable subsystem/producer identity (`scheduler`, `campaign`, `opsi`, `runtime` и т. п.), не transport |
| `profile_id` | canonical AzurPilot profile/config name |
| `runtime_instance_id` | optional process/session identity для диагностики; не заменяет profile |
| `subject` | typed `{kind, id}` reference на task/campaign/fleet/etc., если применимо |
| `severity` | `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `occurred_at` | timezone-aware UTC timestamp фактического occurrence |
| `data` | concrete registered typed payload schema для данного event type/version |
| `dedup_key` | optional producer-defined identity логического occurrence, не arbitrary message hash |
| `correlation_id` | optional ID общей операции/incident |
| `causation_id` | optional ID события/операции, породившей это event |
| `sensitivity` | bounded enum (`NORMAL`, `SENSITIVE`); влияет на projection/logging, не содержит secret |

### Что не входит в event

**Решение.** Следующее относится к delivery, а не к domain event:

- channel id;
- provider credentials;
- attempt counter;
- retry/backoff timestamps;
- external provider response;
- HTTP status;
- rendered channel markup;
- delivery state.

### Title/message и локализация

**Решение.** Локализованные `title`/`content` **не являются обязательными canonical event fields**. Event хранит semantic type + typed data. Renderer выбирается через event-type registry и получает locale/channel capabilities.

Чтобы история отражала фактически доставленный текст, `DeliveryAttempt`/delivery projection может хранить sanitized rendered snapshot: `locale`, `rendered_title`, `rendered_body`, без secret metadata. Это отделяет неизменяемый факт от presentation.

Free-form fallback не становится универсальным escape hatch. Для developer test существует отдельный typed `notification.test.requested`; новые production events обязаны зарегистрировать schema и renderer.

### Metadata boundary

**Решение.** У каждого event type собственная typed data model. Расширение payload делается через новую optional typed field либо новую schema version. Общий произвольный `metadata: dict[str, Any]` не является публичным контрактом.

Низкоуровневый traceback, environment dump, raw config, access token, webhook URL, authorization header и неочищенный exception object в event запрещены.

## Event taxonomy

### Naming

**Решение.** Event types используют lowercase dotted names:

```text
<domain>.<entity-or-condition>.<occurrence>
```

Имя описывает факт, а не transport и не UI action. Запрещены типы вида `telegram.send`, `launcher.push` или `onepush.error` для domain occurrences.

### Versioning

- type name остаётся стабильным при обратно совместимом расширении payload;
- несовместимое изменение typed payload увеличивает `schema_version`;
- новый type создаётся, если меняется семантика occurrence, а не только структура данных;
- routing matcher работает по semantic type и не требует изменения central backend при регистрации нового producer schema.

### Initial registry, выведенный из current producers

```text
task.completed
task.recovered
task.failed
task.failure_limit.reached

runtime.game.unavailable
runtime.game.stuck
runtime.game.error
runtime.game.page_unknown
runtime.emulator.unavailable
runtime.emulator.recovered
runtime.recovery.succeeded
runtime.recovery.failed

campaign.stop_condition.reached
campaign.auto_search.configuration_failed
commission.reward.received

opsi.action_point.changed
opsi.action_point.low
opsi.resources.insufficient
opsi.scheduler.configuration.invalid
opsi.scheduler.coin_task.executed
opsi.ship_exp.check_failed
opsi.fleet.auto_change.completed
opsi.fleet.auto_change.failed

notification.test.requested
```

**Решение.** `campaign.stop_condition.reached` использует bounded `kind` в typed payload для `run_count`, `level`, `coin`, `new_ship` и будущих stop conditions. Backend routing не получает новый `if` для каждого kind.

### Severity

**Решение.** Default severity принадлежит event-type descriptor registry. Например successful completion — `INFO`, recoverable runtime incident — `WARNING`, невосстановимая task/runtime failure — `ERROR`, fail-closed sensitive/unsafe state — `CRITICAL`.

Producer может задать override только если schema данного type явно разрешает contextual severity. Пользовательская конфигурация **не переписывает canonical severity**; она меняет routing/suppression threshold. Так event history остаётся семантически стабильной.

## Routing policy

### Resolution

```text
NotificationEvent
    ↓
NotificationPolicyResolver
    ↓
PolicyDecision
    ├── selected named channel ids
    ├── suppression reason, если suppressed
    ├── cooldown/dedup policy
    └── renderer/locale hints
```

**Решение.** Policy rules являются данными/configuration, а не `if event.type == ...` в notification service.

Правило содержит bounded matcher:

- exact type или dotted-prefix pattern (`opsi.*`);
- minimum/exact severity;
- optional profile selector;
- optional subject kind/id selector;
- explicit priority.

Action rule содержит:

- channel set;
- suppress flag/reason;
- optional cooldown;
- optional presentation profile.

Rules сортируются по explicit priority; **первое совпавшее правило является итоговым**. В конце обязателен default rule. Это исключает неявное merge-поведение нескольких частично совпавших правил.

### Resolution precedence до rules

1. `Notifications.Global.enabled=false` → durable `SUPPRESSED(global_disabled)`;
2. event validation/type registry;
3. ordered policy match;
4. disabled channel instances удаляются из selected set;
5. cooldown/dedup decision;
6. для каждого оставшегося channel создаётся durable Delivery.

**Решение.** Global/channel disable не запрещает записать сам canonical event: history должна объяснять, что occurrence был и почему delivery suppressed.

### Per-profile behavior

Per-profile override допускается, но это слой policy, а не копия transport YAML внутри каждого producer. Глобальный default остаётся source of truth; profile rule добавляется только когда реально нужен другой routing.

## Channel contract

### Neutral adapter port

**Решение.** Channel adapter получает подготовленный delivery, а не domain config object:

```text
PreparedDelivery
├── delivery_id
├── event projection
├── rendered title/body
├── idempotency_key
├── timeout
└── channel-safe structured attributes
```

Channel configuration/secret resolve выполняется infrastructure composition layer и не попадает в event/history DTO.

### Result

Adapter возвращает typed `DeliveryResult`:

```text
DELIVERED
TRANSIENT_FAILURE
PERMANENT_FAILURE
```

Result содержит только bounded/sanitized fields: provider message id при наличии, retry-after hint, safe error code и safe diagnostic summary. Raw response body, token, URL credentials и headers по умолчанию не сохраняются.

`SUPPRESSED` — policy/delivery state, а не результат channel send.

### Timeout и failure classification

- каждый channel имеет обязательный bounded timeout;
- timeout, connection reset, 429/5xx обычно transient;
- invalid credentials/invalid destination/unsupported payload после adapter validation — permanent, если provider contract не говорит обратное;
- provider-specific mapping инкапсулирован в adapter;
- notification dispatcher никогда не вызывает provider без timeout contract.

### Registry

Channels регистрируются по stable id/type через registry/composition root. Central notification service не импортирует Telegram/Webhook/Desktop/OnePush classes и не содержит provider switch.

Channel capabilities являются typed metadata, например max title/body length, markup mode и support для idempotency key. Renderer получает capabilities до отправки.

## Persistence / outbox contract

### Durable entities

#### Notification Event

Immutable semantic occurrence. Создаётся один раз и не меняет type/data после commit.

#### Delivery

Одна строка на `(event_id, channel_instance_id)`. Содержит routing result и текущее state.

Целевые states:

```text
PENDING
IN_FLIGHT
RETRY_WAIT
DELIVERED
FAILED
SUPPRESSED
```

`FAILED` означает terminal delivery failure после permanent result или исчерпания attempts. `SUPPRESSED` терминален и содержит bounded reason code.

#### Delivery Attempt

Append-only запись каждой фактической отправки:

- attempt number;
- started/finished time;
- result class;
- sanitized adapter error code/detail;
- provider external id при наличии;
- next retry time, если применимо.

### Atomicity и настоящий transactional outbox

**Reference.** AWS Transactional Outbox описывает atomic запись бизнес-изменения и outbox event в одной DB transaction и отдельно предупреждает о duplicate delivery/idempotent consumers: <https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html>.

**Решение.** Термин `transactional outbox` используется только там, где notification occurrence рождается в той же PostgreSQL transaction, что и существующее durable domain изменение. Event + Delivery rows записываются в эту же transaction, а внешняя отправка выполняется только после commit.

Для game/runtime/error occurrences, у которых нет общей PostgreSQL transaction с игровым действием, `publish()` открывает отдельную короткую durable transaction. Это обеспечивает durability **с момента публикации**, но не объявляется atomic transaction с действием в Azur Lane/эмуляторе.

Ни один external channel не вызывается внутри DB transaction producer-а.

### Dispatcher и concurrency

**Решение.** Текущий масштаб не требует Kafka/RabbitMQ/Redis или отдельного notification microservice. Target baseline — PostgreSQL + один dispatcher в AzurPilot backend process/service boundary.

Schema/claim contract сразу допускает второй dispatcher: eligible rows выбираются детерминированно с row locks; PostgreSQL `FOR UPDATE ... SKIP LOCKED` допускается для queue-like table consumers. Reference: <https://www.postgresql.org/docs/current/sql-select.html>.

`IN_FLIGHT` имеет lease/claimed-at boundary. После crash просроченный lease возвращается в retryable state, чтобы сообщение не зависало навсегда.

### Delivery guarantee

**Решение.** Внутренняя гарантия — **at-least-once attempt**, не exactly-once external delivery. External provider может принять запрос и потерять response, поэтому duplicate send полностью исключить невозможно без provider idempotency support.

Idempotency strategy:

- `event.id` идентифицирует occurrence;
- optional `dedup_key` задаётся producer-ом для повторного publish того же logical occurrence;
- `(profile_id, type, dedup_key)` не создаёт второй canonical event, если `dedup_key` задан и уже существует;
- delivery idempotency key формируется из stable `event_id + channel_instance_id` и передаётся adapter/provider, если channel capability это поддерживает;
- cooldown является policy suppression и не подменяет identity/dedup.

Producer обязан включать occurrence dimension в `dedup_key`; один постоянный key для всех будущих событий одного типа запрещён.

### Retry

**Решение.** Retry принадлежит dispatcher, не producer и не WebUI.

Target defaults являются configuration defaults, а не разбросанными константами:

- `max_attempts = 5`;
- exponential backoff с jitter;
- base delay 5 s;
- cap 15 min;
- provider `Retry-After` может увеличить next-attempt time;
- permanent failure не retry-ится;
- manual requeue в будущем создаёт отдельное operator action/audit, а не обнуляет history молча.

### Ordering

Глобальный total order между профилями/channels не требуется. Dispatcher выбирает pending rows по durable creation order для одного channel/profile, но retry одного delivery не блокирует независимые более новые events. Если будущий channel потребует strict ordering, это объявляется capability/policy отдельно.

### Retention и history

**Решение.** Event + delivery + sanitized attempt history хранится PostgreSQL и доступна WebUI через application read service. Default retention — 30 дней, configurable глобально; cleanup удаляет только terminal records старше retention и не затрагивает active retry/in-flight deliveries.

Rendered snapshot хранится только для фактических/suppressed delivery projections, без secret config.

## Delivery reliability

1. `publish()` сначала валидирует registered event schema.
2. Event и policy decision фиксируются durable.
3. Dispatcher не зависит от producer lifetime.
4. Каждая попытка имеет bounded timeout и отдельный attempt row.
5. Crash до send → lease вернёт row в retry.
6. Crash после provider accept, но до local commit → возможен duplicate; idempotency key используется, если provider умеет.
7. Permanent failure сохраняется как terminal history и не блокирует основную game task.
8. Notification subsystem failure не должен превращать успешную game task в failed task, кроме отдельно объявленного fail-closed operational requirement; текущих таких требований аудит не обнаружил.

## WebUI / Desktop / VPS boundary

### Domain и history

**Решение.** WebUI history — read projection durable notification data через application service. WebUI не является transport persistence и не владеет queue.

### WebUI live stream

SSE остаётся подходящим server→browser transport, но только как projection поверх durable history, а не единственное место хранения.

Target stream:

- authenticated;
- выдаёт SSE `id` на каждое событие projection;
- принимает resume cursor/`Last-Event-ID`;
- после reconnect дочитывает bounded backlog из PostgreSQL;
- затем подписывается на новые committed notification changes;
- несколько browser consumers получают свои независимые streams, а не делят destructive queue.

**Reference.** WHATWG/MDN определяют `id` и `Last-Event-ID`/reconnect semantics: <https://html.spec.whatwg.org/dev/server-sent-events.html>, <https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events>.

### Desktop/Launcher как channel

**Решение.** Desktop/Launcher — отдельный `NotificationChannel`, а не `/api/notify` side effect.

Для текущего all-local режима backend и Desktop Agent могут находиться на одном PC, но contract не зависит от loopback.

Для будущего VPS split:

```text
Windows PC / Desktop Agent
       │
       └── outbound authenticated HTTPS connection ──► AzurPilot backend/VPS
                                                       │
                                                       ├── durable delivery backlog
                                                       └── live server→agent stream
```

Предпочтительный transport contract Stage 4: authenticated outbound SSE для server→agent stream + authenticated HTTPS ACK endpoint для `delivery_id`. Агент не открывает домашний inbound port. После reconnect он возобновляет cursor, получает bounded backlog и подтверждает delivery после локального OS/launcher handoff.

Если при реализации окажется, что Desktop нуждается в существенном bidirectional realtime control помимо ACK, это отдельный gate в пользу WebSocket; notification architecture от этого не меняется.

### WebUI stream не равен Desktop channel

Browser WebUI live stream показывает history/projection и не должен создавать отдельную external Delivery row на каждый browser tab. Desktop Agent является реальным delivery channel и имеет delivery/ack state.

### MCP boundary

**Решение.** Ни Dev MCP, ни Game MCP не являются notification transport.

- Dev MCP может в будущем давать диагностический read-only status/health платформы.
- Game MCP при необходимости может выдавать только sanitized notification history DTO через application boundary.
- config secrets, raw provider responses и sensitive metadata в MCP не выдаются.

## Security and secrets

### Current findings

- `Error.OnePushConfig` и `OpsiGeneral.OpsiOnePushConfig` могут содержать provider credentials в YAML string.
- Read-only Game MCP уже redacts эти поля; это подтверждает, что их нельзя считать обычным non-sensitive config.
- legacy rendered notification для sensitive task включает строковое представление ошибки; такой текст потенциально может содержать лишнюю диагностическую информацию.
- raw notification API routes не имеют собственного auth gate; reachability зависит от bind/topology.

### Target contract

1. Channel secret принадлежит channel configuration/secret resolver, не event и не policy.
2. Event/history никогда не хранит token/password/private key/authorization header/raw secret config.
3. Diagnostic error detail имеет bounded sanitized code + summary; raw response body не сохраняется по умолчанию.
4. WebUI history/API требует authentication и authorization независимо от bind address.
5. Desktop Agent имеет отдельный revocable credential с минимальным scope: stream/ack только для разрешённых profile/channel deliveries.
6. Browser session credential и Desktop credential не взаимозаменяемы.
7. Sensitive events по умолчанию скрывают/редактируют sensitive `data` fields в WebUI/MCP projection; renderer получает только разрешённую projection.
8. Logs не печатают channel config целиком.

### Future Webhook channel

Webhook URL следует считать sensitive destination data. Adapter обязан:

- разрешать `https` по умолчанию;
- валидировать destination до записи config;
- запрещать loopback/private/link-local/metadata destinations по умолчанию;
- поддерживать явный trusted-local allowlist только как отдельную конфигурацию;
- учитывать DNS re-resolution/rebinding при соединении;
- не следовать небезопасным redirects на запрещённые сети;
- ограничивать body/response size;
- не логировать credentials/query secrets;
- подписывать payload отдельным secret, если receiver contract это требует.

Это закрывает основной SSRF/secret leakage risk до появления production Webhook adapter.

## OnePush decision

**Reference.** OnePush предоставляет общий Python provider layer для Bark, Discord, Telegram, ServerChan, WeChat, pushplus, go-cqhttp, Qmsg, DingTalk, Lark, SMTP и Custom providers: <https://github.com/y1ndan/onepush>.

**Решение.** OnePush **не является фундаментом target architecture и после полного cutover не нужен**. Он может существовать только как временный channel adapter в migration window, чтобы не ломать реально используемый provider до готовности first-class adapter.

**Decision gate перед удалением dependency.** Нужно получить sanitized inventory только provider names из пользовательских profile configs без credentials. Для каждого реально используемого provider должно быть одно из двух:

1. first-class AzurPilot channel adapter готов и проверен;
2. пользователь явно отказался от этого provider.

После выполнения gate удаляются OnePush adapter, `handle_notify`, YAML provider config и `onepush==1.2.0` одной migration итерацией. Долгоживущий compatibility wrapper не остаётся.

## Migration matrix

| Current | Target | Stage | Removal condition |
| --- | --- | --- | --- |
| `module/notify/notify.py::handle_notify` | `NotificationPublisher` → durable Delivery → Channel registry | Stage 3–4 | все production/developer callsites migrated |
| `module/notify/notify.py::notify_webui` | Desktop channel + WebUI history/live projection | Stage 4 | launcher/desktop consumer cutover и ACK contract проверены |
| `module/notify/__init__.py` lazy wrappers | application notification package exports only | Stage 4 | legacy functions не импортируются |
| `alas.py` generic task-result `handle_notify` | typed `task.completed/recovered/failed` publish | Stage 4 | scheduler tests переведены на canonical events |
| `alas.py` sensitive task error path | `task.failed` + sensitivity/cause | Stage 4 | fail-closed behavior и redaction tests PASS |
| `alas.py` game unavailable/stuck/client-error paths | `runtime.game.*` + recovery events | Stage 4 | recovery tests assert events, не transports |
| `alas.py` emulator recovery paths | `runtime.emulator.*` / `runtime.recovery.*` | Stage 4 | emulator recovery tests assert events + delivery non-blocking |
| `alas.py` fatal Script/Page/HumanTakeover/unhandled paths | typed fatal events | Stage 4 | fatal exit behavior unchanged and events durable before process exit |
| `alas.py` repeated task failure limit | `task.failure_limit.reached` | Stage 4 | scheduler limit behavior unchanged |
| campaign direct `handle_notify` callsites | `campaign.stop_condition.reached` typed kind | Stage 4 | all stop-condition notifications routed by policy |
| GemsFarming fast-forward direct OnePush | `campaign.auto_search.configuration_failed` | Stage 4 | direct import removed |
| Commission direct OnePush + WebUI | `commission.reward.received` + renderer | Stage 4 | reward tests cover event/data/render policy |
| `CoinTaskMixin.notify_push` | удалить; Opsi producers publish events | Stage 4 | все seven current `notify_push` producer branches migrated |
| Opsi producer-local notification cooldown flags | durable policy suppression/dedup | Stage 4 | behavior parity tests на thresholds/cooldown |
| `Scheduler.PushNotification` | Notifications policy for task events | Stage 2 config + Stage 4 cutover | no reader in scheduler |
| EventShop semantic override of `Scheduler.PushNotification` | explicit failure policy matcher for EventShop subject | Stage 2/4 | `notification_policy.py` transport override удалён |
| `Error.OnePushConfig` | named channel config + secret refs | Stage 2/3 | no legacy external caller; provider gate complete |
| `OpsiGeneral.NotifyOpsiMail` | `opsi.*` policy external channels | Stage 2/4 | no `notify_push` reader |
| `OpsiGeneral.LauncherPush` | `opsi.*` policy Desktop channel | Stage 2/4 | Desktop channel cutover |
| `OpsiGeneral.IndependentPush` | policy selects named channel instance | Stage 2/4 | dedicated provider config migrated |
| `OpsiGeneral.OpsiOnePushConfig` | optional named channel instance secret/config | Stage 2/3 | no reader and provider migrated |
| `CommissionNotifyReward` | policy rule | Stage 2/4 | commission event emitted regardless of delivery preference |
| `CommissionNotifyRewardStatistics` | presentation option for commission renderer | Stage 2/4 | legacy string composition removed |
| `/api/notify` POST | удалить как internal transport endpoint | Stage 4 | no `notify_webui` callers; Desktop Agent uses channel endpoint |
| `_notification_queue` | durable PostgreSQL delivery/history | Stage 3/4 | all live streams backed by durable cursor |
| `/api/notify_stream` current destructive SSE | authenticated resumable WebUI projection stream; separate Desktop stream | Stage 4 | old launcher consumer migrated |
| `module/webui/app_developer_tools.py` direct Error.OnePush test | dev-only typed test publish/channel health use case | Stage 4 | no direct legacy import |
| `dev_tools/cyclic_notify.py` | bounded dev harness через application API либо удалить | Stage 4 | canonical developer test exists |
| tests patching `handle_notify`/`notify_webui` | event publisher/policy/channel contract tests | Stage 2–4 | no legacy patch target remains |
| translation structural gate special-casing `handle_notify`/`notify_push` prose | registry/event renderer-aware structural rules либо удалить obsolete checks | Stage 4 | old syntax no longer exists |
| `pyproject.toml` `onepush==1.2.0` | first-class adapters only | final cutover | sanitized provider inventory gate complete |
| generated/template/i18n legacy notification keys | generated `Notifications` model | Stage 2–4 | source YAML migrated and generators produce no legacy keys |
| bridge generated `Error.OnePushConfig` compatibility fields | remove/regenerate if bridge source still inherits shared Error group | final cutover | repo-wide search shows no required legacy reader |
| docs/UI copy mentioning OnePush/legacy push | target channel/policy terminology | final cutover | user-visible configuration cutover complete |

## Decisions

Stage 1 закрывает следующие решения:

1. **Boundary:** notification domain/application API находится в `module.application`; transports и persistence — adapters.
2. **Canonical event:** immutable typed event с identity/type/version/source/profile/subject/severity/time/typed data/dedup/provenance/sensitivity.
3. **Type versioning:** dotted semantic type + отдельный integer schema version.
4. **Severity:** semantic default в event registry; config влияет на routing, не переписывает факт.
5. **Routing:** ordered config rules, first-match-wins, registry channels; no event-specific backend `if/elif`.
6. **Channels:** neutral typed port + capabilities + typed delivery result.
7. **Delivery result:** delivered / transient failure / permanent failure; suppression принадлежит policy.
8. **Durability:** PostgreSQL Event + Delivery + Attempt; external send после commit.
9. **Transactional outbox:** только для occurrences, записываемых в одну DB transaction с domain mutation; runtime events получают собственную durable transaction.
10. **Reliability:** at-least-once attempts, stable idempotency key, dispatcher-owned retry, producer-defined occurrence dedup identity.
11. **History:** PostgreSQL-backed, 30-day configurable default, sanitized rendered delivery snapshot.
12. **OnePush:** transitional adapter only; после полного cutover удалить.
13. **Desktop:** first-class channel, не WebUI queue side effect.
14. **VPS split:** Windows Agent инициирует outbound authenticated connection; home inbound port не требуется.
15. **Legacy removal:** `handle_notify`, `notify_webui`, current notify API/queue, OnePush configs/dependency и producer-specific transport routing удаляются после migration matrix gates.
16. **Stage 2:** typed core contracts, event registry, policy/config model и unit-level contracts; без преждевременного producer cutover.

## External references

Эти источники используются как design references, не как обязательные зависимости:

- CloudEvents spec: <https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md>. Полезны идеи `id`, `source`, `type`, `subject`, `time`; AzurPilot не обязан становиться CloudEvents transport implementation.
- AWS Transactional Outbox: <https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html>.
- PostgreSQL locking/`SKIP LOCKED`: <https://www.postgresql.org/docs/current/sql-select.html>.
- WHATWG SSE: <https://html.spec.whatwg.org/dev/server-sent-events.html>.
- MDN SSE: <https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events>.
- OnePush: <https://github.com/y1ndan/onepush>.

CloudEvents остаётся reference envelope vocabulary. В частности, AzurPilot берёт идею стабильной identity/source/type и времени occurrence, но не принимает unrestricted extension attributes как замену typed event schemas.

## Explicit non-goals

Stage 1 намеренно не делает следующее:

- не создаёт production `NotificationService`/dispatcher;
- не добавляет PostgreSQL migration/outbox table;
- не добавляет Kafka/RabbitMQ/Redis;
- не создаёт notification microservice или отдельный server;
- не реализует Telegram/Webhook/Desktop adapters;
- не удаляет OnePush;
- не меняет `handle_notify` или `notify_webui`;
- не меняет `/api/notify`/`/api/notify_stream`;
- не меняет launcher protocol;
- не меняет generated config/i18n;
- не мигрирует producers;
- не реализует Windows Agent;
- не использует MCP как notification transport;
- не исправляет найденный `Custom` OnePush risk в рамках docs-only Stage 1.

## Stage 2 entry criteria

Stage 2 — **Notification Core и единая конфигурационная модель** — может начинаться, когда:

1. этот архитектурный документ принят как contract;
2. Stage 1 PR остаётся docs-only и CI/CodeRabbit не обнаружили фактических противоречий;
3. `module.application` dependency direction сохраняется;
4. Stage 2 не реализует provider adapters раньше typed event/policy contracts;
5. source-of-truth новой `Notifications` config определяется в existing argument/generator pipeline, а не отдельным ручным JSON;
6. event registry и payload schemas имеют тесты на type/version/validation;
7. policy resolver имеет tests на priority, default, global disable, per-profile match, channel disable и suppression;
8. public application DTO не использует unrestricted `dict[str, Any]`;
9. secrets остаются вне events/history/tests fixtures;
10. migration code не создаёт долгоживущий parallel notification stack.

### Stage 2 concrete scope

Stage 2 должен реализовать только foundation, необходимый следующим stages:

- typed `NotificationEvent` и registered payload schemas для initial taxonomy;
- event type descriptor registry;
- typed `NotificationPolicy`/decision model и deterministic resolver;
- neutral `NotificationChannel` port/result/capabilities;
- source-of-truth `Notifications` configuration schema через существующий generator pipeline;
- unit tests и migration mapping tests для legacy config semantics;
- interfaces для persistence/publisher, если они нужны для dependency direction, без преждевременной внешней доставки.

PostgreSQL tables/dispatcher и реальные channels остаются следующей реализационной стадией, чтобы Stage 2 не смешивал core contract с network/provider behavior.
