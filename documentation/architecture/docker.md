# Docker-инфраструктура

AzurPilotRu использует единый Compose project:

```text
azurpilot-infrastructure
```

Он содержит не только observability, но и production PostgreSQL/Redis и операторские сервисы.

## Основные сервисы

### PostgreSQL

`postgres` — постоянная database.

Host binding по умолчанию:

```text
127.0.0.1:5432
```

### Redis

`redis` — reconstructable runtime cache.

```text
127.0.0.1:6379
```

### postgres-bootstrap

Одноразовый Compose profile/service, создающий или обновляющий app/migrator роли и права.

Он не является постоянной database.

### pgAdmin

Локальный операторский UI PostgreSQL:

```text
http://127.0.0.1:5050/
```

### RedisInsight

Локальный UI Redis:

```text
http://127.0.0.1:5540/
```

Не является зависимостью runtime cache: если RedisInsight не настроен, Redis может продолжать работать.

## Observability services

Compose также содержит:

- Alloy;
- Loki;
- Prometheus;
- Tempo;
- Grafana.

Подробнее: [Наблюдаемость](observability.md).

## Caddy

Caddy включается только через opt-in profile `remote-ingress`.

Если public host не настроен, Start останавливает Caddy в этом project вместо публикации случайного endpoint.

При включении используются порты 80/443.

## .env

InfrastructureService требует canonical:

```text
infrastructure/observability/compose.yaml
.env
```

`.env` содержит локальные secrets и не должен попадать в Git или публичную диагностику.

## Что делает azur start

Start не просто выполняет произвольный `docker compose up`.

InfrastructureService:

1. проверяет canonical paths;
2. определяет, настроен ли Caddy;
3. выполняет controlled compose migration;
4. валидирует compose config;
5. поднимает PostgreSQL и Redis с health;
6. выполняет postgres-bootstrap;
7. подготавливает PostgreSQL runtime;
8. при необходимости поднимает Caddy;
9. проверяет фактические сервисы.

## Ownership

Docker Desktop/Engine + Compose владеют lifecycle контейнеров.

Не нужно параллельно вручную запускать второй PostgreSQL/Redis в WSL только потому, что приложение использует эти технологии.

## Named volumes

Постоянные volumes принадлежат Compose topology.

Обычный restart не требует их удаления.

Destructive volume cleanup — отдельная recovery operation, а не стандартная кнопка «починить Docker».
