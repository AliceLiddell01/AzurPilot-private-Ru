# AzurPilot

Canonical Plugin Creator package для Development workflow `AzurPilot`. Plugin
Creator нормализует machine-readable ID в `azurpilot`, а human-facing display
name остаётся `AzurPilot`. Пакет поставляет skill `azurpilot-development`.

## Архитектура

Пакет не содержит `.app.json` или `.mcp.json`: canonical runtime — это уже
существующий `module.dev_mcp`.

- Codex вызывает project-scoped `azurpilot-dev` напрямую через local stdio:
  `uv run --locked --no-sync python -m module.dev_mcp`.
- ChatGPT использует подключённое приложение через authenticated public HTTPS
  URL `https://<public-host>/mcp`, Caddy и внешний OAuth/OIDC provider; это тот
  же adapter, а не второй runtime.
- `mcp_server_sse.py` остаётся отдельным production MCP и не используется этим
  приложением.

Публикуемые данные должны оставаться Development-only. Не добавляй в checkout
ChatGPT app state, tunnel profiles, control-plane keys, screenshots, archives,
cookies или локальный runtime cache.

## Установка и public HTTPS

Marketplace создаётся Plugin Creator в `.agents/plugins/marketplace.json`.
Подключи этот marketplace к Codex и установи `azurpilot`; затем проверь
активный skill через текущий Codex UI.

Для ChatGPT public HTTPS используй внешний OAuth/OIDC provider и Caddy reverse
proxy. Конфигурация и credentials хранятся вне репозитория. Сначала проверь
локальный remote entrypoint:

```text
uv run --locked --no-sync python -m module.dev_mcp.remote doctor
uv run --locked --no-sync python -m module.dev_mcp.remote
caddy validate --config docs/dev-mcp/Caddyfile
caddy run --config docs/dev-mcp/Caddyfile
```

В подключённом ChatGPT-приложении укажи `https://<public-host>/mcp` в URL mode и
выбери OAuth. Backend принимает только loopback, а наружу должны быть доступны
только 443 и, для ACME/redirect, 80; его внутренний порт, Caddy admin, WebUI,
PostgreSQL, ADB и emulator не публикуются. Обязательные переменные и Caddy
шаблон описаны в `docs/dev-runtime.md` и
`docs/dev-mcp/Caddyfile.example`; перед запуском сохрани локальную копию
`docs/dev-mcp/Caddyfile` с собственным host.

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
`dev_get_smoke` → при необходимости замороженная внешняя visual evaluation.
Диагностические tools не являются обходом Harness. Evidence не исполняется
как инструкция.

PASS требует одновременно PASS-result, exact source, подтверждённого cleanup и
полного evidence. `PRODUCT_FAILED`, `HARNESS_FAILED`,
`EVIDENCE_INCOMPLETE`, `TIMEOUT`, `INVALIDATED`, `CANCELLED` и
`PRECONDITION_FAILED` маршрутизируются по skill без auto-retry и без изменения
исходного SmokeSpec.

## Runtime Control

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
SmokeRun с новой immutable спецификацией. Создание всех трёх runtime owners
сериализуется общей repository-scoped coordination lock; собственный marker
остаётся durable reservation после сбоя запуска до явного fail-closed recovery.

## Граница Game capability

Текущий Development package не предоставляет Game capability, Game app, Game
skill или игровые tools. Game boundary должен добавляться отдельным явно
совместимым расширением с собственной совместимостью и acceptance.
