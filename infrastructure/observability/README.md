# Локальная observability-инфраструктура AzurPilot

Эта папка содержит переносимый Docker Compose-контур для приёма OTLP и
локального хранения logs, metrics и traces. Compose project имеет постоянное
имя azurpilot-observability.

## Состав

| Сервис | Назначение | Образ |
| --- | --- | --- |
| alloy | loopback OTLP endpoint и маршрутизация telemetry | grafana/alloy:v1.19.2 |
| loki | хранение logs | grafana/loki:3.7.4 |
| prometheus | хранение metrics и remote-write receiver | prom/prometheus:v3.14.0 |
| tempo | хранение traces и OTLP receiver | grafana/tempo:2.10.5 |
| grafana | локальная визуализация подключённых data sources | grafana/grafana:13.2.1 |

В compose.yaml для каждого образа зафиксированы version tag и digest.
Образы являются официальными образами соответствующих проектов.

Alloy принимает OTLP по 127.0.0.1:4317 (gRPC) и 127.0.0.1:4318 (HTTP).
Grafana доступна по 127.0.0.1:3000. Loki, Prometheus и Tempo не публикуются
на host: Alloy и Grafana обращаются к ним через стандартную Compose network и
service DNS.

## Данные и секрет

Состояние хранится в именованных volumes:

- azurpilot-observability_alloy-data;
- azurpilot-observability_loki-data;
- azurpilot-observability_prometheus-data;
- azurpilot-observability_tempo-data;
- azurpilot-observability_grafana-data.

Все секреты этого контура хранятся в общем локальном .env, игнорируемом Git,
в корне репозитория. Сейчас используются переменные
AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER и
AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD; новые секреты этой
архитектуры нужно добавлять туда же. Пароль начального администратора Grafana
передаётся через Compose secret и не попадает в репозиторий.

Если переменных ещё нет, добавьте их в корневой .env. Для ротации уже
добавленного пароля используйте PowerShell-команду ниже: она сохраняет новое
значение напрямую в .env и ничего не выводит в stdout.

    AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=admin
    AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=<случайный_секрет>

    $envFile = Resolve-Path ..\..\.env
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $password = [Convert]::ToHexString($bytes).ToLowerInvariant()
    $lines = Get-Content -LiteralPath $envFile
    $lines -replace '^AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=.*$', "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=$password" |
        Set-Content -LiteralPath $envFile -Encoding utf8NoBOM
    Remove-Variable password

## Запуск и обслуживание

Из этой папки:

    docker compose --env-file ../../.env config
    docker compose --env-file ../../.env pull
    docker compose --env-file ../../.env up -d
    docker compose --env-file ../../.env ps
    docker compose --env-file ../../.env logs --tail=100 alloy loki prometheus tempo grafana

docker compose --env-file ../../.env config проверяет итоговую топологию и отсутствие
неожиданного host binding. pull загружает зафиксированные образы после чистого
clone. Повторный up -d должен быть идемпотентным.

Для штатной остановки выполните:

    docker compose --env-file ../../.env stop
    docker compose --env-file ../../.env start

Не используйте docker compose --env-file ../../.env down -v: volumes содержат
накопленные данные. Если нужно удалить сам Compose-контур без данных,
используйте docker compose --env-file ../../.env down; volumes и их содержимое
останутся доступными для последующего запуска.

## Хранилище и retention

Контур рассчитан на один локальный узел. Loki и Tempo используют filesystem
storage в своих named volumes; Prometheus хранит TSDB в отдельном volume.
Retention ограничен 7 днями для Loki и Tempo и 15 днями для Prometheus.
Эти настройки предназначены для локального контура и могут быть заменены
deployment-specific override без изменения service topology.

## Подключение application logs

AzurPilot подключает application logs явно после настройки штатного локального
logger-а через `set_file_logger()`. Подключение не выполняется при импорте
модулей: пока endpoint не задан, приложение работает в обычном offline-режиме
только с console/WebUI/file handlers. После opt-in приложение использует
официальные OpenTelemetry Logs bridge и OTLP/HTTP protobuf BatchLogRecordProcessor;
прямых зависимостей от Loki, Prometheus, Tempo или Grafana в application code
нет.

Для локального Compose-контура перед запуском AzurPilot передайте процессу
стандартные `OTEL_*` переменные:

    $env:OTEL_EXPORTER_OTLP_LOGS_ENDPOINT = 'http://127.0.0.1:4318/v1/logs'
    $env:OTEL_EXPORTER_OTLP_LOGS_PROTOCOL = 'http/protobuf'
    $env:OTEL_RESOURCE_ATTRIBUTES = 'deployment.environment.name=local'
    $env:OTEL_PYTHON_LOG_HANDLER_LEVEL = 'INFO'

Допустим также общий `OTEL_EXPORTER_OTLP_ENDPOINT`; для logs используется
`http/protobuf`. Таймауты и bounded batch-параметры читаются из стандартных
`OTEL_EXPORTER_OTLP_LOGS_TIMEOUT`, `OTEL_BLRP_SCHEDULE_DELAY`,
`OTEL_BLRP_MAX_QUEUE_SIZE`, `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` и
`OTEL_BLRP_EXPORT_TIMEOUT`. `OTEL_SDK_DISABLED=true` отключает application
exporter независимо от endpoint. `.env` Compose автоматически не загружается
в процесс AzurPilot: это намеренно отдельные границы конфигурации.

В удалённую запись попадают стабильный `service.name=azurpilot`, окружение,
`profile`, canonical task context, component, run id, process id/command и
структурированные exception attributes. Человеческое body очищается от ANSI,
Rich markup, секретов и локальных абсолютных путей; локальный `LogRecord` не
изменяется. В Loki только `service.name` и
`deployment.environment.name` являются index labels, остальные metadata остаются
structured metadata. Ошибка exporter, его недоступность или bounded shutdown не
останавливают gameplay, WebUI, console и локальный file fallback.

Исторические `log/`-артефакты не импортируются и не удаляются. `log/error/`
остаётся локальным incident store со скриншотами и `log.txt`, диагностические и
архивные каталоги сохраняются, CSV/JSON относятся к data/export или legacy
storage и не считаются application logs. Скриншоты и object-store слой в этот
контур не отправляются.

Portable base Compose не содержит Windows drive letters, WSL paths,
host.docker.internal, захардкоженные IP, host networking или публичные
bindings. Межсервисные URL используют service DNS, конфигурации подключаются
repository-relative paths, а persistent state отделён named volumes. Поэтому
тот же base contract можно перенести на VPS с отдельными secrets, host
bindings и внешним endpoint в deployment-specific настройках, не меняя
топологию сервисов.
