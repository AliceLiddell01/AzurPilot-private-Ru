# Запуск и обслуживание AzurPilot

Эта страница относится к ветке `personal/stable` после завершения Stage 2.

Эксплуатационный контур разделён на четыре явные PowerShell-команды:

```text
Start-AzurPilot.ps1
Update-AzurPilot.ps1
Repair-AzurPilot.ps1
Build-AzurPilot.ps1
```

Команды не заменяют друг друга и не выполняют скрытые обязанности соседних компонентов.

## Обычный запуск

Откройте **AzurPilot** в меню «Пуск».

Windows shortcut запускает:

```text
PowerShell 7
C:\AzurPilot\scripts\Start-AzurPilot.ps1
```

Рабочий каталог устанавливается в `C:\AzurPilot`, окно PowerShell скрывается, а после готовности WebUI открывается системный браузер:

```text
http://127.0.0.1:25548/
```

Shortcut и его icon не зависят от `alas-launcher.exe`.

## Ручной запуск

Для диагностики используйте обычное окно PowerShell 7:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Start-AzurPilot.ps1"
```

Start:

- проверяет обязательные пути и config;
- использует только project Python из `.venv`;
- не выполняет Git update;
- не перестраивает зависимости;
- не запускает Repair;
- предотвращает второй backend;
- ждёт готовность WebUI;
- открывает браузер;
- записывает журнал.

### Запуск без браузера

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Start-AzurPilot.ps1" -NoBrowser
```

### Остановка ручного запуска

Нажмите `Ctrl+C` в консоли Start.

Supervisor завершит всё дерево Python-процессов. Порт `25548` после остановки должен освободиться.

Закрытие вкладки браузера не является остановкой backend.

## Обновление

Перед обновлением полностью остановите AzurPilot:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Update-AzurPilot.ps1"
```

Update является единственным владельцем Git update-flow.

Он проверяет:

- ветку `personal/stable`;
- `origin`;
- tracking branch;
- `upstream.pushurl = DISABLED`;
- отсутствие незавершённых Git-операций;
- чистое рабочее дерево;
- отсутствие запущенного AzurPilot.

Разрешён только flow:

```text
git fetch
проверка истории
git merge --ff-only
```

Update не применяет reset, clean, rebase, force checkout или force push.

## Диагностика и Repair

Полная диагностика с безопасным восстановлением при необходимости:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Repair-AzurPilot.ps1"
```

Только диагностика:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Repair-AzurPilot.ps1" -DiagnosticOnly
```

Repair:

- не обновляет Git;
- не меняет ветки и remotes;
- не удаляет пользовательские config, базы и runtime-данные;
- создаёт backup перед заменой `.venv`;
- ведёт transaction journal;
- восстанавливает исходную среду при неудаче;
- повторно продолжает или откатывает прерванную транзакцию.

## Build

Build используется для первоначальной подготовки уже полученного checkout:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Build-AzurPilot.ps1"
```

Build:

- не клонирует и не обновляет Git;
- проверяет prerequisites;
- использует зафиксированные official artifacts `uv` и Android platform-tools;
- проверяет SHA-256;
- подготавливает Python `3.14.6`;
- создаёт frozen `.venv`;
- сохраняет локальные `uv` и ADB;
- создаёт `config\deploy.yaml` только при его отсутствии;
- не изменяет существующий config;
- создаёт или восстанавливает shortcut.

Если существующая `.venv` повреждена, Build не уничтожает её автоматически и направляет к Repair.

## Журналы

Журналы команд:

```text
%LOCALAPPDATA%\AzurPilot\logs
```

Updater recovery:

```text
%LOCALAPPDATA%\AzurPilot\dependency-transactions
```

Repair, Build и shortcut создают backup и transaction-данные в отдельных подкаталогах:

```text
%LOCALAPPDATA%\AzurPilot
```

Не удаляйте незавершённые transaction-каталоги вручную.

## Коды завершения Start

| Код | Значение |
|---:|---|
| `0` | Успешный запуск или корректная работа с уже готовым backend |
| `20` | Не выполнено обязательное условие |
| `21` | Порт занят чужим или неидентифицируемым процессом |
| `22` | Другой Start не довёл backend до готовности за отведённое время |
| `23` | Project Python или окружение неисправны |
| `24` | WebUI не стал готов за отведённое время |
| `25` | Backend не запустился или завершился с ошибкой |
| `26` | Backend готов, но браузер открыть не удалось |
| `30` | Непредусмотренная ошибка |

## Коды завершения Repair

| Код | Значение |
|---:|---|
| `0` | Среда исправна или восстановлена |
| `20` | Не выполнено обязательное условие |
| `21` | AzurPilot запущен |
| `22` | Конфликт или неоднозначная transaction |
| `23` | Не найден безопасный bootstrap |
| `24` | Repair не удался, исходная среда восстановлена |
| `25` | Rollback не удался |
| `26` | Диагностика завершилась ошибкой |
| `27` | Ошибка восстановления shortcut |
| `28` | Для all-users shortcut нужны права администратора |
| `30` | Непредусмотренная ошибка |

## Коды завершения Build

| Код | Значение |
|---:|---|
| `0` | Установка подготовлена или уже исправна |
| `20` | Не выполнено обязательное условие |
| `21` | AzurPilot запущен |
| `22` | Другой Build уже работает |
| `23` | Bootstrap недоступен или не прошёл проверку |
| `24` | Существующая `.venv` повреждена; требуется Repair |
| `25` | Не удалось собрать зависимости |
| `26` | Ошибка подготовки ADB |
| `27` | Ошибка config |
| `28` | Ошибка shortcut |
| `29` | Требуются права администратора |
| `30` | Непредусмотренная ошибка |

## Типовые ситуации

### WebUI продолжает открываться после закрытия вкладки

Это нормально: вкладка браузера и backend являются разными процессами.

### После `Ctrl+C` WebUI всё ещё доступен

Для актуального Start это ненормально. Проверьте журнал Start и наличие Python-процессов, связанных с `C:\AzurPilot\gui.py`.

### Start сообщает о сломанной `.venv`

Запустите Repair. Start намеренно не выполняет скрытый dependency sync.

### Update отказывается работать из-за dirty tree

Сначала выясните происхождение локальных изменений. Update намеренно не удаляет и не прячет их.

### Порт `25548` занят чужим процессом

Start не завершает неизвестный процесс. Освободите порт самостоятельно или выясните владельца.
