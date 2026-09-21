# Диагностика и восстановление

AzurPilotRu разделяет **проверку состояния** и **изменяющее восстановление**.

Для начала почти всегда используйте `doctor`.

## azur doctor

```powershell
azur doctor
```

Doctor работает только на чтение. Он проверяет фундаментальные возможности проекта и не должен автоматически «чинить» найденную проблему.

Среди проверок:

- repository root и проектные маркеры;
- Git branch/tracking/remote identity;
- Python-контракт;
- доступность `uv`;
- проектное окружение `.venv`;
- `config/deploy.yaml`;
- runtime WebUI;
- установленную консольную команду и PATH;
- ADB;
- Docker Compose;
- Redis;
- RedisInsight.

Полная проверка внешних интеграций:

```powershell
azur doctor --full
```

Она дороже обычной проверки и используется только когда действительно нужны внешние integration probes.

## Как читать статусы

Типичные категории:

- **ready** — возможность подтверждена;
- **not configured** — функция не настроена;
- **unavailable/failed** — требуемый компонент недоступен или проверка провалена;
- **unknown** — безопасно доказать состояние не удалось.

`unknown` не следует трактовать как «наверное всё хорошо».

## azur repair --diagnostic-only

Для проверки именно repair-состояния без записи:

```powershell
azur repair --diagnostic-only
```

Repair проверяет:

- наличие `uv.lock`;
- проектный Python;
- проектный `uv`;
- незавершённые Build/Update/Repair transaction;
- остановленный WebUI.

Если обнаружена проблема, diagnostic-only сообщает её, но не заменяет `.venv`.

## azur repair

Полное восстановление:

```powershell
azur repair
```

Repair предназначен для окружения проекта. Он **не меняет Git, пользовательскую конфигурацию или PostgreSQL**.

Перед восстановлением WebUI должен быть остановлен.

Repair использует собственную transaction/backup-логику и не должен вслепую удалять неизвестную `.venv`.

## Ярлык Windows

Восстановить ярлык вместе с repair:

```powershell
azur repair --repair-shortcut
```

Восстановить только ярлык:

```powershell
azur repair --shortcut-only
```

На POSIX Windows shortcut считается неподдерживаемой возможностью.

## Когда Repair не нужен

Не запускайте Repair, если проблема явно находится в другом слое:

- неверный ADB serial;
- ошибочная игровая настройка;
- занятый чужим процессом порт;
- неработающий Docker Desktop;
- некорректный Git remote;
- проблема текущего профиля.

Сначала используйте вывод Doctor как классификатор проблемы.
