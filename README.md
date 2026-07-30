<p align="center">
  <img src="doc/logo.webp" alt="Логотип AzurPilot" width="400">
</p>

<h1 align="center">AzurPilot Private RU</h1>

<p align="center">
  Персональная русская ветка AzurPilot с безопасным обновлением и прозрачным PowerShell-контуром запуска, восстановления и подготовки установки.
</p>

<p align="center">
  <a href="https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki">
    <img src="https://img.shields.io/badge/Wiki-документация-2f81f7?style=flat-square" alt="Русская Wiki">
  </a>
  <img src="https://img.shields.io/badge/Stage_2-завершён-2ea44f?style=flat-square" alt="Stage 2 завершён">
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

Цель персональной ветки — сохранить полезные возможности AzurPilot, но сделать запуск, обновление и восстановление понятными, проверяемыми и менее зависимыми от исходной внешней инфраструктуры.

## Текущий статус

| Этап | Результат | Статус |
|---|---|---|
| Stage 1 | Контролируемое обновление из `origin/personal/stable` | Завершён |
| Stage 2 | Прозрачные команды Start / Update / Repair / Build | Завершён |
| Stage 2 | Запуск из меню «Пуск» без обязательного `alas-launcher.exe` | Подтверждён |
| Stage 2 | Транзакционное восстановление `.venv` | Готово |
| Документация | Русская Wiki по эксплуатационному контуру | Готова и расширяется |
| Установщик и release pipeline | Отдельный будущий этап | Не реализованы |

Финальный commit Stage 2:

```text
9602b2dbc345a12da8365c8b2cbd90163740ad0b
```

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

## Четыре команды обслуживания

| Команда | Назначение | Чего она не делает |
|---|---|---|
| `Start-AzurPilot.ps1` | Запускает подготовленную установку и контролирует backend | Не обновляет Git и не перестраивает `.venv` |
| `Update-AzurPilot.ps1` | Получает безопасное fast-forward обновление | Не выполняет rebase, reset или force push |
| `Repair-AzurPilot.ps1` | Диагностирует и восстанавливает существующую `.venv` | Не меняет ветки, remotes и пользовательские данные |
| `Build-AzurPilot.ps1` | Подготавливает уже полученный checkout | Не клонирует репозиторий и не обновляет HEAD |

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

## Документация

Вся пользовательская и эксплуатационная документация хранится в GitHub Wiki. Отдельная папка `docs/` в персональной ветке намеренно не используется.

| Раздел | Ссылка |
|---|---|
| Главная страница | [Русская Wiki](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki) |
| Запуск и обслуживание | [Запуск и обслуживание AzurPilot](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Запуск-и-обслуживание-AzurPilot) |
| Архитектура Stage 2 | [Архитектура Stage 2](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Архитектура-Stage-2) |
| Обновление | [Обновление AzurPilot](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Обновление-AzurPilot) |
| Ошибки updater | [Ошибки при обновлении](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Ошибки-при-обновлении) |
| Внутренняя логика updater | [Как устроено обновление](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Как-устроено-обновление) |
| Отличия персональной версии | [Отличия персональной версии](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Отличия-персональной-версии) |

## Основные гарантии персональной ветки

- Только `Update-AzurPilot.ps1` владеет Git update-flow.
- Обновления принимаются только из `origin/personal/stable`.
- Разрешена схема `fetch → проверка истории → merge --ff-only`.
- Локальные изменения не удаляются автоматически.
- `Repair` использует backup, journal и rollback.
- `Build` проверяет bootstrap-артефакты по SHA-256.
- Существующий `config\deploy.yaml` не перезаписывается.
- Неизвестный процесс на порту `25548` не завершается автоматически.
- Обычный запуск не зависит от `alas-launcher.exe`.

Автоматически не используются:

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

Транзакции updater:

```text
%LOCALAPPDATA%\AzurPilot\dependency-transactions
```

Repair, Build и shortcut используют отдельные каталоги внутри `%LOCALAPPDATA%\AzurPilot`.

Не удаляйте незавершённые transaction-каталоги вручную: они нужны следующему безопасному запуску для восстановления состояния.

## Модель веток

| Ветка или remote | Назначение |
|---|---|
| `master` | Чистое зеркало исходной версии AzurPilot |
| `personal/stable` | Рабочая стабильная персональная версия |
| `origin` | Личный репозиторий и единственный автоматический источник обновлений |
| `upstream` | Исходный проект для контролируемого ручного переноса изменений |

Изменения из `upstream` не переносятся автоматически.

## Что пока не заменено

Этапы 1–2 не переписывали:

- основной WebUI;
- планировщик игровых задач;
- распознавание интерфейса;
- внутреннюю ADB-логику;
- игровые модули;
- пользовательские конфигурации;
- все оставшиеся сетевые интеграции;
- полноценный installer и release pipeline.

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
