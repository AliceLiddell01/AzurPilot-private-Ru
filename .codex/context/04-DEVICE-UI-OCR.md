# Device, UI и OCR

## Device

`module/device/` объединяет:

- ADB-соединение;
- обнаружение эмулятора и пакета;
- screenshot backend;
- control/input backend;
- управление жизненным циклом приложения;
- установку и проверку вспомогательных компонентов.

Backends могут существенно различаться по latency, формату изображения, reconnect и доступным командам. Не исправлять проблему одного backend изменением общего кода без проверки остальных.

## Диагностика соединения

Проследить отдельно:

```text
выбор serial
→ adb connect/reconnect
→ обнаружение package
→ создание backend
→ первый screenshot
→ первый input
```

При ошибке различать:

- эмулятор не запущен;
- ADB offline/unauthorized;
- неправильный serial;
- пакет не найден;
- screenshot backend не работает;
- input backend не работает;
- игра запущена, но неизвестен экран.

## Screenshot

Внутренний контракт `module/device/screenshot.py` использует BGR `numpy.ndarray`, совместимый с OpenCV. Отдельные screenshot backends могут получать RGB или иной формат, но обязаны явно преобразовать его в BGR на своей границе до передачи в общий pipeline. Перед обработкой проверить:

- размер изображения;
- порядок каналов;
- server-specific asset;
- область crop;
- масштабирование/DPI;
- переходный кадр;
- кэш текущего screenshot.

## Button и Template

Упрощённо:

- `Button` хранит область распознавания и область клика;
- `Template` выполняет шаблонное сопоставление;
- average/color checks дешевле полного template matching;
- assets могут различаться по серверу и теме.

Не менять threshold без набора положительных и отрицательных кадров. Локальное улучшение similarity может повысить ложные совпадения в другом состоянии.

## UI-граф

`Page` описывает состояние экрана и связи переходов. `ui_goto`/`ui_ensure` ищут путь по графу.

При добавлении или изменении страницы проверить:

- уникальность check button;
- обе стороны нужных связей;
- варианты темы/сервера;
- общие popup handlers;
- корректность результата `ui_get_current_page`;
- отсутствие цикла, когда две страницы считаются разными, но визуально эквивалентны.

## OCR

OCR-слой содержит классы разного назначения:

- общий текст;
- числа;
- счётчики `current/total`;
- длительности;
- server/language-specific модели;
- возможные ONNX/NCNN/RPC backends.

Перед выбором нового OCR не использовать общий класс автоматически. Найти существующий аналог с похожим шрифтом, цветом, размером и форматом результата.

## Надёжный OCR-поток

```text
crop
→ цветовая/морфологическая предобработка
→ распознавание
→ postprocess типичных ошибок
→ проверка диапазона и контекста
→ только затем использование значения
```

Не принимать OCR-результат без domain validation, если он влияет на покупку, расход ресурсов, выбор флота или длительный цикл.

## Тестовые данные

Для распознавания предпочтительны реальные сохранённые screenshots:

- минимум один успешный кадр;
- отрицательный кадр похожего экрана;
- переходный/анимированный кадр;
- разные значения OCR;
- server/theme variants, если затронуты.

## Dock Inventory: стабильный кадр и атрибуты карточки

`module/dock_inventory/` разделяет обход Dock на последовательные доказуемые
этапы: prerequisite/navigation, стабильный detached viewport, динамическая
регистрация карточек, canonical identity и атрибуты. Stage 5 принимает один и
тот же viewport вместе с результатами card-grid/identity и обязан проверить
совпадение index, scroll position, порядка `PRESENT` и `slot.area`.

- level ROI и star ROI вычисляются только относительно `slot.area`;
- `ABSENT` не передаётся OCR/CV, а любой Stage 3 `UNKNOWN` блокирует полный pass;
- level использует отдельный `DockLevelOcr`, наследующий общий `LevelOcr`, но
  заменяющий только Dock-specific preprocessing числового блока; combat OCR не
  изменяется, multi-pass proof OCR/reconciliation не используются, а значение
  принимается только после проверки диапазона из pinned `ship_level` source;
- raw stars определяются визуально как filled/empty/total, без подстановки из
  identity catalog;
- progression выводится только из raw stars и ровно одной совместимой static
  semantic state; retrofit/research и конфликтующие состояния не получают
  вымышленную ordinary limit-break метку;
- overlap соседних viewport сохраняется: cross-viewport dedup относится к
  следующему этапу, а не к scanner атрибутов.

Traversal использует scrollbar как независимый источник истины о позиции,
верхе, низе и факте прогресса. Известное host-side поведение стрелок MuMu не
считается доказательством эквивалентности Android keyevent: runtime сначала
пробует `KEYCODE_DPAD_UP`/`KEYCODE_DPAD_DOWN` через ADB и после каждого действия
обязан получить новый стабильный кадр и измерить scrollbar. DPAD сохраняется
только при доказанном движении в ожидаемую сторону; иначе он отключается и
используется ранее принятый canonical `Scroll` fallback. `DockTraversalResult`
сохраняет `dpad_actions`, `dpad_progress_actions` и `scroll_fallback_calls`,
чтобы реальный smoke мог явно показать, какой путь движения сработал.

Runtime не читает сеть: identity и progression catalogs являются отдельными
детерминированными generated sidecars с независимыми fingerprints.

## Global/EN asset и OCR contract

- canonical root — `assets/en`; CN/JP/TW roots и string fallback недопустимы;
- generator читает module list из EN и fail-closed при missing asset;
- package detection использует exact match `com.YoStarEN.AzurLane`;
- 18 OCR files — Global recognition либо shared detection/generic resources;
- registry exposes `azur_lane`; `cnocr`, JP и TW aliases отклоняются;
- shared `det`, English routing, RPC allowlist, recovery и privacy controls сохраняются.
