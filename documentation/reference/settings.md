# Настройки

Конфигурация AzurPilotRu организована по схеме:

```text
задача → группа → параметр
```

Пример:

```text
Main.StopCondition.OilLimit
```

## Scheduler

Типичные параметры:

| Параметр | Назначение |
|---|---|
| `Enable` | добавить задачу в планировщик |
| `PushNotification` | итоговое push-уведомление |
| `NextRun` | время следующей готовности |
| `Command` | внутреннее имя задачи |
| `SuccessInterval` | задержка после успеха |
| `FailureInterval` | задержка после сбоя |
| `ServerUpdate` | время серверного обновления |
| `Sensitive` | критическая задача с fail-closed поведением |

`NextRun` обычно вычисляется автоматически.

## Emulator

Ключевые параметры:

- `Serial`;
- `PackageName`;
- `ScreenshotMethod`;
- `ControlMethod`;
- игровые настройки устройства.

Текущий package:

```text
com.YoStarEN.AzurLane
```

## StopCondition

Для campaign-задач:

- `OilLimit`;
- `CoinLimit`;
- `RunCount`;
- `MapAchievement`;
- `StageIncrease`;
- `GetNewShip`;
- `ReachLevel`.

## Fleet

Основные параметры:

- `Fleet1`, `Fleet2`;
- формация;
- режим боя;
- `FleetOrder`;
- шаг перемещения;
- пропуск подготовки.

## Submarine

- номер флота;
- режим использования;
- режим auto-search;
- дистанция до босса.

## Emotion

Для каждого флота задаются:

- расчётное morale;
- порог контроля;
- способ восстановления;
- oath;
- onsen.

## HpControl

- баланс HP;
- аварийный ремонт;
- low-HP retreat;
- пороги ремонта/отступления.

## Error

Содержит:

- обработку ошибок;
- сохранение error evidence;
- strict restart;
- лимит error logs;
- OnePush provider YAML;
- screenshot context;
- game-stuck/ADB recovery;
- необязательную LLM-диагностику.

Часть полей sensitive.

## Optimization

Содержит OCR backend/device/model, интервалы screenshots и поведение при ожидании/пустой очереди.

## Где брать актуальное значение

Эта Wiki объясняет смысл параметров, но source of truth для допустимых options остаётся generated configuration текущей версии.

Если option изменился в WebUI после обновления, не подставляйте старое значение вручную только потому, что оно встречается в старом профиле.
