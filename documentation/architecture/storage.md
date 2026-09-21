# Хранение данных

Текущая production-архитектура AzurPilotRu разделяет **постоянные данные** и **восстанавливаемый cache**.

## PostgreSQL

PostgreSQL — durable source of truth для production storage.

Application/WebUI/MCP composition roots подключают PostgreSQL provider и проверяют health до приёма соответствующей работы.

Database-specific детали не должны проходить через application boundary наружу.

## Redis

Redis — reconstructable runtime cache.

Ключевой принцип:

> потеря Redis не должна означать потерю доменных данных.

В Redis не переносится ownership постоянных Commission/AP/RewardDorm/Main и других игровых схем только ради скорости.

При cache miss корректный результат может быть `None`, после чего authoritative данные читаются по штатной границе.

## Почему нет silent in-memory fallback

Если canonical Redis недоступен, runtime не должен незаметно переключиться на отдельный процессный cache и создать два разных представления состояния.

Ошибки cache boundary классифицируются явно: not configured, unavailable, timeout, auth failed, invalid data, unknown.

## PostgreSQL в Docker

Canonical PostgreSQL управляется Docker Compose.

По умолчанию host endpoint:

```text
127.0.0.1:5432
```

Внутри Compose используется service DNS `postgres:5432`.

## Redis в Docker

Host endpoint:

```text
127.0.0.1:6379
```

Внутри Compose:

```text
redis:6379
```

Redis использует отдельные app/admin credentials. App user ограничен namespace `azurpilot:*` и cache-командами.

## Named volumes

PostgreSQL и Redis используют Docker volumes.

Для PostgreSQL volume содержит durable database.

Redis volume сохраняет AOF/cache между обычными restart, но эти данные всё равно считаются reconstructable.

## Backup

`azur update` создаёт внешний PostgreSQL custom-format dump.

Он:

- находится вне checkout;
- проверяется;
- получает digest/provenance;
- блокирует Update при неуспехе.

Redis backup не заменяет PostgreSQL backup.

## Что нельзя делать как обычное обслуживание

Не используйте без явной recovery-задачи:

```text
docker compose down -v
docker volume prune
docker system prune
```

Также raw копирование PostgreSQL PGDATA не является штатным логическим backup проекта.

Подробнее: [Данные и резервные копии](../maintenance/data-backups.md).
