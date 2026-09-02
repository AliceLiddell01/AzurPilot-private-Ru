# Платформа уведомлений

## Цель и границы

Этот документ является долговечным архитектурным контрактом платформы уведомлений AzurPilot. Он фиксирует фактическое состояние `personal/stable` на момент Stage 1 и целевую модель, к которой последующие этапы должны прийти без двух параллельных стеков уведомлений.

Аудит Stage 1 выполнен относительно `personal/stable` `c92c7b7ece6994a5987ce37442734cd7dcd590c4`.

Целевой поток:

```text
NotificationEvent
        ↓
NotificationPolicy
        ↓
Durable Delivery State
        ↓
NotificationChannel
        ↓
Delivery
```

Stage 1 не меняет поведение production-кода. Здесь нет новых таблиц, диспетчера, адаптеров каналов, новых WebUI endpoints или миграции источников уведомлений.

Обозначения в документе:

- **Факт** — подтверждено текущим кодом или read-only runtime-интерфейсом.
- **Решение** — целевой архитектурный контракт.
- **Внешний источник** — внешний design reference, а не описание текущего AzurPilot.
- **Критерий решения** — выбор, который нельзя безопасно сделать без данных, отсутствующих на Stage 1.

## Текущее фактическое состояние

### Общая картина

**Факт.** В текущем коде нет единой domain/application-границы уведомлений. Существуют как минимум четыре пересекающихся механизма:

1. синхронный внешний push через `module.notify.handle_notify()` и OnePush;
2. локальный `notify_webui()`, который синхронно делает HTTP POST на `127.0.0.1`;
3. `CoinTaskMixin.notify_push()` в Operation Siren, самостоятельно выбирающий WebUI и OnePush;
4. process-local `asyncio.Queue` + SSE `/api/notify_stream` для launcher-facing доставки.

**Факт.** У этих путей нет общего event identity, единой taxonomy, durable history, общей retry state, общей deduplication state или единого policy engine.

**Факт.** `module/notify/__init__.py` является только lazy-import wrapper для `handle_notify` и `notify_webui`; отдельной абстракции он не добавляет.

### `handle_notify()`

**Факт.** `module/notify/notify.py::handle_notify()`:

- принимает YAML-строку с конфигурацией провайдера;
- объединяет YAML documents;
- выбирает provider через `onepush.core.get_notifier()`;
- передаёт provider-specific параметры прямо в OnePush;
- содержит специальные ветки для `Custom` и `gocqhttp`;
- выполняет `notifier.notify()` синхронно в потоке источника;
- для `requests.Response` считает успешным только HTTP 200;
- дополнительно проверяет `status == "failed"` у `gocqhttp`;
- ловит `OnePushException` и произвольные исключения и возвращает `False`;
- не имеет собственной retry/backoff/dedup/persistence модели;
- не задаёт собственный единый timeout-контракт для provider-вызова.

Следствие: ошибка доставки обычно не выбрасывается в источник уведомления, но синхронная отправка всё равно занимает его execution path до возврата underlying provider.

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

### Аутентификация и сетевая граница текущих endpoints

**Факт.** `module/webui/fastapi.py` добавляет API routes в тот же Starlette app, что и PyWebIO routes. Middleware в этом слое включает gzip/header behavior, но не общий authentication middleware для raw API routes.

**Факт.** WebUI password flow реализован внутри PyWebIO session login. Notification routes `/api/notify` и `/api/notify_stream` не имеют собственного `is_local_request` gate и не проходят отдельную route-level authentication проверку.

**Факт.** Нельзя утверждать, что эти endpoints всегда глобально доступны: фактическая reachability зависит от bind address и внешней сетевой топологии. Generic deploy templates допускают wildcard host, тогда как персонализированный Windows build нормализует WebUI host к loopback. Поэтому security contract должен исправляться на уровне API/auth boundary, а не исходить из предположения о конкретном bind.

**Факт.** Соседний `/api/launcher/stream` имеет отдельный `is_local_request` gate. Поэтому notification cutover не должен механически удалять существующий `/api/launcher/*` control plane: это другой контракт.

## Инвентаризация источников уведомлений

В таблице `External` означает OnePush через `handle_notify`, `Local` — `notify_webui` → WebUI queue/SSE.

| Текущий источник / точка вызова | Предметное событие и условие | Текущий контекст / транспорт / конфигурация | Текущая надёжность | Покрытие | Канонический тип события |
| --- | --- | --- | --- | --- | --- |
| `alas.py::_check_sensitive_exit()` | Чувствительная задача завершилась с ошибкой; AzurPilot останавливается | profile=`config_name`; External + Local; `Error.OnePushConfig` | синхронно; без retry/persistence; Local max 2 s | `tests/test_scheduler_core_runtime_messages.py`, `tests/test_alas_error_handling.py` | `task.failed` + `sensitivity=SENSITIVE` |
| `alas.py::run()` — `GameNotRunningError` | Игра не запущена, планируется `Restart` | External + Local; `Error.OnePushConfig` | best-effort, блокирует источник на время вызовов | error-handling tests | `runtime.game.unavailable` |
| `alas.py::run()` — предел repeated game recovery | Достигнут предел повторных восстановлений Azur Lane | External + Local | best-effort, без durable attempt state уведомления | recovery tests | `runtime.recovery.failed` |
| `alas.py::run()` — `GameStuckError`/`GameTooManyClickError`, старт recovery | Игра зависла/клик-цикл; запускается проверяемый restart | External; при успешном game-only recovery Local success; при дальнейших исходах External + Local | notification send не является частью recovery transaction | recovery tests | `runtime.game.stuck` и `runtime.recovery.succeeded/failed` |
| `alas.py::run()` — успешная Stage 2 emulator recovery | MuMu восстановлен штатно или hard-kill path, финальная UI-проверка прошла | External + Local | best-effort | emulator recovery tests | `runtime.emulator.recovered` |
| `alas.py::run()` — game recovery полностью неуспешна | Game restart и разрешённая emulator escalation не дали healthy game | External + Local | best-effort | emulator recovery tests | `runtime.recovery.failed` |
| `alas.py::run()` — обрабатываемая ошибка игрового клиента | Ошибка клиента; планируется автоматический Restart | External + Local | best-effort | scheduler/error tests | `runtime.game.error` |
| `alas.py::run()` — `GamePageUnknownError` | Состояние страницы не определено, аварийное завершение | External + Local | best-effort | structural/runtime tests | `runtime.game.page_unknown` |
| `alas.py::run()` — `ScriptError` | Ошибка сценария, текущую задачу продолжить нельзя | External + Local | best-effort | structural/runtime tests | `task.failed` с cause=`script_error` |
| `alas.py::run()` — `EmulatorNotRunningError`, recovery success | Эмулятор был недоступен и восстановлен | External + Local | best-effort | emulator recovery tests | `runtime.emulator.recovered` |
| `alas.py::run()` — `EmulatorNotRunningError`, recovery failure | Эмулятор недоступен и восстановить его нельзя | External + Local | best-effort | emulator recovery tests | `runtime.emulator.unavailable` |
| `alas.py::run()` — `RequestHumanTakeover` | Автоматизация не может безопасно продолжить | External + Local | best-effort | scheduler/runtime tests | `task.failed` + cause=`human_takeover_required` |
| `alas.py::run()` — unhandled exception | Необработанная ошибка задачи/runtime | External + Local | best-effort | scheduler/runtime tests | `task.failed` + cause=`unhandled_exception` |
| `alas.py::loop()` — per-task result | После `run`: success / recoverable / failure, только если `Scheduler.PushNotification=true` | External only; `Error.OnePushConfig` используется как transport config | вызов обёрнут в `try/except`; без Local | scheduler continuation/core tests | `task.completed`, `task.recovered`, `task.failed` |
| `alas.py::loop()` — repeated task failure limit | Одна задача достигла предела последовательных ошибок, scheduler останавливается | External + Local | best-effort | scheduler core tests | `task.failure_limit.reached` |
| `module/campaign/run.py::CampaignRun.triggered_stop_condition()` | Run-count limit достигнут | External; `Error.OnePushConfig` | синхронный best-effort wrapper; нет retry | прямого notification-specific test не найдено | `campaign.stop_condition.reached`, kind=`run_count` |
| тот же метод | Reach-level limit достигнут | External | то же | то же | `campaign.stop_condition.reached`, kind=`level` |
| тот же метод | Получен новый корабль при включённом stop condition | External | то же | то же | `campaign.stop_condition.reached`, kind=`new_ship` |
| `module/campaign/campaign_event.py::coin_limit_triggered()` | Coin limit достигнут; campaign откладывается | External | то же | прямого notification-specific test не найдено | `campaign.stop_condition.reached`, kind=`coin` |
| `module/handler/fast_forward.py` — GemsFarming auto-search setup failure | Не удалось применить ожидаемые настройки auto-search/fleet order | External; `Error.OnePushConfig` | synchronous best-effort | structural translation gate | `campaign.auto_search.configuration_failed` |
| `module/commission/commission.py::commission_receive()` | Получена награда комиссии и `CommissionNotifyReward=true` | External + Local; `Error.OnePushConfig` | best-effort; без retry/history | отдельного delivery test не найдено | `commission.reward.received` |
| `CoinTaskMixin.check_and_notify_action_point_threshold()` | Изменились очки действия | Local и/или External через `notify_push` | producer-local cooldown, без общей durable dedup | общего channel-contract test нет | `opsi.action_point.changed` |
| `_handle_smart_scheduling_no_task()` — overflow cleanup | Для предотвращения overflow выполнен Meowfficer farming | `notify_push` | best-effort | общего notification contract test нет | `opsi.scheduler.coin_task.executed` |
| `_notify_coins_ap_insufficient()` | Одновременно недостаточно yellow coins и AP | `notify_push` + producer-local cooldown | best-effort | то же | `opsi.resources.insufficient` |
| `_notify_ap_insufficient()` | AP ниже minimum reserve | `notify_push` + producer-local cooldown | best-effort | то же | `opsi.action_point.low` |
| `_dispatch_coin_task()` — no coin task | Не включена ни одна задача пополнения yellow coins | `notify_push` | best-effort | то же | `opsi.scheduler.configuration.invalid` |
| `_notify_coin_task_proxy()` | Scheduler прокси-выполнил задачу пополнения yellow coins | `notify_push` + producer-local attempt state | best-effort | то же | `opsi.scheduler.coin_task.executed` |
| `CoinTaskMixin.notify_action_point_threshold()` | Generic helper для AP threshold; in-repo caller аудитом не найден | содержит прямой `notify_push` sink | потенциальный legacy sink | прямой caller не найден | `opsi.action_point.changed` при сохранении helper semantics |
| `OpsiHazard1Leveling._cl1_ap_check()` | AP ниже резерва; первая проверка уведомляет, следующие подавляются до recovery | `notify_push`; `OpsiHazard1_PreviousApInsufficient` | producer-specific dedup flag | общего notification contract test нет | `opsi.action_point.low` |
| `OpsiHazard1Leveling.os_check_leveling()` — сбор данных неуспешен | Не удалось собрать ship-exp data | `notify_push` | best-effort | общего notification contract test нет | `opsi.ship_exp.check_failed` |
| `OpsiHazard1Leveling.os_check_leveling()` — отчёт | Ship-exp data собраны, сформирован очередной отчёт | `notify_push` | best-effort | общего notification contract test нет | `opsi.ship_exp.check_completed` |
| `OpsiHazard1Leveling.os_check_leveling()` — весь флот достиг цели | Все корабли флота достигли целевого ограничения опыта | `notify_push` | best-effort | общего notification contract test нет | `opsi.ship_exp.target_reached` |
| `OpsiHazard1Leveling._check_custom_positions_full_exp()` | Все выбранные пользовательские позиции достигли цели | `notify_push` | best-effort | общего notification contract test нет | `opsi.ship_exp.target_reached`, scope=`custom_positions` |
| `OpsiFleetAutoChange._notify_auto_change_complete()` | Автоподбор флота завершён | `notify_push` | исключение вызова подавляется источником | общего notification contract test нет | `opsi.fleet.auto_change.completed` |
| `OpsiFleetAutoChange._handle_auto_change_error()` | Автоподбор не выполнен, функция отключена, назначается Restart | `notify_push` | исключение вызова подавляется источником | общего notification contract test нет | `opsi.fleet.auto_change.failed` |
| `module/webui/app_developer_tools.py::_test_notify_error()` | Ручной developer test `Error.OnePushConfig` | External only | напрямую тестирует legacy wrapper | UI developer utility | `notification.test.requested` в будущей dev-only service |
| `dev_tools/cyclic_notify.py` | Бесконечный тестовый loop каждые 0.5 s с локально редактируемым OnePush YAML | External only | ad-hoc utility | нет | удалить; заменить bounded dev test harness |

### Полный аудит прямых `notify_push` sinks

**Факт.** В трёх production-файлах находятся **14 прямых выражений вызова** `self.notify_push(...)`, не считая определения `notify_push()`:

| Файл | Прямые sinks | Семантическое объединение |
| --- | ---: | --- |
| `module/os/tasks/scheduling.py` | 7 | 6 активных веток: AP changed, overflow cleanup, resources insufficient, AP low, no coin task, delegated coin task; плюс `notify_action_point_threshold()` — generic helper без найденного in-repo caller |
| `module/os/tasks/hazard_leveling.py` | 5 | AP low; ship-exp check failed; ship-exp report; target reached для всего флота; target reached для custom positions |
| `module/os/tasks/fleet_auto_change.py` | 2 | auto-change completed; auto-change failed |

**Решение.** Stage 4 не считается завершённым, пока все 14 прямых sinks либо мигрированы на canonical events, либо удалены как доказанно мёртвый код. Семантически одинаковые sinks могут публиковать один event type с typed `kind`/`scope`, но физический legacy call site нельзя оставить только потому, что соседняя branch уже мигрирована.

### Локальное подавление повторов у источников

**Факт.** Operation Siren уже содержит локальные механизмы подавления повторов: cooldown timestamps, `OpsiHazard1_PreviousApInsufficient`, last-notified/last-attempt runtime attributes и минимальный интервал для части smart-scheduling уведомлений. Эти механизмы не являются общей delivery state и не переживают все типы restart одинаково.

**Решение.** Семантика «это новое предметное событие или тот же incident» остаётся у producer/event schema. Семантика delivery cooldown, suppression и duplicate delivery переходит в `NotificationPolicy` + durable delivery state. Producer-specific flags удаляются после подтверждения эквивалентного поведения.

## Инвентаризация конфигурации

Source of truth для перечисленных пользовательских полей — `module/config/argument/argument.yaml`; `module/config/config_generated.py`, `config/template.json` и i18n являются generated/representation слоями.

| Текущий ключ | Значение по умолчанию / тип | Секретный | Текущие readers / semantics | Цель | Условие удаления |
| --- | --- | --- | --- | --- | --- |
| `Scheduler.PushNotification` | `false`, checkbox/bool | нет | `alas.py::loop()`; generic task-result OnePush. Для EventShop тот же ключ имеет другую semantics | policy rule для `task.completed/recovered/failed` с task/profile subject | все scheduler producers переведены на events; EventShop больше не переиспользует ключ |
| `Error.OnePushConfig` | `provider: null`, YAML textarea/string | **да**: может содержать token/key/url/password | почти все legacy external producers, Opsi fallback, developer test | channel-owned secret/config reference; routing отдельно | legacy OnePush paths удалены и first-class adapters покрывают нужные providers |
| `OpsiGeneral.NotifyOpsiMail` | `true`, bool | нет | включает внешний OnePush в `CoinTaskMixin.notify_push()` | policy rule `opsi.*` → external channel set | все `notify_push` sinks migrated/removed |
| `OpsiGeneral.LauncherPush` | `true`, bool | нет | включает Local/WebUI push для `notify_push()` | policy rule `opsi.*` → Desktop channel | Desktop channel cutover завершён |
| `OpsiGeneral.IndependentPush` | `false`, bool | нет | выбирает между `OpsiOnePushConfig` и `Error.OnePushConfig` | удаляется; отдельный named channel instance выбирается policy rule | routing умеет ссылаться на named channel instances |
| `OpsiGeneral.OpsiOnePushConfig` | `provider: null`, YAML textarea/string | **да** | отдельный OnePush config при `IndependentPush=true` | channel-owned named adapter config/secret reference | provider/account перенесён и legacy key больше не читается |
| `Commission.CommissionNotifyReward` | `false`, bool | нет | producer gate для reward notification | policy rule для `commission.reward.received`; event публикуется независимо от delivery preference | commission producer emits canonical event |
| `Commission.CommissionNotifyRewardStatistics` | `true`, bool | нет | добавляет cumulative gem statistics в legacy rendered text | presentation option; event payload хранит только разрешённый typed snapshot | commission renderer migrated |
| `EventShop.Scheduler.PushNotification` | schema default `false`, bool | нет | `notification_policy.py` трактует поле **только как error notification permission**, выключая generic completion push | явная policy rule для EventShop failure events | удалён semantic override/hack |

**Факт.** `ActionPointNotifyLevels` встречается в комментарии/описании `module/os/tasks/scheduling.py`, но актуального generated user setting с таким canonical key аудит не обнаружил. Он не должен переноситься в новую конфигурацию как будто существует.

**Факт runtime.** Read-only Game MCP для профиля `ap` возвращает notification credential fields redacted. На момент аудита `OpsiGeneral.NotifyOpsiMail=true`, `LauncherPush=true`, `IndependentPush=false`; `Scheduler.PushNotification` у просмотренных task configurations выключен. Это observation текущего профиля, а не schema default и не основание хардкодить target policy.

**Решение.** Новая пользовательская модель имеет один корень `Notifications`, но не является механическим переименованием legacy keys:

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

## Текущая транспортная топология

### Внешний OnePush

```text
источник
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

### Локальные WebUI / лаунчер

```text
источник
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

## Архитектурные проблемы

1. **Transport-driven sources.** Источники знают про OnePush, YAML provider config и/или localhost WebUI.
2. **Нет canonical identity.** Одно domain occurrence, внешний push и локальный push не связаны общим event/delivery ID.
3. **Нет durable state.** Restart теряет local queue и попытки доставки.
4. **Нет общей retry model.** Retry/backoff отсутствует в notification layer.
5. **Неопределённая idempotency.** Повтор producer path может создать повторный внешний push без общей защиты.
6. **Local queue не broadcast.** Несколько SSE consumers делят сообщения.
7. **False-positive local success.** `notify_webui()` не проверяет HTTP status/body.
8. **Loopback coupling.** `127.0.0.1` предполагает co-location backend и launcher.
9. **Config semantic overload.** `Scheduler.PushNotification` имеет особую EventShop semantics; Opsi routing кодирует transport selection через несколько связанных полей.
10. **Secrets смешаны с transport YAML.** Один string содержит provider selection и credentials.
11. **Rendered text рождается у источника.** Локализация и channel-specific presentation невозможно централизованно контролировать.
12. **Producer-specific suppression.** Opsi cooldown/dedup state размазан по runtime-коду.
13. **Developer tooling обходит application boundary.** Dev button и `cyclic_notify.py` вызывают OnePush wrapper напрямую.

## Целевая архитектура

### Граница

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
    PostgreSQL notification event/policy/delivery/attempt repositories

infrastructure adapters
    Desktop/Launcher
    Telegram
    Webhook
    temporary OnePush adapter during cutover only

module.webui
    authenticated + authorized history/read API
    authenticated + authorized live projection stream
```

Имена конкретных Python packages/classes подтверждаются реализацией Stage 2 по существующим conventions, но dependency direction выше является закрытым решением.

## Контракт `NotificationEvent`

### Каноническая модель

**Решение.** Canonical event — immutable typed application DTO. Он не является `dict[str, Any]` и не содержит channel configuration.

| Поле | Контракт |
| --- | --- |
| `id` | уникальный UUID occurrence; повторная публикация того же occurrence сохраняет identity |
| `type` | stable dotted semantic name, например `runtime.game.stuck` |
| `schema_version` | положительное целое; меняется при несовместимой эволюции typed event data |
| `source` | stable subsystem/producer identity (`scheduler`, `campaign`, `opsi`, `runtime`), не transport |
| `profile_id` | canonical AzurPilot profile/config name |
| `runtime_instance_id` | optional process/session identity для диагностики; не заменяет profile |
| `subject` | typed `{kind, id}` reference на task/campaign/fleet/etc., если применимо |
| `severity` | `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `occurred_at` | timezone-aware UTC timestamp фактического occurrence |
| `data` | concrete registered typed payload schema для данного event type/version |
| `dedup_key` | optional producer-defined identity логического occurrence, не arbitrary message hash |
| `correlation_id` | optional ID общей операции/incident |
| `causation_id` | optional ID события/операции, породившей event |
| `sensitivity` | bounded enum (`NORMAL`, `SENSITIVE`); влияет на projection/logging, не содержит secret |

### Что не входит в событие

Следующее относится к delivery, а не к domain event:

- channel id;
- provider credentials;
- attempt counter;
- retry/backoff timestamps;
- external provider response;
- HTTP status;
- rendered channel markup;
- delivery state.

### Заголовок, сообщение и локализация

**Решение.** Локализованные `title`/`content` **не являются обязательными canonical event fields**. Event хранит semantic type + typed data. Renderer выбирается через event-type registry и получает locale/channel capabilities.

Чтобы история отражала фактически доставленный текст, `DeliveryAttempt`/delivery projection может хранить sanitized rendered snapshot: `locale`, `rendered_title`, `rendered_body`, без secret metadata. Это отделяет неизменяемый факт от presentation.

Free-form fallback не становится универсальным escape hatch. Для developer test существует отдельный typed `notification.test.requested`; новые production events обязаны зарегистрировать schema и renderer.

### Граница метаданных

**Решение.** У каждого event type собственная typed data model. Расширение payload делается через новую optional typed field либо новую schema version. Общий произвольный `metadata: dict[str, Any]` не является публичным контрактом.

Низкоуровневый traceback, environment dump, raw config, access token, webhook URL, authorization header и неочищенный exception object в event запрещены.

## Таксономия событий

### Именование

**Решение.** Event types используют lowercase dotted names:

```text
<domain>.<entity-or-condition>.<occurrence>
```

Имя описывает факт, а не transport и не UI action. Запрещены типы вида `telegram.send`, `launcher.push` или `onepush.error` для domain occurrences.

### Версионирование

- type name остаётся стабильным при обратно совместимом расширении payload;
- несовместимое изменение typed payload увеличивает `schema_version`;
- новый type создаётся, если меняется семантика occurrence, а не только структура данных;
- routing matcher работает по semantic type и не требует изменения central backend при регистрации нового producer schema.

### Начальный реестр, выведенный из текущих источников

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
opsi.ship_exp.check_completed
opsi.ship_exp.target_reached
opsi.fleet.auto_change.completed
opsi.fleet.auto_change.failed

notification.test.requested
```

**Решение.** `campaign.stop_condition.reached` использует bounded `kind` в typed payload для `run_count`, `level`, `coin`, `new_ship` и будущих stop conditions. `opsi.ship_exp.target_reached` использует bounded `scope` (`fleet`, `custom_positions`) вместо отдельных transport-driven типов.

### Уровень серьёзности

**Решение.** Default severity принадлежит event-type descriptor registry. Например successful completion — `INFO`, recoverable runtime incident — `WARNING`, невосстановимая task/runtime failure — `ERROR`, fail-closed sensitive/unsafe state — `CRITICAL`.

Producer может задать override только если schema данного type явно разрешает contextual severity. Пользовательская конфигурация **не переписывает canonical severity**; она меняет routing/suppression threshold.

## Политика маршрутизации

### Разрешение политики

```text
NotificationEvent
    ↓
NotificationPolicyResolver
    ↓
PolicyDecision
    ├── matched rule
    ├── routed / suppressed
    ├── selected named channel ids
    ├── suppression reason
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

### Порядок обработки

**Решение.** Invalid или unregistered event никогда не записывается как `SUPPRESSED` и не попадает в normal history. Порядок обработки:

1. validate `type`, `schema_version` и typed payload по event registry;
2. проверить producer-defined `dedup_key` в той же короткой PostgreSQL transaction, в которой создаётся canonical event; повтор того же occurrence возвращает существующий event/decision вместо создания второго;
3. записать валидный canonical `NotificationEvent`;
4. если `Notifications.Global.enabled=false`, записать durable `PolicyDecision(state=SUPPRESSED, reason=global_disabled)` и **не создавать `Delivery`**;
5. выполнить ordered policy match;
6. если rule подавляет событие, записать `PolicyDecision(state=SUPPRESSED, reason=<bounded code>)` и **не создавать `Delivery`**;
7. иначе записать `PolicyDecision(state=ROUTED)` с matched rule identity/version и выбранными channel ids;
8. для каждого выбранного channel создать ровно один durable `Delivery` благодаря unique `(event_id, channel_instance_id)`;
9. disabled channel, cooldown или channel-level suppression представлены соответствующим `Delivery(state=SUPPRESSED, reason=<bounded code>)`; активный channel начинает с `PENDING`.

Так global/rule-level suppression не требует synthetic `channel_instance_id`, а channel-level suppression остаётся частью `Delivery`.

### Поведение по профилям

Per-profile override допускается, но это слой policy, а не копия transport YAML внутри каждого producer. Глобальный default остаётся source of truth; profile rule добавляется только когда реально нужен другой routing.

## Контракт каналов

### Нейтральный порт адаптера

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

### Результат

Adapter возвращает typed `DeliveryResult`:

```text
DELIVERED
TRANSIENT_FAILURE
PERMANENT_FAILURE
```

Result содержит только bounded/sanitized fields: provider message id при наличии, retry-after hint, safe error code и safe diagnostic summary. Raw response body, token, URL credentials и headers по умолчанию не сохраняются.

`SUPPRESSED` — policy/delivery state, а не результат channel send.

### Таймаут и классификация ошибок

- каждый channel имеет обязательный bounded timeout;
- timeout, connection reset, 429/5xx обычно transient;
- invalid credentials/invalid destination/unsupported payload после adapter validation — permanent, если provider contract не говорит обратное;
- provider-specific mapping инкапсулирован в adapter;
- notification dispatcher никогда не вызывает provider без timeout contract.

### Реестр каналов

Channels регистрируются по stable id/type через registry/composition root. Central notification service не импортирует Telegram/Webhook/Desktop/OnePush classes и не содержит provider switch.

Channel capabilities являются typed metadata, например max title/body length, markup mode и support для idempotency key. Renderer получает capabilities до отправки.

## Контракт хранения и outbox

### Долговечные сущности

#### `NotificationEvent`

Immutable semantic occurrence. Создаётся один раз и не меняет type/data после commit.

#### `PolicyDecision`

Одна durable запись на canonical event. Она содержит:

- `event_id`;
- `state`: `ROUTED` или `SUPPRESSED`;
- identity/version matched policy rule;
- bounded suppression reason при policy-level suppression;
- выбранные channel ids для `ROUTED`;
- `evaluated_at`;
- version/hash policy snapshot, достаточный для объяснения решения без хранения секретов.

`PolicyDecision` не имеет `channel_instance_id`. Global disable и rule-level suppression существуют только здесь. Повторная публикация того же deduplicated occurrence не создаёт второй `PolicyDecision`.

`PolicyDecision` хранится и очищается с тем же retention, что и связанный `NotificationEvent`, и показывается в history как причина отсутствия channel deliveries.

#### `Delivery`

Одна строка на `(event_id, channel_instance_id)` только для channel, выбранного успешным routing decision.

States:

```text
PENDING
IN_FLIGHT
RETRY_WAIT
DELIVERED
FAILED
SUPPRESSED
```

`FAILED` означает terminal delivery failure после permanent result или исчерпания attempts. `SUPPRESSED` здесь означает только channel-level suppression: channel disabled, cooldown либо другой bounded channel-policy reason.

#### `DeliveryAttempt`

Append-only запись каждой фактической отправки:

- attempt number;
- started/finished time;
- result class;
- sanitized adapter error code/detail;
- provider external id при наличии;
- next retry time, если применимо.

### Атомарность и настоящий transactional outbox

**Внешний источник.** AWS Transactional Outbox описывает atomic запись бизнес-изменения и outbox event в одной DB transaction и отдельно предупреждает о duplicate delivery/idempotent consumers: <https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html>.

**Решение.** Термин `transactional outbox` используется только там, где notification occurrence рождается в той же PostgreSQL transaction, что и существующее durable domain изменение. Event + PolicyDecision + Delivery rows записываются в эту же transaction, а внешняя отправка выполняется только после commit.

Для game/runtime/error occurrences, у которых нет общей PostgreSQL transaction с игровым действием, `publish()` открывает отдельную короткую durable transaction. Это обеспечивает durability **с момента публикации**, но не объявляется atomic transaction с действием в Azur Lane/эмуляторе.

Ни один external channel не вызывается внутри DB transaction источника.

### Диспетчер и конкурентность

**Решение.** Текущий масштаб не требует Kafka/RabbitMQ/Redis или отдельного notification microservice. Target baseline — PostgreSQL + один dispatcher в AzurPilot backend process/service boundary.

Schema/claim contract сразу допускает второй dispatcher: eligible rows выбираются детерминированно с row locks; PostgreSQL `FOR UPDATE ... SKIP LOCKED` допускается для queue-like table consumers. Внешний источник: <https://www.postgresql.org/docs/current/sql-select.html>.

`IN_FLIGHT` имеет lease/claimed-at boundary. После crash просроченный lease возвращается в retryable state, чтобы сообщение не зависало навсегда.

### Гарантия доставки

**Решение.** Внутренняя гарантия — **at-least-once attempt**, не exactly-once external delivery. External provider может принять запрос и потерять response, поэтому duplicate send полностью исключить невозможно без provider idempotency support.

Idempotency strategy:

- `event.id` идентифицирует occurrence;
- optional `dedup_key` задаётся producer-ом для повторного publish того же logical occurrence;
- `(profile_id, type, dedup_key)` не создаёт второй canonical event, если `dedup_key` задан и уже существует;
- delivery idempotency key формируется из stable `event_id + channel_instance_id` и передаётся adapter/provider, если channel capability это поддерживает;
- unique `(event_id, channel_instance_id)` не допускает второй `Delivery` для того же канала;
- cooldown является policy suppression и не подменяет identity/dedup.

Producer обязан включать occurrence dimension в `dedup_key`; один постоянный key для всех будущих событий одного типа запрещён.

### Повторные попытки

**Решение.** Retry принадлежит dispatcher, не producer и не WebUI.

Target defaults являются configuration defaults, а не разбросанными константами:

- `max_attempts = 5`;
- exponential backoff с jitter;
- base delay 5 s;
- cap 15 min;
- provider `Retry-After` может увеличить next-attempt time;
- permanent failure не retry-ится;
- manual requeue в будущем создаёт отдельное operator action/audit, а не обнуляет history молча.

### Порядок доставки

Глобальный total order между профилями/channels не требуется. Dispatcher выбирает pending rows по durable creation order для одного channel/profile, но retry одного delivery не блокирует независимые более новые events. Если будущий channel потребует strict ordering, это объявляется capability/policy отдельно.

### Хранение и история

**Решение.** Event + PolicyDecision + Delivery + sanitized attempt history хранится PostgreSQL и доступна WebUI через application read service. Default retention — 30 дней, configurable глобально; cleanup удаляет только terminal records старше retention и не затрагивает active retry/in-flight deliveries.

Rendered snapshot хранится только для фактических/suppressed delivery projections, без secret config.

## Надёжность доставки

1. `publish()` сначала валидирует registered event schema; invalid/unregistered event отклоняется до normal history.
2. Валидный Event, PolicyDecision и создаваемые Delivery фиксируются durable до внешней отправки.
3. Dispatcher не зависит от lifetime источника.
4. Каждая попытка имеет bounded timeout и отдельный attempt row.
5. Crash до send → lease вернёт row в retry.
6. Crash после provider accept, но до local commit → возможен duplicate; idempotency key используется, если provider умеет.
7. Permanent failure сохраняется как terminal history и не блокирует основную game task.
8. Notification subsystem failure не должен превращать успешную game task в failed task, кроме отдельно объявленного fail-closed operational requirement; текущих таких требований аудит не обнаружил.

## Граница WebUI / Desktop / VPS

### Домен и история

**Решение.** WebUI history — read projection durable notification data через application service. WebUI не является transport persistence и не владеет queue.

### Живой поток WebUI

SSE остаётся подходящим server→browser transport, но только как projection поверх durable history, а не единственное место хранения.

Для stream вводится monotonic durable `projection_seq`, scoped как минимум `profile_id` + projection type. SSE `id` содержит cursor, производный от `projection_seq`, а не UUID события.

Target reconnect invariant:

1. principal аутентифицируется и проходит object-level authorization для запрошенного `profile_id`;
2. live subscription активируется **до** фиксации snapshot/high-water mark и начинает буферизовать committed changes;
3. после активации subscription сервер фиксирует durable high-water mark `H`;
4. из PostgreSQL читается backlog `(resume_cursor, H]`;
5. во время чтения backlog новые live changes буферизуются;
6. после backlog буфер дедуплицируется по `projection_seq`: `<= H` отбрасываются, `> H` выдаются по порядку;
7. если конкретный backend не может гарантировать subscription-before-snapshot, перед переходом в live mode сервер обязан повторно прочитать durable gap `(last_emitted_seq, current_high_water]`;
8. только после закрытия gap поток переходит в steady-state live delivery.

Так событие, committed между чтением backlog и подключением live feed, потерять нельзя.

Несколько browser consumers имеют независимые cursors/streams и не делят destructive queue.

### Desktop/Launcher как канал

**Решение.** Desktop/Launcher — отдельный `NotificationChannel`, а не `/api/notify` side effect.

Для будущего VPS split:

```text
Windows PC / Desktop Agent
       │
       └── outbound authenticated HTTPS connection ──► AzurPilot backend/VPS
                                                       │
                                                       ├── durable delivery backlog
                                                       └── live server→agent stream
```

Предпочтительный transport contract Stage 4: authenticated outbound SSE для server→agent stream + authenticated HTTPS ACK endpoint для `delivery_id`. Агент не открывает домашний inbound port.

Desktop stream использует тот же no-gap invariant, что и WebUI: subscription-before-high-water либо durable gap reread. Его cursor относится к durable Desktop deliveries, а ACK означает успешный локальный OS/launcher handoff, не просто получение сетевого payload.

Если при реализации окажется, что Desktop нуждается в существенном bidirectional realtime control помимо ACK, это отдельный gate в пользу WebSocket; notification architecture от этого не меняется.

### Поток WebUI не равен каналу Desktop

Browser WebUI live stream показывает history/projection и не создаёт отдельную external `Delivery` row на каждый browser tab. Desktop Agent является реальным delivery channel и имеет delivery/ack state.

Существующий `/api/launcher/*` control plane не является notification channel автоматически. Его migration допускается только если отдельный аудит launcher protocol докажет необходимость изменения.

### Граница MCP

**Решение.** Ни Dev MCP, ни Game MCP не являются notification transport.

- Dev MCP может в будущем давать диагностический read-only status/health платформы.
- Game MCP при необходимости может выдавать только sanitized notification history DTO через application boundary.
- config secrets, raw provider responses и sensitive metadata в MCP не выдаются.

## Безопасность и секреты

### Текущие находки

- `Error.OnePushConfig` и `OpsiGeneral.OpsiOnePushConfig` могут содержать provider credentials в YAML string.
- Read-only Game MCP уже redacts эти поля; их нельзя считать обычным non-sensitive config.
- legacy rendered notification для sensitive task включает строковое представление ошибки; такой текст потенциально может содержать лишнюю диагностическую информацию.
- raw notification API routes не имеют собственного auth gate; reachability зависит от bind/topology.

### Целевой контракт

1. Channel secret принадлежит channel configuration/secret resolver, не event и не policy.
2. Event/history никогда не хранит token/password/private key/authorization header/raw secret config.
3. Diagnostic error detail имеет bounded sanitized code + summary; raw response body не сохраняется по умолчанию.
4. WebUI history/API требует authentication **и object-level authorization по `profile_id`** независимо от bind address.
5. Principal имеет явный набор разрешённых profiles/scopes; list/get/history/live-stream/resume операции обязаны применять этот scope на server side до чтения данных.
6. Resume cursor всегда привязан к разрешённому `profile_id`/projection scope. Cursor из одного profile нельзя использовать для чтения другого profile; server-side query всегда добавляет authorization predicate, а не доверяет profile/cursor от клиента.
7. Channel-delivery read/ack endpoints проверяют одновременно principal, `profile_id`, `channel_instance_id` и конкретный `delivery_id`; доступ по одному user-controlled ID запрещён.
8. Desktop Agent имеет отдельный revocable credential с минимальным scope: stream/ack только для разрешённых profile/channel deliveries.
9. Browser session credential и Desktop credential не взаимозаменяемы.
10. Sensitive events по умолчанию скрывают/редактируют sensitive `data` fields в WebUI/MCP projection; renderer получает только разрешённую projection.
11. Logs не печатают channel config целиком.

### Будущий канал Webhook

Webhook URL следует считать sensitive destination data. Production Webhook adapter обязан:

- принимать только `https://` destinations; plain HTTP и HTTPS→HTTP downgrade запрещены;
- redirects держать выключенными по умолчанию;
- если redirects явно разрешены, проверять **каждый** hop заново: scheme остаётся HTTPS, DNS re-resolution выполняется перед соединением, destination повторно проходит allowed-network policy;
- cross-origin redirect в baseline запрещать; если когда-либо появится отдельный явно разрешённый cross-origin режим, authentication headers, cookies, signatures и другие credentials не должны переноситься на новый origin;
- никогда не пересылать credential-bearing headers на origin, не совпадающий с разрешённым origin назначения;
- запрещать loopback/private/link-local/metadata destinations по умолчанию;
- trusted-local allowlist, если он действительно нужен, делать отдельной конфигурацией, но не использовать его для ослабления HTTPS requirement production Webhook adapter;
- ограничивать request/response body size;
- не логировать credentials/query secrets;
- подписывать payload отдельным secret, если receiver contract это требует.

Это закрывает основной SSRF/secret leakage risk до появления production Webhook adapter.

## Решение по OnePush

**Внешний источник.** OnePush предоставляет общий Python provider layer для Bark, Discord, Telegram, ServerChan, WeChat, pushplus, go-cqhttp, Qmsg, DingTalk, Lark, SMTP и Custom providers: <https://github.com/y1ndan/onepush>.

**Решение.** OnePush **не является фундаментом target architecture и после полного cutover не нужен**. Он может существовать только как временный channel adapter в migration window, чтобы не ломать реально используемый provider до готовности first-class adapter.

**Критерий решения перед удалением dependency.** Нужно получить sanitized inventory только provider names из пользовательских profile configs без credentials. Для каждого реально используемого provider должно быть одно из двух:

1. first-class AzurPilot channel adapter готов и проверен;
2. пользователь явно отказался от этого provider.

После выполнения gate удаляются OnePush adapter, `handle_notify`, YAML provider config и `onepush==1.2.0` одной migration итерацией. Долгоживущий compatibility wrapper не остаётся.

## Матрица миграции

| Сейчас | Цель | Этап | Условие удаления / завершения |
| --- | --- | --- | --- |
| `module/notify/notify.py::handle_notify` | `NotificationPublisher` → durable Delivery → Channel registry | Stage 3–4 | все production/developer callsites migrated |
| `module/notify/notify.py::notify_webui` | Desktop channel + WebUI history/live projection | Stage 4 | launcher/desktop consumer cutover и ACK contract проверены |
| `module/notify/__init__.py` lazy wrappers | application notification package exports only | Stage 4 | legacy functions не импортируются |
| `alas.py` generic task-result `handle_notify` | typed `task.completed/recovered/failed` publish | Stage 4 | scheduler tests переведены на canonical events |
| `alas.py` sensitive task error path | `task.failed` + `sensitivity=SENSITIVE` + cause | Stage 4 | fail-closed behavior и redaction tests PASS |
| `alas.py` game unavailable/stuck/client-error paths | `runtime.game.*` + recovery events | Stage 4 | recovery tests assert events, не transports |
| `alas.py` emulator recovery paths | `runtime.emulator.*` / `runtime.recovery.*` | Stage 4 | emulator recovery tests assert events + delivery non-blocking |
| `alas.py` fatal Script/Page/HumanTakeover/unhandled paths | typed fatal events | Stage 4 | fatal exit behavior unchanged and events durable before process exit |
| `alas.py` repeated task failure limit | `task.failure_limit.reached` | Stage 4 | scheduler limit behavior unchanged |
| campaign direct `handle_notify` callsites | `campaign.stop_condition.reached` typed kind | Stage 4 | all stop-condition notifications routed by policy |
| GemsFarming fast-forward direct OnePush | `campaign.auto_search.configuration_failed` | Stage 4 | direct import removed |
| Commission direct OnePush + WebUI | `commission.reward.received` + renderer | Stage 4 | reward tests cover event/data/render policy |
| 14 прямых `self.notify_push(...)` sinks в `scheduling.py`, `hazard_leveling.py`, `fleet_auto_change.py` | Opsi producers publish typed events | Stage 4 | **все 14** мигрированы или доказанно мёртвый helper удалён; repo-wide search не оставляет прямых legacy sinks |
| Opsi producer-local notification cooldown flags | durable policy suppression/dedup | Stage 4 | behavior parity tests на thresholds/cooldown |
| `Scheduler.PushNotification` | Notifications policy for task events | Stage 2 config + Stage 4 cutover | no reader in scheduler |
| EventShop semantic override `Scheduler.PushNotification` | explicit failure policy matcher for EventShop subject | Stage 2/4 | `notification_policy.py` transport override удалён |
| `Error.OnePushConfig` | named channel config + secret refs | Stage 2/3 | no legacy external caller; provider gate complete |
| `OpsiGeneral.NotifyOpsiMail` | `opsi.*` policy external channels | Stage 2/4 | no `notify_push` reader |
| `OpsiGeneral.LauncherPush` | `opsi.*` policy Desktop channel | Stage 2/4 | Desktop channel cutover |
| `OpsiGeneral.IndependentPush` | policy selects named channel instance | Stage 2/4 | dedicated provider config migrated |
| `OpsiGeneral.OpsiOnePushConfig` | optional named channel instance secret/config | Stage 2/3 | no reader and provider migrated |
| `CommissionNotifyReward` | policy rule | Stage 2/4 | commission event emitted regardless of delivery preference |
| `CommissionNotifyRewardStatistics` | presentation option for commission renderer | Stage 2/4 | legacy string composition removed |
| `/api/notify` POST | удалить как internal transport endpoint | Stage 4 | no `notify_webui` callers; Desktop Agent uses channel endpoint |
| `_notification_queue` | durable PostgreSQL delivery/history | Stage 3/4 | all live streams backed by durable cursor |
| `/api/notify_stream` destructive SSE | authenticated, profile-authorized, resumable WebUI projection stream | Stage 4 | old launcher consumer migrated; no-gap reconnect tests PASS |
| `module/webui/app_developer_tools.py` direct Error.OnePush test | dev-only typed test publish/channel health use case | Stage 4 | no direct legacy import |
| `dev_tools/cyclic_notify.py` | bounded dev harness через application API либо удалить | Stage 4 | canonical developer test exists |
| tests patching `handle_notify`/`notify_webui` | event publisher/policy/channel contract tests | Stage 2–4 | no legacy patch target remains |
| translation structural gate special-casing legacy notify syntax | registry/event renderer-aware structural rules либо удалить obsolete checks | Stage 4 | old syntax no longer exists |
| `pyproject.toml` `onepush==1.2.0` | first-class adapters only | final cutover | sanitized provider inventory gate complete |
| generated/template/i18n legacy notification keys | generated `Notifications` model | Stage 2–4 | source YAML migrated and generators produce no legacy keys |
| bridge generated `Error.OnePushConfig` compatibility fields | remove/regenerate if bridge source still inherits shared Error group | final cutover | repo-wide search shows no required legacy reader |
| docs/UI copy mentioning OnePush/legacy push | target channel/policy terminology | final cutover | user-visible configuration cutover complete |

## Зафиксированные решения

Stage 1 закрывает следующие решения:

1. **Boundary:** notification domain/application API находится в `module.application`; transports и persistence — adapters.
2. **Canonical event:** immutable typed event с identity/type/version/source/profile/subject/severity/time/typed data/dedup/provenance/sensitivity.
3. **Type versioning:** dotted semantic type + отдельный integer schema version.
4. **Severity:** semantic default в event registry; config влияет на routing, не переписывает факт.
5. **Routing:** ordered config rules, first-match-wins, registry channels; no event-specific backend `if/elif`.
6. **Policy persistence:** один durable `PolicyDecision` на event; global/rule suppression не создаёт synthetic channel delivery.
7. **Channels:** neutral typed port + capabilities + typed delivery result.
8. **Delivery result:** delivered / transient failure / permanent failure; channel suppression — state `Delivery`.
9. **Durability:** PostgreSQL Event + PolicyDecision + Delivery + Attempt; external send после commit.
10. **Transactional outbox:** только для occurrences, записываемых в одну DB transaction с domain mutation; runtime events получают собственную durable transaction.
11. **Reliability:** at-least-once attempts, stable idempotency key, dispatcher-owned retry, producer-defined occurrence dedup identity.
12. **History:** PostgreSQL-backed, 30-day configurable default, sanitized rendered delivery snapshot.
13. **Reconnect:** backlog→live transition обязан быть no-gap через subscription-before-high-water либо durable gap reread.
14. **Authorization:** history/live/resume/delivery endpoints применяют object-level authorization по `profile_id`.
15. **OnePush:** transitional adapter only; после полного cutover удалить.
16. **Desktop:** first-class channel, не WebUI queue side effect.
17. **VPS split:** Windows Agent инициирует outbound authenticated connection; home inbound port не требуется.
18. **Legacy removal:** `handle_notify`, `notify_webui`, current notify API/queue, OnePush configs/dependency и все 14 прямых Opsi `notify_push` sinks удаляются по migration gates.
19. **Stage 2:** typed core contracts, event registry, policy/config model и unit-level contracts; без преждевременного producer cutover.

## Внешние источники

Эти источники используются как design references, не как обязательные зависимости:

- CloudEvents spec: <https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md>. Полезны идеи `id`, `source`, `type`, `subject`, `time`; AzurPilot не обязан становиться CloudEvents transport implementation.
- AWS Transactional Outbox: <https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html>.
- PostgreSQL locking/`SKIP LOCKED`: <https://www.postgresql.org/docs/current/sql-select.html>.
- WHATWG SSE: <https://html.spec.whatwg.org/dev/server-sent-events.html>.
- MDN SSE: <https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events>.
- OnePush: <https://github.com/y1ndan/onepush>.

CloudEvents остаётся reference envelope vocabulary. В частности, AzurPilot берёт идею стабильной identity/source/type и времени occurrence, но не принимает unrestricted extension attributes как замену typed event schemas.

## Явно исключено из Stage 1

Stage 1 намеренно:

- не создаёт production `NotificationService`/dispatcher;
- не добавляет PostgreSQL migration/outbox table;
- не добавляет Kafka/RabbitMQ/Redis;
- не создаёт notification microservice или отдельный server;
- не реализует Telegram/Webhook/Desktop adapters;
- не удаляет OnePush;
- не меняет `handle_notify` или `notify_webui`;
- не меняет `/api/notify`/`/api/notify_stream`;
- не меняет `/api/launcher/*` control protocol;
- не меняет generated config/i18n;
- не мигрирует producers;
- не реализует Windows Agent;
- не использует MCP как notification transport;
- не исправляет найденный `Custom` OnePush risk в рамках docs-only Stage 1.

## Критерии входа в Stage 2

Stage 2 — **Notification Core и единая конфигурационная модель** — может начинаться, когда:

1. этот архитектурный документ принят как contract;
2. Stage 1 PR остаётся docs-only и CI/CodeRabbit не обнаружили неразобранных фактических противоречий;
3. `module.application` dependency direction сохраняется;
4. Stage 2 не реализует provider adapters раньше typed event/policy contracts;
5. source-of-truth новой `Notifications` config определяется в existing argument/generator pipeline, а не отдельным ручным JSON;
6. event registry и payload schemas имеют тесты на type/version/validation;
7. policy resolver имеет tests на priority, default, global disable, per-profile match, channel disable и suppression;
8. persistence contract имеет тесты на `PolicyDecision`, channel-level `SUPPRESSED` и unique `(event_id, channel_instance_id)`;
9. public application DTO не использует unrestricted `dict[str, Any]`;
10. secrets остаются вне events/history/tests fixtures;
11. WebUI/agent stream contract имеет no-gap reconnect tests и object-level authorization tests;
12. migration code не создаёт долгоживущий parallel notification stack.

### Конкретный объём Stage 2

Stage 2 должен реализовать только foundation, необходимый следующим stages:

- typed `NotificationEvent` и registered payload schemas для initial taxonomy;
- event type descriptor registry;
- typed `NotificationPolicy`/`PolicyDecision` model и deterministic resolver;
- neutral `NotificationChannel` port/result/capabilities;
- source-of-truth `Notifications` configuration schema через существующий generator pipeline;
- unit tests и migration mapping tests для legacy config semantics;
- interfaces для persistence/publisher, если они нужны для dependency direction, без преждевременной внешней доставки.

PostgreSQL tables/dispatcher и реальные channels остаются следующей реализационной стадией, чтобы Stage 2 не смешивал core contract с network/provider behavior.
