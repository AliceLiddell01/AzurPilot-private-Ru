# Основные подсистемы

Ниже — навигационная карта репозитория. Это не полный список каталогов, а ответ на вопрос **«где искать владельца поведения?»**.

## module/

Основной runtime AzurPilot.

Здесь находятся:

- WebUI;
- конфигурация;
- планировщик;
- device/ADB;
- screenshot/input;
- OCR;
- игровые handlers;
- campaign/combat;
- Operation Siren;
- island;
- application layer;
- persistence;
- Game/Dev MCP runtime-компоненты;
- observability hooks.

Если изменение влияет на фактическое игровое поведение, его владелец чаще всего находится здесь или в `campaign/`.

## campaign/

Данные и логика конкретных карт кампании/событий.

Не путайте этот каталог с общим orchestrator campaign runtime в `module/campaign/`.

## azurpilot/

Операционный слой персональной версии:

- CLI `azur`;
- Build/Start/Stop/Repair/Update;
- Docker deployment tooling;
- Git delivery/PR tooling;
- MCP reconciliation;
- внешние integration adapters.

Если проблема возникает до запуска WebUI или при обслуживании установки, искать следует здесь.

## config/

Пользовательские и deploy-конфигурации:

- profiles;
- `deploy.yaml`;
- templates;
- generated config.

Не все файлы `config/` являются пользовательскими профилями.

## module/config/argument/

Source-описание пользовательской конфигурации:

- `task.yaml`;
- `argument.yaml`;
- другие YAML definitions.

Из них generator строит производные JSON/Python структуры.

## assets/

Изображения и другие ресурсы распознавания UI.

Изменения assets должны проверяться против фактического Global/EN интерфейса.

## infrastructure/

Docker Compose и локальная инфраструктура:

- PostgreSQL;
- Redis;
- Grafana/Loki/Prometheus/Tempo/Alloy;
- Caddy;
- pgAdmin/RedisInsight.

## dev_tools/

Инженерные утилиты, migrations, audits, gates и служебные проверки.

Это не основной пользовательский CLI.

## tests/

Автоматический test suite, организованный по доменам.

Повторно используемые test helpers живут в `tests/support/`, fixtures — в `tests/fixtures/<domain>/`.

## tools/acceptance/

Контролируемые live/browser/emulator acceptance scenarios.

Они намеренно отделены от обычного pytest collection.

## plugins/

Пакет интеграции AzurPilot с ChatGPT/Codex и compatibility metadata.

Не является владельцем самого game runtime.

## docs/

Инженерный corpus проекта: подробные migration/architecture/contracts документы.

## documentation/

Публичная GitBook-документация, которую вы сейчас читаете.

## .codex/context/

Внутренний agent context.

Он оптимизирован для coding agents и не является пользовательской или developer Wiki. Не копируйте его в `documentation/` как источник готового текста.
