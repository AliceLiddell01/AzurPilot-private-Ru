# Тестирование

AzurPilotRu использует `pytest` как основной test runner. Текущая CI-группа фиксирует `pytest 9.1.1`, `pytest-xdist 3.8.0` и диагностический `pytest-cov`.

## Быстрый targeted запуск

Запускайте сначала тесты затронутого домена.

Например:

```bash
uv run --locked --no-sync python -m pytest -q tests/runtime
```

Другие крупные области находятся в:

- `tests/application/`;
- `tests/contracts/`;
- `tests/event/`;
- `tests/game/`;
- `tests/mcp/`;
- `tests/observability/`;
- `tests/persistence/`;
- `tests/runtime/`;
- `tests/webui/`;
- `tests/platform/`.

## Полный suite

Canonical параллельный режим:

```bash
uv run --locked --no-sync python -m pytest -q --dist=loadgroup -n auto tests
```

Количество workers выбирается автоматически.

Некоторые тесты объединены в `xdist_group`, чтобы не было гонок за общую PostgreSQL schema или host-wide game runtime lease.

## Pytest configuration

`pytest.ini` задаёт:

```text
testpaths = tests
--strict-markers
```

Проект зарегистрировал marker `windows_integration`; остальные устойчивые границы в основном выражаются структурой каталогов.

## Lint

CI запускает Ruff для ошибок исполнения/импортов.

Локальный эквивалент:

```bash
uv run --locked ruff check . --select E9,F63,F7,F82 --ignore F821,F722
```

## Locked dependencies

До полного прогона:

```bash
uv lock --check
uv sync --locked --group ci
```

CI не должен получать другой dependency graph только потому, что локальная среда давно не обновлялась.

## PostgreSQL

Python CI использует disposable PostgreSQL 18 и проверяет:

- Alembic `base → head → base → head`;
- single-head/autogenerate;
- importer/migration;
- conflict/idempotency;
- dump/restore;
- repository/storage contracts.

Для изменения persistence targeted unit tests недостаточны, если меняется migration/data contract.

## Генераторы

CI запускает config/assets generators и затем проверяет, что рабочее дерево осталось чистым.

Если source YAML изменился, ожидаемые generated outputs должны быть обновлены и закоммичены вместе с source.

## Русификация и Global/EN

Постоянный runtime localization audit входит в обычный pytest suite.

Он защищает:

- русский operator-facing prose;
- `ru-RU`;
- server `en`;
- Global package;
- `assets/en`;
- EN metadata;
- OCR namespace.

Это постоянный product gate, а не одноразовая проверка перевода.

## Coverage

Coverage пока не является required CI gate и глобального минимального процента нет.

Диагностический запуск:

```bash
uv sync --locked --group ci
uv run --locked --no-sync python -m pytest -q \
  --dist=loadgroup -n auto \
  --cov=module --cov=campaign --cov=tools --cov-branch \
  --cov-report=term-missing tests
```

Покрытие помогает найти непротестированные области, но не заменяет содержательный regression test.

## Live acceptance

Сценарии с реальным MuMu/device/browser находятся в `tools/acceptance/`.

Они не должны автоматически запускаться обычным pytest, особенно если могут изменять состояние игры или завершать эмулятор.
