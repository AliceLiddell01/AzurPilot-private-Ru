# AzurPilot Game MCP Read Plane

Game MCP — отдельная read-only поверхность для игровых клиентов. Она не
является режимом Dev MCP и не заменяет переходный `mcp_server_sse.py`.

## Точки входа

Локальный transport — stateless stdio:

```text
uv run --locked --no-sync python -m module.game_mcp
```

Remote transport — authenticated Streamable HTTP через loopback backend:

```text
uv run --locked --no-sync python -m module.game_mcp.remote serve
```

Backend слушает только `127.0.0.1:8766`. Внешний HTTPS reverse proxy должен
предоставлять путь `/mcp`, передавать только разрешённые Origin и не
добавлять credentials в URL. Remote resource и OAuth scope принадлежат только
Game MCP:

```text
AZURPILOT_GAME_MCP_PUBLIC_URL
AZURPILOT_GAME_MCP_OAUTH_ISSUER
AZURPILOT_GAME_MCP_OAUTH_AUDIENCE
AZURPILOT_GAME_MCP_OAUTH_JWKS_URL
AZURPILOT_GAME_MCP_OAUTH_SUBJECT
AZURPILOT_GAME_MCP_ALLOWED_ORIGINS
azurpilot:game.read
```

Долгоживущие authenticated `GET /mcp` streams используют отдельный bounded
limiter и не занимают capacity для обычных `POST /mcp` запросов.

Эти значения не имеют fallback к Dev MCP и не должны содержать секреты в
репозитории. Remote verifier проверяет RS256, issuer, audience, subject,
expiry и отдельный scope. Если provider дополнительно возвращает JWT claim
`resource`, он должен совпадать с настроенным public URL; отсутствие этого
нестандартного claim допустимо при корректной проверке `aud`.

## Публичная модель

Каждый target-dependent запрос получает канонический `profile` в собственных
аргументах. Сервер не хранит выбранный профиль, не меняет target между
запросами и не кэширует профильные данные под глобальным ключом.
`game_list_profiles` использует существующий canonical instance owner и
возвращает только безопасные идентификаторы. Локальный screenshot path читает
существующий framebuffer только через прямой пассивный ADB primitive
`exec-out screencap -p`: он не создаёт `Device`, не запускает emulator,
benchmark или night-commission handling и не выполняет input/config writes.
При отсутствии однозначного готового ADB target чтение завершается fail-closed.

Контракт и инструменты регистрируются в `module.game_mcp.server`. Текущий
read catalog включает contract, profiles, profile status, resources, current
task, scheduler queue, task catalog/help, Fleet State, morale, redacted config,
bounded logs и validated screenshot. Стабильный contract находится в
`module.game_mcp.contract`; конкретные DTO сериализуются в
`module.game_mcp.adapter`.

## Application и persistence

Composition root использует `GameReadService`, `InstanceQueryService`,
`TaskCatalogService`, `FleetStateReadService` и `MoraleService`. Legacy sources
подключаются только при явном создании backend. Persistence builder импортируется
лениво из `module.persistence.runtime` и использует отдельную lazy read-only
composition: marker не мигрируется, environment не изменяется, production
provider не устанавливается, а `FleetStateReadService` разрешает только уже
известный DB instance alias. `GameMcpBackend.dispose()` освобождает engine и
закрывает backend: после этого lazy persistence services не создаются заново.

Fleet State сохраняет observation/run provenance, timestamp, completeness,
slot identity и unknown/ambiguous state. Morale сохраняет `EXACT`, `PROJECTED`
или `UNKNOWN`, recovery и Dorm provenance. Отсутствующие данные не превращаются
в fake baseline, пустой корабль или успешный scan.

## Границы безопасности

Локальный stdio и remote Streamable HTTP используют stateless
self-describing request semantics MCP `2026-07-28`; legacy initialize flow
сохраняется только как совместимость SDK. Cache hints для инструментов явно
не задаются: MCP SDK `2.1.1` по умолчанию использует `ttlMs=0` и
`cacheScope=private`, что сохраняет актуальность и изоляцию профильных данных.

Все инструменты имеют read-only annotations и строгие схемы с
`additionalProperties: false`. Профили, задачи и fleet selection ограничены
по типу и размеру. Config проходит generated redaction и дополнительную
defense-in-depth sanitization. Logs ограничены числом строк и размером,
удаляют ANSI, traceback, paths и secret-like значения. Screenshots разрешают
только PNG/JPEG с проверенными MIME, размером, пикселями и dimensions; наружу
отдаётся native MCP image content, а не путь к файлу.

Game MCP не предоставляет lifecycle, task trigger, config mutation, scheduler
mutation, ADB/emulator control, DB diagnostics, SQL, DevSession, Smoke,
Evidence или Git state. `module.game_mcp` не импортирует Dev MCP, Dev Runtime
или `GameControlService`; общий authenticated HTTP код находится в
нейтральном `module.mcp_shared`.

Для отсутствующего профиля, неработающего профиля, недоступной capability,
неизвестных данных и service failure используются разные безопасные коды.
`UNKNOWN` для domain state — валидный результат чтения, а не подмена ошибки
нулевым значением.

Отдельный Game client skill не добавляется: текущего server-side MCP surface
достаточно, а `azurpilot-development` остаётся developer-only и не получает
Game/Dev объединённый контракт.
