---
name: azurpilot-game-control
description: "Штатная работа через подключённое приложение `AzurPilot Game`: чтение и bounded control игровых профилей, задач и runtime по актуальному Game MCP contract."
---

# AzurPilot Game Control

## Назначение и граница

Используй этот skill, когда пользователь хочет штатно работать с standalone
Game MCP: посмотреть профили, ресурсы, задачу, очередь, Fleet State, morale,
redacted config, sanitized logs или screenshot, получить справку по task,
запустить или остановить профиль, поставить поддерживаемую scheduler task, изменить
разрешённый параметр или выполнить опубликованное runtime-control действие.

Источник действий — подключённое приложение `AzurPilot Game` и его фактический
callable catalog. Этот skill не добавляет MCP-сервер, не вызывает Dev MCP через
MCP и не превращается в произвольный shell/ADB или GUI automation слой.

Не используй его для Dev Runtime, Universal Smoke Harness, DevSession, Git/CI
или для неизвестной ошибки слоя. В первом случае используй
`azurpilot-development`, во втором — `azurpilot-troubleshooting`.

## Входные данные и capability discovery

Для target-dependent запроса нужен явный canonical `<profile>`. Если профиль
не указан, сначала вызови `game_list_profiles` и попроси выбрать профиль только
при неоднозначности; не подставляй известное или историческое имя.

Перед незнакомой или неоднозначной операцией:

1. Получи `game_get_contract`, если он доступен в текущем callable surface.
2. Для task-oriented запроса получи `game_list_tasks`.
3. Для выбранной task получи `game_get_task_help` и используй только реально
   опубликованные metadata и аргументы.

Для однозначного простого read-запроса не вызывай contract механически перед
каждым чтением. При этом контракт backend и callable tools текущей сессии —
разные источники evidence. Если нужный tool отсутствует в текущей сессии,
остановись fail-closed и передай проблему в `azurpilot-troubleshooting`; не
вызывай похожий старый tool.

Если contract публикует `tool_count` или `tool_catalog_sha256`, фиксируй их как
текущие значения ответа. Не зашивай число, hash или статический список в этот
skill: добавление capability не должно требовать его переписывания.

## Модель состояния

Разделяй домены состояния и называй доказательство каждого из них:

| Домен | Что можно утверждать |
| --- | --- |
| AzurPilot profile | `game_get_profile_status` описывает lifecycle worker/profile. |
| emulator | Только отдельное emulator-control действие или его postcondition. |
| ADB | Только отдельное ADB evidence/action и его postcondition. |
| game application process | Только явное runtime evidence, если его публикует current contract. |
| game foreground/UI | Только authoritative foreground/UI evidence; screenshot сам по себе не login proof. |
| scheduler/task execution | `game_get_scheduler_queue` и `game_get_current_task` в пределах их contract. |

Поэтому `profile stopped` не означает `emulator stopped` или `game app
stopped`, а `game app foreground` не означает `profile running`. `UNKNOWN`,
`unavailable` и отсутствующее evidence сохраняй как неизвестное состояние.
`game_get_profile_status` никогда не является доказательством emulator, ADB,
game process, foreground или login/main state.

## Нормальный read workflow

1. Зафиксируй пользовательскую цель и `<profile>`.
2. При необходимости прочитай contract, profiles, task catalog и task help.
3. Выбери существующее Game MCP capability, а не внутреннее имя из догадки.
4. Для чтения используй только bounded/sanitized output: не извлекай secrets,
   arbitrary filesystem, SQL, raw credentials или несвязанный Dev state.
5. Не считай screenshot действием ввода. Не строь поверх него последовательность
   координатных кликов; Game MCP сам определяет разрешённый control surface.

Естественное описание вроде «запусти исследование» сначала сопоставляй с
текущим `game_list_tasks` и `game_get_task_help`. Task существует только если
она опубликована каталогом. Не предполагай отдельную scheduler task для login
или другой capability, которой нет в catalog.

## Control и lifecycle

Перед control action проверь профиль, требуемый scope и precondition из
contract. Выполняй одну осознанную mutation за раз, с явным `<profile>` и без
автоматического retry (automatic retry запрещён). После ответа проверь authoritative postcondition,
который требует именно этот tool:

- `game_start_profile` и `game_stop_profile` относятся к lifecycle профиля;
- `game_trigger_task` ставит только generated task из catalog;
- `game_clear_scheduler_queue` меняет только bounded scheduler queue;
- `game_update_config` изменяет один разрешённый нечувствительный параметр и
  требует readback;
- `game_restart_emulator` — emulator lifecycle и не является автоматически
  запуском приложения или login;
- `game_restart_adb` — ADB maintenance после проверки ownership target.

Если актуальный contract действительно публикует дополнительные bounded
actions, например `game_restart_runtime` или `game_login_runtime`, сначала
прочитай их описание и output schema. Не считай эти имена доступными только
потому, что они упомянуты в документации:

- `game_restart_runtime` не доказывает login-to-main, если это не указано его
  contract; обычно отдельно проверяются emulator, ADB, app running и
  foreground;
- `game_login_runtime` допустим только как официальный current action. Его
  bounded login flow должен завершиться authoritative UI/main и ADB/app
  postconditions; не подменяй его scheduler task или прямым вводом.

Для live workflow `restart → login` в свежей callable session соблюдай ровно
такой порядок:

```text
pre-mutation read-only status
→ ровно один game_restart_runtime(<profile>)
→ только после success ровно один game_login_runtime(<profile>)
→ authoritative read-only postcondition
```

Финальное postcondition должно отдельно доказать emulator ready, ADB ready,
game running, game foreground и login/main-ready по фактическому output
contract. Если `game_login_runtime` отсутствует в текущем callable surface,
остановись и передай mismatch в `azurpilot-troubleshooting`; не выполняй
restart как замену login.

При `TIMEOUT`, неизвестном результате, конфликте ownership или нарушенном
postcondition не повторяй mutation. Останови writes, сохрани последний
подтверждённый state и переключись в `azurpilot-troubleshooting`.

## Event- и server-agnostic правила

Не хардкодь profile, server, package, emulator instance, ADB serial, port,
active event, map, task list или coordinates. Server/package/account и
применимый UI flow принадлежат config/profile и существующим application/UI
abstractions. Для текущей task/config всегда используй catalog/help и
authoritative readback; новый event или server variant не должен требовать
обновления skill только из-за нового ID.

Архитектурные факты, на которых основан этот workflow, собраны в
[references/architecture.md](references/architecture.md). При конфликте с
ними приоритет имеют текущие `game_get_contract`, `game_list_tasks`, output
schema и фактический код целевой версии.

## Отчёт

После каждой mutation сообщи кратко:

- `tool` и `<profile>`;
- machine-readable `code` и `state`;
- существенные bounded `details` без credentials;
- проверенное postcondition и его evidence;
- `retry: no` либо честно укажи, что mutation не доказана.

После диагностики верни работу сюда. Если пользователь просит Dev Runtime —
верни её в `azurpilot-development`; если причина находится в catalog, app,
auth, transport, runtime или postcondition — сначала используй
`azurpilot-troubleshooting`.
