<p align="center">
  <img src="doc/logo.webp" alt="Логотип AzurPilot" width="400">
</p>

<h1 align="center">AzurPilot Private RU</h1>

<p align="center">
  Персональная русская версия AzurPilot с контролируемым обновлением, прозрачным запуском и сокращённым набором внешних сетевых зависимостей.
</p>

<p align="center">
  <a href="https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki">
    <img src="https://img.shields.io/badge/Wiki-документация-2f81f7?style=flat-square" alt="Русская Wiki">
  </a>
  <a href="https://deepwiki.com/AliceLiddell01/AzurPilot-private-Ru">
    <img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki">
  </a>
  <img src="https://img.shields.io/badge/телеметрия-удалена-2ea44f?style=flat-square" alt="Телеметрия удалена">
  <img src="https://img.shields.io/badge/Python-3.14.6-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.14.6">
  <img src="https://img.shields.io/badge/PowerShell-7.6-5391FE?style=flat-square&logo=powershell&logoColor=white" alt="PowerShell 7.6">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/лицензия-GPLv3-2ea44f?style=flat-square" alt="Лицензия GPLv3">
  </a>
</p>

> [!IMPORTANT]
> Этот README относится к ветке [`personal/stable`](https://github.com/AliceLiddell01/AzurPilot-private-Ru/tree/personal/stable).
> Ветка `master` сохраняется как чистое зеркало исходного проекта и намеренно не содержит персональных изменений.

## Что это за проект

AzurPilot — инструмент автоматизации для мобильной игры **Azur Lane**. Он подключается к Android-эмулятору или устройству, распознаёт элементы игрового интерфейса и выполняет настроенные задачи.

Этот репозиторий является персональным форком [`wess09/AzurPilot`](https://github.com/wess09/AzurPilot), который основан на [`LmeSzinc/AzurLaneAutoScript`](https://github.com/LmeSzinc/AzurLaneAutoScript).

Цель персональной версии — сохранить полезную игровую автоматизацию, но сделать запуск, обновление, восстановление и сетевое поведение понятными и контролируемыми.

## Основные особенности персональной версии

### Отдельные команды запуска и обслуживания

Основной кроссплатформенный интерфейс — устанавливаемая команда `azur` и
эквивалентный модуль `python -m azurpilot`. Сервисы используют проверенный
корень репозитория, общий типизированный конверт результата, ограниченный
запуск процессов и внешние блокировки/журналы.

`azur build` после успешной подготовки регистрирует каталог консольных команд
проекта только в пользовательском `PATH` Windows. Поэтому после открытия нового
терминала команда работает из любого каталога без активации `.venv`:

```powershell
Set-Location $HOME
azur doctor
```

Если установка ещё не подготовлена и `azur` пока недоступен, bootstrap
запускается один раз из checkout через внешний `uv`:

```powershell
uv --project <repository-root> run --locked python -m azurpilot build
```

Открытые до регистрации PATH окна PowerShell не получают изменения окружения
задним числом; для них нужна новая оболочка. `azur doctor` не меняет PATH, а
проверяет наличие пакета, регистрацию на уровне пользователя и доступность команды в
текущей оболочке. Эквивалентный прямой вызов для автоматизации —
`python -m azurpilot` в установленной среде.

```text
azur doctor
azur build
azur start
azur stop
azur repair [--diagnostic-only|--repair-shortcut|--shortcut-only]
azur update
```

Для машинной интеграции добавьте `--json`: stdout содержит ровно один JSON
отчёт без Rich/ANSI. `--repository-root PATH` имеет высший приоритет; без него
корень берётся только из проверенной конфигурации или идентичности установки.
CWD сам по себе не считается доказательством проекта. `azur doctor` отдельно
показывает наличие консольной команды и её регистрации на уровне пользователя; сам doctor
остаётся доступным только для чтения.

На Windows `azur build` по умолчанию проверяет и устанавливает закреплённый ADB
37.0.0 по SHA-256 и создаёт пользовательский ярлык `.lnk` в меню «Пуск».
Флаг `azur build --no-shortcut` отключает только создание или проверку ярлыка.
На POSIX ярлык Windows имеет типизированный статус `unsupported`; ADB использует
штатное обнаружение и управление, предусмотренное для POSIX, а отсутствие
настроенного ADB обозначается как `not_configured`.

Ниже перечислены сохраняемые legacy PowerShell wrappers. Они пока остаются
слоем совместимости и паритета Windows, но служба Python не делегирует им операции
среды выполнения. Удаление wrappers требует отдельного двойного запуска, проверки
паритета и переключения.

```text
scripts/
├── Start-AzurPilot.ps1
├── Stop-AzurPilot.ps1
├── Update-AzurPilot.ps1
├── Repair-AzurPilot.ps1
└── Build-AzurPilot.ps1
```

| Команда | Назначение | Чего она не делает |
|---|---|---|
| `Start-AzurPilot.ps1` | Запускает подготовленную установку и контролирует backend | Не обновляет Git и не перестраивает `.venv` |
| `Stop-AzurPilot.ps1` | Штатно останавливает backend только текущего checkout | Не останавливает PostgreSQL и не завершает посторонние процессы |
| `Update-AzurPilot.ps1` | Получает безопасное fast-forward обновление | Не выполняет rebase, reset или force push |
| `Repair-AzurPilot.ps1` | Диагностирует и восстанавливает существующую `.venv` | Не меняет ветки, remotes и пользовательские данные |
| `Build-AzurPilot.ps1` | Подготавливает уже полученный checkout | Не клонирует репозиторий и не обновляет HEAD |

Обычный запуск не зависит от `alas-launcher.exe` и не выполняет скрытое обновление.

### Один владелец обновления

Обновление пользовательской установки выполняет только сервис `azur update`.
`Update-AzurPilot.ps1` временно сохраняется как Windows parity-wrapper до
отдельного dual-run/cutover решения.

Из WebUI и Python runtime удалены:

- встроенная страница обновления;
- автоматические проверки и запуск обновления;
- самостоятельные Git-операции;
- удалённая команда обновления через MCP;
- legacy installer, geo redirect и Git-over-CDN runtime;
- workflows и upload scripts для публикации Git-over-CDN artifacts.

Разрешённая схема обновления:

```text
git fetch
→ проверка истории
→ git merge --ff-only
```

Локальные изменения и собственные commits не удаляются автоматически.

### First-party MCP

Dev и Game MCP используют один canonical bundle в
`config/mcp-versions.toml`. Прямой project-scoped stdio и authenticated
loopback HTTP для Codex Desktop являются равноправными transport routes одной
backend identity; public и third-party MCP surfaces остаются отдельными и не
подменяют их. Производные plugin metadata проверяются и согласуются через
`azur mcp`, а успешный `azur update` автоматически выполняет такую проверку
без изменения tracked source или plugin version.

```text
azur mcp status
azur mcp versions
azur mcp reconcile [--source [--bump auto|patch|minor|major]]
azur mcp start | stop | restart
```

При изменении plugin/skill snapshot или уже открытой session возвращается
`RELOAD_REQUIRED`/`reload_required`; новая session или штатный restart должны
быть подтверждены отдельно.

### Приватность

Из активной версии удалены:

- автоматическая отправка статистики CL1;
- удалённая отправка журналов ошибок;
- Microsoft Clarity;
- удалённая система project-controlled объявлений;
- предустановленный upstream SSH endpoint для удалённого доступа;
- связанные настройки, интерфейс и локализация;
- методы отправки статистики и отчётов в проектном API-клиенте.

Персональная сборка не должна обращаться к project-controlled API исходного AzurPilot за объявлениями. Удалённый доступ по умолчанию выключен и не содержит предустановленного SSH-сервера; при необходимости провайдер задаётся пользователем явно.

Сторонние интеграции, которые пользователь настраивает самостоятельно — уведомления, LLM, MCP, удалённый доступ, прокси и другие сервисы — могут передавать данные выбранным провайдерам.

Подробности: [Приватность личной сборки](PRIVACY_AND_DISCLAIMER.md).

### Пассивное чтение конфигурации

Чтение `config\deploy.yaml` больше не должно:

- выполнять geo lookup;
- выбирать CDN или зеркало обновления;
- менять адрес репозитория;
- активировать Git-over-CDN;
- записывать файл только из-за его чтения.

Старые и неизвестные ключи сохраняются для совместимости, но устаревшие Git/updater-параметры не управляют активным runtime.

### Единый русский интерфейс

WebUI всегда использует локализацию `ru-RU`. Язык браузера не меняет интерфейс, а переключатель языка удалён.

При первом запуске после обновления старое значение `Language` в `config\deploy.yaml` мигрирует в `ru-RU` отдельной patch-only операцией:

- остальные настройки, комментарии и неизвестные ключи сохраняются;
- game server, package name и OCR-настройки не изменяются;
- повторный запуск не переписывает уже корректный файл;
- повреждённый или неоднозначный YAML не перезаписывается, запуск останавливается с диагностикой.

Весь активный first-party интерфейс переведён на русский: навигация, OOBE, настройки, конфигурация задач, уведомления, проверки ввода, HTML/JS-виджеты и безопасные состояния ошибок. Английские технические значения, оригинальные названия событий и внешний контент отображаются только там, где перевод изменил бы машинный контракт или первоисточник.

Игровой контур поддерживает только Global/EN: пакет `com.YoStarEN.AzurLane`, сервер `en` и канонический каталог `assets/en`. Runtime WebUI использует только `ru-RU`; `en-US.json` сохранён исключительно как build-time источник ключей и placeholders. Названия событий берутся из EN metadata без CN/JP/TW fallback. Все 18 OCR-файлов сохранены как Global/shared ресурсы; foreign OCR aliases недоступны. Неизвестный или foreign package/server отклоняется до device/game side effects.

Постоянная проверка `dev_tools/runtime_russianization_audit.py` анализирует только доказанные operator-facing consumer sites и fail-closed отклоняет любой CJK либо неклассифицированный английский текст в проверяемом дереве. Технические токены, пути, URL, package/game identifiers и deferred exception text не считаются переводом автоматически. Проверка одновременно защищает `ru-RU`, единственный сервер `en`, Global package, `assets/en`, EN metadata без foreign fallback и единственный публичный OCR namespace `azur_lane`.

Для веток `codex/translate-*` дополнительно действует base→head structural gate: перевод может менять только одобренный prose при неизменных AST, call shape, placeholders и machine contracts. Обычные feature/bugfix PR могут менять поведение согласно заявленному scope, но всё равно проходят permanent runtime localization integrity через общий pytest suite.

### Русские инфраструктурные журналы и безопасная диагностика

First-party сообщения инфраструктуры переведены на русский в контуре запуска и обслуживания, deploy-модулях, конфигурации, logger, WebUI lifecycle, управлении процессами, MCP и точках входа приложения.

При переводе сохраняются машинные контракты:

- команды, пути, имена процессов и exit codes не переводятся;
- `WebUI`, `Electron`, `SSL`, `IPv4`, `IPv6`, `PID`, `taskkill`, `psutil`, `uv`, `spawn`, `fork` и другие технические идентификаторы остаются без механической замены;
- raw stdout/stderr и исходный внешний контекст сохраняются;
- placeholders, severity и порядок событий не изменяются.

Подробный traceback в WebUI проходит redaction чувствительных данных и HTML escaping до отображения. Вертикальная прокрутка принадлежит странице, а горизонтальная прокрутка длинных строк остаётся внутри traceback. Поведение проверено для светлой и тёмной тем, включая масштаб браузера 200%.

Журналы игровой логики переводятся отдельно от инфраструктурного контура.

Проверенная Windows/MuMu/Global граница включает запуск и перезапуск WebUI, ADB/screenshot/control backends, compact и general-English OCR, а также безопасную навигацию Campaign/Operation Siren. Боевые, расчётные и расходующие ресурс действия не выполняются как диагностический smoke: для них используются текущие product tests и отдельная явно подтверждённая acceptance.

## Быстрый запуск

После подготовленной установки выполните `azur build`, затем `azur start`.
Перед первым запуском `azur doctor` показывает состояние project-bound и
optional capabilities.

`azur start` перед запуском WebUI выполняет проверяемую предварительную проверку
Compose/PostgreSQL и штатную миграцию observability. Если публичный host Caddy не настроен,
локальный WebUI запускается с ограниченным предупреждением; работающий PostgreSQL
этой командой не останавливается.

`azur start` ожидает подтверждённые идентичность процесса и готовность WebUI:

```text
http://127.0.0.1:25548/
```

Запуск вручную:

```text
azur start
```

Запуск с открытием браузера:

```text
azur start --browser
```

> [!NOTE]
> Закрытие вкладки браузера само по себе не останавливает backend. `Ctrl+C` останавливает AzurPilot только в том окне Start, которое само запустило backend. Если Start сообщил, что открыл уже работающий WebUI, используйте штатную команду Stop:
>
> ```text
> azur stop
> ```
>
> Повторный Stop безопасен и сообщает, что AzurPilot уже остановлен. Команда проверяет точный checkout, Python проекта, `gui.py` и дерево процессов; процесс, который лишь занял тот же порт, она не завершает.

## Обслуживание установки

Перед Update, Repair или Build полностью остановите AzurPilot.

### Обновление

```text
azur update
```

### Диагностика и восстановление

```text
azur repair
```

Только диагностика:

```text
azur repair --diagnostic-only
```

Проверка или восстановление пользовательского Windows ярлыка:

```text
azur repair --repair-shortcut
azur repair --shortcut-only
```

### Первоначальная подготовка checkout

```text
azur build
```

Legacy wrappers доступны для parity-проверок Windows:

```powershell
pwsh -NoLogo -NoProfile -File "scripts/Start-AzurPilot.ps1"
pwsh -NoLogo -NoProfile -File "scripts/Stop-AzurPilot.ps1"
pwsh -NoLogo -NoProfile -File "scripts/Update-AzurPilot.ps1"
pwsh -NoLogo -NoProfile -File "scripts/Repair-AzurPilot.ps1"
pwsh -NoLogo -NoProfile -File "scripts/Build-AzurPilot.ps1"
```

## Основные гарантии

- Обновления принимаются только из `origin/personal/stable`.
- Разрешён только fast-forward без переписывания истории.
- Перед любым изменением `azur update` подтверждает каноническую идентичность Git и
  внешнюю проверенную логическую резервную копию PostgreSQL в формате `pg_dump -Fc`.
- `Start`, `Repair` и `Build` не обновляют Git.
- `Repair` использует резервную копию, журнал и откат.
- `Build` проверяет bootstrap-артефакты по SHA-256.
- Незавершённые операции сохраняют внешний журнал; неоднозначные сведения о
  владении, откате или очистке блокируют повторное изменение до восстановления.
- Существующий `config\deploy.yaml` не перезаписывается целиком при обычном чтении или точечном изменении.
- Неизвестный процесс на порту `25548` не завершается автоматически.
- Scheduler, очередь задач, worker lifecycle, startup-run и локальная ADB-логика сохранены.
- Локальная статистика и базы не удаляются автоматически.

В эксплуатационном контуре не используются:

```text
git reset --hard
git clean
git checkout -f
git pull
git rebase
git push --force
```

## Журналы и восстановление

Основные журналы:

```text
%LOCALAPPDATA%\AzurPilot\logs
```

Транзакции обновления:

```text
%LOCALAPPDATA%\AzurPilot\dependency-transactions
```

Repair, Build и ярлык используют отдельные каталоги внутри `%LOCALAPPDATA%\AzurPilot`.

Не удаляйте незавершённые transaction-каталоги вручную: они нужны следующему безопасному запуску для восстановления состояния.

## Документация

Вся пользовательская и эксплуатационная документация хранится в GitHub Wiki. Отдельная папка `docs/` в персональной ветке намеренно не используется.

| Раздел | Ссылка |
|---|---|
| Главная страница | [Русская Wiki](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki) |
| Запуск и обслуживание | [Запуск и обслуживание AzurPilot](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Запуск-и-обслуживание-AzurPilot) |
| Архитектура запуска и обслуживания | [Архитектура запуска и обслуживания](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Архитектура-запуска-и-обслуживания) |
| Русский интерфейс и миграция языка | [Русский интерфейс и миграция языка](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Русский-интерфейс-и-миграция-языка) |
| Инфраструктурные журналы | [Русские инфраструктурные журналы](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Русские-инфраструктурные-журналы) |
| Диагностика WebUI | [Безопасный traceback в WebUI](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Безопасный-traceback-в-WebUI) |
| Обновление | [Обновление AzurPilot](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Обновление-AzurPilot) |
| Ошибки обновления | [Ошибки при обновлении](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Ошибки-при-обновлении) |
| Приватность и сетевое поведение | [Приватность и сетевое поведение](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Приватность-и-сетевое-поведение) |
| Отличия персональной версии | [Отличия персональной версии](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Отличия-персональной-версии) |

## Модель веток

| Ветка или remote | Назначение |
|---|---|
| `master` | Чистое зеркало исходной версии AzurPilot |
| `personal/stable` | Рабочая стабильная персональная версия |
| `origin` | Личный репозиторий и единственный автоматический источник обновлений |
| `upstream` | Исходный проект для контролируемого ручного переноса изменений |

Изменения из `upstream` не переносятся автоматически.

## Что пока не реализовано

- установщик «в один клик» для нового пользователя;
- полноценный release pipeline;
- новый desktop shell с tray, уведомлениями и autostart;
- автоматическое удаление внешних Cloudflare, ESA, 123pan и SSH-ресурсов, которые могли быть настроены вне репозитория.

## Скриншот интерфейса

<p align="center">
  <img src="doc/GUI.png" alt="Веб-интерфейс AzurPilot" width="800">
</p>

## Происхождение и благодарности

Персональная версия основана на работе авторов и участников следующих проектов:

- [`wess09/AzurPilot`](https://github.com/wess09/AzurPilot);
- [`LmeSzinc/AzurLaneAutoScript`](https://github.com/LmeSzinc/AzurLaneAutoScript);
- [`yukikaze21/AzurLaneAutoScript`](https://github.com/yukikaze21/AzurLaneAutoScript);
- [`Zuosizhu/Alas-with-Dashboard`](https://github.com/Zuosizhu/Alas-with-Dashboard);
- [`guoh064/AzurLaneAutoScript`](https://github.com/guoh064/AzurLaneAutoScript);
- [`sui-feng-cb/AzurLaneAutoScript`](https://github.com/sui-feng-cb/AzurLaneAutoScript);
- другим участникам экосистемы AzurLaneAutoScript.

Часть кода исходного проекта и персональных изменений создавалась или изменялась с помощью ИИ. Такие изменения должны проверяться тестами и ручным анализом перед использованием.

## Лицензия

Проект распространяется на условиях [GNU General Public License v3.0](LICENSE).

Программа предоставляется без гарантий. Используйте автоматизацию и экспериментальные функции на свой риск.
