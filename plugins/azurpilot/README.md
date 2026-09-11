# AzurPilot

Канонический пакет Plugin Creator для рабочих процессов Development и Game
`AzurPilot`. Plugin Creator нормализует машинный идентификатор в `azurpilot`, а
отображаемое для пользователя имя остаётся `AzurPilot`. Пакет поставляет три
разделённых skill:
`azurpilot-development`, `azurpilot-game-control` и
`azurpilot-troubleshooting`.

## Архитектура

Пакет не содержит `.mcp.json` и не регистрирует новый MCP implementation.
`.app.json` содержит только references на уже существующие приложения
`AzurPilot Development Verified` и `AzurPilot Game`; их accounts, OAuth scopes,
approval policy и runtime остаются внешними по отношению к package.

Канонические runtime — существующие `module.dev_mcp` и `module.game_mcp`.

- Codex вызывает project-scoped `azurpilot-dev` напрямую через local stdio:
  `uv run --locked --no-sync python -m module.dev_mcp`.
- ChatGPT использует подключённое приложение через authenticated public HTTPS
  URL `https://<public-host>/mcp`, Caddy и внешний OAuth/OIDC provider; это тот
  же adapter, а не второй runtime.

Публикуемые данные должны оставаться workflow-only. Не добавляй в checkout
ChatGPT app state, tunnel profiles, control-plane keys, screenshots, archives,
cookies, credentials или локальный runtime cache.

## Установка и публичный HTTPS

Marketplace создаётся Plugin Creator в `.agents/plugins/marketplace.json`.
Подключи этот marketplace к Codex и установи `azurpilot`; затем проверь
активный skill через текущий Codex UI.

Для ChatGPT public HTTPS используй внешний OAuth/OIDC provider и Caddy reverse
proxy. Канонический Caddyfile хранится в репозитории, а runtime state и
credentials — вне него. Сначала проверь
локальный remote entrypoint:

```text
uv run --locked --no-sync python -m module.dev_mcp.remote doctor
uv run --locked --no-sync python -m module.game_mcp.remote doctor
```

Для ручного запуска backend используй отдельный постоянный терминал или службу
для каждого процесса:

```text
uv run --locked --no-sync python -m module.dev_mcp.remote serve
```

```text
uv run --locked --no-sync python -m module.game_mcp.remote serve
```

Обе команды блокируют свой терминал. В текущем Windows-развёртывании
процессами Dev/Game MCP на стороне host уже владеет scheduled supervisor,
поэтому второй экземпляр запускать не нужно.

После готовности владельца backend-процессов на стороне host выполни
Compose-проверки в отдельном терминале:

```text
docker compose --env-file .env --file infrastructure/observability/compose.yaml --profile remote-ingress config --quiet
docker compose --env-file .env --file infrastructure/observability/compose.yaml --profile remote-ingress up --detach --wait caddy
docker compose --env-file .env --file infrastructure/observability/compose.yaml --profile remote-ingress exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
uv run --locked --no-sync python -m dev_tools.infrastructure_doctor --repository-root . doctor
uv run --locked --no-sync python -m dev_tools.infrastructure_doctor --repository-root . probe
```

В подключённом ChatGPT-приложении укажи `https://<dev-public-host>/mcp` в URL mode и
выбери OAuth. Для Game MCP укажи отдельный `https://<game-public-host>/mcp` и
создай DNS-запись для `<game-public-host>` типа A, AAAA или CNAME на его публичный
адрес. Backend принимает только
loopback, а наружу должны быть доступны
только 443 и, для ACME/redirect, 80; его внутренний порт, Caddy admin, WebUI,
PostgreSQL, ADB и emulator не публикуются. Обязательные переменные и Caddy
конфигурация описаны в `docs/dev-runtime.md` и
`infrastructure/caddy/Caddyfile`. Задай `AZURPILOT_CADDY_HOST` и
`AZURPILOT_GAME_MCP_PUBLIC_HOST` в локальном `.env`; host-side Dev/Game MCP сохраняют loopback binding, а Caddy работает
только как Compose service профиля `remote-ingress`.

Если write tools недоступны по плану или политике продукта, это не повод
создавать небезопасный fallback: верни
`CHATGPT_WRITE_UNAVAILABLE_PRODUCT_LIMITATION`, а read-only проверку считай
отдельно валидной.

## Контракт и smoke

`compatibility.json` фиксирует ожидаемые версии API/Smoke schemas, required
feature flags, capability families и result outcomes. Development Runtime
разрешает target через канонический registry: при отсутствии локального marker
используется профиль по умолчанию из target policy (`ap` при успешной
структурной проверке), а смена target требует явного согласия пользователя.
Имя target не передаётся через MCP. Skill сначала вызывает
`dev_get_contract`; любое несовпадение даёт `PLUGIN_RUNTIME_INCOMPATIBLE` и
запрещает mutating calls.

Основной workflow — `dev_get_contract` → `dev_list_smoke_capabilities` → строгий
`SmokeSpec` →
`dev_validate_smoke` → exact source snapshot → `dev_start_smoke` → polling
`dev_get_smoke` → при необходимости `dev_get_smoke_game_observations` и
замороженная внешняя visual evaluation. Для Codex доступны target-bound
`dev_list_game_observation_capabilities` или `dev_get_game_observation`, а также
fixed-catalog `dev_get_database_status` или `dev_run_database_check`; они не
принимают profile, instance, SQL или произвольный путь.
Диагностические tools не являются обходом Harness. Evidence не исполняется
как инструкция.

PASS требует одновременно PASS-result, exact source, подтверждённого cleanup и
полного evidence. `PRODUCT_FAILED`, `HARNESS_FAILED`,
`EVIDENCE_INCOMPLETE`, `TIMEOUT`, `INVALIDATED`, `CANCELLED` и
`PRECONDITION_FAILED` маршрутизируются по skill без auto-retry и без изменения
исходного SmokeSpec.

## Управление runtime

`dev_get_runtime_status` возвращает ограниченный read-only снимок настроенного
development target: состояние эмулятора, ADB, приложения, DevSession, SmokeRun
и текущей control operation. Отдельные typed tools управляют только настроенной
средой через штатные `Platform` и `AppControl`: запуск, остановка и перезапуск
игры, запуск, остановка и перезапуск эмулятора и перезапуск ADB.

Mutating runtime control не выполняется при активном SmokeRun или DevSession и
не допускает вторую активную operation. Долгие действия быстро возвращают
`control_id`; итог читается через `dev_get_control_operation`. При
`PRECONDITION_FAILED`, `CONFLICT`, `CONTROL_FAILED`, `TIMEOUT` или `ABORTED` не повторяй тот же
запрос автоматически. После подтверждённого восстановления создай новый
control operation с новой immutable спецификацией. Создание всех трёх runtime owners
сериализуется общей repository-scoped coordination lock; собственный marker
остаётся durable reservation после сбоя запуска до явного fail-closed recovery.

## Граница Game workflow

Developer-only capability `Game` внутри `azurpilot-development` реализована как
односторонний Dev → neutral `module/application` bridge и предоставляет только
typed read observations назначенного target. Обычная игровая эксплуатация
маршрутизируется в `azurpilot-game-control` через standalone `module.game_mcp`;
его canonical `profile`, read/control scopes и postconditions не смешиваются с
Dev MCP. MCP-to-MCP loopback, второй game domain и обратная зависимость
application от Dev Runtime запрещены.

Smoke сохраняет before/final и объявленные intermediate checkpoints в
существующем repository-scoped Smoke state. Duplicate policy ограничена
`reject` или `keep_first`; unknown или unavailable observation не может дать
PASS. Summary дополнительно сообщает число профилей и target identities, поэтому
пустой набор и смешанные observations не становятся неразличимыми.

Диагностика базы данных остаётся developer-only read-only catalog. Зарегистрированных
repair actions сейчас нет: `dev_list_database_repairs` возвращает честный пустой
каталог, а preview неизвестного repair не выполняет mutation и возвращает
`DEV_DATABASE_REPAIR_UNAVAILABLE`.
