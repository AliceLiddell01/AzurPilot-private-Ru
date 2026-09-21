# Поддерживаемая версия игры

Текущий AzurPilotRu поддерживает **только Global/EN** версию Azur Lane.

Это продуктовая граница, а не рекомендация.

## Canonical runtime identity

```text
server: en
Android package: com.YoStarEN.AzurLane
asset root: assets/en
WebUI locale: ru-RU
OCR namespace: azur_lane
```

## Что происходит с другим регионом

Неизвестный или foreign package/server должен быть отклонён до device/game side effects. Удалённые asset roots CN/JP/TW не используются как автоматический fallback.

Поэтому нельзя включить JP простой заменой `PackageName` в JSON.

## Event metadata

Событийные отображаемые данные используют EN metadata. Если имя события нельзя получить ожидаемым способом, система использует стабильный технический identifier, а не fallback в CN/JP/TW.

## OCR

Runtime сохраняет Global/shared OCR resources. Поддержка общих English OCR моделей не означает поддержку другого игрового региона.

## Русский интерфейс и английская игра

Русский WebUI не требует русифицированного клиента Azur Lane:

```text
Azur Lane Global/EN
        +
AzurPilotRu WebUI ru-RU
```

## Почему ограничение жёсткое

UI, assets, OCR, event metadata и gameplay assumptions зависят от региона. Fail-closed ограничение безопаснее, чем попытка управлять неподдерживаемым клиентом по частично похожим экранам.

## Если у вас CN/JP/TW

Текущая документация и runtime не обещают корректную работу. Используйте поддерживаемый Global package либо отдельный проект/ветку, которая явно заявляет поддержку нужного региона.
