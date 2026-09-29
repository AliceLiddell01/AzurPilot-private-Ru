# Среда разработки

Среда разработчика должна быть максимально близка к проверяемому проектному контракту, а не собрана из случайных глобальных пакетов.

## Python

Формальный контракт проекта:

```text
>=3.14.6,<3.15
```

Текущий проверяемый runtime — Python **3.14.6**.

Окружение проекта создаётся через `uv` и хранится в `.venv`.

## uv

Зависимости зафиксированы через:

```text
pyproject.toml
uv.lock
```

Для developer/CI зависимостей используйте locked sync:

```bash
uv lock --check
uv sync --locked --group ci
```

Не обновляйте lock-файл «заодно» с несвязанной задачей.

## Windows

Основной пользовательский acceptance AzurPilotRu ориентирован на Windows/MuMu/Global.

Windows особенно важен для изменений, затрагивающих:

- ADB;
- shortcut;
- PATH;
- process lifecycle;
- MuMu;
- native Windows behavior.

## Linux

CI Python job выполняется на Ubuntu 24.04 и покрывает большую часть кроссплатформенной логики, storage и contracts.

Для обычной разработки Linux/WSL полезен для тестов и tooling, но не заменяет Windows acceptance там, где изменение зависит от Windows API или эмулятора.

## Docker

Для PostgreSQL, Redis и полного infrastructure-контура нужен Docker Engine/Compose; на Windows canonical пользовательский путь — Docker Desktop.

Не запускайте параллельно второй случайный PostgreSQL/Redis и не меняйте endpoint только ради того, чтобы тест «зазеленел».

## Git

Перед работой получите актуальный `personal/stable` и создайте отдельную ветку.

Не используйте destructive Git-команды как обычный способ синхронизации локальной работы:

```text
git reset --hard
git clean
git rebase
git push --force
```

если конкретная recovery-задача явно этого не требует и не доказана безопасность.

## Полезная начальная проверка

После подготовки checkout:

```bash
uv lock --check
uv sync --locked --group ci
uv run --locked --no-sync python -m pytest -q tests/runtime
```

Выберите targeted test directory по затронутому домену.

## Реальное устройство и игра

Большинство unit/contract tests не требуют игрового аккаунта.

Live emulator/game acceptance хранится отдельно в `tools/acceptance/` и запускается только когда изменение действительно требует такого доказательства.

Не превращайте обычный тест в действие, расходующее игровые ресурсы.
