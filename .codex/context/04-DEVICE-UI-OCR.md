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

## Устойчивые переходы Formation Info

UI-driven сканирование флотов не должно считать один detector-positive кадр доказательством завершённого перехода. Переходные кадры и быстрые последовательные screenshot могут кратковременно выглядеть как целевое состояние, после чего следующий свежий кадр снова показывает предыдущую boundary.

Контракт Formation scanner:

- `_open_info()` возвращает управление только после ограниченной серии последовательных свежих подтверждений `info_opened`; отрицательный кадр сбрасывает серию;
- `_close_info()` возвращает управление только после последовательных подтверждений одновременно `not info_opened` и распознанной `page_fleet`;
- существующие timeout/loop остаются ограничителями; не добавлять безлимитные retry или blind continuation;
- после подтверждения открытия scanner использует новый screenshot и повторно валидирует, что Info всё ещё открыт;
- после физического сбоя продолжать следующий флот можно только из восстановленной детерминированной Formation boundary; при неизвестном UI состоянии выполнение останавливается fail-closed;
- `complete == False` относится к структурной неполноте распознавания состава и не является физической ошибкой navigation/scanner слоя.

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
- canonical level ROI имеет точную геометрию `58x31`; выход этого ROI за frame
  означает нарушение входной геометрии Stage 3→5 и является
  `DockAttributeInputError`, а не guessed `UNKNOWN` level;
- clipped star ROI остаётся per-slot визуальным `UNKNOWN` с причиной
  `star_roi_clipped`; это не ослабляет отдельный input-contract level scanner;
- level использует отдельный `DockLevelOcr`, наследующий общий `LevelOcr`, но
  заменяющий только Dock-specific preprocessing числового блока; combat OCR не
  изменяется, multi-pass proof OCR/reconciliation не используются, а значение
  принимается только после проверки диапазона из pinned `ship_level` source;
- low-level стиль реального v15 `Lv.1` допускает отдельный low-contrast
  one-digit preprocessing только после raw-range и правого digit-slot evidence;
  preprocessing не подставляет значение и заканчивается тем же единственным
  bundled OCR-проходом. Реальный v15 fixture обязан дать OCR `1` и через
  production `DockLevelOcrAdapter`;
- raw stars определяются визуально как filled/empty/total, без подстановки из
  identity catalog. First-glyph proof откалиброван также на low-level стиле v15;
  изменение проверено против всего v15 star corpus и ideal corpus без изменения
  ранее наблюдавшихся результатов;
- progression выводится только из raw stars и ровно одной совместимой static
  semantic state; retrofit/research и конфликтующие состояния не получают
  вымышленную ordinary limit-break метку;
- overlap соседних viewport сохраняется: cross-viewport dedup относится к
  следующему этапу, а не к scanner атрибутов.

Stage 3 не обязан видеть separator над самой верхней полностью видимой строкой.
Если уже доказаны минимум две последовательные строки с canonical
`ROW_DELTA`, scanner может вывести ровно один preceding row origin из их grid
phase. Такой origin не принимается по одной геометрии: inferred row должен
полностью помещаться в supported scan area и независимо пройти обычный
per-slot presence scan с хотя бы одним `PRESENT`. При отсутствии presence
строка не восстанавливается; identity/name/ship/slot-specific данные в этом
решении не участвуют.

### Dock Inventory: MuMu-first traversal

MuMu является основным runtime для текущего Stage 5. Его host-side Slide mapping
воспроизводится не Android `DPAD` keyevent, а непосредственным ADB swipe. Реальный
v15 показал `2` отправленных `KEYCODE_DPAD_DOWN` и `0` доказанных progress
actions, после чего весь traversal выполнил `Scroll` fallback; поэтому DPAD не
является preferred production path.

После доказанного перехода к top `DockMuMuInventoryTraversal` один раз пробует
малый initial viewport nudge через ADB swipe `(640, 360) -> (640, 336)`. Сам
факт изменения пикселей кадра не является evidence: Dock содержит локальные
анимации, которые в v15 дали ложный `initial_nudge_applied`.

Nudge вообще не отправляется, если исходный stable frame не поддерживает
MuMu 1280x720 motion-proof ROI. Это сохраняет generic fallback для другой
геометрии и не оставляет UI в неизвестном положении после жеста, который нельзя
проверить.

После отправленного nudge новый stable frame принимается только при
одновременном выполнении условий:

- scrollbar всё ещё внутри подтверждённого top threshold;
- phase correlation по центральной Dock ROI доказывает почти вертикальный
  глобальный сдвиг: `|dx| <= 8`, `-36 <= dy <= -12`;
- phase-correlation response не ниже `0.55`.

Если phase correlation при хорошем response доказывает практически отсутствие
глобального движения (`|dy| <= 4`), traversal использует исходный detached top
frame и не вызывает `Scroll.set_top`: это отделяет локальную анимацию от
реального движения списка.

Если scrollbar после nudge действительно вышел за top threshold,
`Scroll.set_top` разрешён только как rollback. После него traversal обязан не
только снова увидеть top scrollbar, но и phase-correlation доказать возврат к
исходной content-phase (`|dy| <= 4`). Такой rollback отмечается отдельным
`initial_nudge_reverted` и не увеличивает счётчик реальных
`Scroll.next_page` fallback-переходов.

Любой другой доказанный micro-shift, слишком слабый phase response либо потеря
scrollbar evidence после уже отправленного nudge являются operational failure.
Top-position scrollbar сам по себе не считается доказательством исходной
геометрии: реальные пользовательские screenshots показали, что содержимое может
быть сдвинуто примерно на одну малую фазу при неизменном top thumb.

После initial normalization основной MuMu path посылает фиксированный ADB swipe
`(640, 560) -> (640, 160)`. Ни отправка команды, ни длина жеста сами по себе не
доказывают progress. После каждого swipe обязательно берётся новый stable frame
и измеряется scrollbar. Scrollbar остаётся независимым authority для позиции,
верхней/нижней границы и факта движения; число кораблей и длина scrollbar thumb
не используются для вычисления позиции следующего окна.

Safety-счётчик основного traversal увеличивается только на реально отправленные
MuMu swipe либо выполненные `Scroll.next_page`; ошибка ADB sender до отправки
жеста не расходует бюджет fallback. Одноразовый setup nudge и его максимум один
rollback ограничены самим control-flow и отдельно отражаются в evidence.

Если MuMu ADB swipe недоступен, бросает transport error или ограниченное число
попыток не подтверждает движение к низу, этот path отключается и traversal
продолжает через существующий canonical `Scroll.next_page` fallback.
Невозможность получить stable frame не маскируется fallback-логикой.
`DockMuMuTraversalResult` сохраняет `mumu_swipe_actions`,
`mumu_swipe_progress_actions`, `initial_nudge_shift_y`,
`initial_nudge_phase_response`, `initial_nudge_reverted`, а базовый контракт
сохраняет `scroll_fallback_calls` и `initial_nudge_applied`, чтобы реальный smoke
явно показывал фактически использованный путь движения.

Runtime не читает сеть: identity и progression catalogs являются отдельными
детерминированными generated sidecars с независимыми fingerprints.

## Global/EN asset и OCR contract

- canonical root — `assets/en`; CN/JP/TW roots и string fallback недопустимы;
- generator читает module list из EN и fail-closed при missing asset;
- package detection использует exact match `com.YoStarEN.AzurLane`;
- 18 OCR files — Global recognition либо shared detection/generic resources;
- registry exposes `azur_lane`; `cnocr`, JP и TW aliases отклоняются;
- shared `det`, English routing, RPC allowlist, recovery и privacy controls сохраняются.
