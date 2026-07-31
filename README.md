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

```text
scripts/
├── Start-AzurPilot.ps1
├── Update-AzurPilot.ps1
├── Repair-AzurPilot.ps1
└── Build-AzurPilot.ps1
```

| Команда | Назначение | Чего она не делает |
|---|---|---|
| `Start-AzurPilot.ps1` | Запускает подготовленную установку и контролирует backend | Не обновляет Git и не перестраивает `.venv` |
| `Update-AzurPilot.ps1` | Получает безопасное fast-forward обновление | Не выполняет rebase, reset или force push |
| `Repair-AzurPilot.ps1` | Диагностирует и восстанавливает существующую `.venv` | Не меняет ветки, remotes и пользовательские данные |
| `Build-AzurPilot.ps1` | Подготавливает уже полученный checkout | Не клонирует репозиторий и не обновляет HEAD |

Обычный запуск не зависит от `alas-launcher.exe` и не выполняет скрытое обновление.

### Один владелец обновления

Обновление пользовательской установки выполняет только `Update-AzurPilot.ps1`.

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

### Приватность

Из активной версии удалены:

- автоматическая отправка статистики CL1;
- удалённая отправка журналов ошибок;
- Microsoft Clarity;
- связанные настройки, интерфейс и локализация;
- методы отправки статистики и отчётов в проектном API-клиенте.

Публичные объявления могут загружаться только в режиме чтения. Проектный клиент объявлений не должен отправлять игровую статистику, журналы, снимки экрана или стабильный идентификатор компьютера.

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

## Быстрый запуск

После подготовленной установки откройте **AzurPilot** в меню «Пуск».

Ярлык запускает PowerShell 7, затем `scripts\Start-AzurPilot.ps1`, ожидает готовность WebUI и открывает:

```text
http://127.0.0.1:25548/
```

Ручной диагностический запуск:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Start-AzurPilot.ps1"
```

Запуск без автоматического открытия браузера:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Start-AzurPilot.ps1" -NoBrowser
```

> [!NOTE]
> Закрытие вкладки браузера само по себе не останавливает backend. При ручном запуске используйте `Ctrl+C` в окне Start.

## Обслуживание установки

Перед Update, Repair или Build полностью остановите AzurPilot.

### Обновление

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Update-AzurPilot.ps1"
```

### Диагностика и восстановление

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Repair-AzurPilot.ps1"
```

Только диагностика:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Repair-AzurPilot.ps1" -DiagnosticOnly
```

### Первоначальная подготовка checkout

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Build-AzurPilot.ps1"
```

## Основные гарантии

- Обновления принимаются только из `origin/personal/stable`.
- Разрешён только fast-forward без переписывания истории.
- `Start`, `Repair` и `Build` не обновляют Git.
- `Repair` использует backup, journal и rollback.
- `Build` проверяет bootstrap-артефакты по SHA-256.
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
