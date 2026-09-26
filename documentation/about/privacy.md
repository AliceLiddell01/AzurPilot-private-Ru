# Приватность

Базовый принцип персональной версии: AzurPilotRu **не должен автоматически отправлять владельцу форка игровую статистику, error logs, действия WebUI или стабильный идентификатор компьютера**.

## Удалённые механизмы

Из активного runtime удалены:

- автоматическая отправка CL1 statistics на project server;
- автоматическая отправка error logs и traceback;
- Microsoft Clarity;
- remote project announcements;
- предустановленный upstream remote-access server.

## Удалённый доступ

Безопасные значения по умолчанию:

```text
EnableRemoteAccess: false
SSHServer: null
SignalingServer: null
```

Включение внешнего доступа — отдельное пользовательское решение.

## Что остаётся локально

На компьютере могут находиться профили, screenshots, diagnostic data, PostgreSQL, Redis cache, observability data и legacy local databases.

Локальность этих данных сама по себе не означает, что их безопасно публиковать.

## Внешние функции по выбору пользователя

Данные могут уходить выбранному provider при настройке OnePush, LLM, MCP remote, WebRTC/SSH remote access, proxy, external mirrors, MAA reporting и других интеграций.

Перед включением такой функции оцените её собственную privacy policy и содержимое передаваемых данных.

## Известные внешние запросы

### Фон WebUI

CSS случайного фона может обращаться к:

```text
https://api.yppp.net/api.php
```

Эта зависимость оставлена намеренно и не относится к telemetry/announcements AzurPilot.

### WebRTC STUN

При включённом WebRTC и отсутствии собственного списка STUN используется:

```text
stun:stun.l.google.com:19302
```

### Event Calculator

Обращение к внешней Wiki происходит только после явного пользовательского обновления; локальный cache сам по себе сети не требует.

### MAA reporting

Penguin Statistics / YiTuLiu reporting сохранён как opt-in и по умолчанию выключен.

## Публичная диагностика

Перед публикацией issue, профиля, screenshot или вывода удалите tokens, API keys, локальные пути, игровые identifiers, credentials и приватные URLs.

Подробнее о сетевой архитектуре: [Приватность и сетевое поведение](../architecture/privacy-network.md).
