# Конфигурация и локализация

## Трёхуровневая модель

Пользовательские параметры обычно адресуются как:

```text
<Task>.<Group>.<Argument>
```

В Python часто используется связанное имя вида:

```text
self.config.Group_Argument
```

Не предполагать путь по названию GUI. Проверить `task.yaml`, `argument.yaml`, generated `args.json` и фактический bind.

## Источники структуры

В `module/config/argument/` обычно находятся:

- `task.yaml` — какие группы принадлежат задачам и как строится меню;
- `argument.yaml` — тип, default, options и validation аргумента;
- `default.yaml` — task-specific defaults;
- `override.yaml` — принудительные значения и свойства;
- `gui.yaml` — дополнительные GUI/i18n-ключи;
- `dashboard.yaml` — ресурсы dashboard, если используется.

Имена и точный набор файлов проверять по текущему генератору.

## Производные файлы

Генератор может обновлять:

- `module/config/argument/args.json`;
- `module/config/argument/menu.json`;
- `module/config/config_generated.py`;
- `config/template.json`;
- `module/config/i18n/*.json`;
- deploy templates.

Правило: структура создаётся через source YAML и генератор. Не добавлять рабочий параметр только в generated JSON/Python.

## Рабочий порядок изменения параметра

1. Найти существующую группу и аналогичный аргумент.
2. Изменить source YAML.
3. Проверить defaults и overrides для всех задач, использующих группу.
4. Запустить:

```text
uv run -m module.config.config_updater
```

5. Проверить весь generated diff.
6. Перевести новые ключи для поддерживаемых локалей.
7. Проверить загрузку старого пользовательского config и миграцию.
8. Запустить конфигурационные тесты.

## Русская локаль

В персональном форке русский язык — продуктовая часть, а не дополнительный перевод.

Проверять:

- наличие и целостность `ru-RU`;
- выбор русского языка при первом запуске и после обновления;
- миграцию старых значений языка;
- отсутствие непереведённого китайского пользовательского текста в персональном пути, кроме оригинальных названий игровых серверов, событий, каналов распространения, собственных имён и внешних metadata, для которых сохранение оригинала явно предусмотрено контрактом;
- корректное сохранение technical identifiers без перевода;
- форматирование placeholders;
- fallback только там, где он явно допустим.

## Разделение кода и текста

Не переводить:

- имена Python-символов;
- command identifiers;
- ключи конфигурации;
- имена файлов assets;
- protocol/API field names.

Переводить пользовательские labels, help, errors и инструкции, если это не ломает внешний контракт.

## Миграция

При изменении ключа или default проверить:

- чтение существующего `config/<instance>.json`;
- redirect/migration-функции;
- повторный запуск миграции;
- сохранение пользовательского значения;
- rollback при невалидном значении;
- различия server-specific defaults.

## Global/EN product boundary

- server — только `en`; package — только `com.YoStarEN.AzurLane`;
- legacy `auto` — sentinel device detection и допустим только после exact-match Global package;
- foreign/unknown package или server отклоняется до device/game side effects;
- runtime WebUI — `ru-RU`; `en-US.json` — только build-time key/placeholder parity;
- `ja-JP`, `zh-CN`, `zh-MIAO`, `zh-TW` не runtime-selectable;
- event metadata source — `en`, foreign fallback order пуст.
