# Сетевые адреса и порты

Большинство локальных сервисов AzurPilotRu привязаны к loopback и не должны быть доступны из внешней сети по умолчанию.

## Пользовательские сервисы

| Сервис | Адрес/порт по умолчанию | Назначение |
|---|---:|---|
| WebUI | `127.0.0.1:25548` | основной интерфейс |
| ADB server | обычно `127.0.0.1:5037` | Android Debug Bridge |
| OCR RPC | `127.0.0.1:22268` | опциональный OCR server |

WebUI deploy template может слушать `0.0.0.0`, но локальный tooling URL нормализуется на `127.0.0.1`. Не публикуйте WebUI наружу без отдельной модели доступа.

## PostgreSQL и Redis

| Сервис | Loopback |
|---|---:|
| PostgreSQL | `127.0.0.1:5432` |
| Redis | `127.0.0.1:6379` |
| pgAdmin | `127.0.0.1:5050` |
| RedisInsight | `127.0.0.1:5540` |

Порты PostgreSQL/Redis/pgAdmin/RedisInsight могут быть переопределены environment variables.

## Observability

| Сервис | Loopback/внутренний порт |
|---|---:|
| Grafana | `127.0.0.1:3000` |
| OTLP gRPC | `127.0.0.1:4317` |
| OTLP HTTP | `127.0.0.1:4318` |
| Loki | internal `3100` |
| Prometheus | internal `9090` |
| Tempo | internal `3200` |

Loki/Prometheus/Tempo в compose не публикуются на host напрямую.

## MCP

Host-side backend endpoints:

```text
Dev MCP:  127.0.0.1:8765
Game MCP: 127.0.0.1:8766
```

First-class authenticated local HTTP aliases для Desktop supervisor:

```text
Dev:  127.0.0.1:8775
Game: 127.0.0.1:8776
```

Это разные transport routes одной first-party MCP системы, а не два независимых продукта.

## Caddy remote ingress

Caddy — opt-in.

Если remote ingress настроен, compose публикует:

- TCP 80;
- TCP 443;
- UDP 443 для HTTP/3.

Admin API Caddy и backend-порты MCP/WebUI/PostgreSQL не должны автоматически публиковаться наружу.

## Правило диагностики портов

Если нужный порт занят:

1. определите владельца;
2. не завершайте процесс вслепую;
3. используйте `azur doctor`/штатный lifecycle;
4. только после подтверждения решайте конфликт.
