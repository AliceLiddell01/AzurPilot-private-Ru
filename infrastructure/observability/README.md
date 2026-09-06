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
`OTEL_BLRP_EXPORT_TIMEOUT`. `OTEL_SDK_DISABLED=true` отключает все application
signals независимо от endpoint. `.env` Compose автоматически не загружается
в процесс AzurPilot: это намеренно отдельные границы конфигурации.

В удалённую запись попадают стабильный `service.name=azurpilot`, окружение,
`profile`, canonical task context, component, run id, process id/command и
структурированные exception attributes. Человеческое body очищается от ANSI,
Rich markup, секретов и локальных абсолютных путей; локальный `LogRecord` не
изменяется. В Loki только `service.name` и
`deployment.environment.name` являются index labels, остальные metadata остаются
structured metadata. Ошибка exporter, его недоступность или bounded shutdown не
останавливают gameplay, WebUI, console и локальный file fallback.

## Подключение application metrics

Application metrics подключаются независимо от logs. В процессе используется один
process-local `MeterProvider` с официальным `PeriodicExportingMetricReader` и
OTLP/HTTP protobuf exporter. Без metrics endpoint приложение не создаёт metrics
provider и не выполняет сетевых запросов.

Для локального Compose-контура перед запуском AzurPilot задайте:

    $env:OTEL_EXPORTER_OTLP_METRICS_ENDPOINT = 'http://127.0.0.1:4318/v1/metrics'
    $env:OTEL_EXPORTER_OTLP_METRICS_PROTOCOL = 'http/protobuf'
    $env:OTEL_METRIC_EXPORT_INTERVAL = '60000'
    $env:OTEL_METRIC_EXPORT_TIMEOUT = '30000'

Signal-specific endpoint передаётся exporter-у как полный URL. При использовании
общего `OTEL_EXPORTER_OTLP_ENDPOINT` официальный exporter добавляет стандартный
путь `/v1/metrics`; `OTEL_EXPORTER_OTLP_METRICS_TIMEOUT` имеет приоритет над
общим `OTEL_EXPORTER_OTLP_TIMEOUT`. Поддерживается только `http/protobuf`.
`OTEL_SDK_DISABLED=true` отключает все application signals.

Стандартный `OTEL_METRICS_EXEMPLAR_FILTER` остаётся под управлением OTel SDK.
При активной scheduler task SDK может связать exemplar с текущим application
trace; application code не добавляет trace/span IDs в metric labels. Наличие
exemplar проверяется только по фактически собранному SDK reader, а не по
предположению о downstream storage.

В текущей конфигурации отправляются два инструмента на одной canonical task
boundary scheduler-а в `Alas.loop`:

| OTel name | Type | Unit | Attributes |
| --- | --- | --- | --- |
| `azurpilot.task.run` | Counter | `{run}` | `azurpilot.profile`, `azurpilot.task`, `azurpilot.task.outcome` |
| `azurpilot.task.duration` | Histogram | `s` | `azurpilot.profile`, `azurpilot.task`, `azurpilot.task.outcome` |

`azurpilot.task.outcome` ограничен значениями `success`, `recoverable`,
`failure`, `stopped` и `unknown`. Значение profile проверяется через canonical
project identity и допускает Unicode и внутренние пробелы, а task принимает
только bounded ASCII-имя из registry. Неизвестные или небезопасные значения
становятся `unknown`; новый произвольный task не создаёт новую metric series.
Scheduler queue gauge намеренно не добавляется: у scheduler нет единственного
authoritative queue snapshot для корректного значения.

SDK использует cumulative temporality, совместимую с текущим Alloy metrics
path. При явном отличном от `cumulative` значении
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` application metrics
отключаются fail-open, потому что downstream Prometheus path не принимает
delta series.

Alloy переносит только `service.name` и `deployment.environment.name` из
resource attributes в datapoint attributes. `resource_to_telemetry_conversion`
остаётся выключенным; `target_info`, `otel_scope_info` и scope labels также не
создаются, поэтому произвольные resource attributes не превращаются в
Prometheus labels. Logs и traces проходят по прежним маршрутам. Prometheus не
является прямой application dependency.

Пример bounded PromQL для числа запусков по outcome:

    sum by (azurpilot_task_outcome) (rate(azurpilot_task_run_total[5m]))

Для безопасной локальной проверки без публикации Prometheus на host выполните
запрос из существующего Compose network:

    docker compose --env-file ../../.env exec -T prometheus wget -qO- 'http://127.0.0.1:9090/api/v1/query?query=azurpilot_task_run_total'

Недоступность metrics exporter, ошибка записи и bounded shutdown не меняют
результат задачи и не останавливают scheduler. Локальные logs продолжают
работать, даже если metrics signal не удалось инициализировать или отправить.

## Подключение application traces

Application traces подключаются независимо от logs и metrics. Включение
происходит только после явного endpoint opt-in; без trace endpoint приложение не
импортирует OTel SDK, не запускает worker и не выполняет сетевых запросов.
Используется официальный OTLP/HTTP protobuf exporter через существующий Alloy,
без прямых зависимостей application code от Tempo.

Для локального Compose-контура перед запуском AzurPilot задайте:

    $env:OTEL_EXPORTER_OTLP_TRACES_ENDPOINT = 'http://127.0.0.1:4318/v1/traces'
    $env:OTEL_EXPORTER_OTLP_TRACES_PROTOCOL = 'http/protobuf'
    $env:OTEL_EXPORTER_OTLP_TRACES_TIMEOUT = '30000'
    $env:OTEL_TRACES_SAMPLER = 'parentbased_always_on'

Допустим общий `OTEL_EXPORTER_OTLP_ENDPOINT`; signal-specific endpoint имеет
приоритет над ним. Для traces также поддерживаются общий
`OTEL_EXPORTER_OTLP_PROTOCOL` и `OTEL_EXPORTER_OTLP_TIMEOUT`, а
`OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` имеет приоритет над общим таймаутом.
Поддерживается только `http/protobuf`. При signal-specific endpoint полный
путь используется как задан; при общем endpoint SDK добавляет ровно
`/v1/traces`, поэтому дублирование этого пути не допускается. Параметры
`OTEL_BSP_SCHEDULE_DELAY`, `OTEL_BSP_MAX_QUEUE_SIZE`,
`OTEL_BSP_MAX_EXPORT_BATCH_SIZE` и `OTEL_BSP_EXPORT_TIMEOUT` ограничиваются
локальным bounded contract. `OTEL_TRACES_SAMPLER` и
`OTEL_TRACES_SAMPLER_ARG` передаются стандартному SDK. `OTEL_SDK_DISABLED=true`
отключает traces вместе с logs и metrics.

Корневой span создаётся один раз на фактическую scheduler task в границе
`Alas._run_scheduler_task` с именем `azurpilot.task.run`. В нём находятся
только bounded canonical `azurpilot.profile`, `azurpilot.task` и исход
`azurpilot.task.outcome`; `success`, `stopped` и `recoverable` не получают
ошибочный статус, а `failure` и необработанное исключение получают `ERROR`.
Внутри этой границы допускаются только значимые стабильные операции, например
`azurpilot.device.screenshot`, `azurpilot.ocr.process` и
`azurpilot.ui.wait`; generic `Alas.run("goto_main")` отдельный task root не
создаёт. Root и child spans используют `BatchSpanProcessor`; они не создаются
для каждого клика или события.

Trace runtime process-local, идемпотентен и fail-open. После fork унаследованный
provider отключается и child process требует свежего bootstrap; network и
OTel worker не стартуют при импорте. Shutdown выполняет bounded flush и
закрытие provider, а ошибка exporter или timeout не останавливает scheduler,
WebUI, gameplay или остальные signals. Logs получают correlation из текущего
OTel context через штатный logging bridge: `trace_id` и `span_id` доступны в
структурированном log record и не становятся Loki index labels.

В span attributes, log body и exception events не передаются raw OCR/UI data,
абсолютные пути, credentials, токены, cookies или необработанные exception
objects. Ошибки записываются только в bounded sanitized форме. Trace IDs не
используются как metric labels; связь metric exemplar с активным span зависит
от фактически поддержанного SDK reader и проверяется измерением.

Проверка локального пути выполняется через существующий Compose project:
`docker compose --env-file ../../.env config --quiet` и `ps` должны быть
успешны, приложение отправляет OTLP только на loopback Alloy, а запросы к
Loki, Prometheus и Tempo выполняются из соответствующего Compose network.

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
