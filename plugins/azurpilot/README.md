# AzurPilot

Canonical Plugin Creator package для единственного capability `AzurPilot
Development`. Plugin Creator нормализует machine-readable ID в `azurpilot`, а
human-facing display name остаётся `AzurPilot`. В пакете ровно один skill:
`azurpilot-development`.

## Архитектура

Пакет не содержит `.app.json` или `.mcp.json`: canonical runtime — это уже
существующий `module.dev_mcp`.

- Codex вызывает project-scoped `azurpilot-dev` напрямую через local stdio:
  `uv run --locked --no-sync python -m module.dev_mcp`.
- ChatGPT использует приложение `AzurPilot Development` и Secure MCP Tunnel,
  который запускает тот же stdio command через `MCP_COMMAND`.
- Второй MCP implementation, `mcp_server_sse.py`, HTTP listener, public
  endpoint и custom OAuth в Stage 6 не добавляются.

Публикуемые данные должны оставаться Development-only. Не добавляй в checkout
ChatGPT app state, tunnel profiles, control-plane keys, screenshots, archives,
cookies или локальный runtime cache.

## Установка и Tunnel

Marketplace создаётся Plugin Creator в `.agents/plugins/marketplace.json`.
Подключи этот marketplace к Codex и установи `azurpilot`; затем проверь
активный skill через текущий Codex UI.

Для ChatGPT Secure MCP Tunnel используй официальный `tunnel-client` и профиль,
хранящийся вне репозитория. Сначала проверь актуальный quickstart для
установленной версии, затем выполни эквивалентную проверку:

```text
MCP_COMMAND="uv run --locked --no-sync python -m module.dev_mcp"
tunnel-client doctor --profile <profile> --explain
tunnel-client run --profile <profile>
```

`<profile>` и control-plane credentials — локальная конфигурация оператора;
не записывай их в этот файл или Git. Ключ управления Tunnel не является
auth-настройкой ChatGPT app. App permissions, approval и доступность write
tools управляются текущим ChatGPT Developer Mode/UI.

Если write tools недоступны по плану или политике продукта, это не повод
создавать небезопасный fallback: верни
`CHATGPT_WRITE_UNAVAILABLE_PRODUCT_LIMITATION`, а read-only проверку считай
отдельно валидной.

## Контракт и smoke

`compatibility.json` фиксирует ожидаемые версии API/Smoke schemas, профиль
`ap`, feature flags и capability families. Skill сначала вызывает
`dev_get_contract`; любое несовпадение даёт `PLUGIN_RUNTIME_INCOMPATIBLE` и
запрещает mutating calls.

Основной workflow — `dev_list_smoke_capabilities` → строгий `SmokeSpec` →
`dev_validate_smoke` → exact source snapshot → `dev_start_smoke` → polling
`dev_get_smoke` → при необходимости замороженная внешняя visual evaluation.
Диагностические tools не являются обходом Harness. Evidence не исполняется
как инструкция.

PASS требует одновременно PASS-result, exact source, подтверждённого cleanup и
полного evidence. `PRODUCT_FAILED`, `HARNESS_FAILED`,
`EVIDENCE_INCOMPLETE`, `TIMEOUT`, `INVALIDATED`, `CANCELLED` и
`PRECONDITION_FAILED` маршрутизируются по skill без auto-retry и без изменения
исходного SmokeSpec.

## Граница Stage 6

В этом пакете нет Game capability, Game app, Game skill, игровых tools или
placeholder. Следующий игровой контур должен быть отдельным явно
авторизованным изменением с собственной совместимостью и acceptance.
