# MCP

AzurPilotRu содержит собственные серверы Model Context Protocol для работы AI/инструментов с проектом.

Есть две разные first-party surface:

- **Dev MCP** — разработка и development runtime;
- **Game MCP** — пользовательский игровой runtime.

Их не следует смешивать.

## Dev MCP

Dev MCP предназначен для developer workflows.

Он работает поверх общего WebUI runtime и development target, а не создаёт второй WebUI.

Через него доступны, среди прочего:

- preflight/doctor;
- contract;
- каталог задач;
- планирование development session;
- start/status/stop/cleanup/recover;
- evidence/timeline;
- screenshot;
- smoke capabilities.

## Game MCP

Game MCP предоставляет bounded access к игровому runtime.

Его capabilities включают профили, задачи, queue/status, configuration read, logs/screenshots и поддерживаемые control operations.

Game MCP использует собственную application/control boundary и не является thin wrapper над Dev MCP.

## Local stdio

Canonical standalone registrations:

```text
azurpilot-dev
azurpilot-game
```

Они запускаются project-scoped через `uv run --locked --no-sync`.

## Local HTTP

Desktop supervisor предоставляет authenticated loopback routes:

```text
Dev:  http://127.0.0.1:8775/mcp
Game: http://127.0.0.1:8776/mcp
```

Токены берутся из user-level environment.

Это first-class local transports, а не аварийный fallback.

## Backend ports

First-party host backend процессы используют:

```text
127.0.0.1:8765
127.0.0.1:8766
```

Они не должны автоматически публиковаться наружу.

## Public remote

Для явно настроенного удалённого доступа используются отдельные authenticated HTTPS surfaces через Caddy/OAuth/OIDC.

Наличие local MCP не означает, что public endpoint включён.

## Версии и контракт

Canonical bundle версий находится в:

```text
config/mcp-versions.toml
```

Он связывает:

- server SemVer;
- API/contract identity;
- catalog fingerprints;
- source digests;
- plugin compatibility.

Plugin metadata — derived snapshot, а не второй источник истины.

## azur mcp

Операторский CLI:

```text
azur mcp status
azur mcp versions
azur mcp start
azur mcp stop
azur mcp restart
azur mcp reconcile
```

`reconcile --source` — mutating developer operation.

## Безопасность

MCP contracts ограничивают входы/выходы и не должны выдавать произвольные:

- filesystem paths;
- environment;
- credentials;
- stack traces.

Read-only и mutating capabilities разделяются явно.
