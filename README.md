<p align="center">
  <img src="doc/logo.webp" alt="Логотип AzurPilot" width="400">
</p>

<h1 align="center">AzurPilot Private RU</h1>

<p align="center">
  Персональная русская ветка AzurPilot с контролируемым обновлением и прозрачным PowerShell-контуром запуска, восстановления и подготовки установки.
</p>

<p align="center">
  <a href="https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki">
    <img src="https://img.shields.io/badge/Wiki-документация-2f81f7?style=flat-square" alt="Русская Wiki">
  </a>
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

Этот репозиторий является персональным форком проекта [`wess09/AzurPilot`](https://github.com/wess09/AzurPilot), который, в свою очередь, основан на [`LmeSzinc/AzurLaneAutoScript`](https://github.com/LmeSzinc/AzurLaneAutoScript).

Цель персональной ветки — сохранить полезные возможности AzurPilot, но сделать установку, запуск, обновление и восстановление понятными, контролируемыми и менее зависимыми от исходной внешней инфраструктуры.

## Текущий статус

| Область | Состояние |
|---|---|
| Контролируемое обновление из `origin/personal/stable` | Готово |
| Безопасное fast-forward обновление Git | Готово |
| Транзакционное обслуживание `.venv` | Готово |
| Использование официального PyPI в активном потоке зависимостей | Готово |
| Прозрачные команды Start / Update / Repair / Build | Готово |
| Пользовательский запуск из меню «Пуск» | Готово |
| Независимость обычной работы от `alas-launcher.exe` | Подтверждена |
| Полная русская документация всего проекта | В работе |

> [!NOTE]
> Этапы 1 и 2 завершили контролируемое обновление и замену эксплуатационных обязанностей оригинального launcher-а. Репозиторий пока не является установщиком «в один клик» и не содержит release pipeline.

## Быстрые ссылки

| Раздел | Ссылка |
|---|---|
| Главная страница документации | [Русская Wiki](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki) |
| Запуск и обслуживание | [Запуск и обслуживание AzurPilot](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Запуск-и-обслуживание-AzurPilot) |
| Архитектура нового запуска | [Архитектура Stage 2](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Архитектура-Stage-2) |
| Как обновить AzurPilot | [Обновление AzurPilot](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Обновление-AzurPilot) |
| Что делать при ошибке | [Ошибки при обновлении](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Ошибки-при-обновлении) |
| Как работает система обновления | [Как устроено обновление](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Как-устроено-обновление) |
| Отличия от исходного проекта | [Отличия персональной версии](https://github.com/AliceLiddell01/AzurPilot-private-Ru/wiki/Отличия-персональной-версии) |
| Исходный AzurPilot | [`wess09/AzurPilot`](https://github.com/wess09/AzurPilot) |

## Основные отличия персональной ветки

### Четыре явные команды

Эксплуатационный контур разделён на четыре команды:

```text
scripts/
├── Start-AzurPilot.ps1
├── Update-AzurPilot.ps1
├── Repair-AzurPilot.ps1
└── Build-AzurPilot.ps1
```

- `Start` запускает уже подготовленную установку и не обновляет Git.
- `Update` является единственным владельцем `fetch` и `merge --ff-only`.
- `Repair` диагностирует и транзакционно восстанавливает существующую `.venv`.
- `Build` подготавливает уже полученный checkout и не клонирует репозиторий.

Обычный запуск больше не требует `alas-launcher.exe`. Windows shortcut указывает на PowerShell 7, `scripts\Start-AzurPilot.ps1` и project-owned icon.

### Контролируемый источник обновлений

Автоматическое обслуживание персональной версии принимает изменения только из:

```text
origin/personal/stable
git@github.com:AliceLiddell01/AzurPilot-private-Ru.git
```

Перед обновлением скрипт проверяет ветку, адрес `origin`, tracking branch, состояние рабочего дерева, незавершённые операции Git и наличие запущенных процессов AzurPilot.

### Только безопасный fast-forward

Updater не пытается автоматически исправлять историю Git и не уничтожает локальные изменения.

Он не использует:

```text
git reset --hard
git clean
git checkout -f
git pull
git rebase
git push --force
```

Разрешена только последовательная схема:

```text
git fetch → проверка истории → git merge --ff-only
```

Если локальная ветка опережает удалённую или история разошлась, обновление останавливается без автоматического merge, rebase или reset.

### Транзакционное обслуживание зависимостей

Если новая версия изменяет `pyproject.toml` или `uv.lock`, updater сначала:

1. проверяет новые файлы зависимостей;
2. создаёт и проверяет резервную копию `.venv`;
3. синхронизирует среду Python;
4. записывает фазу операции в журнал восстановления;
5. только после этого обновляет Git HEAD.

Repair использует отдельную транзакцию и восстанавливает исходную `.venv`, если новая среда не прошла проверку.

### Официальные источники

В активном потоке Python-зависимостей используются:

```text
https://pypi.org/simple
https://files.pythonhosted.org
```

Build загружает зафиксированные официальные artifacts `uv` и Android platform-tools и проверяет их SHA-256 до использования.

## Запуск приложения

### Через меню «Пуск»

После подготовленной установки откройте **AzurPilot** в меню «Пуск». Shortcut запускает hidden PowerShell supervisor, ожидает готовность WebUI и открывает:

```text
http://127.0.0.1:25548/
```

Закрытие вкладки браузера само по себе не означает остановку backend.

### Ручной диагностический запуск

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Start-AzurPilot.ps1"
```

При ручном запуске `Ctrl+C` завершает supervisor и всё дерево Python-процессов. После остановки порт `25548` освобождается.

### Запуск без открытия браузера

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Start-AzurPilot.ps1" -NoBrowser
```

## Обслуживание установки

Перед Update, Repair или Build полностью остановите AzurPilot.

### Обновление

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Update-AzurPilot.ps1"
```

Update работает только с `origin/personal/stable` и допускает только fast-forward.

### Диагностика и восстановление

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Repair-AzurPilot.ps1"
```

Только диагностика без изменения `.venv`:

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Repair-AzurPilot.ps1" -DiagnosticOnly
```

### Первоначальная подготовка checkout

```powershell
pwsh -NoLogo -NoProfile -File "C:\AzurPilot\scripts\Build-AzurPilot.ps1"
```

Build создаёт отсутствующий `config\deploy.yaml`, подготавливает managed Python, frozen `.venv`, локальные `uv` и ADB. Существующий config сохраняется без изменений.

## Журналы и резервные копии

Основные журналы:

```text
%LOCALAPPDATA%\AzurPilot\logs
```

Транзакции updater:

```text
%LOCALAPPDATA%\AzurPilot\dependency-transactions
```

Транзакции и backup Repair, Build и shortcut хранятся в отдельных подкаталогах `%LOCALAPPDATA%\AzurPilot`.

Не удаляйте незавершённые transaction-каталоги вручную: следующий безопасный запуск использует их для восстановления.

## Основные коды завершения

| Код | Общее значение |
|---:|---|
| `0` | Операция успешна или изменение не требовалось |
| `10` | Update не смог получить данные из `origin`; локальная версия не изменена |
| `20` | Не выполнено обязательное условие |
| `21`–`29` | Контролируемая ошибка конкретной команды |
| `30` | Непредусмотренная ошибка |

Точное значение кода выводится в журнал соответствующей команды и описывается в Wiki.

## Модель веток

| Ветка или remote | Назначение |
|---|---|
| `master` | Чистое зеркало исходной версии AzurPilot |
| `personal/stable` | Рабочая стабильная персональная версия |
| `origin` | Личный репозиторий и единственный автоматический источник обновлений |
| `upstream` | Исходный проект; используется только для контролируемого ручного переноса изменений |

Изменения из `upstream` не переносятся автоматически. Каждый перенос должен отдельно проверяться и адаптироваться под персональную архитектуру.

## Что этапы 1–2 не меняли

Новые Update и launcher-контур не означают, что весь проект переписан или полностью изолирован от внешних сервисов.

Этапы 1–2 не заменяли:

- основной WebUI;
- планировщик игровых задач;
- распознавание интерфейса;
- подключение к эмулятору;
- игровые модули;
- пользовательские конфигурации;
- все оставшиеся сетевые интеграции проекта;
- installer и release pipeline.

Эти части рассматриваются отдельно и не входят в завершённый Stage 2.

## Разработка из исходного кода

Проект использует Python `3.14.6`, `uv`, корневой `pyproject.toml` и зафиксированный `uv.lock`.

Для уже настроенной среды разработчика:

```powershell
uv sync --frozen --no-dev
uv run python gui.py
```

Эти команды предназначены для разработки и не заменяют пользовательский `Start-AzurPilot.ps1`.

## Скриншот интерфейса

<p align="center">
  <img src="doc/GUI.png" alt="Веб-интерфейс AzurPilot" width="800">
</p>

## Безопасность и сохранность данных

- Не выполняйте разрушительные Git-команды без резервной копии и отдельного плана восстановления.
- Не публикуйте токены, пароли, приватные SSH-ключи, пользовательские конфигурации и журналы с чувствительными данными.
- Перед экспериментальными изменениями сохраняйте пользовательские настройки.
- При непонятном состоянии эксплуатационные команды намеренно останавливаются вместо автоматической очистки или переписывания истории.
- `Start`, `Repair` и `Build` не обновляют Git.
- Не используйте original launcher как штатный способ запуска или обновления персональной ветки.

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
