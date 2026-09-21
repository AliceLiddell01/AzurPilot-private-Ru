# WebUI и рабочие процессы

WebUI — пользовательская поверхность AzurPilotRu, но не единственный владелец состояния проекта.

Архитектурно важно различать:

- WebUI server;
- пользовательские профили;
- worker конкретного профиля;
- планировщик;
- application services;
- storage.

## Один WebUI, несколько профилей

WebUI является общей управляющей поверхностью для профилей.

Профиль имеет собственную конфигурацию и runtime context. Его worker может выполнять scheduler task независимо от того, какую страницу сейчас открыл пользователь.

## ProcessManager и legacy ownership

Исторически часть runtime ownership остаётся в `module.webui`, включая `ProcessManager`.

Проект постепенно выносит нейтральные операции в application layer, но не создаёт второй параллельный runtime только ради новой архитектуры.

Это означает: новые transports должны переиспользовать существующий authoritative runtime, а не поднимать собственную копию WebUI.

## Application services

Нейтральные сервисы дают доступ к:

- списку профилей;
- статусу одного/всех профилей;
- каталогу задач;
- metadata/help;
- Fleet State;
- morale;
- storage.

DTO не должны протаскивать наружу внутренние UI/device/process objects.

## Runtime state и scheduler projection

Существует принципиальная разница между:

- **что выполняется сейчас**;
- **что запланировано дальше**.

`Scheduler.NextRun`, pending/waiting task и очередь описывают будущую работу.

Они не являются доказательством текущей активной задачи.

## Worker identity

Read-only registry сам по себе может содержать запись уже завершившегося процесса.

Поэтому authoritative status path дополнительно проверяет фактическую process identity.

Если состояние неоднозначно или snapshot повреждён, система должна сохранить `unknown`, а не превращать отсутствие доказательства в «stopped».

## Dev Runtime

Development runtime не поднимает второй WebUI.

Он создаёт логическую development session поверх общего WebUI owner и отдельного development target.

Так developer tools не конкурируют с основной инфраструктурой за другой server.

## Почему это важно пользователю

Эта архитектура объясняет несколько наблюдаемых эффектов:

- закрытие браузера не останавливает worker;
- `NextRun` не означает, что текущая задача закончилась;
- повторный `azur start` проверяет существующий owner;
- Game MCP не должен угадывать execution по логам;
- developer session не должна создавать второй WebUI.
