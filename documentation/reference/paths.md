# Пути и файлы

Все пути ниже указаны относительно корня checkout, если не сказано обратное.

## Пользовательская конфигурация

```text
config/
```

Здесь находятся пользовательские профили `*.json`, шаблоны и другие configuration files.

Обычные profile JSON лежат непосредственно в корне `config/`.

## Операционная конфигурация

```text
config/deploy.yaml
```

Создаётся/подготавливается Build и используется tooling-командами.

Шаблон:

```text
config/deploy.template.yaml
```

## Виртуальное окружение

```text
.venv/
```

На Windows:

```text
.venv/Scripts/python.exe
.venv/Scripts/uv.exe
.venv/Scripts/adb.exe
```

Это воспроизводимое окружение, не пользовательский backup.

## Lock и проектное описание

```text
pyproject.toml
uv.lock
```

Build и Update используют их для воспроизводимых зависимостей.

## Снимки DropRecord

Значение по умолчанию:

```text
./screenshots
```

Это каталог записываемых screenshot-результатов DropRecord, а не общий runtime log.

## Docker infrastructure

```text
infrastructure/observability/compose.yaml
```

Локальные Docker secrets/настройки берутся из:

```text
.env
```

`.env` следует считать секретным файлом.

## GitBook

```text
documentation/
```

Публичная русская документация проекта.

Основные файлы:

```text
documentation/gitbook-docs.yaml
documentation/.gitbook.yaml
documentation/SUMMARY.md
documentation/README.md
```

## Инженерные документы

```text
docs/
```

Это repository engineering corpus. Он не равен пользовательской GitBook Wiki.

## Agent context

```text
.codex/context/
```

Внутренний контекст для coding agents. Он не является пользовательской документацией.

## Резервные копии PostgreSQL

Update требует backup **вне checkout**.

Путь можно задать через `PostgreSqlBackupRoot`; иначе используется внешний state layout tooling.

Не размещайте PostgreSQL backup внутри рабочей копии репозитория.
