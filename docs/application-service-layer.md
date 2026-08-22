# Application/Service Layer foundation

Stage 3 вводит нейтральную прикладную границу для будущего AzurPilot Web. Она
пока не подключена к production WebUI или MCP и обслуживает только read-only
сценарии:

- список экземпляров, один статус и статусы всех экземпляров;
- список задач и metadata/help выбранной задачи.

Публичные результаты представлены неизменяемыми dataclass DTO. Через границу
не передаются `ProcessManager`, config dictionaries, `State`, device objects,
paths или transport-specific responses. Ошибки нормализованы в небольшой набор
`ApplicationError`, который будущий HTTP/MCP-адаптер сможет самостоятельно
преобразовать в свой протокол.

## Границы и зависимости

`module.application.services` зависит только от узких read-only Protocol.
`LegacyInstanceRuntimeAdapter` и `GeneratedTaskCatalogAdapter` — временные
адаптеры к существующим источникам. Они загружают legacy-модули только при
явном вызове; простой `import module.application` не читает config, не создаёт
`ProcessManager` и не запускает runtime.

Canonical task metadata остаются в generated `module/config/argument/args.json`
и `module/config/i18n/ru-RU.json`. Новый слой не заменяет генератор и не копирует
`McpConfigHelper`: адаптер формирует immutable-проекцию из тех же источников.

Физическое размещение `ProcessManager` в `module.webui` — зафиксированный legacy
ownership debt. На этой стадии менеджер не переносится и не дублируется. Его
status properties могут очищать устаревшие записи process registry, поэтому
чтение runtime status намеренно не объявляется pure operation.

## Замороженное production wiring

`module/webui/app.py`, `mcp_server_sse.py`, текущие route/tool catalogs и runtime
entrypoints не используют новый слой. Их переключение, write-команды, auth,
transport schemas и перенос process ownership относятся к последующим стадиям.

## Пример будущей composition root

```python
from module.application import InstanceQueryService, TaskCatalogService
from module.application.legacy_adapters import (
    GeneratedTaskCatalogAdapter,
    LegacyInstanceRuntimeAdapter,
)

instances = InstanceQueryService(LegacyInstanceRuntimeAdapter())
tasks = TaskCatalogService(GeneratedTaskCatalogAdapter.from_generated_sources())
```

Создание generated-адаптера является явной I/O-операцией. Транспорт должен
создавать его в своей composition root, а не на уровне импорта модуля.
