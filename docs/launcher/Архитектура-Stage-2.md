# Архитектура Stage 2

Stage 2 заменяет эксплуатационные обязанности original `alas-launcher.exe` прозрачными PowerShell-командами.

Он не переписывает WebUI, игровые модули, распознавание, ADB-логику приложения или весь legacy-код проекта.

## Целевая структура

```text
scripts/
├── Start-AzurPilot.ps1
├── Update-AzurPilot.ps1
├── Repair-AzurPilot.ps1
├── Build-AzurPilot.ps1
└── lib/
    └── AzurPilot.Shortcut.psm1

assets/
└── AzurPilot.ico
```

Публичных команд ровно четыре. Shortcut-модуль является внутренней общей реализацией Build и Repair.

## Владение обязанностями

| Обязанность | Владелец |
|---|---|
| Обычный запуск | `Start-AzurPilot.ps1` |
| Git fetch и fast-forward | `Update-AzurPilot.ps1` |
| Восстановление существующей `.venv` | `Repair-AzurPilot.ps1` |
| Первичная подготовка checkout | `Build-AzurPilot.ps1` |
| Managed Python и frozen dependency sync | `deploy\uv.py` |
| WebUI и игровая логика | `gui.py` и существующие Python-модули |
| Shortcut serialization и validation | `AzurPilot.Shortcut.psm1` |

## Original flow

```text
Windows shortcut
→ alas-launcher.exe
→ launcher UI и launcher state
→ скрытая подготовка или update-flow
→ запуск приложения
```

Проблемы original flow для персональной ветки:

- launcher являлся непрозрачной обязательной точкой входа;
- запуск, обновление и подготовка окружения были связаны;
- часть поведения зависела от launcher state;
- icon и shortcut зависели от executable launcher-а;
- пользователь не видел точного владельца Git и dependency операций.

## Новый flow

### Запуск

```text
Windows shortcut
→ pwsh
→ Start-AzurPilot.ps1
→ project Python
→ gui.py
→ readiness check
→ системный браузер
```

### Обновление

```text
явная команда Update-AzurPilot.ps1
→ precondition checks
→ git fetch
→ проверка ancestry
→ dependency transaction при необходимости
→ git merge --ff-only
```

### Repair

```text
явная команда Repair-AzurPilot.ps1
→ диагностика
→ backup и journal
→ deploy\uv.py
→ validation
→ commit transaction или rollback
```

### Build

```text
уже полученный checkout
→ Build-AzurPilot.ps1
→ verified uv bootstrap
→ managed Python
→ frozen .venv
→ local uv и ADB
→ safe config creation
→ shortcut
```

## Инварианты безопасности

- Start не вызывает Update или Repair.
- Start не выполняет Git-команды.
- Repair и Build не обновляют Git.
- Только Update выполняет `fetch` и `merge --ff-only`.
- Неизвестный владелец порта `25548` не завершается автоматически.
- Существующий `config\deploy.yaml` не перезаписывается.
- Repair не удаляет user config, базы, логи и runtime-данные.
- Потенциально опасная замена `.venv` выполняется через backup, journal и rollback.
- Bootstrap artifacts фиксируются по версии и SHA-256.
- Shortcut и icon не зависят от `alas-launcher.exe`.
- Hidden elevation запрещён.
- Commit и push являются отдельными явно согласуемыми действиями.

## Lifecycle Start

Start является supervisor-ом Python backend.

После запуска он:

1. проверяет окружение;
2. получает repo-scoped mutex;
3. проверяет владельца порта;
4. запускает project Python;
5. ждёт TCP и HTTP readiness;
6. открывает браузер;
7. остаётся связанным с backend.

При `Ctrl+C`, ошибке или выходе supervisor-а Start завершает всё дерево backend-процессов. Это предотвращает orphan Python и запись в уничтоженный stdout.

## Single-instance

Start различает:

- уже готовый AzurPilot;
- первый Start, который ещё запускается;
- посторонний процесс на `25548`.

Повторный Start не создаёт второй backend. Посторонний процесс не уничтожается.

## Shortcut

All-users shortcut хранится в Windows Start Menu.

Он содержит:

- Target: PowerShell 7;
- arguments: hidden запуск `Start-AzurPilot.ps1` с `-FromShortcut`;
- WorkingDirectory: `C:\AzurPilot`;
- IconLocation: project-owned `assets\AzurPilot.ico`.

Миграция:

1. проверяет elevation;
2. сохраняет исходный shortcut;
3. создаёт и валидирует временный shortcut;
4. атомарно заменяет target;
5. восстанавливает исходный файл при ошибке;
6. становится no-op при повторном запуске.

## Изоляция original launcher

Stage 2G подтвердил:

- Start не вызывает launcher;
- Update не вызывает launcher;
- Repair не вызывает launcher;
- Build не вызывает launcher;
- shortcut и icon не ссылаются на launcher;
- normal operation работает при временно переименованном `alas-launcher.exe`;
- launcher восстанавливается с тем же SHA-256;
- `C:\AzurPilot-Launcher` остаётся read-only.

Original launcher может временно оставаться локальным аварийным референсом, но не участвует в штатном flow.

## Validation matrix

Финальная проверка включает:

- Parser всех PowerShell-файлов;
- PSScriptAnalyzer без Warning/Error;
- cold start и `-NoBrowser`;
- single-instance и foreign port;
- ошибки окружения и readiness;
- Repair no-op, rebuild, rollback и interrupted transaction;
- Build clean checkout, pinned bootstrap, config preservation и idempotency;
- shortcut migration, rollback, no-op и cold start;
- launcher isolation;
- `git diff --check`;
- scan запрещённых Git-операций;
- scan `;` command separators;
- scan backtick line continuations;
- audit немедленного сохранения `$LASTEXITCODE`.

## Зафиксированные компоненты Stage 2

| Компонент | Назначение |
|---|---|
| `Start-AzurPilot.ps1` | browser-first запуск и lifecycle backend |
| `Update-AzurPilot.ps1` | контролируемый fast-forward updater Stage 1 |
| `Repair-AzurPilot.ps1` | транзакционное восстановление |
| `Build-AzurPilot.ps1` | подготовка checkout |
| `AzurPilot.Shortcut.psm1` | внутренняя shortcut orchestration |
| `AzurPilot.ico` | независимый project icon |

## Что остаётся вне Stage 2

- installer;
- release pipeline;
- Tauri или другой новый desktop shell;
- массовое удаление legacy/CDN/Chinese infrastructure;
- переработка WebUI;
- перенос tray и launcher UI;
- полная изоляция всех сетевых интеграций;
- автоматический upstream merge.

Эти задачи требуют отдельных этапов и не должны смешиваться с финальной фиксацией Stage 2.
