# Отличия AzurPilotRu

AzurPilotRu не является только переводом интерфейса. Персональная версия содержит собственные эксплуатационные и архитектурные изменения.

## Русский runtime-интерфейс

Активный WebUI работает с `ru-RU`. Русифицированы navigation, настройки, task configuration, пользовательские сообщения, error states и значительная часть инфраструктурных сообщений.

Технические identifiers сохраняются в исходном написании там, где перевод нарушил бы машинный контракт.

## Только Global/EN

Персональная версия сознательно ограничена:

```text
server: en
package: com.YoStarEN.AzurLane
assets: assets/en
locale: ru-RU
```

CN/JP/TW не используются как runtime fallback.

## Собственный lifecycle CLI

Основные операции разделены: `azur doctor`, `build`, `start`, `stop`, `repair`, `update`.

Start не владеет Git update. Repair не должен менять Git, PostgreSQL или пользовательскую конфигурацию. Build готовит уже полученный checkout. Update является отдельным контролируемым владельцем обновления.

## Контролируемый Update

Update использует проверенную fast-forward модель вместо destructive Git operations. Перед изменением он проверяет repository identity, историю и создаёт внешний PostgreSQL backup.

## Приватность

Из активной персональной версии удалены CL1 telemetry upload, remote error-log upload, Microsoft Clarity, project-controlled announcements и предустановленный upstream SSH endpoint. Удалённый доступ выключен по умолчанию.

Подробнее: [Приватность](privacy.md).

## PostgreSQL и Redis

Production storage использует PostgreSQL как durable source и Redis как reconstructable runtime cache.

## Observability

Добавлен локальный контур Grafana, Loki, Prometheus, Tempo и Alloy. Он предназначен для диагностики и не является источником runtime truth.

## First-party MCP

Проект содержит отдельные Dev MCP и Game MCP с собственными контрактами и compatibility metadata.

## Passive configuration

Чтение deploy config не должно само по себе делать geo lookup, выбирать CDN, менять repository, активировать Git-over-CDN или переписывать файл только из-за чтения.

## Что осталось от upstream

Основная игровая автоматизация, task model, device/OCR/campaign ecosystem и многие исторические структуры наследуются из AzurPilot/ALAS. Персональная версия не переписывает проект с нуля — она постепенно усиливает и ограничивает конкретные границы.
