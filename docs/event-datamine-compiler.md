# Event Datamine и наблюдения UI

Stage 3 переносит объективные факты события из ручного EventPlan в закреплённый локальный artifact, собранный из расшифрованного `AzurLaneTools/AzurLaneLuaScripts` snapshot.

## Граница доверия

- `SourceSnapshot` требует server, repository и полный 40-символьный Git SHA.
- `ShareCfgLoader` разбирает только поддерживаемые Lua-таблицы и не исполняет Lua.
- Streamed ShareCfg читается через одноимённый `sharecfgdata` companion; отсутствие companion даёт структурированную ошибку.
- Parser, normalized model, validator/generator и atomic writer разделены.
- Неизвестный grid, effect или land-based mechanic блокирует production generation. Известное исключение допускается только через `CompatibilityPatch` с причиной, source evidence и ожидаемым эффектом.

## Локальный artifact и состояние пользователя

Встроенный [`rose_tower.json`](../module/event_datamine/data/rose_tower.json) — детерминированный historical golden, а не live-сетевой cache. Его provenance закрепляет repository/revision/server/activity ID. Digest проверяется до чтения, а writer заменяет файл атомарно только после validation.

`EventSpec` содержит факты игры: identity, даты, карты, shop rows, валюты, milestones, PT sources, asset references, provenance и findings. В Stage 4 PT sources классифицируются только по структурным связям `taskConfig`: `daily`, `weekly`, `one_time`, `first_clear`, `daily_first_clear`, `repeatable_map_clear`, `challenge` или `unknown`. Текст задания, размер награды и `is_head` не используются как эвристика.

`config/state/event_user_state/` отдельно хранит только пользовательскую политику: желаемые количества покупок. Старые ручные current PT и статусы recurring sources мигрируются в `legacy_debug_evidence`; они не становятся production truth и не участвуют в UI/прогнозе.

`config/state/event_observation/` хранит типизированные runtime-наблюдения отдельно для профиля, события и сервера. Запись атомарна, повреждённый JSON переносится в `*.corrupt-*`, старые наблюдения не превращаются в нули. Fixture/replay evidence разрешено только явным тестовым флагом и по умолчанию отвергается production reader.

После полного `EventShopClerk.scan_all()` runtime rows сопоставляются с catalog rows только по уникальному exact key: filter token, price, total stock, currency relation и amount. Для `unmatched`, `ambiguous` и некорректных счётчиков purchased остаётся неизвестным. Любая покупка инвалидирует snapshot до следующего полного сканирования. Желаемое количество остаётся независимой пользовательской политикой.

Обычный Event UI не позволяет добавлять или редактировать source facts и не использует Wiki/BWiki. Текущий PT берётся только из свежей Dashboard/OCR записи. Ручных действий `Получено`/`Пропуск` нет. Если PT, стоимость карты, награда за clear, число проходов или mission status не доказаны runtime-наблюдением, UI показывает `Нет данных`/`Автостатус пока недоступен`. `PtLimit`, Scheduler и EventShop меняются лишь существующими явными действиями пользователя; небезопасный shop selector сохраняет fail-closed поведение.

## Developer CLI

```powershell
uv run python -m dev_tools.map_extractor `
  --source-root C:\path\to\AzurLaneLuaScripts `
  --server EN `
  --revision <full-sha> `
  --activity-id 5941 `
  --artifact .\event.json
```

`--maps-output` включается отдельно. Map modules не генерируются, если artifact содержит blocking findings. Generated config включает только факты карты; произвольная runtime policy не добавляется.

## Известные границы и долг Stage 5

- Точное PT за повторный clear карты отсутствует в исследованной ShareCfg family и помечено `map_pt_amount_unavailable`.
- В репозитории нет scanner-а отдельных mission rows с надёжным task identity. До его появления mission completion не автоматизируется.
- Нет доказанного runtime provider для PT/oil/coin на каждой карте, звёзд и числа прохождений; поля остаются optional.
- Локальный resolver использует существующие EventShop templates и безопасный placeholder. Остальные game asset references не загружаются из сети.
- Свежий CN snapshot проверен как непроизводственный probe; production CN→EN reconciliation и переключение live event не выполнялись.
