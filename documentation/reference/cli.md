# Команды azur

`azur` — основной командный интерфейс обслуживания AzurPilotRu.

Большинство обычных пользователей регулярно используют только:

```text
azur doctor
azur build
azur start
azur stop
azur repair
azur update
```

Остальные команды относятся к развёртыванию, MCP, Git delivery, PR и developer-интеграциям.

## Общие параметры

Поддерживаются:

- `--repository-root PATH` — явно указать проверенный корень репозитория;
- `--json` — вывести один машинно-читаемый JSON;
- `--no-color` — отключить ANSI/Rich;
- `--verbose` — дополнительные сведения;
- `--debug` — диагностический синоним verbose.

## doctor

```text
azur doctor [--full]
```

Read-only диагностика проекта.

`--full` добавляет внешние интеграции.

## build

```text
azur build [--shortcut|--no-shortcut] [--timeout SECONDS]
```

Подготавливает Python-окружение без Git update.

На Windows также работает с проектным ADB, PATH и ярлыком.

## start

```text
azur start [--browser|--no-browser] [--foreground] [--timeout SECONDS]
```

Запускает WebUI после проверки readiness и ownership.

## stop

```text
azur stop [--timeout SECONDS]
```

Останавливает только подтверждённое дерево WebUI.

## repair

```text
azur repair [--diagnostic-only] [--repair-shortcut|--shortcut-only] [--timeout SECONDS]
```

Диагностирует и транзакционно восстанавливает окружение.

## update

```text
azur update [--expected-branch BRANCH] [--remote NAME] [--remote-branch BRANCH]
            [--expected-origin-url URL] [--timeout SECONDS]
```

Выполняет проверенный fast-forward update.

Переопределять Git-параметры обычной установке обычно не требуется.

## deploy docker

```text
azur deploy docker
```

Developer/операторская команда явного Docker deployment.

Поддерживает параметры image/container/port/source, `--replace`, общий timeout и readiness timeout.

Не является обычным способом запуска desktop WebUI.

## mcp

```text
azur mcp status
azur mcp versions
azur mcp start
azur mcp stop
azur mcp restart
azur mcp reconcile
```

Управляет first-party MCP source/runtime.

`reconcile --source` может изменять tracked canonical manifest и derived plugin metadata, поэтому это developer-операция.

## integrations

```text
azur integrations status
azur integrations doctor
```

Проверяет внешние developer-интеграции.

У отдельных provider есть `status`, `doctor`, `probe` и специализированные действия.

## delivery и pr

`azur delivery` и `azur pr` — repository workflow tooling для безопасной публикации изменений и draft PR.

Обычному пользователю AzurPilotRu они не нужны.

## JSON-режим

Для автоматизации:

```powershell
azur doctor --json
```

stdout содержит один structured result без Rich/ANSI.

Используйте JSON-режим для скриптов вместо разбора человекочитаемого текста.
