# Платформа уведомлений AzurPilot

Статус документа: архитектурный контракт Stage 1 и фактическая граница
реализации Stage 2. Stage 2 добавляет только durable typed foundation;
production adapters, producer cutover и handover wiring остаются будущими
этапами.

## 1. Контекст аудита и границы

### [Факт] База и источник истины

Аудит выполнен 2026-09-09 от exact base:

| Поле | Значение |
| --- | --- |
| Репозиторий | AliceLiddell01/AzurPilot-private-Ru |
| Base branch | codex/mcp-profile-runtime-handover |
| Base SHA | 33e3de3be85c5772a0fb62c2052dabf3dfade923 |
| Рабочая ветка Stage 1 | codex/notification-platform-architecture |
| Production migration head | 0008_dorm_morale_idempotency |

Источники фактического поведения проверялись в текущем коде и тестах. Старые
версии этого документа, прежние SHA и исторические PR используются только как
контекст и не переопределяют текущий код.

### [Решение] Различение запроса и прикреплённого контракта

Прямая просьба пользователя — выполнить работу по приложенному промту.
Файл Stage_1_Notification_Platform_Architecture_Prompt.md задаёт технический
контракт этой работы: Stage 1 ограничен аудитом и архитектурным документом,
требует Draft PR и запрещает production implementation новой платформы,
перевод PR в Ready и merge без отдельной команды. Ниже описаны решения,
принятые в рамках этого контракта; они не являются утверждением, что
перечисленные будущие компоненты уже существуют.

### [Решение] Язык утверждений

Каждое существенное утверждение помечено одним из ярлыков:

- [Факт] — проверенное состояние текущей ветки;
- [Решение] — целевой контракт, который должен быть реализован на будущих этапах;
- [Внешний design reference] — принцип из официальной внешней спецификации;
- [Открытый критерий/риск] — вопрос, который требует будущей реализации,
  измерения или live evidence.

## 2. Текущая notification system

### [Факт] Legacy OnePush boundary

module/notify/notify.py предоставляет два несвязанных процедурных API:

1. handle_notify(_config, **kwargs) разбирает YAML, выбирает provider через
   onepush.get_notifier и синхронно вызывает notifier.notify;
2. notify_webui(instance, title, content, **kwargs) отправляет HTTP POST на
   loopback WebUI.

В pyproject.toml закреплён onepush==1.2.0. handle_notify не имеет собственной
durable persistence, общей retry/backoff политики, deduplication identity или
correlation с runtime/task. Provider configuration, включая credentials,
передаётся как YAML-строка.

Текущая проверка ответа не образует общего delivery contract:

- для requests.Response успехом считается HTTP 200;
- специальная обработка gocqhttp читает JSON status;
- другие значения, возвращённые provider, могут считаться успехом;
- OnePushException и прочие исключения переводят вызов в bool False;
- timeout и retry зависят от provider и не нормализуются общей границей.

В ветке также выявлен отдельный production finding, который Stage 1 только
фиксирует: ветка Custom в handle_notify при отсутствии поля data обращается к
config["data"] внутри проверки и может завершиться KeyError. Исправление
намеренно не входит в этот documentation-only этап.

### [Факт] Local WebUI transport

notify_webui:

- читает State.deploy_config.WebuiPort, при ошибке использует 25548;
- обращается только к http://127.0.0.1:PORT/api/notify;
- использует timeout 2 секунды;
- возвращает True, если requests.post не бросил исключение;
- не проверяет HTTP status и response body.

Из этого следует ложноположительное подтверждение: HTTP 4xx/5xx может быть
представлен вызывающему коду как успешная отправка.

module/webui/api.py реализует:

- POST /api/notify — принимает произвольный JSON и кладёт его в
  process-local asyncio.Queue;
- GET /api/notify_stream — выдаёт элементы через SSE и удаляет их из очереди
  вызовом queue.get().

У текущего потока нет event id, history, cursor/resume, ACK, durable backlog,
schema validation или broadcast fan-out. Несколько consumers конкурируют за
один destructive queue, а не получают независимую проекцию. В самих
notification endpoints нет той же проверки is_local_request, которая
применяется к чувствительным /api/launcher/* операциям. Consumer
/api/notify_stream в Python call graph репозитория не найден; его фактический
внешний клиент не подтверждён текущим кодом.

### [Факт] Runtime и handover

module/application/runtime_handover.py уже содержит application-level enum
NotificationOutcome:

| Текущее значение | Фактический смысл |
| --- | --- |
| ACCEPTED | boundary приняла работу, но пользовательская доставка не доказана |
| DELIVERED | вызывающий hook утверждает подтверждённую доставку |
| FAILED | отправка завершилась ошибкой |
| UNAVAILABLE | notification boundary недоступна |

ProfileHandoverCoordinator продолжает handover только при точном
NotificationOutcome.DELIVERED. При ACCEPTED, FAILED или UNAVAILABLE он
возвращает RUNTIME_HANDOVER_NOTIFICATION_FAILED, не запрашивает quiesce и не
останавливает текущий worker.

WebUIRuntimeControlOwner при отсутствии injected notifier вызывает legacy
notify_webui. True этого вызова переводится в ACCEPTED с явным комментарием,
что enqueue не равен доставке пользователю. State.init() создаёт owner без
production notifier injection, поэтому текущий fallback не закрывает busy
handover.

### [Факт] PR #177 и его blocker

На момент аудита PR #177:

| Поле | Значение |
| --- | --- |
| URL | https://github.com/AliceLiddell01/AzurPilot-private-Ru/pull/177 |
| State | OPEN, Draft |
| Head | codex/mcp-profile-runtime-handover at 33e3de3be85c5772a0fb62c2052dabf3dfade923 |
| PR base | personal/stable at cf54dfb3901c8d1ab8374840549db7deaa29b2f8 |
| Review decision | CHANGES_REQUESTED |
| CI contexts | Python, Windows, Security — SUCCESS на момент проверки |

Текущий blocker не является обычной ошибкой текста уведомления. Для busy
handover нет доказанной цепочки active consumer -> authenticated receipt ->
ACK -> NotificationOutcome.DELIVERED. Исторический idle acceptance или
простой вызов notify_webui не доказывает busy delivery. PR body также содержит
устаревший tested SHA, поэтому этот документ опирается на live head и текущий
код.

### [Факт] Текущая persistence и observability

module/persistence — каноническая PostgreSQL infrastructure boundary. В ней
есть lazy QueuePool на PID, StorageHealthChecker, короткие Unit of Work и
единственный Alembic schema path. Роли разделены на azurpilot_owner,
azurpilot_migrator и azurpilot_app; grant-app.sql выдаёт приложению права в
schema azurpilot без предоставления DDL-владения.

RuntimeStorageService уже хранит часть runtime state в PostgreSQL. В частности,
AP notification checkpoint имеет typed API get_ap_notification/set_ap_notification
и коммитится короткой транзакцией. Cooldown/attempt timestamps и часть Opsi
suppression остаются в config/process state.

В текущей schema нет notification event, policy, delivery или attempt tables.
Существующие FOR UPDATE и FOR UPDATE SKIP LOCKED применяются к другим
queue-like операциям, например fleet manual scan, а не к уведомлениям.
LISTEN/NOTIFY в notification runtime не используется.

infrastructure/observability предоставляет локальный Compose-контур PostgreSQL,
Alloy OTLP, Loki, Prometheus, Tempo, Grafana и Caddy. Grafana MCP остаётся
read-only diagnostic consumer и не является частью delivery path.

## 3. Инвентаризация producers и конфигурации

### [Факт] Полный production producer inventory

AST/поиск вызовов по текущей ветке выявил следующие production sources:

| Source | Occurrence / условие | Текущий путь |
| --- | --- | --- |
| alas.py, _check_sensitive_exit | Sensitive task exception | OnePush через Error_OnePushConfig, затем WebUI, процесс завершается |
| alas.py, AzurLaneAutoScript.run | GameNotRunningError | OnePush + WebUI, планируется Restart |
| alas.py, run | GameStuckError / GameTooManyClickError | предупреждение; затем game recovery, предел recovery, emulator recovery или terminal failure; ветви используют OnePush и/или WebUI |
| alas.py, run | GameBugError | OnePush + WebUI, планируется Restart |
| alas.py, run | GamePageUnknownError при доступном server | OnePush + WebUI, fail-closed exit |
| alas.py, run | ScriptError, EmulatorNotRunningError, RequestHumanTakeover, generic exception | OnePush + WebUI в соответствующих terminal/recovery ветвях |
| alas.py, loop | task success, recoverable result или failure при Scheduler_PushNotification | OnePush для итогового состояния; WebUI здесь не вызывается |
| alas.py, loop | повторные failure или strict sensitive stop | OnePush + WebUI, terminal exit |
| module/campaign/run.py, CampaignRun.triggered_stop_condition | run count, reach level, new ship | OnePush через Error_OnePushConfig |
| module/campaign/campaign_event.py, coin_limit_triggered | coin limit reached | OnePush, после task delay |
| module/handler/fast_forward.py, handle_auto_search_setting | GemsFarming не может применить auto-search setting | OnePush; при False выбрасывается AutoSearchSetError и задача отключается |
| module/commission/commission.py, _record_commission_income | tracked Gem reward при включённой reward notification | сначала PostgreSQL record_commission_income, затем OnePush + WebUI |
| module/os/tasks/scheduling.py, CoinTaskMixin.notify_push | smart scheduling events и resource/task conditions | локальный launcher push и/или OnePush; возвращается OR двух bool |
| module/os/tasks/scheduling.py, AP helpers | AP changed, insufficient coins/AP, no coin task, proxy suppression | notify_push с process/config cooldown и PostgreSQL AP checkpoint |
| module/os/tasks/hazard_leveling.py | low AP, ship data failure, report, fleet/custom position target reached | 5 прямых self.notify_push sinks |
| module/os/tasks/fleet_auto_change.py | auto-change completed/failed | 2 прямых self.notify_push sinks |

Внутри Opsi найдено ровно 14 прямых self.notify_push sinks: 7 в
scheduling.py, 5 в hazard_leveling.py и 2 в fleet_auto_change.py. В
scheduling.py сам notify_push дополнительно выбирает два транспорта и скрывает
их независимые результаты за bool.

### [Факт] Developer/test sources и boundary calls

WebUI developer tool _test_notify_error вызывает handle_notify напрямую.
dev_tools/cyclic_notify.py бесконечно вызывает handle_notify с test payload
каждые 0.5 секунды; это не production domain occurrence, но он может создавать
реальный внешний push и должен быть отдельно мигрирован или удалён на будущем
этапе.

runtime_control_owner.py вызывает notify_webui как handover fallback. Это не
producer игрового события, а критическая application boundary, которую нельзя
оставлять неявно подключённой к legacy transport после cutover.

Текущие тестовые seams:

- tests/test_scheduler_core_runtime_messages.py и
  tests/test_alas_error_handling.py patch-ят alas.handle_notify и
  alas.notify_webui;
- tests/test_application_runtime_handover.py проверяет, что ACCEPTED не
  проходит handover;
- tests/test_webui_runtime_control_owner.py использует injected notifier и
  NotificationOutcome.DELIVERED;
- tests/test_shared_webui_localization_contracts.py проверяет наличие
  notification routes.

### [Факт] Config inventory

Источником пользовательской конфигурации остаётся
module/config/argument/argument.yaml. Производные args.json,
config_generated.py, menu.json и template.json Stage 1 не редактирует.

| Config key | Текущий смысл | Текущее состояние |
| --- | --- | --- |
| Scheduler.PushNotification | итоговое уведомление scheduler | default false |
| Error.OnePushConfig | YAML provider config для ошибок и большинства producer’ов | sensitive; default provider: null |
| OpsiGeneral.NotifyOpsiMail | включение OnePush в Opsi | default true |
| OpsiGeneral.LauncherPush | включение локального launcher path в Opsi | default true |
| OpsiGeneral.IndependentPush | выбор отдельного Opsi provider config | default false |
| OpsiGeneral.OpsiOnePushConfig | отдельный Opsi YAML provider config | default provider: null |
| Commission.CommissionNotifyReward | уведомлять о tracked commission rewards | default false |
| Commission.CommissionNotifyRewardStatistics | добавлять reward statistics в текст | default true |
| EventShop.Scheduler.PushNotification | legacy permission только для error push EventShop | читается через special notification_policy |
| Deploy.WebuiPort | loopback port WebUI, куда отправляется notify_webui | default fallback 25548 |

module/shop_event/notification_policy.py специально переписывает общий
Scheduler_PushNotification и при выключенном EventShop push подставляет
Error_OnePushConfig=provider: null. Это существующая semantic overload, которую
будущая policy model должна заменить явным event/policy rule, не меняя её
смысл на Stage 1.

ActionPointNotifyLevels встречается в комментарии/описании
module/os/tasks/scheduling.py, но актуального generated user setting с таким
canonical key аудит не обнаружил. Его нельзя переносить в новую конфигурацию
как будто это существующий пользовательский параметр.

Opsi AP state неоднороден: предыдущий AP хранится через PostgreSQL
RuntimeStorageService, а minimum interval, last attempt и suppression flags
частично живут в config/process memory. Это нельзя считать общей durable
notification idempotency.

## 4. Текущие проблемы и Stage 1 findings

### [Факт] Нерешённые ограничения

1. Producer знает про OnePush, YAML, WebUI или presentation text.
2. Нет единого immutable event identity и deduplication contract.
3. Local queue теряется при process restart и распределяет событие
   destructive competition между SSE consumers.
4. notify_webui не различает transport success и HTTP failure.
5. Нет durable delivery/attempt/lease state и общего retry policy.
6. Handover fallback маппит enqueue в ACCEPTED, а не в DELIVERED.
7. Config keys одновременно описывают событие, policy и transport.
8. Secrets смешаны с provider selection и payload boundary.
9. Suppression/cooldown размазаны по producer-specific коду.
10. История и live projection не имеют profile authorization и resume cursor.
11. Нет доказанного active consumer для PR #177 busy acceptance.
12. Custom OnePush branch имеет отмеченный выше потенциальный KeyError.

### [Решение] Что именно Stage 1 закрывает

Этот документ фиксирует vocabulary, dependency direction, durable state model,
handover proof, transport decision, failure semantics, migration order и
acceptance gates. Он не утверждает, что какая-либо из этих сущностей уже
реализована в production.

## 5. Целевая архитектура

### [Решение] Dependency direction и publish boundary

Целевая логическая boundary располагается в нейтральном
module.application.notifications:

~~~text
domain/runtime producers
        |
        v
application NotificationPublisher port
        |
        v
typed validation -> dedup -> policy resolution
        |
        +--> module.persistence PostgreSQL repositories
        |
        +--> dispatcher -> NotificationChannel adapters
        |
        +--> WebUI history/live projection
        |
        +--> handover delivery-proof use case
~~~

Producer зависит только от application port/use case. Он не импортирует
Telegram, WebSocket, OnePush, HTTP endpoint, requests, retry implementation,
DB polling или Grafana. Composition root связывает application port с
persistence и adapters. Имена Python packages/classes уточняются на этапе
реализации и не должны создавать production implementation в Stage 1.

### [Решение] Application API

Целевой API должен разделять обычную публикацию и критический handover proof:

~~~text
publish(event: NotificationEvent) -> PublishResult
publish_for_handover(event, deadline) -> HandoverNotificationResult
~~~

PublishResult — typed result, а не bool:

~~~text
persisted | duplicate | suppressed | identity_conflict |
validation_failed | unavailable
~~~

`identity_conflict` означает, что существующая occurrence identity получила
другие immutable fields или payload_digest; обычный caller не повторяет такую
публикацию. `unavailable` означает временно недоступную durable boundary и
может быть повторён только с тем же occurrence identity. `duplicate`,
`suppressed` и `validation_failed` не являются успешной доставкой и не
требуют provider retry. Обычная публикация не обещает, что человек прочитал
сообщение.
Handover use case ждёт только до переданного bounded deadline и возвращает
NotificationOutcome для существующего runtime contract. Он не знает, каким
каналом получен ACK.

Отдельный synchronous wait не должен вызывать provider внутри DB transaction.
Сначала фиксируется durable event/policy/delivery, затем dispatcher выполняет
внешнюю работу и обновляет state.

## 6. Canonical event model

### [Решение] Immutable typed NotificationEvent

Canonical event — immutable typed DTO факта occurrence:

| Поле | Семантика |
| --- | --- |
| id | UUID identity occurrence; повторная попытка сохраняет тот же id |
| source | стабильный доменный producer scope, например scheduler, campaign, opsi или runtime |
| type | стабильное lowercase dotted semantic event type |
| schema_version | положительное целое для typed payload version |
| profile_id | canonical AzurPilot profile/instance scope; не секрет |
| runtime_instance_id | необязательная эфемерная process/session identity для диагностики |
| subject | typed reference с bounded kind и id на task/campaign/fleet/resource |
| severity | INFO, WARNING, ERROR или CRITICAL |
| occurred_at | timezone-aware UTC время фактического occurrence |
| persisted_at | server-side UTC время успешной durable записи |
| profile_sequence | server-assigned monotonic sequence within profile; durable cursor/history order |
| data | concrete registered typed payload для type + schema_version |
| dedup_key | optional stable logical-occurrence key, scoped by source/profile/type |
| correlation | typed task/runtime/trace/causation references |
| sensitivity | NORMAL или SENSITIVE; влияет на projection/logging, но не разрешает secrets |
| payload_digest | digest canonical immutable identity + typed payload; server-computed and immutable |

Identity deduplication определяется как idempotent combination of source + id
для повторной публикации события и, если producer знает логическую операцию,
source + profile_id + type + dedup_key для повторного occurrence. Для каждой
такой identity application boundary сравнивает immutable event fields и
payload_digest. Exact match возвращает уже существующие event/decision/delivery
без republish; mismatch возвращает bounded error identity_conflict и ничего не
публикует. Arbitrary hash от title/body не является dedup identity. Один
occurrence может иметь несколько deliveries, но один event не должен порождать
второй canonical row из-за retry publisher.

### [Решение] Source, subject и correlation

source идентифицирует область, где произошёл факт, а не транспорт. subject
идентифицирует объект в этой области и не содержит свободного произвольного
JSON.

correlation является отдельным typed value object и может содержать:

- task_id или bounded task reference;
- runtime_session_id;
- parent event/causation id;
- trace_id и span_id для observability correlation.

trace_id/span_id никогда не заменяют event id, delivery id или ownership
identity. High-cardinality IDs живут в structured logs, traces и history, но
не в Prometheus labels.

### [Решение] Occurrence и transport/presentation boundary

К canonical event относятся type, typed data, occurrence time, source, subject,
profile и correlation. К delivery относятся channel id, provider credentials,
attempt number, lease, retry timestamps, HTTP status, provider response и
rendered channel markup.

title/body не обязательны в canonical event. Renderer получает typed event,
locale и channel capabilities. Чтобы history показывала фактически
подготовленную presentation, delivery сохраняет sanitized rendered snapshot:
locale, renderer_version, rendered_title и rendered_body. Snapshot не должен
содержать token, raw config, credentials, unrestricted traceback или
непроверенный exception object.

### [Внешний design reference] CloudEvents vocabulary

CloudEvents полезен как нейтральная терминология для id, source, type, subject,
time и разделения occurrence data от context metadata. AzurPilot не становится
автоматически CloudEvents implementation, не обязан использовать CloudEvents
wire format и не получает unrestricted extension metadata:
<https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md>.

### [Решение] Schema evolution

- совместимое добавление optional typed field сохраняет type и меняет
  schema_version только по явно принятому compatibility policy;
- несовместимое изменение data увеличивает schema_version;
- изменение смысла occurrence создаёт новый type;
- renderer и policy descriptor регистрируются вместе с type/version;
- общий metadata: dict[str, Any] не является публичным escape hatch;
- malformed, unregistered или schema-incompatible event отклоняется до
  durable normal history.

Для rejected event сохраняются только bounded reason и correlation-safe
diagnostics; raw payload не пишется в logs/traces.

## 7. Event taxonomy и severity

### [Решение] Initial semantic taxonomy

Ниже initial candidate registry выведен из текущих producer’ов. Это registry
семантики, а не if/elif dispatcher и не hardcoded список будущих маршрутов.

~~~text
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
runtime.handover.preemption_requested
~~~

campaign.stop_condition.reached использует bounded payload kind:
run_count, level, coin или new_ship. opsi.ship_exp.target_reached использует
bounded scope fleet или custom_positions. Новые producer’ы расширяют registry
через typed descriptor, а не через транспортное имя.

### [Решение] Typed descriptor для handover preemption

Target descriptor `(runtime.handover.preemption_requested, schema_version=1)`
имеет отдельные validator, policy descriptor и renderer descriptor. Его
минимальный typed payload:

~~~text
operation_id       required bounded handover operation identity
source_profile_id  required canonical profile id; must equal event.profile_id
owner_epoch        required authoritative ownership epoch/version
session_id         optional bounded runtime session reference
current_task       optional typed subject reference, never raw task payload
reason_code        required bounded enum/code for the preemption cause
deadline_at        required UTC deadline, within caller deadline and <= 300 sec
~~~

Payload использует canonical encoding и ограничен initial target 4 KiB; строки
имеют индивидуальные bounded limits, а каждая вложенная mapping/sequence также
ограничена 256 items до полной сериализации. Свободные dict/list и raw
exception, device id, screenshot, credentials и config запрещены. `occurred_at` является
requested time и не дублируется в data. `dedup_key` равен operation_id, поэтому
retry publisher возвращает exact existing result, а изменение любого
immutable field или payload_digest даёт identity_conflict без republish.

Policy descriptor помечает событие handover-critical и выбирает только
profile-bound Desktop Agent receipt path; suppression или unavailable всегда
дают fail-closed HandoverNotificationResult и никогда не становятся proof.
Renderer descriptor для type/version строит channel-safe presentation из
bounded reason/task reference и не передаёт secrets. Пара
type/version считается registered только когда validator, dedup, policy и
renderer descriptors совместимы; unregistered pair отклоняется до durable
normal history. Это Stage 1 design contract, а не добавление production
event class или registry.

### [Решение] Severity

Default severity принадлежит descriptor type/version. Ориентир:

| Occurrence | Default |
| --- | --- |
| completed/recovered/informational state | INFO |
| recoverable runtime incident или low resource | WARNING |
| task/runtime failure | ERROR |
| sensitive fail-closed state или unsafe unknown state | CRITICAL |

Producer может предложить contextual override только если descriptor явно
разрешает его. Config не переписывает сам факт severity; config влияет на
routing threshold и suppression.

## 8. Policy model

### [Решение] Deterministic ordered rules

Policy является versioned data/configuration:

~~~text
NotificationEvent
    -> validate descriptor
    -> global enable check
    -> ordered rule matcher
    -> PolicyDecision
    -> zero or more Delivery rows
~~~

Каждое правило имеет explicit priority и bounded matchers:

- exact type или dotted prefix;
- exact/minimum severity;
- optional profile selector;
- optional subject kind/id selector;
- optional source selector.

Action содержит channel instance ids, suppression flag/reason, cooldown/dedup
parameters, locale и presentation profile. Первое совпавшее правило является
результатом; merge частичных правил запрещён. В конце есть явное default rule,
чтобы отсутствие совпадения имело определённый результат.

Dispatcher не содержит event-specific routing if/elif. Он получает уже
разрешённый PolicyDecision и вызывает зарегистрированные channel adapters.

### [Решение] Publish transaction и suppression

Для валидного event application boundary делает в короткой PostgreSQL
transaction:

1. проверяет schema/type и bounded payload;
2. применяет unique identity/dedup constraint;
3. если occurrence уже существует, сравнивает immutable fields и
   payload_digest: exact match возвращает существующие decision/delivery без
   повторного publish, mismatch возвращает identity_conflict;
4. атомарно выдаёт profile_sequence и записывает immutable NotificationEvent;
5. при global disabled записывает PolicyDecision SUPPRESSED с bounded reason и
   не создаёт Delivery;
6. иначе выбирает первое правило;
7. записывает одну PolicyDecision с rule identity, policy_version и snapshot
   выбранных channel ids;
8. создаёт не более одной Delivery на пару event_id + channel_instance_id.

profile_sequence выдаётся только для нового committed occurrence. Целевой
durable allocator хранит counter по profile_id и выдаёт следующее значение
под row lock в той же PostgreSQL transaction, которая вставляет
NotificationEvent; rollback не публикует sequence. Уникальность
(profile_id, profile_sequence) и этот allocator сериализуют concurrent
publishers внутри профиля. History и opaque cursor используют
profile_sequence, затем event/delivery identity как tie-breaker; occurred_at и
event_id не заменяют sequence и не задают порядок reconnect.

Channel-level suppression, например disabled channel или cooldown, записывается
в самой Delivery как SUPPRESSED. Global/rule-level suppression остаётся в
PolicyDecision и не требует искусственного channel id.

Policy change после создания delivery не переписывает старые decisions,
rendered snapshots или delivery state. Явный re-evaluation — отдельная
операция с новым audit record и не является частью Stage 1.

## 9. Durable delivery model

### [Решение] Сущности

#### NotificationEvent

Immutable факт occurrence с typed data, identity и persisted_at.

#### PolicyDecision

Одна запись на canonical event. Содержит ROUTED или SUPPRESSED, matched rule
identity/version, bounded reason, selected channels и policy snapshot/hash.

#### Delivery

Одна строка на event + channel instance. Целевые состояния:

~~~text
PENDING -> IN_FLIGHT
IN_FLIGHT -> RETRY_WAIT -> IN_FLIGHT
IN_FLIGHT -> FAILED [permanent failure or retry budget exhausted]
IN_FLIGHT -> PROVIDER_ACCEPTED [Telegram/Webhook: terminal]
PROVIDER_ACCEPTED -> AWAITING_AGENT_ACK -> DELIVERED [Desktop Agent]
AWAITING_AGENT_ACK -> RETRY_WAIT -> IN_FLIGHT [Agent timeout/disconnect]
AWAITING_AGENT_ACK -> FAILED [retry budget exhausted]
RETRY_WAIT -> FAILED [retry budget or absolute deadline exhausted]
PENDING -> SUPPRESSED [channel policy before send]
~~~

Global/rule-level suppression — отдельный terminal результат PolicyDecision
SUPPRESSED, при котором Delivery row не создаётся; он не является переходом
Delivery.

Delivery хранит lease_owner, lease_token, lease_until, attempt_count,
next_attempt_at, last_safe_error_code и immutable rendered snapshot. Для
Desktop Agent PROVIDER_ACCEPTED — промежуточный результат принятия frame
транспортом, а не terminal state: он обязан перейти в AWAITING_AGENT_ACK с
отдельным bounded ack deadline. Только проверенный ACK переводит эту Delivery
в DELIVERED; timeout, disconnect или истечение lease переводят
AWAITING_AGENT_ACK в RETRY_WAIT, затем в новый IN_FLIGHT либо в FAILED по
retry budget. Для Telegram/Webhook capability receipt=provider_acceptance
делает PROVIDER_ACCEPTED terminal state: после успешного provider response не
запускаются ACK wait или повторная отправка из-за отсутствующего Agent ACK.
Канал с более сильным receipt contract может выбрать другой переход, но это
должно быть явно задано channel capability и acceptance test.

#### DeliveryAttempt

Append-only audit фактической попытки: ordinal, start/finish time, result
class, safe error code, bounded retry-after, provider message id при наличии,
lease token и trace correlation. Raw response body, authorization header,
credentials и секретные URL не сохраняются.

#### Suppression / terminal failure

SUPPRESSED — channel-level decision, при котором отправка не выполнялась;
global/rule-level вариант живёт только в PolicyDecision. FAILED — terminal
delivery state после permanent failure или исчерпания retry budget/absolute
deadline. Unavailable до истечения policy deadline остаётся retryable state, а
не успешной доставкой.

### [Решение] Строгая семантика результата

Система различает следующие наблюдаемые факты:

| Факт | Что он доказывает | Что он не доказывает |
| --- | --- | --- |
| event persisted | occurrence записан в PostgreSQL | policy применена или канал отправил |
| delivery created | выбран channel и сохранена durable работа | adapter принял вызов |
| adapter accepted | adapter принял локальную работу | provider/Agent или пользователь получили её |
| provider accepted | внешний provider вернул контрактный success и, возможно, message id | человек прочитал сообщение |
| Agent ACK | авторизованный Agent подтвердил receipt конкретного delivery | человек прочитал сообщение |
| user read | только если конкретный channel имеет отдельный read receipt contract | не следует из HTTP 200 или enqueue |

DELIVERED — не универсальный синоним HTTP 200. Для handover это authenticated
Agent ACK, связанный с profile_id, delivery_id, event_id, connection/session
epoch и допустимым deadline. Telegram/Webhook HTTP 2xx обычно даёт
PROVIDER_ACCEPTED; channel adapter может вернуть DELIVERED только при более
сильном контрактном доказательстве.

### [Решение] Lease, retry и at-least-once

Dispatcher атомарно claim-ит PENDING/RETRY_WAIT rows с due
next_attempt_at, выставляет owner/token/deadline и коммитит claim до внешнего
вызова. После результата запись обновляется только при совпадении lease_token.

Crash с IN_FLIGHT не теряет работу: после lease_until отдельный recovery scan
переводит её в RETRY_WAIT или FAILED по retry budget. Это at-least-once
attempt semantics. Старый worker может завершить внешний вызов после истечения
lease, поэтому channel idempotency key и consumer/provider dedup обязательны.

Retry policy хранит bounded max attempts, exponential backoff с jitter,
absolute deadline и channel-specific classification. 429 использует bounded
Retry-After только в разрешённом диапазоне; timeout/reset/5xx обычно
transient; invalid credentials, invalid destination и schema rejection обычно
permanent. Точное значение budget и SLO требует Stage 2 load/chaos evidence.

Для Desktop Agent переход в AWAITING_AGENT_ACK и его ack deadline должны быть
durable. ACK handler атомарно проверяет event_id, delivery_id, profile_id,
lease_token, payload digest, connection/session epoch и срок действия; только
успешная проверка делает DELIVERED. ACK старой попытки после lease recovery
обязан быть отклонён по lease_token или session epoch. Timeout/disconnect не
может оставить delivery навсегда в ожидании: recovery переводит её в RETRY_WAIT
или FAILED, после чего обычный
lease claim выполняет повторную попытку с тем же idempotency key.

`NotificationDispatcher.dispatch_once()` возвращает bounded `DispatchReport`:
`updated` отражает durable result update, `stale_updates` — отклонённые lease
token, а `failed` — элементы с исключением adapter, channel contract или
storage update. Ошибка одного элемента не прерывает уже claim-нутый batch;
ошибка `channel.send` сначала переводится в безопасный typed unavailable result,
а lease остаётся доступным для bounded recovery, если durable update не удался.

### [Внешний design reference] Transactional outbox

Transactional outbox нужен только когда durable domain change и outbox event
записываются в одной DB transaction; это устраняет dual-write gap, но не
устраняет duplicate delivery и необходимость idempotent consumers:
<https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html>.

Для game/runtime/error occurrence, у которого нет общей PostgreSQL transaction
с игровым действием, publish transaction является отдельной durable записью.
Её нельзя выдавать за атомарность с действием в Azur Lane или эмуляторе.

## 10. Handover safety contract

### [Решение] Что считается DELIVERED

Busy handover разрешается продолжать только если до deadline выполнена вся
цепочка:

1. текущий authoritative profile и ownership всё ещё valid;
2. preemption event валиден и durable persisted;
3. policy создала handover-critical Delivery для нужного profile;
4. authenticated Desktop Agent сам инициировал connection к backend/Caddy;
5. Agent получил delivery с event_id и delivery_id;
6. Agent отправил ACK по authenticated profile-bound contract;
7. backend принял ACK один раз, проверил lease/session epoch и commit-нул
   DELIVERED;
8. handover hook получил точный NotificationOutcome.DELIVERED.

ACK означает receipt целевым Agent/UI, а не доказанное чтение человеком.

### [Решение] Bounded deadline и fail-closed

Handover передаёт bounded deadline, ограниченный существующим runtime maximum
300 сек. Нельзя ждать бесконечно, возобновлять operation после deadline или
продолжать handover на основании только ACCEPTED/PROVIDER_ACCEPTED.

Следующие состояния обязаны оставлять handover fail-closed:

- PENDING или RETRY_WAIT в момент deadline;
- IN_FLIGHT без валидного Agent ACK;
- PROVIDER_ACCEPTED или AWAITING_AGENT_ACK без Agent receipt;
- FAILED, SUPPRESSED или UNAVAILABLE;
- stale/unknown/expired ownership;
- profile mismatch, invalid session epoch или rejected ACK;
- backend restart, после которого нельзя доказать текущий operation state.

При таком результате текущий worker не quiesce-ится и не вытесняется. Durable
delivery продолжает retry до собственного retry budget/deadline независимо от
уже завершённого failed handover, но late success не меняет исход старой
операции.

### [Решение] Disconnect, duplicate и late ACK

- Agent disconnect до ACK: delivery остаётся durable, lease/retry recovery
  продолжает работу; текущий handover получает UNAVAILABLE или timeout;
- reconnect: Agent запрашивает backlog после resume cursor; уже ACK-нутый
  delivery не показывается как новая работа, если policy не создала новый event;
- duplicate delivery: стабильный delivery_id/idempotency_key позволяет Agent
  повторно принять и отобразить одну logical notification;
- duplicate ACK с теми же identity и результатом идемпотентен;
- ACK с другим event_id, profile, lease_token, connection epoch или payload
  digest отклоняется и только санитизированно журналируется;
- ACK после handover deadline принимается для durable audit только если
  delivery всё ещё валиден, но не переводит старый handover в success;
- backend restart восстанавливает rows по PostgreSQL и lease scan, а не по
  памяти SSE queue.

### [Решение] Mapping текущего NotificationOutcome

| Current outcome | Target proof | Handover |
| --- | --- | --- |
| ACCEPTED | enqueue/adapter/provider acceptance без Agent receipt | всегда fail-closed |
| DELIVERED | durable authenticated Agent ACK по handover contract | может продолжить |
| FAILED | terminal or rejected delivery | fail-closed |
| UNAVAILABLE | boundary/provider/backend unavailable | fail-closed |

Никакой compatibility mapping не повышает ACCEPTED до DELIVERED. Именно это
позволит честно закрыть blocker PR #177: State.init() в будущей реализации
должен inject-ить application notifier с реальным Agent ACK, а не оставлять
fallback notify_webui единственным production path. Stage 1 не меняет
State.init(), owner или handover code.

## 11. Desktop / Agent transport decision

### [Решение] Сравнение кандидатов

| Transport | Сильные стороны | Ограничения в AzurPilot | Решение |
| --- | --- | --- | --- |
| Текущий destructive SSE | уже есть Starlette endpoint, простой server -> client поток | один process-local queue, нет auth/ACK/id/resume/backlog, consumers конкурируют | отвергнут как foundation |
| Resumable SSE + отдельный ACK endpoint | one-way notification соответствует модели push, browser/native support, легко использовать через Caddy; Last-Event-ID/cursor и отдельный HTTPS ACK дают replay и proof | ACK требует второго outbound HTTP вызова; backpressure и flow control нужно явно задать; connection reconnect logic остаётся | рекомендованный target |
| WebSocket + typed ACK messages | двусторонний канал, ACK и будущий control traffic могут быть одним соединением | больше stateful/auth/proxy complexity, multiplexing control и notification расширяет blast radius; durable backlog и idempotency всё равно нужны; WebSocket API сам по себе не source of truth | отвергнут для Stage 1 target, пересмотр по measured need |

Рекомендованный target — resumable authenticated SSE для server -> Agent и
отдельный authenticated HTTPS ACK endpoint для Agent -> server. Agent всегда
сам инициирует outbound connection к backend через Caddy. Home PC не требует
public inbound port. Топология сохраняет возможность будущего VPS split:
Agent подключается к публичной Caddy boundary, а внутренние backend и
PostgreSQL не публикуются. Уведомление и runtime control остаются отдельными
authorization scopes, даже если в будущем появится другой duplex control
transport.

SSE/WebSocket — только transport, не source of truth. Потеря соединения
означает отсутствие текущего receipt, а не потерю durable notification.

### [Внешний design reference] SSE и WebSocket

SSE документирован как server -> page event stream с EventSource; ACK поэтому
требует отдельного client -> server пути:
<https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events>.

WebSocket предоставляет connection interface для отправки и получения
сообщений, но его двусторонность не заменяет durable backlog, authorization
или idempotency:
<https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API>.

### [Открытый критерий/риск] Agent protocol acceptance

До Stage 2 нужно измерить reconnect delay, backlog replay rate, bounded batch
size, max unacked window, slow Agent behavior, Caddy timeout и server restart
recovery на Windows Agent. При необходимости duplex runtime control решение
может быть пересмотрено только по результатам этих измерений и отдельному
security review; простого аргумента «WebSocket современнее» недостаточно.

## 12. PostgreSQL и dispatcher

### [Решение] Baseline

Baseline — текущий canonical PostgreSQL + dispatcher в существующей backend
boundary. Это согласуется с уже работающими module/persistence, Alembic,
health/readiness, role separation и observability. Новая RabbitMQ, Redis,
Kafka или отдельный notification microservice на Stage 1 не добавляется.

PostgreSQL является source of truth для event, policy, delivery, attempt,
lease, history и cursor. Dispatcher — projection/worker, а не владелец
семантики event taxonomy.

### [Решение] Claim query и несколько workers

Будущий claim выполняется короткой transaction приблизительно по схеме:

~~~text
SELECT due PENDING/RETRY_WAIT rows
  ORDER BY priority, next_attempt_at, sequence
  FOR UPDATE SKIP LOCKED
  LIMIT bounded_batch;
UPDATE rows with lease_owner, lease_token, lease_until;
COMMIT;
~~~

Точный SQL и indexes остаются Stage 2 implementation. SKIP LOCKED подходит
для конкурирующих queue-like workers, но не обещает глобальный порядок при
нескольких workers. Если порядок нужен, он определяется per profile/subject
sequence и отдельным acceptance test; global total order не обещается.

В реализации Stage 2 просроченные `PENDING/RETRY_WAIT` сначала атомарно
переводятся в `FAILED` с bounded `handover_deadline_expired`, а в active claim
попадают только rows с `deadline_at` в будущем. `lease_until` и
`PreparedDelivery.timeout_seconds` не выходят за оставшееся до deadline время.

Lock предотвращает concurrent claim только в пределах короткой transaction,
пока lease активен; lease token fencing защищает durable state update от
устаревшего worker. После lease_until recovery может claim-ить row, пока
старый worker ещё находится во внешнем вызове, поэтому overlapping external
attempts допустимы и adapter/provider idempotency обязательна. Crash recovery
не полагается на in-memory queue.

### [Внешний design reference] PostgreSQL locking

PostgreSQL документирует locking clauses и применение FOR UPDATE к строкам;
SKIP LOCKED подходит для queue-like consumers, но ограничения порядка и
изоляции нужно учитывать отдельно:
<https://www.postgresql.org/docs/current/sql-select.html>.

### [Решение] LISTEN/NOTIFY

LISTEN/NOTIFY допускается только как wake-up optimization:

- payload содержит только bounded event/delivery key;
- table state остаётся source of truth;
- после LISTEN команда коммитится до initial state inspection;
- startup/reconnect всегда выполняет durable scan, закрывающий documented race;
- потерянный сигнал не означает потерянную работу;
- переполненная notification queue или disconnect не ломают correctness.

Документация PostgreSQL прямо указывает на startup race для LISTEN и рекомендует
сначала LISTEN/commit, затем initial inspection:
<https://www.postgresql.org/docs/current/sql-listen.html>.
Payload NOTIFY ограничен и не предназначен для передачи больших данных; key
должен указывать на durable table:
<https://www.postgresql.org/docs/current/sql-notify.html>.

### [Решение] Когда оправдан отдельный broker

Broker можно рассматривать только при одновременно выполненных условиях:

1. существующий PostgreSQL claim/lease, indexes и batch tuning не удерживают
   согласованный backlog-age/delivery-latency SLO на измеренной максимальной
   нагрузке;
2. нужна независимая fan-out/replay topology для нескольких внешних
   consumers или hosts, а PostgreSQL projection уже создаёт подтверждённую
   lock/connection contention;
3. есть повторяемый load/chaos evidence за несколько release windows, а не
   единичный пик;
4. выбранный broker предоставляет durable retention, auth, idempotent consumer
   contract, bounded replay и operational ownership;
5. migration plan имеет single source of truth, bounded rollback и не оставляет
   два долгоживущих notification stack.

Если единственная причина — желание использовать более современный инструмент,
broker не оправдан. Redis/RabbitMQ/Kafka не могут быть добавлены как shortcut
для отсутствующей event identity или policy model.

## 13. Neutral channel contract

### [Решение] NotificationChannel

Channel adapter регистрируется по stable channel_instance_id/type и получает
только:

~~~text
PreparedDelivery
    delivery_id
    event projection
    rendered snapshot
    idempotency_key
    bounded timeout
    channel-safe structured attributes
~~~

Configuration и secret resolver находятся в infrastructure composition layer.
Dispatcher не делает provider switch по event type.

Typed capabilities как минимум описывают max payload, title/body limits,
markup mode, idempotency support, receipt/ACK strength и health check.
Renderer получает capabilities до подготовки snapshot.

### [Решение] DeliveryResult

~~~text
DELIVERED
PROVIDER_ACCEPTED
TRANSIENT_FAILURE
PERMANENT_FAILURE
UNAVAILABLE
SUPPRESSED
~~~

Result содержит только bounded safe_error_code, safe_summary,
retry_after_hint и provider_message_id при наличии. SUPPRESSED обычно
является policy/delivery state, но может быть результатом adapter, если channel
проверил собственную capability policy. bool запрещён как публичный result.

Будущие обязательные channels:

- Desktop Agent — resumable SSE + HTTPS ACK; единственный channel с
  handover-grade receipt proof;
- Telegram — provider accepted не равен user read;
- generic Webhook — HTTP result с SSRF/redirect restrictions;
- OnePush — только transitional legacy adapter.

### [Решение] OnePush migration boundary

Целевая application architecture не импортирует onepush. В переходный период
legacy adapter может преобразовать PreparedDelivery в текущий provider call,
но сохраняет typed DeliveryResult и не получает права объявлять HTTP 200
DELIVERED для handover.

После полного cutover OnePush dependency, YAML provider config и legacy
wrappers удаляются одним bounded change. Бессрочный
LegacyNotificationServiceV2 или постоянный второй registry не создаётся.

## 14. History и live projection

### [Решение] PostgreSQL-backed history

History строится из event + PolicyDecision + Delivery + sanitized attempt/
rendered snapshot. Read API не меняет delivery state.

Целевой API должен поддерживать:

- profile-scoped authorization;
- bounded limit;
- opaque cursor;
- stable sort по monotonic profile sequence, затем event/delivery identity;
- фильтры по type/severity/state/channel;
- отсутствие raw secrets и unbounded payload;
- объяснимую suppression reason.

Cursor содержит или криптографически связывает profile scope и high-water
sequence. Timestamp alone не является resume cursor: одинаковые timestamps и
clock skew могут создавать gaps/duplicates.

### [Решение] Live updates и reconnect

Live projection использует отдельную non-destructive подписку поверх durable
table и единый snapshot/cursor protocol:

1. authenticated consumer регистрирует subscription (для PostgreSQL
   LISTEN — LISTEN и commit) до initial state read;
2. в одной согласованной DB snapshot transaction читает bounded initial page
   и cursor последней фактически выданной записи, затем commit-ит snapshot;
3. сервер делает gap fill из PostgreSQL после этого cursor, проверяет
   непрерывность retained profile_sequence и повторяет snapshot/catch-up при
   обнаруженной гонке или несовместимом cursor;
4. выдаёт SSE event id, равный durable cursor;
5. при reconnect consumer передаёт Last-Event-ID или эквивалентный opaque
   cursor;
6. сервер сначала выполняет gap fill от этого cursor, затем продолжает live
   projection. События, committed после initial snapshot, покрываются
   subscription signal или тем же durable gap fill.

High-water не является произвольным MAX(sequence) за пределами выданной
страницы: cursor обязан соответствовать последней фактически выданной записи
или явно переданному caller cursor. Timestamp alone не заменяет snapshot или
profile_sequence.

Каждый consumer имеет собственный cursor. Никакой shared destructive
asyncio.Queue не используется. Медленный consumer ограничивается bounded
batch/window и должен снова сделать durable catch-up; потеря connection не
удаляет history.

Будущие WebUI endpoints, например GET history и GET stream, должны быть
защищены authentication + object-level profile authorization. Названия
endpoint не являются Stage 1 implementation contract.

### [Решение] Retention

Retention policy является explicit bounded configuration и применяется
одинаково к event, decision, delivery, attempts и rendered snapshots с
учётом legal/privacy requirements. Cleanup:

- выполняется отдельным bounded batch job;
- выбирает только terminal rows и сохраняет все rows, связанные с
  non-terminal PENDING, RETRY_WAIT и AWAITING_AGENT_ACK, независимо от
  наличия active lease;
- не удаляет active lease;
- сначала оставляет минимальный terminal audit, если это требуется policy;
- журналирует количество и возраст удалённых rows без payload;
- не является implicit side effect publisher/dispatcher.

Точный default retention и archive policy требуют Stage 2 privacy/storage
acceptance. После удаления late ACK не восстанавливает history и не меняет
старый handover result.

## 15. Security contract

### [Решение] Agent authentication и authorization

Desktop Agent получает отдельную identity при enrollment. Целевой transport —
HTTPS через Caddy; для будущего WebSocket варианта — WSS. Credentials должны
быть device-bound или short-lived с rotation/revocation, а не общим
provider token из пользовательского OnePush YAML.

Каждый delivery и ACK проверяются по:

- authenticated agent identity;
- authorized profile/instance scope;
- delivery_id + event_id;
- connection/session epoch и lease token;
- bounded expiry/deadline.

Profile authorization обязательна и для history, stream, ACK, runtime-control
и handover. Authentication connection без object-level check недостаточна.
Home PC не открывает public inbound port; Agent устанавливает outbound
connection к server/Caddy.

### [Решение] Secrets и payload

Provider credentials, webhook secret и Agent credential хранятся в
существующем secret/deployment boundary, а не в NotificationEvent,
PolicyDecision, history, rendered snapshot, logs или traces. Raw config,
Authorization header, cookie, access token и credentials в URL запрещены.

Payload имеет bounded size; исходный ориентир — не более 64 KiB на wire
event, после чего channel-specific caps могут быть строже. Oversize и
malformed payload отклоняются до persistence normal history.

### [Решение] Webhook safety

Generic Webhook adapter принимает только разрешённые HTTPS origins. Он:

- блокирует loopback, link-local, private, multicast, reserved и metadata
  destinations после DNS resolution;
- повторяет эту проверку на каждом redirect либо запрещает redirect;
- использует bounded connect/read/total timeout и response size;
- не пересылает inbound Authorization, cookies или provider credentials;
- валидирует certificate/hostname и разрешённый method/content type;
- хранит только safe response class/code.

Endpoint allowlist и DNS rebinding defense обязательны до включения канала.

### [Решение] Replay, ACK и privacy

ACK — idempotent, profile-bound и защищённый от replay bounded expiry/session
epoch. Повтор exact ACK безопасен; изменённый/stale ACK не меняет state.
Sensitive event projection по умолчанию минимальна, а notification payload
не включает raw exception, environment dump, screenshot, device id или
secret-bearing config.

Существующий log/observability sanitizer остаётся обязательной последней
границей, но не заменяет раннюю schema validation и channel-specific
redaction.

## 16. Observability

### [Решение] Metrics

Целевые low-cardinality metrics:

| Metric | Labels |
| --- | --- |
| notification_publish_total | source_domain, result |
| notification_policy_total | policy_state, reason |
| notification_delivery_attempt_total | channel_type, result_class |
| notification_retry_total | channel_type, reason |
| notification_delivery_latency_seconds | channel_type, result_class |
| notification_backlog_age_seconds | channel_type, state |
| notification_channel_health | channel_type, health_state |
| notification_agent_ack_timeout_total | channel_type, reason |
| notification_event_rejected_total | source_domain, reason |

Не помещать в Prometheus labels notification_id, delivery_id, trace_id,
arbitrary title/message или неограниченный profile data. Profile/event/delivery
identity допускается в structured logs, traces и authorized history.

### [Решение] Logs и traces

Structured logs должны связывать event_id, delivery_id, profile reference,
task/runtime correlation, result class, attempt и safe error code. Эти поля
sanitised и bounded.

Целевые spans:

~~~text
notification.publish
notification.policy.resolve
notification.dispatch.claim
notification.channel.send
notification.agent.ack
notification.history.read
~~~

HTTP adapter spans используют согласованные OTel HTTP attributes, включая
method, route/path, status и error type; raw URLs с credentials, body, title и
secret headers не записываются. Retry/resend correlation связывается через
trace context, но trace id не становится event identity.

### [Решение] Alerts и Grafana

Grafana Alerting отвечает за operational/infrastructure conditions:
PostgreSQL unavailable, dispatcher отсутствует, oldest backlog age выше SLO,
lease recovery storm, channel health failure, 429/5xx spike и OTel export
failure. Grafana не является event bus для обычных игровых уведомлений.
Grafana MCP остаётся read-only диагностическим consumer.

OpenTelemetry semantic conventions используются как naming reference, а более
конкретные существующие conventions AzurPilot имеют приоритет:
<https://opentelemetry.io/docs/specs/semconv/general/>.

## 17. Migration / cutover matrix

### [Решение] Принцип cutover

Переход выполняется по bounded этапам с одним owner/source of truth. Временно
допустим shadow verification или dual-publish только при явной policy, когда
новый path не создаёт второй пользовательский push. После завершения migration
в production не остаётся двух долгоживущих notification stacks.

| Legacy element | Current behavior | Target replacement | Future cutover | Safe removal criterion |
| --- | --- | --- | --- | --- |
| alas.py error/recovery producers | прямые OnePush +/− WebUI calls | typed runtime/task events через NotificationPublisher | после event schema + policy acceptance | все ветви имеют event contract и producer tests; прямых transport imports нет |
| CampaignRun / campaign_event | stop conditions вызывают Error_OnePushConfig | campaign.stop_condition.reached | после campaign adapter acceptance | каждое bounded kind покрыто event/policy/history tests |
| FastForwardHandler | GemsFarming error сам вызывает OnePush | campaign.auto_search.configuration_failed | после error renderer и retry semantics | AutoSearchSetError path больше не знает provider |
| RewardCommission | PostgreSQL record, затем OnePush + WebUI | commission.reward.received с transaction-aware outbox при наличии общей DB transaction | после commission idempotency/replay acceptance | нет двойного push, recovery после commit доказан |
| Opsi scheduling.notify_push | smart scheduling выбирает launcher/OnePush и OR-ит bool | opsi typed events + policy-selected channels | после Opsi cooldown/dedup migration | durable delivery state заменил process/config suppression |
| Opsi hazard/fleet sinks | 14 self.notify_push sinks | opsi action/resource/ship/fleet event descriptors | по группам после producer contract tests | AST/grep подтверждает отсутствие прямых sink calls |
| module.notify.handle_notify | YAML -> onepush provider, bool | OnePushChannel transitional adapter -> remove | сначала adapter, затем producer migration | все producers используют application port, onepush import удалён |
| module.notify.notify_webui | loopback POST, true при отсутствии exception | DesktopChannel + durable Agent ACK | после busy handover live acceptance | fallback owner больше не вызывает legacy function |
| POST /api/notify | arbitrary JSON enqueue в local queue | authenticated publish/agent ingress только через application API | после auth/schema contract | old endpoint удалён и documented clients migrated |
| _notification_queue | destructive process-local asyncio.Queue | PostgreSQL-backed projection + independent cursor | после replay/gap-fill acceptance | restart и два consumer scenario зелёные |
| GET /api/notify_stream | non-resumable SSE без ACK/id | resumable SSE stream с cursor/high-water | после Agent reconnect acceptance | нет production client, зависящего от old contract |
| runtime_control_owner fallback | notify_webui True -> ACCEPTED | injected handover notifier, подтверждённый Agent ACK | только после PR #177 busy acceptance | State.init/owner wiring verified; ACCEPTED не используется как proof |
| NotificationOutcome | current ACCEPTED/DELIVERED/FAILED/UNAVAILABLE | сохранить mapping; добавить evidence behind application boundary при необходимости | вместе с handover implementation | regression test запрещает ACCEPTED -> success |
| User config keys | Scheduler/Opsi/Error/EventShop flags перегружены | versioned policy source с explicit migration | после config compatibility plan | old keys имеют telemetry=0 и documented rollback window |
| WebUI developer test | button вызывает handle_notify | notification.test.requested через test-only authenticated use case | после dev tooling migration | нет прямого provider call из developer UI |
| dev_tools/cyclic_notify.py | бесконечный реальный OnePush loop | bounded test harness или удаление | до включения new channel in non-prod | инструмент не может бесконечно отправлять внешний push |
| tests patch targets | patch direct alas/module.notify и route presence | application port fakes, channel result fakes, contract tests | параллельно каждому producer cutover | tests подтверждают event/policy/delivery semantics |
| onepush dependency | runtime dependency onepush==1.2.0 | transitional adapter only | последний после полного producer cutover | lock/config/import scan не содержит production dependency |

Для любого будущего запуска `dev_tools/cyclic_notify.py` test-only harness
сначала обязан доказать изоляцию test config/profile от пользовательской
конфигурации; при невозможности доказательства запуск немедленно
отказывается. Harness обязан иметь bounded duration/iteration budget,
использовать test sink или явно non-production channel и запрещать
irreversible game actions. Пока эти свойства не проверены, безопасный вариант
только удаление инструмента, а не запуск его текущего бесконечного loop.

### [Открытый критерий/риск] Rollback

Rollback implementation stage обязан возвращать producers к единственному
известному legacy path только в пределах заранее заданного окна. До cutover
фиксируется immutable cutover watermark по каждому profile: последний
committed profile_sequence/event identity нового stack, состояние его
deliveries и legacy source offset. Timestamp alone watermark не заменяет.

Оба path используют один cross-stack idempotency contract: event key равен
source + event_id, delivery key — event_id + channel_instance_id. Новый stack
остаётся source of truth для всех event/decision/delivery rows, committed до
watermark, и для external attempts, которые он уже начал; rollback не
переписывает их history. После freeze новых publishers owner выбирается
однозначно: либо bounded drain нового dispatcher, либо legacy takeover только
после reconciliation watermark и проверки тех же idempotency keys.

| Частичное состояние при rollback | Reconciliation | Запрещённый результат |
| --- | --- | --- |
| event committed, delivery не создана | сохранить event в history и создать legacy work только после проверки отсутствия этого event key в legacy | blind republish или потеря occurrence |
| PENDING/RETRY_WAIT/AWAITING_AGENT_ACK | bounded drain нового stack либо передача ownership legacy с тем же delivery key и audit handoff | два dispatcher или пропуск delivery |
| IN_FLIGHT с неизвестным provider outcome | query/provider idempotency или оставить row под новым stack до разрешения uncertainty | blind resend и duplicate push |
| PROVIDER_ACCEPTED/DELIVERED | считать external attempt уже состоявшейся и не replay-ить | второй пользовательский push |
| SUPPRESSED/FAILED | сохранить terminal decision/audit; повтор возможен только как новая явно audited occurrence | тихое изменение старого decision |

Если reconciliation не может доказать ownership или idempotency, rollback
останавливается fail-closed, а не включает второй долгоживущий stack.
Удаление legacy path разрешается только после закрытия rollback window,
export/audit доказательств отсутствия active clients и pending legacy queue,
нулевых legacy metrics и успешного replay/rollback rehearsal.

## 18. Failure-mode matrix

### [Решение] Общие правила

Ниже перечислены ожидаемые semantics до реализации. Ни один внешний response
не может молча повысить состояние до DELIVERED.

| Failure scenario | Durable behavior | User/handover behavior |
| --- | --- | --- |
| PostgreSQL временно недоступен | publish/claim fail closed; нет ложного persisted/delivered; bounded retry/health signal | обычный producer получает UNAVAILABLE; handover немедленно fail-closed |
| Publisher crash до commit | transaction rollback, event отсутствует | delivery не существует; повтор producer использует тот же occurrence/dedup contract |
| Publisher crash сразу после commit | event/decision/delivery остаются в PostgreSQL | dispatcher scan продолжает работу; occurrence не теряется |
| Dispatcher crash с IN_FLIGHT | lease expires; recovery переводит в RETRY_WAIT или FAILED по budget | нет DELIVERED без proof; возможен повторный at-least-once attempt |
| Два dispatcher одновременно | row lock/skip_locked и lease token разделяют claim | один logical delivery; duplicate external attempt допускается только с idempotency |
| Duplicate publish | unique event identity/dedup возвращает существующий record | второй пользовательский push не создаётся |
| Duplicate provider response | attempt idempotently записывает один result; поздний дубль не меняет terminal state | история не удваивается |
| Provider timeout | TRANSIENT_FAILURE, retry/backoff до deadline/budget | не DELIVERED; handover ждёт только bounded deadline |
| HTTP 429 | transient; bounded Retry-After, rate limit metric | не DELIVERED; при deadline handover fail-closed |
| HTTP 4xx permanent | PERMANENT_FAILURE/FAILED, кроме provider-specific documented retryable code | ручное исправление config/destination; handover fail-closed |
| HTTP 5xx | TRANSIENT_FAILURE с bounded retry | не DELIVERED до сильного receipt contract |
| Desktop Agent offline | delivery остаётся PENDING/RETRY_WAIT или выходит из AWAITING_AGENT_ACK по lease/timeout; no receipt | handover UNAVAILABLE/timeout, текущий worker не вытесняется |
| Agent ACK timeout/disconnect | AWAITING_AGENT_ACK атомарно переходит в RETRY_WAIT, затем повторно claim-ится или становится FAILED по budget | handover не получает DELIVERED без ACK; повторная попытка не возобновляет старый handover |
| Agent reconnect | resume cursor + durable gap fill; ACK-нутые rows idempotent | новые события доставляются без gaps; старый failed handover не возобновляется |
| Duplicate ACK | exact same ACK — no-op success; append bounded audit if needed | не запускает второй quiesce/handover |
| Stale ACK | token/profile/epoch/deadline проверка отклоняет ACK | state не меняется; handover остаётся fail-closed |
| ACK после handover timeout | сохраняется как late audit или игнорируется после retention | старый handover никогда не переходит в success |
| WebUI restart | process queue не является source of truth; history/stream catch-up из PostgreSQL | live consumer reconnects; delivery не теряется |
| Backend restart | lease recovery и dispatcher scan восстанавливают rows | handover operation revalidates authoritative state; нет implicit continuation |
| Retention cleanup | удаляются только eligible terminal rows bounded batches; PENDING/RETRY_WAIT/AWAITING_AGENT_ACK сохраняются | late ACK не восстанавливает удалённую history |
| Misconfigured channel | validation до send или PERMANENT_FAILURE; secret не логируется | policy/admin diagnostic; handover fail-closed |
| Malformed event | rejected до normal event persistence; bounded reject metric/log | producer получает validation failure; no delivery |
| Global suppression | event + PolicyDecision SUPPRESSED, Delivery не создаётся | отсутствие push объяснимо history/policy reason |
| Policy change после delivery | старые decision/rendered snapshot/state неизменны | изменение влияет только на новые events или explicit audited re-evaluation |
| Backend signal LISTEN потерян | durable scan после reconnect/startup находит due rows | нет потери delivery; signal не является queue |
| Slow live consumer | bounded window/backpressure; consumer catch-up по cursor | отдельные consumers не крадут сообщения друг у друга |
| Webhook DNS/rebind/redirect violation | PERMANENT_FAILURE без запроса к запрещённому origin | безопасный отказ, без credential forwarding |

## 19. Внешние design references

### [Внешний design reference] Проверенные официальные источники

Источники открыты и проверены 2026-09-09:

1. CloudEvents 1.x vocabulary и event identity:
   <https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md>
2. AWS Prescriptive Guidance, transactional outbox, atomicity и duplicate/
   idempotent consumers:
   <https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html>
3. PostgreSQL SELECT locking и SKIP LOCKED:
   <https://www.postgresql.org/docs/current/sql-select.html>
4. PostgreSQL LISTEN startup sequence:
   <https://www.postgresql.org/docs/current/sql-listen.html>
5. PostgreSQL NOTIFY payload/queue semantics:
   <https://www.postgresql.org/docs/current/sql-notify.html>
6. MDN WebSocket API:
   <https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API>
7. MDN Server-sent events:
   <https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events>
8. OpenTelemetry general semantic conventions:
   <https://opentelemetry.io/docs/specs/semconv/general/>

Эти ссылки используются как design references, а не как доказательство того,
что соответствующая технология уже есть в AzurPilot.

## 20. Что намеренно не реализуется на Stage 1

### [Решение] Запрещённый scope

На этом этапе не добавляются:

- production NotificationService;
- production event classes или registry;
- PostgreSQL notification tables и Alembic migration;
- dispatcher, broker, Redis, RabbitMQ, Kafka или microservice;
- Desktop Agent, WebSocket endpoint, resumable SSE endpoint или ACK endpoint;
- Telegram/Webhook adapter;
- handover или State.init changes;
- mapping ACCEPTED в DELIVERED;
- изменение OnePush behavior или удаление legacy path;
- массовая миграция producers;
- изменение пользовательской config semantics;
- cherry-pick PR #173;
- permanent compatibility wrappers.

Настоящий production bug Custom и security/transport findings фиксируются
выше как findings и переносятся в отдельный implementation/bugfix scope. Они
не маскируются документационным изменением.

## 21. План будущей реализации и acceptance

### [Решение] Stage 2 — foundation

Реализовать typed event descriptors, application port, schema validation,
policy resolver, PostgreSQL schema/repositories, unique identity и
transaction/lease tests. Acceptance:

- malformed/duplicate/policy suppression tests;
- runtime.handover.preemption_requested/schema_version=1 descriptor registration,
  bounded payload and renderer/policy compatibility tests;
- exact duplicate immutable fields + payload_digest returns existing state,
  while changed payload returns identity_conflict with no republish;
- publisher crash до/после commit;
- two dispatcher claim and lease recovery;
- concurrent publishers получают уникальный monotonic profile_sequence, а
  slow external attempt после lease expiry покрыт overlapping-attempt test;
- secret/log/history redaction;
- Alembic cycle и current head;
- no imports from application layer to transport providers.

### [Решение] Stage 3 — Desktop Agent и handover

Реализовать outbound authenticated Agent, resumable SSE, ACK endpoint,
profile authorization, cursor gap fill и durable receipt. Acceptance:

- Agent ACK timeout: AWAITING_AGENT_ACK -> RETRY_WAIT -> IN_FLIGHT или
  FAILED;
- concurrent commits, reconnect и gap fill возвращают profile_sequence без
  gaps/duplicates в пределах committed history;
- snapshot/cursor race test с публикацией между initial read и subscription не
  пропускает committed event;
- Agent offline/reconnect/backlog;
- channel-specific receipt test: Desktop Agent ждёт ACK, а
  Telegram/Webhook завершаются в terminal PROVIDER_ACCEPTED без ACK wait;
- duplicate/stale/late ACK;
- mismatched event_id и stale lease_token после recovery отклоняются без
  перехода в DELIVERED;
- WebUI/backend restart;
- exact NotificationOutcome mapping;
- busy PR #177 live acceptance с одним владельцем, одним exact runtime root,
  recorded Agent receipt и безопасным cleanup;
- отсутствие второго WebUI и отсутствие false ACCEPTED success.

### [Решение] Stage 4 — external channels

Добавить typed Telegram/Webhook adapters и, только если нужен безопасный
переход, OnePush transitional adapter. Acceptance:

- timeout/429/4xx/5xx classification;
- provider idempotency and retry;
- webhook SSRF/redirect/auth tests;
- credentials absent from payload/history/logs/traces;
- channel health and backlog metrics.

### [Решение] Stage 5 — producer/config migration

Перевести producers группами: runtime/alas, campaign, commission, Opsi,
developer tools. Для каждой группы нужны event contract, policy fixtures,
history projection, failure/retry tests и bounded rollback. Existing
configuration flags мигрируют в policy только после подтверждения эквивалентной
семантики.

### [Решение] Stage 6 — legacy removal

После telemetry=0, active-client inventory, replay/rollback rehearsal и
точного PR head review удалить old endpoint, queue, direct imports,
OnePush dependency и obsolete config semantics. Финальный acceptance
подтверждает, что в production один durable notification stack.

### [Открытый критерий/риск] SLO и retention values

Точные значения publish latency, handover wait, delivery latency, retry
budget, backlog age, max Agent gap, retention days и broker thresholds нельзя
честно вывести из Stage 1 кода. Они фиксируются перед Stage 2 acceptance после
baseline load, storage estimate, privacy review и Windows Agent test.

## 22. Итог Stage 1

### [Решение] Architectural closure

Документ закрепляет один target: canonical typed events, PostgreSQL durable
policy/delivery state, dispatcher с lease и at-least-once attempts,
resumable SSE + отдельный authenticated Agent ACK, profile-scoped history,
существующий OTel stack и transitional-only OnePush.

Ключевая safety property: только доказанный authenticated Agent ACK может
дать handover NotificationOutcome.DELIVERED; enqueue, provider HTTP 200,
PROVIDER_ACCEPTED и human-readable intent этого не делают.

Stage 1 остаётся documentation-only. Любое изменение runtime, persistence,
transport, producer или config semantics требует отдельного implementation
этапа с собственным exact-head review, CI и live acceptance.

## 23. Фактическая граница Stage 2 implementation

### [Факт] Durable foundation

В Stage 2 добавлены `module.application.notifications` и PostgreSQL adapter
`PostgresNotificationRepository`. Migration `0009_notification_foundation`
создаёт event, policy decision, delivery, append-only attempt и per-profile
sequence allocator в schema `azurpilot`. Публикация выполняет validation,
deduplication, policy snapshot и создание delivery в одной короткой
транзакции; внешний channel вызывается только после commit dispatcher claim.

### [Факт] Безопасная граница транспорта

Зарегистрирован typed descriptor только для
`runtime.handover.preemption_requested/v1`; остальные initial taxonomy entries
явно deferred до producer migration и не принимают generic payload. В текущей
ветке нет Desktop Agent, Telegram, Webhook или OnePush adapter, а production
channel registry по умолчанию пуст. `PROVIDER_ACCEPTED` остаётся
промежуточным состоянием, а `DELIVERED` требует Agent ACK capability.

### [Факт] Незатронутые legacy boundaries

`State.init`, `WebUIRuntimeControlOwner`, `handle_notify`, `notify_webui`,
`_notification_queue`, существующие user config keys и PR #177 не подключены к
новому foundation. Текущий PR #177 остаётся отдельным busy-handover blocker:
`ACCEPTED` legacy fallback не является доказательством `DELIVERED`; его
production wiring переносится в Stage 3 после authenticated Agent receipt.
