# Приватность и сетевое поведение

Персональная версия AzurPilotRu проектируется так, чтобы **не отправлять владельцу форка игровую телеметрию и журналы автоматически**.

## Что удалено

Из активной сборки удалены:

- автоматическая отправка CL1 statistics на project server;
- автоматическая отправка error logs/traceback на project server;
- Microsoft Clarity;
- project-controlled announcements;
- предустановленный upstream SSH endpoint;
- настройки, которые могли повторно включить эти удалённые механизмы.

## Удалённый доступ

По умолчанию:

```text
EnableRemoteAccess: false
SSHServer: null
SignalingServer: null
```

Форк не предустанавливает свой SSH/signaling server.

## Локальные данные

В установке могут храниться:

- профили;
- diagnostics;
- screenshots;
- локальные базы/legacy data;
- PostgreSQL;
- Redis cache;
- observability data.

Локальность этих данных сама по себе не означает, что их безопасно публиковать.

## Сторонние интеграции

По явной настройке пользователя наружу могут обращаться:

- OnePush;
- LLM;
- удалённый доступ;
- MCP public remote;
- proxy;
- external mirrors;
- другие developer integrations.

Данные отправляются соответствующему выбранному provider, а не «AzurPilotRu вообще».

## WebRTC

Если пользователь включает WebRTC remote access и не задаёт STUN самостоятельно, текущая конфигурация содержит публичный:

```text
stun:stun.l.google.com:19302
```

Это инфраструктурная зависимость NAT traversal, не telemetry endpoint проекта.

## Фон WebUI

Текущая privacy policy фиксирует одно намеренно сохранённое сетевое исключение: CSS случайного фона может загружать изображение с `api.yppp.net`.

Это не announcements/telemetry AzurPilot, но это внешний запрос.

## Event Calculator

Использует локальный cache и обращается к внешней Wiki только при явном пользовательском обновлении.

## MAA reporting

Reporting в Penguin Statistics / YiTuLiu сохранён как opt-in и по умолчанию выключен.

## Docker helper

Проект не должен скрыто определять public IP через внешний сервис.

Если адрес нужен, он передаётся явно через `AZURPILOT_PUBLIC_IP`.

## Перед публикацией диагностики

Удалите:

- tokens;
- API keys;
- credentials;
- local paths;
- игровые identifiers;
- private URLs;
- содержимое `.env`.

Privacy-first defaults уменьшают автоматическую отправку, но не могут защитить данные, которые пользователь сам вставил в публичный issue.
