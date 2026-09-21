# Наблюдаемость

Observability-контур AzurPilotRu собирает **логи, метрики и трассировки** для локальной диагностики.

Он построен на:

- Grafana;
- Loki;
- Prometheus;
- Tempo;
- Alloy.

## Поток данных

Упрощённо:

```text
AzurPilot runtime
   │
   └── OTLP / structured signals
           ↓
         Alloy
        /  |  \
     Loki Prometheus Tempo
        \   |   /
          Grafana
```

## Grafana

Локальный UI:

```text
http://127.0.0.1:3000/
```

Anonymous access выключен.

Credentials хранятся в локальном `.env`.

## Loki

Хранит и индексирует application logs.

Loki не публикуется на host отдельным портом в canonical compose.

## Prometheus

Хранит metrics.

Текущая retention policy — 15 дней.

## Tempo

Хранит traces.

Текущий локальный retention ограничен.

## Alloy

Принимает telemetry и маршрутизирует её в остальные компоненты.

На host опубликованы OTLP endpoints:

```text
127.0.0.1:4317
127.0.0.1:4318
```

## Telemetry не владеет runtime

Очень важное ограничение:

- Loki log не доказывает, что task сейчас ещё выполняется;
- scheduler queue не доказывает execution;
- trace не является process ownership.

Authoritative execution определяется runtime state + worker identity через соответствующие application/Game MCP boundaries.

## Fail-open для игры

Ошибка необязательной observability не должна ломать пользовательский logger или игровой runtime.

Console/WebUI и bounded diagnostic context остаются доступны независимо от OTLP.

## Retention

Canonical локальный контур использует:

- Loki — 7 дней;
- Tempo — 7 дней;
- Prometheus — 15 дней.

Это настройки локального deployment, а не вечное архивное хранилище.

## Операторские панели

В Grafana предусмотрены dashboards для:

- общего обзора;
- ошибок/incidents;
- фильтрации по environment/profile/task;
- переходов между signals.

Обычному пользователю Grafana нужна прежде всего для сложной диагностики, а не для ежедневного управления AzurPilotRu.
