# Архитектура выполнения

## Главный процесс задач

Типовой поток:

```text
alas.py
  → загрузка конфигурации экземпляра
  → инициализация Device
  → выбор следующей задачи планировщиком
  → ленивый импорт обработчика
  → выполнение run/handler
  → сохранение результата и NextRun
  → следующая задача или ожидание
```

Перед изменением диспетчеризации проверить:

- как задача называется в конфигурации;
- где команда связывается с методом;
- какой тип результата ожидает вызывающий код;
- какие исключения считаются нормальным окончанием, recoverable-сбоем или ручным takeover;
- когда перечитывается конфигурация;
- кто владеет перезапуском приложения или эмулятора.

## Модель ModuleBase

Большинство игровых классов получают через базовые слои:

- конфигурацию;
- устройство и текущий screenshot;
- методы `appear`, `match`, `click` и OCR;
- общий logger;
- фоновые операции и таймеры.

Из-за mixin-архитектуры реализация метода может находиться не в классе верхнего уровня. Перед рефакторингом проверить MRO, импорты и все override.

## Цикл состояния

Надёжный обработчик повторяет:

1. получить новый screenshot, кроме явно обоснованного первого прохода;
2. проверить условия завершения;
3. обработать общие или приоритетные состояния;
4. выполнить не более одного меняющего экран действия;
5. начать новый проход.

Почему это важно:

- задержки эмулятора непостоянны;
- клики могут не сработать;
- может появиться диалог или переходный кадр;
- фиксированный `sleep` не подтверждает состояние;
- защита от повторных кликов основана на последовательности операций.

## Общие обработчики

`module/handler/` может обрабатывать login, информационные окна, auto-search, enemy searching, fast-forward и другие состояния. При «необъяснимом» переходе проверь, не срабатывает ли общий handler до целевого кода.

## WebUI

Типовой поток:

```text
gui.py
  → параметры запуска и deploy config
  → создание ASGI/PyWebIO приложения
  → ProcessManager для экземпляров
  → запуск/остановка задач в отдельных процессах
```

При изменении lifecycle проверять:

- Windows spawn-семантику;
- очистку дочерних процессов;
- restart event;
- корректное завершение Uvicorn;
- различие между остановкой WebUI и экземпляра задачи;
- совместимость со `Start-AzurPilot.ps1`.

Windows lifecycle пользовательской установки симметричен:

```text
Start-AzurPilot.ps1
  → repository-scoped owner mutex
  → repository-scoped kernel stop event
  → project Python + gui.py

Stop-AzurPilot.ps1
  → exact checkout/process ownership
  → stop event владельцу Start
  → bounded wait и только exact-owned fallback
```

Foreground Start, который сам создал backend, сохраняет управление через
`Ctrl+C`. Повторный Start только подтверждает готовность существующего WebUI,
открывает его и сообщает путь к Stop. Stop не завершает PostgreSQL и не считает
один лишь занятый порт доказательством ownership.

## MCP

MCP не должен становиться обходом конфигурационных и безопасностных границ. Для каждого инструмента проверить:

- schema входа;
- валидацию имени экземпляра и параметров;
- side effects;
- сериализацию ответа;
- обработку ошибок;
- доступ к screenshot, логам и пользовательским данным;
- отключаемость интеграции.

Не фиксировать в документации точное количество инструментов: оно меняется. Источник истины — регистрация tools в текущем коде.

## Основа хранения

Нейтральные DTO и порты хранения принадлежат `module.application`,
PostgreSQL-адаптеры — `module.persistence`. Типы SQLAlchemy/Psycopg не выходят
за инфраструктурную границу. Engine создаётся лениво отдельно в каждом PID
после запуска процесса; импорт пакета не подключается к БД и не выполняет DDL.

Schema изменяется только явной Alembic-командой. Для доменов schema v1 игровые,
WebUI и MCP consumers используют application storage services; только process
composition roots импортируют `module.persistence.runtime`. Обязательный
PostgreSQL marker проверяется fail-closed; SQLite fallback и dual-write
запрещены.

Per-ship Morale Core опирается на append-only Formation Fleet State как на
единственный источник состава. Dorm scanner наблюдает только UI-факты, а
reconciliation связывает их с физическим slot set-based и fail-closed. Exact
Dorm observation хранит baseline/rate/floor; complete двухэтажное отсутствие
хранит `unknown` morale с доказанным outside-Dorm recovery, не fake baseline.
Partial scan, замена occupant, смена формы, stale Fleet State или неоднозначный
slot не переносят состояние. Legacy Combat path этим этапом не подключён.
Canonical marker и другие runtime-state JSON находятся под `config/state/`, а
корневой `config/*.json` является только пространством кандидатов: игровым
профилем считается безопасный regular JSON, прошедший единый structural
classifier `module.config.profile`; произвольный report/state JSON профилем не
становится. Runtime state хранится только в `config/state/`.
Локальный `.env` загружается одним persistence owner и направляет libpq к
защищённым app/migrator passfiles без постоянного `PGPASSWORD`.

Offline migration pipeline проходит через application-owned порты. Legacy
SQLite/JSON adapters живут только в `module.persistence.legacy`, открывают
source read-only и path-bounded; PostgreSQL target пишет bounded chunks и затем
проверяет import ledger вместе с фактическими domain rows. Этот pipeline не
является runtime backend и не запускается из production entry points. После
cutover он сохраняется только для offline recovery из restricted archive.
