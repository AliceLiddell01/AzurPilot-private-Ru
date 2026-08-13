# Event Datamine и наблюдения UI

Подсистема переносит объективные факты события из ручного EventPlan в закреплённый локальный artifact, собранный из расшифрованного `AzurLaneTools/AzurLaneLuaScripts` snapshot.

## Граница доверия

- `SourceSnapshot` требует server, repository и полный 40-символьный Git SHA.
- `ShareCfgLoader` разбирает только поддерживаемые Lua-таблицы и не исполняет Lua.
- Streamed ShareCfg читается через одноимённый `sharecfgdata` companion; отсутствие companion даёт структурированную ошибку.
- Parser, normalized model, validator/generator и atomic writer разделены.
- Неизвестный grid, effect или land-based mechanic блокирует production generation. Известное исключение допускается только через `CompatibilityPatch` с причиной, source evidence и ожидаемым эффектом.

## Локальный artifact и состояние пользователя

Production resolver читает generated [`index.json`](../module/event_datamine/data/index.json), выбирает единственный active или redemption artifact по server-local lifecycle и исключает роль `demo`. Несколько подходящих событий дают fail-closed ambiguity. Текущий EN artifact хранится в [`data/production/`](../module/event_datamine/data/production/) и не выбирается по имени, activity ID или view class.

Встроенный [`rose_tower.json`](../module/event_datamine/data/rose_tower.json) — детерминированный historical golden/demo, а не production default и не live-сетевой cache. Digest каждого artifact и registry проверяется до чтения, а writer заменяет файл атомарно только после validation.

`EventSpec` содержит факты игры: identity, даты, карты, shop rows, валюты, milestones, PT sources, asset references, provenance и findings. В Stage 4 PT sources классифицируются только по структурным связям `taskConfig`: `daily`, `weekly`, `one_time`, `first_clear`, `daily_first_clear`, `repeatable_map_clear`, `challenge` или `unknown`. Текст задания, размер награды и `is_head` не используются как эвристика.

`config/state/event_user_state/` отдельно хранит только пользовательскую политику: желаемые количества покупок. Старые ручные current PT и статусы recurring sources мигрируются в `legacy_debug_evidence`; они не становятся production truth и не участвуют в UI/прогнозе.

`config/state/event_observation/` хранит типизированные runtime-наблюдения отдельно для профиля, события, сервера и source revision. Запись атомарна, повреждённый JSON переносится в `*.corrupt-*`, старые наблюдения не превращаются в нули. Fixture/replay evidence разрешено только явным тестовым флагом и по умолчанию отвергается production reader.

После полного `EventShopClerk.scan_all()` runtime rows сопоставляются с catalog rows только по уникальному exact key: filter token, price, total stock, currency relation и amount. Для `unmatched`, `ambiguous` и некорректных счётчиков purchased остаётся неизвестным. Любая покупка инвалидирует snapshot до следующего полного сканирования. Желаемое количество остаётся независимой пользовательской политикой.

Обычный Event UI не позволяет добавлять или редактировать source facts и не использует Wiki/BWiki. Текущий PT берётся только из свежей Dashboard/OCR записи. Ручных действий `Получено`/`Пропуск` нет. Если PT, стоимость карты, награда за clear, число проходов или mission status не доказаны runtime-наблюдением, UI показывает `Нет данных`/`Автостатус пока недоступен`. `PtLimit`, Scheduler и EventShop меняются лишь существующими явными действиями пользователя; небезопасный shop selector сохраняет fail-closed поведение.

## Developer CLI

Обновление current EN artifact не требует знать новый activity ID. Команда проверяет HEAD source checkout, структурно обнаруживает current major event, компилирует artifact и карты, затем пересобирает registry и local-only asset catalog:

```powershell
uv run python -m dev_tools.event_datamine_build `
  --source-root C:\path\to\AzurLaneLuaScripts `
  --server EN `
  --revision <full-sha> `
  --current `
  --now <server-local-iso-datetime> `
  --output-root .\module\event_datamine\data `
  --maps-output .\campaign\generated_event `
  --overwrite
```

Для воспроизводимых integration tests минимальная source-derived fixture извлекается отдельно; manifest сохраняет source identity, record counts и SHA-256 всех таблиц:

```powershell
uv run python -m dev_tools.event_datamine_fixture `
  --source-root C:\path\to\AzurLaneLuaScripts `
  --server EN `
  --repository AzurLaneTools/AzurLaneLuaScripts `
  --revision <full-sha> `
  --now <server-local-iso-datetime> `
  --output .\tests\fixtures\event_datamine\current_en
```

Исторический extractor с явным activity ID остаётся инструментом golden/regression:

```powershell
uv run python -m dev_tools.map_extractor `
  --source-root C:\path\to\AzurLaneLuaScripts `
  --server EN `
  --revision <full-sha> `
  --activity-id 5941 `
  --artifact .\event.json
```

`--maps-output` включается отдельно. Map modules не генерируются, если artifact содержит blocking findings. Generated config включает только факты карты; произвольная runtime policy не добавляется.

## Известные границы

- Точное PT за повторный clear карты отсутствует в исследованной ShareCfg family и помечено `map_pt_amount_unavailable`.
- В репозитории нет scanner-а отдельных mission rows с надёжным task identity. До его появления mission completion не автоматизируется.
- Нет доказанного runtime provider для PT/oil/coin на каждой карте, звёзд и числа прохождений; поля остаются optional.
- Generated asset catalog связывает canonical source paths с существующими локальными EventShop templates. Неразрешённые references используют безопасный placeholder; runtime ничего не загружает из сети.
- Legacy EventShop automation поддерживает только строки с доказанным runtime filter identity. Неизвестные товары остаются видимыми, но не включаются в автоматизацию.
